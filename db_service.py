from datetime import datetime, date, timedelta, timezone
from typing import Optional
import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from models import SessionLocal, StockInfo, DailyPrice, Watchlist, WatchResult, User, UserWatchlist, UserRiskProfile
from werkzeug.security import generate_password_hash, check_password_hash

def upsert_stock_info(code: str, name: str, market: str = None, industry: str = None):
    """更新或新增股票基本資料"""
    from sqlalchemy import func
    with SessionLocal() as db:
        stmt = insert(StockInfo).values(
            code=code,
            name=name,
            market=market,
            industry=industry,
            updated_at=datetime.utcnow()
        )
        # PostgreSQL 的 UPSERT 語法：若傳入的 industry 為 None，則保留資料庫內原有的 industry
        update_set = {
            'name': stmt.excluded.name,
            'updated_at': stmt.excluded.updated_at
        }
        if market is not None:
            update_set['market'] = stmt.excluded.market
        if industry is not None:
            update_set['industry'] = stmt.excluded.industry

        stmt = stmt.on_conflict_do_update(
            index_elements=['code'],
            set_=update_set
        )
        db.execute(stmt)
        db.commit()

def upsert_daily_prices(code: str, records: list[dict]):
    """
    批次寫入歷史價格資料。
    records 格式預期為: [{"date": "2023-01-01", "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 1000000}, ...]
    """
    if not records:
        return

    with SessionLocal() as db:
        # 將字串 date 轉為 date object
        clean_records = []
        for r in records:
            d = r['date']
            if isinstance(d, str):
                d = datetime.strptime(d, '%Y-%m-%d').date()
            
            clean_records.append({
                "code": code,
                "date": d,
                "open": r.get('open'),
                "high": r.get('high'),
                "low": r.get('low'),
                "close": r.get('close'),
                "volume": r.get('volume')
            })

        stmt = insert(DailyPrice).values(clean_records)
        # 如果同一天已有資料，則更新數值
        stmt = stmt.on_conflict_do_update(
            constraint='uix_code_date',
            set_={
                'open': stmt.excluded.open,
                'high': stmt.excluded.high,
                'low': stmt.excluded.low,
                'close': stmt.excluded.close,
                'volume': stmt.excluded.volume,
                'updated_at': datetime.utcnow()
            }
        )
        db.execute(stmt)
        db.commit()

def get_price_history(code: str, start_date: str = None, end_date: str = None):
    """
    從資料庫取得歷史價格。
    回傳與原先 parse_yfin_text_table() 相同結構的 dict:
    {"open": [], "high": [], "low": [], "close": [], "volume": [], "dates": []}
    """
    with SessionLocal() as db:
        query = db.query(DailyPrice).filter(DailyPrice.code == code)
        
        if start_date:
            query = query.filter(DailyPrice.date >= start_date)
        if end_date:
            query = query.filter(DailyPrice.date <= end_date)
            
        # 依照日期由舊到新排序
        query = query.order_by(DailyPrice.date.asc())
        rows = query.all()

        if not rows:
            return None

        result = {
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "dates": []
        }
        
        for row in rows:
            result["open"].append(row.open)
            result["high"].append(row.high)
            result["low"].append(row.low)
            result["close"].append(row.close)
            result["volume"].append(row.volume)
            result["dates"].append(row.date.strftime('%Y-%m-%d'))
            
        return result


# ===================================================================
# 觀察清單 (Watchlist) CRUD
# ===================================================================

def _is_valid_stock_code(code: str) -> bool:
    """
    判斷股票代號是否為有效的一般股票/ETF/特別股。
    過濾掉認購/認售權證（純6碼數字）和其他衍生商品。
    
    保留：
    - 4碼純數字：一般股票（2330、1101）
    - 00開頭：ETF（0050、00878、006208）
    - 5碼數字+字母：特別股（2881A、2882B）
    
    排除：
    - 6碼純數字：認購/認售權證（020035、738739）
    - 其他奇怪格式的衍生商品
    """
    import re
    if not code:
        return False
    # 4碼純數字：一般股票
    if re.match(r'^[0-9]{4}$', code):
        return True
    # 00 開頭：ETF（包括 4-6 碼）
    if code.startswith('00'):
        return True
    # 5碼：數字+字母結尾（特別股，如 2881A、2882B）
    if re.match(r'^[0-9]{4}[A-Za-z]$', code):
        return True
    # 其餘一律過濾（6碼純數字權證等）
    return False


