import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from tqdm import tqdm

from db_service import upsert_daily_prices, upsert_stock_info
from models import SessionLocal, StockInfo, DailyPrice
from sqlalchemy import func


def get_latest_date_in_db(code: str):
    """
    查詢資料庫中指定股票最新一筆資料的日期。
    若無資料回傳 None。
    """
    with SessionLocal() as db:
        result = db.query(func.max(DailyPrice.date)).filter(DailyPrice.code == code).scalar()
        return result  # 回傳 datetime.date 物件或 None


def get_existing_stocks_from_db():
    """
    從資料庫讀取現有的股票清單，不再從 TWSE/TPEx API 重新抓取。
    這樣可確保你手動清理的特殊股（認購/認售權證、DR 等）不會被重新加回來。
    """
    print("正在從資料庫讀取現有股票清單...")
    stocks = []
    with SessionLocal() as db:
        rows = db.query(StockInfo).all()
        for r in rows:
            if r.market == '上市':
                suffix = '.TW'
            elif r.market == '上櫃':
                suffix = '.TWO'
            else:
                suffix = '.TW'
            stocks.append({
                'code': r.code,
                'market': r.market or '上市',
                'suffix': suffix
            })
    print(f"資料庫中共有 {len(stocks)} 檔股票，開始增量更新歷史股價...")
    return stocks


def fetch_and_save_history(stock_info: dict, period: str = "1y") -> bool:
    """
    智能增量更新：
    - 若資料庫已有資料，只抓「最新日期 + 1 天」到今天的新資料。
    - 若資料庫完全沒有資料（初次匯入），才做一次完整 period（預設 1 年）的全量抓取。
    - 若資料已包含今日（latest_in_db >= today），跳過不抓。
    """
    code = stock_info['code']
    if code == '__RESTART_SERVER__':
        import os, time, threading
        def _exit_proc():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit_proc, daemon=True).start()
        return True

    symbol = f"{code}{stock_info['suffix']}"
    today = date.today()

    try:
        ticker = yf.Ticker(symbol)
        latest_in_db = get_latest_date_in_db(code)

        if latest_in_db is not None:
            # ── 增量模式 ──
            if latest_in_db >= today:
                # 資料庫日期已是最新的今天，跳過
                return True

            days_behind = (today - latest_in_db).days
            # 只抓缺少的那段（最新日期的隔天開始）
            start_str = (latest_in_db + timedelta(days=1)).strftime('%Y-%m-%d')
            # yfinance 的 end 參數是不包含當天的 (exclusive)，因此需傳入 today + 1 天才能抓到今天的收盤價
            end_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')
            df = ticker.history(start=start_str, end=end_str)
            mode = f"增量 ({start_str} ~ {today.strftime('%Y-%m-%d')}，{days_behind} 天)"
        else:
            # ── 初次匯入：全量抓取 ──
            df = ticker.history(period=period)
            mode = f"初次匯入（全量 {period}）"

        if df.empty:
            print(f"\n[{code}] 找不到新資料（{mode}）")
            return False

        records = []
        for date_idx, row in df.iterrows():
            def safe_float(val):
                if pd.isna(val):
                    return None
                return float(val)

            def safe_int(val):
                if pd.isna(val):
                    return None
                return int(val)

            records.append({
                "date":   date_idx.strftime('%Y-%m-%d'),
                "open":   safe_float(row.get('Open')),
                "high":   safe_float(row.get('High')),
                "low":    safe_float(row.get('Low')),
                "close":  safe_float(row.get('Close')),
                "volume": safe_int(row.get('Volume'))
            })

        upsert_daily_prices(code, records)
        print(f"\n[{code}] [OK] {mode}，寫入 {len(records)} 筆")
        return True

    except Exception as e:
        print(f"\n[{code}] 抓取失敗: {e}")
        return False


def main():
    all_stocks = get_existing_stocks_from_db()
    if not all_stocks:
        print("資料庫中沒有股票資料，程式結束。")
        return

    today = date.today()
    print(f"今日日期：{today}")
    print(f"準備增量更新 {len(all_stocks)} 檔股票的歷史股價（只補缺少的日期）...")
    print("每筆間隔 1.5 秒，請保持程式運行直到完成。\n")

    success_count = 0
    skipped_count = 0

    for stock in tqdm(all_stocks, desc="歷史股價更新進度"):
        latest = get_latest_date_in_db(stock['code'])
        if latest is not None and latest >= today:
            skipped_count += 1
            continue  # 已是最新，不必呼叫 yfinance

        if fetch_and_save_history(stock):
            success_count += 1
        time.sleep(1.5)  # 避免被 yfinance 限速

    print(f"\n更新完成！成功 {success_count} 筆 / 跳過（已最新）{skipped_count} 筆 / 總共 {len(all_stocks)} 檔")


if __name__ == "__main__":
    main()