def populate_stock_names():
    """從政府公開資料自動載入並補齊所有台股的中文名稱與產業類別（已過濾認購/認售權證等衍生商品）"""
    import urllib.request
    import re
    import json
    added, updated = 0, 0

    # 1. 從 TWSE 官方 ISIN 清單取得上市股票代碼、名稱與精準產業別
    try:
        req = urllib.request.Request('https://isin.twse.com.tw/isin/C_public.jsp?strMode=2', headers={'User-Agent': 'Mozilla/5.0'})
        html_text = urllib.request.urlopen(req, timeout=15).read().decode('cp950', errors='ignore')
        pattern = re.compile(r'<td[^>]*>([0-9]{4}[A-Za-z0-9]?)\s*[\u3000\s]([^\s<]+)</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>([^<]*)</td>')
        for code, name, industry in pattern.findall(html_text):
            code = code.strip()
            name = name.strip()
            industry = industry.strip()
            if _is_valid_stock_code(code):
                upsert_stock_info(code, name, "上市", industry or None)
                added += 1
    except Exception as e:
        print(f"[populate_stock_names] 上市 ISIN 產業抓取失敗: {e}")

    # 2. 從 TPEX 官方 ISIN 清單取得上櫃股票代碼、名稱與精準產業別
    try:
        req2 = urllib.request.Request('https://isin.twse.com.tw/isin/C_public.jsp?strMode=4', headers={'User-Agent': 'Mozilla/5.0'})
        html_text2 = urllib.request.urlopen(req2, timeout=15).read().decode('cp950', errors='ignore')
        pattern2 = re.compile(r'<td[^>]*>([0-9]{4}[A-Za-z0-9]?)\s*[\u3000\s]([^\s<]+)</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>([^<]*)</td>')
        for code, name, industry in pattern2.findall(html_text2):
            code = code.strip()
            name = name.strip()
            industry = industry.strip()
            if _is_valid_stock_code(code):
                upsert_stock_info(code, name, "上櫃", industry or None)
                added += 1
    except Exception as e:
        print(f"[populate_stock_names] 上櫃 ISIN 產業抓取失敗: {e}")

    # 3. 補充 OpenAPI 當日行情中的股票 (避免遺漏新上市或代號)
    try:
        req3 = urllib.request.Request('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', headers={'User-Agent': 'Mozilla/5.0'})
        data3 = json.loads(urllib.request.urlopen(req3, timeout=10).read().decode('utf-8'))
        for item in data3:
            code = item.get('Code', '').strip()
            name = item.get('Name', '').strip()
            if code and name and _is_valid_stock_code(code):
                upsert_stock_info(code, name, "上市")
    except Exception as e:
        print(f"[populate_stock_names] 上市 OpenAPI 補全失敗: {e}")

    try:
        req4 = urllib.request.Request('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes', headers={'User-Agent': 'Mozilla/5.0'})
        data4 = json.loads(urllib.request.urlopen(req4, timeout=10).read().decode('utf-8'))
        for item in data4:
            code = item.get('SecuritiesCompanyCode', '').strip()
            name = item.get('CompanyName', '').strip()
            if code and name and _is_valid_stock_code(code):
                upsert_stock_info(code, name, "上櫃")
    except Exception as e:
        print(f"[populate_stock_names] 上櫃 OpenAPI 補全失敗: {e}")

    print(f"[populate_stock_names] 完成：已同步更新 {added} 筆股票產業與代號資訊")


def create_watchbot_tables():
    """建立觀察清單相關資料表（若不存在）"""
    from models import Base, engine
    Base.metadata.create_all(bind=engine)
    
    # 1. 升級 watch_results 欄位
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE watch_results ADD COLUMN IF NOT EXISTS inst_data JSONB;"))
    except Exception as e:
        print(f"[create_watchbot_tables] watch_results 欄位檢查失敗: {e}")
        
    # 2. 升級 watchlist 欄位與約束
    try:
        with engine.begin() as conn:
            # 建立 user_id 欄位 (預設值為 1 用於歷史數據相容)
            conn.execute(text("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1;"))
            # 確保 user_id 為 NOT NULL
            conn.execute(text("ALTER TABLE watchlist ALTER COLUMN user_id SET NOT NULL;"))
            # 新增 watchlist_id 欄位
            conn.execute(text("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS watchlist_id INTEGER;"))
    except Exception as e:
        print(f"[create_watchbot_tables] watchlist 欄位升級失敗: {e}")

    # 3. 數據遷移：建立預設自選清單，並將舊資料與預設清單綁定
    try:
        with SessionLocal() as db:
            # 找出所有在 watchlist 中有資料，但沒有任何自選清單的 user_id
            distinct_users = db.query(Watchlist.user_id).distinct().all()
            for (uid,) in distinct_users:
                # 檢查該 user 是否已有任何 user_watchlists
                has_list = db.query(UserWatchlist).filter(UserWatchlist.user_id == uid).first()
                if not has_list:
                    # 建立預設清單
                    default_wl = UserWatchlist(user_id=uid, name="預設清單", created_at=datetime.utcnow())
                    db.add(default_wl)
                    db.commit()
                    db.refresh(default_wl)
                    # 將該使用者的所有 watchlist_id 為空（或者是全部）的舊自選股綁定到此清單
                    db.query(Watchlist).filter(Watchlist.user_id == uid, Watchlist.watchlist_id.is_(None)).update({"watchlist_id": default_wl.id})
                    db.commit()
            
            # 對於所有未綁定的，建立預設清單並綁定
            null_rows = db.query(Watchlist).filter(Watchlist.watchlist_id.is_(None)).all()
            if null_rows:
                # 給 user_id=1 建立預設清單並綁定
                default_wl = db.query(UserWatchlist).filter(UserWatchlist.user_id == 1).first()
                if not default_wl:
                    default_wl = UserWatchlist(user_id=1, name="預設清單", created_at=datetime.utcnow())
                    db.add(default_wl)
                    db.commit()
                    db.refresh(default_wl)
                db.query(Watchlist).filter(Watchlist.watchlist_id.is_(None)).update({"watchlist_id": default_wl.id})
                db.commit()
    except Exception as e:
        print(f"[create_watchbot_tables] watchlist 資料遷移失敗: {e}")

    # 4. 更新唯一約束，確保 (user_id, watchlist_id, code) 唯一
    try:
        with engine.begin() as conn:
            # 移除舊有約束
            conn.execute(text("ALTER TABLE watchlist DROP CONSTRAINT IF EXISTS watchlist_code_key;"))
            conn.execute(text("ALTER TABLE watchlist DROP CONSTRAINT IF EXISTS uix_user_code;"))
            conn.execute(text("ALTER TABLE watchlist DROP CONSTRAINT IF EXISTS uix_user_watchlist_code;"))
            # 建立新的複合唯一約束
            conn.execute(text("ALTER TABLE watchlist ADD CONSTRAINT uix_user_watchlist_code UNIQUE (user_id, watchlist_id, code);"))
    except Exception as e:
        print(f"[create_watchbot_tables] watchlist 唯一約束升級失敗: {e}")

    # 5. 升級 users 欄位
    try:
        with engine.begin() as conn:
            # 建立 is_admin 欄位 (預設值為 FALSE)
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;"))
    except Exception as e:
        print(f"[create_watchbot_tables] users 資料表 is_admin 升級失敗: {e}")
            
    # 當 stock_info 空空如也時，自動補全台股中文名稱
    with SessionLocal() as db:
        try:
            count = db.query(StockInfo).count()
            if count < 100:  # 如果資料庫內還沒有完整資料，自動補全
                print("[db_service] 正在初始化補全全台股中文名稱...")
                populate_stock_names()
                print("[db_service] 台股中文名稱補全完成！")
        except Exception as e:
            print(f"[create_watchbot_tables] 名稱自動補全失敗: {e}")

    # 建立預設管理員帳戶
    try:
        populate_admin_seed()
    except Exception as e:
        print(f"[create_watchbot_tables] 管理員種子帳號建立失敗: {e}")


def get_user_watchlists(user_id: int) -> list[dict]:
    """取得特定使用者的所有自選清單。系統固定每個使用者 3 個清單，若不足 3 個則自動補齊"""
    with SessionLocal() as db:
        lists = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id).order_by(UserWatchlist.id.asc()).all()
        if len(lists) < 3:
            curr_names = [l.name for l in lists]
            needed = 3 - len(lists)
            for idx in range(len(lists) + 1, 4):
                new_name = f"自選清單 {idx}" if idx > 1 else "預設清單"
                while new_name in curr_names:
                    idx += 1
                    new_name = f"自選清單 {idx}"
                curr_names.append(new_name)
                new_wl = UserWatchlist(user_id=user_id, name=new_name, created_at=datetime.utcnow())
                db.add(new_wl)
            db.commit()
            lists = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id).order_by(UserWatchlist.id.asc()).all()
        return [{"id": l.id, "name": l.name, "created_at": l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else None} for l in lists]


def create_user_watchlist(user_id: int, name: str) -> dict:
    """新增自選清單（固定 3 個，不允許額外新增）"""
    raise ValueError("每個帳號固定為 3 個自選清單，不支援額外新增清單")


def rename_user_watchlist(user_id: int, watchlist_id: int, new_name: str) -> bool:
    """重新命名自選清單"""
    name_clean = new_name.strip()
    if not name_clean:
        raise ValueError("清單名稱不能為空")
    if len(name_clean) > 50:
        raise ValueError("清單名稱長度不能超過 50 個字元")

    with SessionLocal() as db:
        wl = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id, UserWatchlist.id == watchlist_id).first()
        if not wl:
            raise ValueError("找不到該自選清單")
        
        # 檢查重複命名 (除了自己以外)
        existing = db.query(UserWatchlist).filter(
            UserWatchlist.user_id == user_id, 
            UserWatchlist.name == name_clean,
            UserWatchlist.id != watchlist_id
        ).first()
        if existing:
            raise ValueError(f"自選清單「{name_clean}」已存在")
        
        wl.name = name_clean
        db.commit()
        return True


def delete_user_watchlist(user_id: int, watchlist_id: int) -> bool:
    """刪除自選清單及其底下的所有股票（必須保留至少 1 個清單）"""
    with SessionLocal() as db:
        # 檢查清單總數
        total_count = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id).count()
        if total_count <= 1:
            raise ValueError("必須保留至少一個自選清單")
            
        wl = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id, UserWatchlist.id == watchlist_id).first()
        if not wl:
            raise ValueError("找不到該自選清單")
            
        # 刪除該清單底下的所有股票
        db.query(Watchlist).filter(Watchlist.user_id == user_id, Watchlist.watchlist_id == watchlist_id).delete()
        # 刪除清單本身
        db.delete(wl)
        db.commit()
        return True


def get_watchlist(user_id: int, watchlist_id: Optional[int] = None) -> list[str]:
    """取得特定使用者的觀察代號列表。若指定 watchlist_id 則取得該清單的股票；若不指定，則取得該使用者所有清單的獨特股票代號列表。"""
    with SessionLocal() as db:
        query = db.query(Watchlist).filter(Watchlist.user_id == user_id)
        if watchlist_id is not None:
            query = query.filter(Watchlist.watchlist_id == watchlist_id)
        rows = query.order_by(Watchlist.sort_order.asc(), Watchlist.added_at.asc(), Watchlist.id.asc()).all()
        
        if watchlist_id is None:
            # 去除重複
            seen = set()
            codes = []
            for r in rows:
                if r.code not in seen:
                    seen.add(r.code)
                    codes.append(r.code)
            return codes
        return [r.code for r in rows]


def reorder_watchlist(user_id: int, watchlist_id: int, ordered_codes: list[str]) -> bool:
    """更新使用者在特定自選清單中的股票排序順序"""
    with SessionLocal() as db:
        items = db.query(Watchlist).filter(
            Watchlist.user_id == user_id,
            Watchlist.watchlist_id == watchlist_id
        ).all()
        
        code_to_item = {item.code: item for item in items}
        for index, code in enumerate(ordered_codes):
            if code in code_to_item:
                code_to_item[code].sort_order = index
        db.commit()
        return True


def add_to_watchlist(user_id: int, code: str, watchlist_id: int) -> bool:
    """新增股票到特定使用者的指定觀察清單，檢查上限 10 隻。若已存在則回傳 False"""
    with SessionLocal() as db:
        wl = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id, UserWatchlist.id == watchlist_id).first()
        if not wl:
            raise ValueError("找不到該自選清單")
            
        # 檢查上限
        count = db.query(Watchlist).filter(Watchlist.watchlist_id == watchlist_id).count()
        if count >= 10:
            raise ValueError(f"自選清單「{wl.name}」已達10隻股票上限，無法再新增")
            
        existing = db.query(Watchlist).filter(
            Watchlist.user_id == user_id,
            Watchlist.watchlist_id == watchlist_id,
            Watchlist.code == code
        ).first()
        if existing:
            return False
            
        # 新增到清單最後
        max_order = db.query(Watchlist.sort_order).filter(
            Watchlist.user_id == user_id,
            Watchlist.watchlist_id == watchlist_id
        ).order_by(Watchlist.sort_order.desc()).first()
        new_order = (max_order[0] + 1) if max_order and max_order[0] is not None else count

        db.add(Watchlist(user_id=user_id, watchlist_id=watchlist_id, code=code, sort_order=new_order, added_at=datetime.utcnow()))
        db.commit()
        return True


def remove_from_watchlist(user_id: int, code: str, watchlist_id: Optional[int] = None) -> bool:
    """從特定使用者的指定觀察清單刪除股票（若不指定 watchlist_id，則從所有清單中刪除該股）"""
    with SessionLocal() as db:
        query = db.query(Watchlist).filter(Watchlist.user_id == user_id, Watchlist.code == code)
        if watchlist_id is not None:
            query = query.filter(Watchlist.watchlist_id == watchlist_id)
        rows = query.all()
        if not rows:
            return False
        for row in rows:
            db.delete(row)
        db.commit()
        return True


def get_stock_watchlists_status(user_id: int, code: str) -> list[int]:
    """查詢某股票目前被加入到哪些自選清單 ID 中"""
    with SessionLocal() as db:
        rows = db.query(Watchlist.watchlist_id).filter(Watchlist.user_id == user_id, Watchlist.code == code).all()
        return [r.watchlist_id for r in rows if r.watchlist_id is not None]


def update_stock_watchlists(user_id: int, code: str, target_watchlist_ids: list[int]) -> None:
    """一次性更新（同步）某隻股票所在的自選清單列表，檢查各清單的 10 隻上限"""
    with SessionLocal() as db:
        # 驗證所有目標清單是否都屬於該使用者
        user_wls = db.query(UserWatchlist).filter(UserWatchlist.user_id == user_id).all()
        user_wl_ids = {wl.id for wl in user_wls}
        user_wl_names = {wl.id: wl.name for wl in user_wls}
        
        for w_id in target_watchlist_ids:
            if w_id not in user_wl_ids:
                raise ValueError("無效的自選清單 ID")
                
        # 取得該股目前的自選關係
        current_rows = db.query(Watchlist).filter(Watchlist.user_id == user_id, Watchlist.code == code).all()
        current_wl_ids = {r.watchlist_id for r in current_rows if r.watchlist_id is not None}
        
        to_add = set(target_watchlist_ids) - current_wl_ids
        to_remove = current_wl_ids - set(target_watchlist_ids)
        
        # 檢查要新增的清單是否會超過 10 隻股票上限
        for w_id in to_add:
            count = db.query(Watchlist).filter(Watchlist.watchlist_id == w_id).count()
            if count >= 10:
                raise ValueError(f"自選清單「{user_wl_names[w_id]}」已達10隻股票上限，無法再新增")
                
        # 刪除已取消勾選的關係
        for r in current_rows:
            if r.watchlist_id in to_remove:
                db.delete(r)
                
        # 新增新勾選的關係
        for w_id in to_add:
            count = db.query(Watchlist).filter(Watchlist.watchlist_id == w_id).count()
            db.add(Watchlist(user_id=user_id, watchlist_id=w_id, code=code, sort_order=count, added_at=datetime.utcnow()))
            
        db.commit()


def get_all_watchlist_codes() -> list[str]:
    """取得所有使用者加入的所有獨特觀察代號列表（供定時全掃描器使用）"""
    with SessionLocal() as db:
        rows = db.query(Watchlist.code).distinct().all()
        return [r.code for r in rows]


def save_scan_result(result: dict):
    """
    儲存掃描結果到 watch_results。
    result 格式：
    {
        "code": "2330", "name": "台積電", "price": 920.0, "change": 5.0,
        "bias": "偏多", "bullish": [...], "bearish": [...],
        "news_sentiment": "正面", "ai_summary": "...",
        "today_analysis": "今日分析", "trend_analysis": "趨勢分析",
        "inst_data": {...}
    }
    """
    # 將 today_analysis / trend_analysis 嵌入 inst_data（避免欄位 schema 變更）
    inst_data = dict(result.get("inst_data") or {})
    today_analysis = result.get("today_analysis", "")
    trend_analysis = result.get("trend_analysis", "")
    if today_analysis:
        inst_data["_today_analysis"] = today_analysis
    if trend_analysis:
        inst_data["_trend_analysis"] = trend_analysis

    with SessionLocal() as db:
        row = WatchResult(
            code=result.get("code"),
            name=result.get("name"),
            price=result.get("price"),
            change=result.get("change"),
            bias=result.get("bias"),
            bullish=result.get("bullish", []),
            bearish=result.get("bearish", []),
            news_sentiment=result.get("news_sentiment"),
            ai_summary=result.get("ai_summary"),
            inst_data=inst_data or None,
            scanned_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()


def _format_tw_time(dt) -> str | None:
    """將 UTC 儲存的掃描時間轉換為台灣時間 (UTC+8) 格式字串"""
    if not dt:
        return None
    try:
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                tw_dt = dt + timedelta(hours=8)
            else:
                tw_dt = dt.astimezone(timezone(timedelta(hours=8)))
            return tw_dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(dt, str):
            clean_str = dt.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(clean_str)
            if parsed.tzinfo is None:
                parsed = parsed + timedelta(hours=8)
            else:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return str(dt)[:16]


def get_latest_results() -> list[dict]:
    """
    取得每支股票最新一筆掃描結果（DISTINCT ON code，依 scanned_at DESC）。
    回傳 list of dict 格式。
    """
    with SessionLocal() as db:
        # 用 raw SQL 取最新一筆
        sql = text("""
            SELECT DISTINCT ON (code)
                result_id AS id, code, name, price, change, bias,
                bullish, bearish, news_sentiment, ai_summary, inst_data, scanned_at
            FROM watch_results
            ORDER BY code, scanned_at DESC
        """)
        rows = db.execute(sql).fetchall()
        results = []
        for r in rows:
            inst = r.inst_data or {}
            results.append({
                "code": r.code,
                "name": r.name,
                "price": r.price,
                "change": r.change,
                "bias": r.bias,
                "bullish": r.bullish or [],
                "bearish": r.bearish or [],
                "news_sentiment": r.news_sentiment,
                "ai_summary": r.ai_summary,
                "today_analysis": inst.get("_today_analysis", ""),
                "trend_analysis": inst.get("_trend_analysis", ""),
                "inst_data": {k: v for k, v in inst.items() if not k.startswith("_")},
                "scanned_at": _format_tw_time(r.scanned_at),
            })
        return results


def get_latest_result_for(code: str) -> dict | None:
    """取得單一股票最新掃描結果"""
    with SessionLocal() as db:
        row = db.query(WatchResult)\
            .filter(WatchResult.code == code)\
            .order_by(WatchResult.scanned_at.desc())\
            .first()
        if not row:
            return None
        inst = row.inst_data or {}
        return {
            "code": row.code,
            "name": row.name,
            "price": row.price,
            "change": row.change,
            "bias": row.bias,
            "bullish": row.bullish or [],
            "bearish": row.bearish or [],
            "news_sentiment": row.news_sentiment,
            "ai_summary": row.ai_summary,
            "today_analysis": inst.get("_today_analysis", ""),
            "trend_analysis": inst.get("_trend_analysis", ""),
            "inst_data": {k: v for k, v in inst.items() if not k.startswith("_")},
            "scanned_at": _format_tw_time(row.scanned_at),
        }


def get_stock_name(code: str) -> str | None:
    """從資料庫取得股票名稱"""
    with SessionLocal() as db:
        row = db.query(StockInfo).filter(StockInfo.code == code).first()
        return row.name if row else None


def get_stock_market(code: str) -> str | None:
    """從資料庫取得股票所屬市場（上市/上櫃）"""
    with SessionLocal() as db:
        row = db.query(StockInfo).filter(StockInfo.code == code).first()
        return row.market if row else None


# ===================================================================
# 使用者帳戶與驗證管理 CRUD
# ===================================================================

def create_user(username: str, email: str = None, password: str = None) -> User | None:
    """建立新使用者（會先檢查 username/email 是否重複）"""
    if not email:
        email = f"{username}@example.com"
    with SessionLocal() as db:
        # 檢查重複
        existing = db.query(User).filter((User.username == username) | (User.email == email)).first()
        if existing:
            return None
        
        pwd_hash = generate_password_hash(password)
        user = User(
            username=username,
            email=email,
            password_hash=pwd_hash,
            name=username,  # 預設真實姓名為帳號名稱
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def authenticate_user(username: str, password: str) -> User | None:
    """驗證使用者帳密，成功回傳 User 物件，失敗回傳 None"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return None
        if check_password_hash(user.password_hash, password):
            return user
        return None


def get_user_profile(user_id: int) -> dict | None:
    """取得特定使用者的個人資料"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "name": user.name or user.username,
            "phone": user.phone or "",
            "occupation": user.occupation or "",
            "is_admin": user.is_admin,
        }


def update_user_profile(user_id: int, data: dict) -> bool:
    """更新特定使用者的個人資料"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        if "name" in data:
            user.name = data["name"]
        if "email" in data and data["email"]:
            # 檢查 email 是否已被他人佔用
            existing = db.query(User).filter(User.email == data["email"], User.id != user_id).first()
            if existing:
                return False
            user.email = data["email"]
        if "phone" in data:
            user.phone = data["phone"]
        if "occupation" in data:
            user.occupation = data["occupation"]
        if "password" in data and data["password"]:
            user.password_hash = generate_password_hash(data["password"])
            
        db.commit()
        return True


def populate_admin_seed():
    """自動建立預設管理員帳號 (admin / admin123)"""
    with SessionLocal() as db:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            print("[db_service] 正在建立預設管理員帳戶 (admin / admin123)...")
            pwd_hash = generate_password_hash("admin123")
            admin = User(
                username="admin",
                email="admin@example.com",
                password_hash=pwd_hash,
                name="系統管理員",
                is_admin=True
            )
            db.add(admin)
            db.commit()
            print("[db_service] 預設管理員帳戶建立完成！")


def get_all_users(admin_id: int) -> list[dict]:
    """取得所有註冊使用者列表 (限管理員)"""
    with SessionLocal() as db:
        # 驗證管理員身份
        admin = db.query(User).filter(User.id == admin_id, User.is_admin == True).first()
        if not admin:
            return []
        users = db.query(User).order_by(User.id.asc()).all()
        return [{
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "name": u.name or "",
            "phone": u.phone or "",
            "occupation": u.occupation or "",
            "is_admin": u.is_admin,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else None
        } for u in users]


def delete_user_by_admin(admin_id: int, target_user_id: int) -> bool:
    """刪除指定使用者及其自選股資料 (限管理員，且不能刪除自己)"""
    if admin_id == target_user_id:
        return False
    with SessionLocal() as db:
        # 驗證管理員身份
        admin = db.query(User).filter(User.id == admin_id, User.is_admin == True).first()
        if not admin:
            return False
        # 尋找目標使用者
        target = db.query(User).filter(User.id == target_user_id).first()
        if not target:
            return False
        # 刪除自選股與自選清單資料 (Watchlist & UserWatchlist)
        db.query(Watchlist).filter(Watchlist.user_id == target_user_id).delete()
        db.query(UserWatchlist).filter(UserWatchlist.user_id == target_user_id).delete()
        # 刪除使用者
        db.delete(target)
        db.commit()
        return True


def reset_user_password_by_admin(admin_id: int, target_user_id: int, new_password: str) -> bool:
    """強制重設指定使用者的密碼 (限管理員)"""
    if not new_password or len(new_password) < 6:
        return False
    with SessionLocal() as db:
        # 驗證管理員身份
        admin = db.query(User).filter(User.id == admin_id, User.is_admin == True).first()
        if not admin:
            return False
        # 尋找目標使用者
        target = db.query(User).filter(User.id == target_user_id).first()
        if not target:
            return False
        target.password_hash = generate_password_hash(new_password)
        db.commit()
        return True


def get_all_stocks_list() -> list[dict]:
    """取得資料庫中所有的股票清單及其歷史價格更新統計（用於管理後台）"""
    with SessionLocal() as db:
        sql = text("""
            SELECT 
                si.code, si.name, si.market, si.industry,
                MAX(dp.date) as last_date, COUNT(dp.date) as price_count
            FROM stock_info si
            LEFT JOIN daily_price dp ON si.code = dp.code
            GROUP BY si.code, si.name, si.market, si.industry
            ORDER BY si.code ASC
        """)
        db_rows = db.execute(sql).fetchall()
        return [{
            "code": r.code,
            "name": r.name,
            "market": r.market or "N/A",
            "industry": r.industry or "N/A",
            "last_date": r.last_date.strftime("%Y-%m-%d") if r.last_date else "無",
            "price_count": r.price_count
        } for r in db_rows]


def cleanup_orphan_stocks() -> dict:
    """
    刪除資料庫中沒有任何歷史股價的衍生商品代號，包含：
    - 純 6 碼數字代號（認購/認售權證、存託憑證 DR）如 020035、738739
    - 混合格式的無效代號（如 2887Z1、910322 DR）
    
    保留：
    - 4 碼純數字（一般股票）
    - 5~6 碼含字母結尾（特別股如 2881B、主動式ETF如 00403A）
    
    回傳：{'deleted': 刪除筆數, 'remaining': 剩餘筆數}
    """
    with SessionLocal() as db:
        # 計算符合刪除條件的代號數量（預覽用）
        # 條件：無歷史股價 AND (純6碼數字 OR 6碼混合含非字母數字 OR 純數字但非4碼一般股/ETF)
        count_sql = text("""
            SELECT COUNT(*) FROM stock_info
            WHERE code NOT IN (SELECT DISTINCT code FROM daily_price)
            AND (
                code ~ '^[0-9]{6}$'
                OR (LENGTH(code) = 6 AND code !~ '^00[0-9]')
            )
        """)
        to_delete = db.execute(count_sql).scalar()

        # 執行刪除
        delete_sql = text("""
            DELETE FROM stock_info
            WHERE code NOT IN (SELECT DISTINCT code FROM daily_price)
            AND (
                code ~ '^[0-9]{6}$'
                OR (LENGTH(code) = 6 AND code !~ '^00[0-9]')
            )
        """)
        db.execute(delete_sql)
        db.commit()

        # 計算剩餘筆數
        remaining_sql = text("SELECT COUNT(*) FROM stock_info")
        remaining = db.execute(remaining_sql).scalar()

        return {
            "deleted": to_delete,
            "remaining": remaining
        }


def get_latest_price_and_change(code: str):
    """從 DailyPrice 中取得最新收盤價與前一日收盤價，計算當日漲跌"""
    with SessionLocal() as db:
        rows = db.query(DailyPrice)\
            .filter(DailyPrice.code == code)\
            .order_by(DailyPrice.date.desc())\
            .limit(2)\
            .all()
        if not rows:
            return None, None
        
        latest_price = float(rows[0].close) if rows[0].close is not None else None
        if len(rows) >= 2:
            prev_price = float(rows[1].close) if rows[1].close is not None else None
            change = latest_price - prev_price if (latest_price is not None and prev_price is not None) else None
        else:
            change = 0.0
            
        return latest_price, change


def get_latest_prices_and_changes(codes: list[str]) -> dict:
    """批次從 DailyPrice 中取得最新收盤價、開高低價、成交量與前一日收盤價"""
    if not codes:
        return {}
    
    with SessionLocal() as db:
        sql = text("""
            WITH ranked_prices AS (
                SELECT 
                    code, close, open, high, low, volume, date,
                    ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) as rn
                FROM daily_price
                WHERE code IN :codes
            )
            SELECT code, close, open, high, low, volume, rn
            FROM ranked_prices
            WHERE rn <= 2
        """)
        rows = db.execute(sql, {"codes": tuple(codes)}).fetchall()
        
        temp = {}
        for r in rows:
            if r.code not in temp:
                temp[r.code] = []
            temp[r.code].append(r)
            
        result = {}
        for code in codes:
            records = temp.get(code, [])
            if not records:
                result[code] = {
                    "price": None, "change": 0.0, "changePercent": 0.0,
                    "open": None, "high": None, "low": None, "volume": 0,
                    "prevClose": None, "buy": None, "sell": None
                }
                continue
            
            latest = records[0]
            latest_price = float(latest.close) if latest.close is not None else None
            open_p = float(latest.open) if latest.open is not None else latest_price
            high_p = float(latest.high) if latest.high is not None else latest_price
            low_p  = float(latest.low) if latest.low is not None else latest_price
            vol    = int(latest.volume) if latest.volume is not None else 0
            
            if len(records) >= 2 and records[1].close is not None:
                prev_price = float(records[1].close)
                change = latest_price - prev_price if latest_price is not None else 0.0
                change_percent = (change / prev_price * 100) if prev_price else 0.0
            else:
                prev_price = open_p if open_p else latest_price
                change = (latest_price - prev_price) if (latest_price and prev_price) else 0.0
                change_percent = (change / prev_price * 100) if prev_price else 0.0

            buy_p = round(latest_price - 0.05, 2) if latest_price else None
            sell_p = round(latest_price + 0.05, 2) if latest_price else None
            
            result[code] = {
                "price": latest_price,
                "change": change,
                "changePercent": change_percent,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "volume": vol,
                "prevClose": prev_price,
                "buy": buy_p,
                "sell": sell_p
            }
        return result



# ===================================================================
# 使用者風險屬性評估結果 CRUD
# ===================================================================

def save_risk_profile(user_id: int, score: int, profile: str, answers: dict) -> bool:
    """儲存（或更新）使用者的風險評估結果"""
    try:
        with SessionLocal() as db:
            existing = db.query(UserRiskProfile).filter(UserRiskProfile.user_id == user_id).first()
            if existing:
                existing.score = score
                existing.profile = profile
                existing.answers = answers
                existing.taken_at = datetime.utcnow()
            else:
                record = UserRiskProfile(
                    user_id=user_id,
                    score=score,
                    profile=profile,
                    answers=answers,
                )
                db.add(record)
            db.commit()
            return True
    except Exception as e:
        print(f'[save_risk_profile] 錯誤: {e}')
        return False


def get_risk_profile(user_id: int) -> dict | None:
    """取得使用者的風險評估結果，若無記錄則回傳 None"""
    try:
        with SessionLocal() as db:
            record = db.query(UserRiskProfile).filter(UserRiskProfile.user_id == user_id).first()
            if not record:
                return None
            return {
                'score': record.score,
                'profile': record.profile,
                'answers': record.answers or {},
                'taken_at': record.taken_at.strftime('%Y-%m-%d %H:%M') if record.taken_at else None,
            }
    except Exception as e:
        print(f'[get_risk_profile] 錯誤: {e}')
        return None
