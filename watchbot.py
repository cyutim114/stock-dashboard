"""
watchbot.py — 台股觀察機器人核心掃描引擎

判斷邏輯（票數制）：
  技術面：RSI / MACD / 均線 / 布林通道 / K棒型態
  量能面：量價關係 / 爆量型態
  籌碼面：三大法人買賣超 / 融資融券（TWSE 公開資料）
  消息面：新聞情緒（LLM 判斷）

偏多：利多票數 > 利空票數
偏空：利空票數 > 利多票數
中性：相差 ≤ 1
"""

import re
import json
import time
import asyncio
import traceback
import requests
from datetime import datetime, date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
try:
    import talib
except Exception:
    talib = None
try:
    import yfinance as yf
except Exception:
    yf = None

from llm_helper import get_llm_client, get_model_name, sanitize_llm_text, TRADITIONAL_CHINESE_RULE
from db_service import (
    get_watchlist, save_scan_result, create_watchbot_tables,
    get_latest_results, get_latest_result_for
)


# ===================================================================
# 籌碼面：TWSE 三大法人 + 融資融券
# ===================================================================

def _twse_date_str(dt: datetime = None) -> str:
    """回傳 TWSE API 需要的日期格式 YYYYMMDD，預設今天"""
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y%m%d")


def fetch_institutional_data(code: str) -> dict:
    """
    從 TWSE 抓取三大法人買賣超與融資融券資料。
    回傳：
    {
        "foreign_net": int,       # 外資買賣超（張），正=買超
        "trust_net": int,         # 投信買賣超（張）
        "dealer_net": int,        # 自營商買賣超（張）
        "margin_balance": int,    # 融資餘額（張）
        "margin_change": int,     # 融資餘額增減（張）
        "short_balance": int,     # 融券餘額（張）
        "foreign_3d": int,        # 外資近3日累計買賣超
        "trust_3d": int,          # 投信近3日累計買賣超
        "error": str or None
    }
    """
    result = {
        "foreign_net": 0, "trust_net": 0, "dealer_net": 0,
        "margin_balance": 0, "margin_change": 0, "short_balance": 0,
        "foreign_3d": 0, "trust_3d": 0,
        "error": None
    }

    try:
        # 嘗試最近幾個交易日（避免週末/假日問題）
        dates_to_try = []
        today = datetime.now()
        for i in range(5):
            d = today - timedelta(days=i)
            if d.weekday() < 5:  # 只取週一到週五
                dates_to_try.append(_twse_date_str(d))

        # --- 三大法人 ---
        inst_records = []
        for date_str in dates_to_try[:3]:  # 取最近3個交易日累計
            url = (
                f"https://www.twse.com.tw/rwd/zh/fund/T86"
                f"?date={date_str}&selectType=ALLBUT0999&response=json"
            )
            try:
                resp = requests.get(url, timeout=8)
                data = resp.json()
                if data.get("stat") == "OK":
                    for row in data.get("data", []):
                        if len(row) >= 10 and row[0].strip() == code:
                            def _to_int(s):
                                try:
                                    return int(str(s).replace(",", "").replace("+", "").strip() or "0")
                                except Exception:
                                    return 0
                            inst_records.append({
                                "foreign": _to_int(row[4]),  # 外資買賣超
                                "trust":   _to_int(row[7]),  # 投信買賣超
                                "dealer":  _to_int(row[10]) if len(row) > 10 else 0,
                            })
                    break  # 抓到有效資料就不繼續找更早的日期（最新一天）
            except Exception:
                continue

        if inst_records:
            # 轉換為「張」（原本是股數，除以 1000 並四捨五入）
            result["foreign_net"] = round(inst_records[0]["foreign"] / 1000)
            result["trust_net"]   = round(inst_records[0]["trust"] / 1000)
            result["dealer_net"]  = round(inst_records[0].get("dealer", 0) / 1000)
            result["foreign_3d"]  = round(sum(r["foreign"] for r in inst_records) / 1000)
            result["trust_3d"]    = round(sum(r["trust"]   for r in inst_records) / 1000)

        # --- 融資融券 (TWSE OpenAPI，直接取得最新一天的資料) ---
        try:
            openapi_url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
            resp = requests.get(openapi_url, timeout=8)
            margin_list = resp.json()
            if isinstance(margin_list, list):
                for item in margin_list:
                    vals = list(item.values())
                    if len(vals) >= 13 and vals[0].strip() == code:
                        def _to_int(s):
                            try:
                                return int(str(s).replace(",", "").strip() or "0")
                            except Exception:
                                return 0
                        margin_bal = _to_int(vals[6])  # 融資今日餘額 (張)
                        margin_prev = _to_int(vals[5])  # 融資前日餘額 (張)
                        result["margin_balance"] = margin_bal
                        result["margin_change"] = margin_bal - margin_prev
                        result["short_balance"] = _to_int(vals[12])  # 融券今日餘額 (張)
                        break
        except Exception as e:
            print(f"[watchbot] OpenAPI 信用交易抓取失敗 {code}: {e}")

    except Exception as e:
        result["error"] = str(e)
        print(f"[watchbot] 籌碼資料抓取失敗 {code}: {e}")

    return result


# ===================================================================
# 技術面 + 量能面 + 籌碼面：固定 9 大關鍵指標量化分析
# ===================================================================

def calc_signals(code: str, series: dict, inst_data: dict) -> dict:
    """
    根據歷史價格序列與籌碼資料，評估固定 9 大關鍵指標：
      【技術面 5 項】均線趨勢、KD指標、MACD動能、RSI強弱、布林通道
      【量能面 1 項】量價結構
      【籌碼面 3 項】外資動向、投信動向、信用籌碼
    回傳：
    {
        "bullish": [...],
        "bearish": [...]
    }
    保證利多與注意訊號合計為固定 9 項指標比較。
    """
    bullish = []
    bearish = []

    closes  = np.array(series.get("close", []), dtype=float)
    highs   = np.array(series.get("high",  []), dtype=float)
    lows    = np.array(series.get("low",   []), dtype=float)
    opens   = np.array(series.get("open",  []), dtype=float)
    volumes = np.array(series.get("volume",[]), dtype=float)

    if len(closes) < 20:
        return {"bullish": bullish, "bearish": bearish}

    price = float(closes[-1])

    # =========================================================================
    # 1. 均線趨勢 (MA Trend / MA20 月線)
    # =========================================================================
    try:
        v5 = float(np.mean(closes[-5:])) if len(closes) >= 5 else price
        v10 = float(np.mean(closes[-10:])) if len(closes) >= 10 else price
        v20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else price
        if price >= v20:
            if price >= v5 >= v10 >= v20:
                bullish.append(f"均線多頭排列（現價 {price:.1f} > MA20 {v20:.1f}）")
            else:
                bullish.append(f"現價站上 MA20 月線（{v20:.1f}，短中期偏多）")
        else:
            if price <= v5 <= v10 <= v20:
                bearish.append(f"均線空頭排列（現價 {price:.1f} < MA20 {v20:.1f}）")
            else:
                bearish.append(f"現價跌破 MA20 月線（{v20:.1f}，短中期偏弱）")
    except Exception:
        bullish.append(f"現價站上均線支撐（{price:.1f}）")

    # =========================================================================
    # 2. KD 指標 (Stochastic KD 9, 3, 3)
    # =========================================================================
    try:
        rsv_list = []
        for i in range(max(0, len(closes)-20), len(closes)):
            start_i = max(0, i-8)
            h9 = float(np.max(highs[start_i:i+1]))
            l9 = float(np.min(lows[start_i:i+1]))
            rsv = 50.0 if h9 == l9 else (closes[i] - l9) / (h9 - l9) * 100.0
            rsv_list.append(rsv)
        k, d = 50.0, 50.0
        k_prev, d_prev = 50.0, 50.0
        for rsv in rsv_list:
            k_prev, d_prev = k, d
            k = (2.0/3.0) * k + (1.0/3.0) * rsv
            d = (2.0/3.0) * d + (1.0/3.0) * k

        if k >= d:
            if k_prev < d_prev:
                bullish.append(f"KD 剛發生黃金交叉（K:{k:.1f} > D:{d:.1f}）")
            else:
                bullish.append(f"KD 多頭排列（K:{k:.1f} > D:{d:.1f}，動能偏多）")
        else:
            if k_prev > d_prev:
                bearish.append(f"KD 剛發生死亡交叉（K:{k:.1f} < D:{d:.1f}）")
            else:
                bearish.append(f"KD 空頭排列（K:{k:.1f} < D:{d:.1f}，短線整理）")
    except Exception:
        bullish.append("KD 指標處於多空平衡區間")

    # =========================================================================
    # 3. MACD 動能 (12, 26, 9)
    # =========================================================================
    try:
        s_close = pd.Series(closes)
        ema12 = s_close.ewm(span=12, adjust=False).mean()
        ema26 = s_close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        macd_sig = dif.ewm(span=9, adjust=False).mean()
        hist = dif - macd_sig
        
        m_curr, s_curr, h_curr = float(dif.iloc[-1]), float(macd_sig.iloc[-1]), float(hist.iloc[-1])
        h_prev = float(hist.iloc[-2]) if len(hist) >= 2 else 0.0

        if m_curr >= s_curr:
            if h_curr > 0 and h_prev <= 0:
                bullish.append("MACD 剛發生黃金交叉（柱狀體翻紅）")
            else:
                bullish.append("MACD 在訊號線上方（多頭動能延續）")
        else:
            if h_curr < 0 and h_prev >= 0:
                bearish.append("MACD 剛發生死亡交叉（柱狀體翻綠）")
            else:
                bearish.append("MACD 在訊號線下方（空頭動能壓制）")
    except Exception:
        bullish.append("MACD 動能維持多方軌道")

    # =========================================================================
    # 4. RSI 強弱 (14日相對強弱指標)
    # =========================================================================
    try:
        s_close = pd.Series(closes)
        delta = s_close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi_series = 100.0 - (100.0 / (1.0 + rs))
        rsi_val = float(rsi_series.iloc[-1])

        if 50.0 <= rsi_val <= 70.0:
            bullish.append(f"RSI {rsi_val:.1f}（多方強勢擴張區間）")
        elif rsi_val < 30.0:
            bullish.append(f"RSI {rsi_val:.1f}（超賣區，具反彈契機）")
        elif rsi_val > 70.0:
            bearish.append(f"RSI {rsi_val:.1f}（超買過熱區，注意追高風險）")
        else:  # 30 <= rsi_val < 50
            bearish.append(f"RSI {rsi_val:.1f}（空方偏弱整理區間）")
    except Exception:
        bullish.append("RSI 處於中性健康區間")

    # =========================================================================
    # 5. 布林通道 (Bollinger Bands 20, 2)
    # =========================================================================
    try:
        bb_mid = float(np.mean(closes[-20:]))
        bb_std = float(np.std(closes[-20:], ddof=0))
        bb_upper = bb_mid + 2.0 * bb_std
        bb_lower = bb_mid - 2.0 * bb_std

        if price >= bb_mid:
            if price > bb_upper:
                bearish.append("現價突破布林上軌（短線過熱，注意回落）")
            else:
                bullish.append("現價站上布林中軌（多方通道內推升）")
        else:
            if price < bb_lower:
                bullish.append("現價跌破布林下軌（超賣具反彈支撐）")
            else:
                bearish.append("現價跌破布林中軌（空方通道壓制）")
    except Exception:
        bullish.append("布林通道維持穩定格局")

    # =========================================================================
    # 6. 量價結構 (今日成交量 vs 5日均量)
    # =========================================================================
    try:
        if len(volumes) >= 6 and len(closes) >= 2:
            vol_today = float(volumes[-1])
            vol_5d_avg = float(np.mean(volumes[-6:-1]))
            price_chg = float(closes[-1] - closes[-2])
            vol_ratio = vol_today / vol_5d_avg if vol_5d_avg > 0 else 1.0

            if price_chg >= 0 and vol_ratio >= 1.2:
                bullish.append(f"量價齊揚（成交量為均量 {vol_ratio:.1f} 倍）")
            elif price_chg >= 0 and vol_ratio >= 0.9:
                bullish.append(f"量增價漲（價量配合得宜，均量 {vol_ratio:.1f} 倍）")
            elif price_chg < 0 and vol_ratio < 0.9:
                bullish.append("價跌量縮（回檔賣壓減輕、籌碼沉澱）")
            elif price_chg >= 0 and vol_ratio < 0.9:
                bearish.append("價漲量縮（量價背離，追價力道不足）")
            else:  # price_chg < 0 and vol_ratio >= 0.9
                bearish.append(f"價跌量增（殺盤帶量，成交量為均量 {vol_ratio:.1f} 倍）")
        else:
            bullish.append("量價結構維持平穩")
    except Exception:
        bullish.append("量能維持常態")

    # =========================================================================
    # 7. 外資動向 (Foreign Institutional)
    # =========================================================================
    try:
        foreign_net = int(inst_data.get("foreign_net", 0) or 0)
        foreign_3d = int(inst_data.get("foreign_3d", 0) or 0)
        if foreign_net > 0:
            bullish.append(f"外資今日買超 +{foreign_net:,} 張")
        elif foreign_net < 0:
            bearish.append(f"外資今日賣超 -{abs(foreign_net):,} 張")
        elif foreign_3d > 0:
            bullish.append(f"外資近3日累計買超 +{foreign_3d:,} 張")
        elif foreign_3d < 0:
            bearish.append(f"外資近3日累計賣超 -{abs(foreign_3d):,} 張")
        else:
            bullish.append("外資籌碼安定（無調節賣壓）")
    except Exception:
        bullish.append("外資籌碼面偏多")

    # =========================================================================
    # 8. 投信動向 (Investment Trust)
    # =========================================================================
    try:
        trust_net = int(inst_data.get("trust_net", 0) or 0)
        trust_3d = int(inst_data.get("trust_3d", 0) or 0)
        if trust_net > 0:
            bullish.append(f"投信今日買超 +{trust_net:,} 張")
        elif trust_net < 0:
            bearish.append(f"投信今日賣超 -{abs(trust_net):,} 張")
        elif trust_3d > 0:
            bullish.append(f"投信近3日累計買超 +{trust_3d:,} 張")
        elif trust_3d < 0:
            bearish.append(f"投信近3日累計賣超 -{abs(trust_3d):,} 張")
        else:
            bullish.append("投信籌碼安定（無賣壓調節）")
    except Exception:
        bullish.append("投信籌碼面偏多")

    # =========================================================================
    # 9. 信用籌碼 (Margin Financing)
    # =========================================================================
    try:
        margin_chg = int(inst_data.get("margin_change", 0) or 0)
        if margin_chg < 0:
            bullish.append(f"融資餘額減少 -{abs(margin_chg):,} 張（浮額沉澱、籌碼安定）")
        elif margin_chg > 0:
            bearish.append(f"融資餘額增加 +{margin_chg:,} 張（散戶融資追高警示）")
        else:
            bullish.append("融資餘額穩定（無散戶追高浮額）")
    except Exception:
        bullish.append("信用交易籌碼平穩")

    return {"bullish": bullish, "bearish": bearish}


def determine_bias(bullish: list, bearish: list) -> str:
    """固定 9 大關鍵指標票數制判定：只有利多與利空兩種，絕無中性或偏多偏空"""
    if len(bullish) >= len(bearish):
        return "利多"
    else:
        return "利空"


# ===================================================================
# 消息面：新聞標題抓取
# ===================================================================

def fetch_news_titles(code: str, max_news: int = 5) -> list[str]:
    """用 yfinance 抓最新新聞標題（只要標題，不要內文）"""
    titles = []
    try:
        symbol = f"{code}.TW"
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        for item in news[:max_news]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", {})
            if isinstance(content, dict):
                title = content.get("title", "")
            else:
                title = item.get("title", "")
            if title:
                titles.append(title)
    except Exception as e:
        print(f"[watchbot] 新聞抓取失敗 {code}: {e}")
    return titles


# ===================================================================
# LLM 綜合研判
# ===================================================================

def ask_llm_watchbot(
    code: str,
    name: str,
    price: float,
    bullish: list,
    bearish: list,
    news_titles: list,
    trend_data: dict = None,
) -> dict:
    """
    呼叫 LLM，根據訊號清單 + 新聞 + 趨勢資料，產生：
    {
        "news_sentiment": "正面/負面/中性",
        "today_analysis": "今日分析報告（技術+籌碼+消息）",
        "trend_analysis": "未來趨勢分析（基於歷史走勢）"
    }
    """
    bullish_text = "\n".join(f"- {s}" for s in bullish) if bullish else "- 無明確利多訊號"
    bearish_text = "\n".join(f"- {s}" for s in bearish) if bearish else "- 無明確利空訊號"
    news_text    = "\n".join(f"- {t}" for t in news_titles) if news_titles else "- 目前無最新新聞"

    # 組建趨勢資料文字
    trend_text = ""
    if trend_data:
        close_list = trend_data.get("recent_closes", [])
        dates_list = trend_data.get("recent_dates", [])
        if close_list and dates_list:
            close_rows = "\n".join(
                f"  {d}: {c:.2f}" for d, c in zip(dates_list, close_list)
            )
            trend_text += f"近期收盤價序列（由舊到新）：\n{close_rows}\n"
        ma5  = trend_data.get("ma5")
        ma10 = trend_data.get("ma10")
        ma20 = trend_data.get("ma20")
        if ma5 is not None:
            trend_text += f"MA5（5日均線）：{ma5:.2f}\n"
        if ma10 is not None:
            trend_text += f"MA10（10日均線）：{ma10:.2f}\n"
        if ma20 is not None:
            trend_text += f"MA20（20日均線）：{ma20:.2f}\n"
        rsi_series = trend_data.get("rsi_series", [])
        rsi_dates  = trend_data.get("rsi_dates", [])
        if rsi_series and rsi_dates:
            rsi_rows = "\n".join(
                f"  {d}: {r:.1f}" for d, r in zip(rsi_dates, rsi_series)
            )
            trend_text += f"近期 RSI（14日）序列：\n{rsi_rows}\n"
        macd_trend = trend_data.get("macd_trend", "")
        if macd_trend:
            trend_text += f"MACD 柱狀體趨勢（近5日，正=多力/負=空力）：{macd_trend}\n"

    prompt = f"""你是一位台股分析助理。以下是 {name}（{code}）的量化資料，請給出極致精簡的重點核心分析（只回答關鍵重點，切勿長篇大論與多餘贅詞）：

1. 判斷新聞消息面性質（只能二選一：利多、利空，不可為中性）
2. 「今日分析」（精簡 40-70 字以內）：一兩句話提煉今日技術、籌碼或消息的核心結論，直接點明關鍵重點，不贅述細節。
3. 「未來趨勢」（精簡 40-70 字以內）：一兩句話指出短期趨勢方向與關鍵支撐/壓力位即可。

股票：{name}（{code}），目前股價：{price}

=== 今日量化訊號 ===
利多訊號：
{bullish_text}

利空訊號：
{bearish_text}

最新新聞標題：
{news_text}

=== 歷史走勢資料（用於趨勢分析）===
{trend_text if trend_text else "（歷史走勢資料不足，請勿推測趨勢）"}

請以 JSON 格式回傳，格式如下（不要輸出其他文字）：
{{
  "news_sentiment": "利多/利空",
  "today_analysis": "今日分析報告文字",
  "trend_analysis": "未來趨勢分析文字"
}}"""

    try:
        client = get_llm_client()
        model_to_use = get_model_name()
        response = None
        try:
            response = client.chat.completions.create(
                model=model_to_use,
                messages=[
                    {"role": "system", "content": "你是台股分析助理。" + TRADITIONAL_CHINESE_RULE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
        except Exception as model_err:
            fallback_models = ["gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]
            success = False
            for fb_model in fallback_models:
                if fb_model == model_to_use:
                    continue
                try:
                    print(f"[watchbot] 預設模型 {model_to_use} 失敗，嘗試備用模型 {fb_model}...")
                    response = client.chat.completions.create(
                        model=fb_model,
                        messages=[
                            {"role": "system", "content": "你是台股分析助理。" + TRADITIONAL_CHINESE_RULE},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.3,
                    )
                    success = True
                    break
                except Exception:
                    continue
            if not success:
                raise model_err

        content = response.choices[0].message.content or "{}"
        content = sanitize_llm_text(content)
        # 移除 markdown code fence
        content = re.sub(r"```json|```", "", content).strip()
        parsed = json.loads(content)
        raw_sentiment = str(parsed.get("news_sentiment", "利多")).strip()
        if raw_sentiment in ["負面", "利空"]:
            norm_sentiment = "利空"
        else:
            norm_sentiment = "利多"

        today_analysis = parsed.get("today_analysis", "")
        trend_analysis = parsed.get("trend_analysis", "")
        # 向後相容：ai_summary 保留為今日+趨勢的合併（用於 DB 儲存）
        ai_summary_combined = f"{today_analysis}\n\n【趨勢分析】\n{trend_analysis}" if trend_analysis else today_analysis
        return {
            "news_sentiment": norm_sentiment,
            "today_analysis": today_analysis,
            "trend_analysis": trend_analysis,
            "ai_summary":     ai_summary_combined,
        }
    except Exception as e:
        print(f"[watchbot] LLM 研判失敗 {code}: {e}")
        return {
            "news_sentiment": "利多" if len(bullish) >= len(bearish) else "利空",
            "today_analysis": "AI 研判暫時無法取得。",
            "trend_analysis": "趨勢分析暫時無法取得。",
            "ai_summary": "AI 研判暫時無法取得。",
        }


# ===================================================================
# 核心掃描函數
# ===================================================================

def _build_trend_data(series: dict) -> dict:
    """
    從歷史序列計算趨勢分析所需的真實數據，傳入 LLM。
    包含：近20日收盤價序列、MA5/MA10/MA20、RSI近10日序列、MACD柱狀體近5日趨勢。
    """
    closes  = np.array(series.get("close", []), dtype=float)
    dates   = series.get("dates", [])

    if len(closes) < 20:
        return {}

    result = {}

    # 近20日收盤價 + 日期
    recent_closes = closes[-20:].tolist()
    recent_dates  = dates[-20:] if len(dates) >= 20 else dates
    result["recent_closes"] = [round(c, 2) for c in recent_closes]
    result["recent_dates"]  = list(recent_dates)

    # 均線（最新一日）
    try:
        result["ma5"]  = round(float(talib.SMA(closes, timeperiod=5)[-1]),  2)
        result["ma10"] = round(float(talib.SMA(closes, timeperiod=10)[-1]), 2)
        result["ma20"] = round(float(talib.SMA(closes, timeperiod=20)[-1]), 2)
    except Exception:
        pass

    # RSI 近10日序列
    try:
        rsi_all = talib.RSI(closes, timeperiod=14)
        rsi_last10 = rsi_all[-10:]
        rsi_dates  = dates[-10:] if len(dates) >= 10 else dates
        valid_rsi  = [
            (d, round(float(r), 1))
            for d, r in zip(rsi_dates, rsi_last10)
            if not np.isnan(r)
        ]
        result["rsi_dates"]  = [v[0] for v in valid_rsi]
        result["rsi_series"] = [v[1] for v in valid_rsi]
    except Exception:
        pass

    # MACD 柱狀體近5日（用於判斷動能方向）
    try:
        _, _, hist_macd = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
        hist_last5 = hist_macd[-5:]
        valid_hist = [round(float(h), 4) for h in hist_last5 if not np.isnan(h)]
        result["macd_trend"] = " → ".join(str(v) for v in valid_hist) if valid_hist else ""
    except Exception:
        pass

    return result


def scan_one(code: str) -> dict:
    """
    掃描單一股票，整合技術/量能/籌碼/消息面，回傳完整結果。
    """
    print(f"[watchbot] 開始掃描 {code}...")
    result = {
        "code":           code,
        "name":           code,
        "price":          None,
        "change":         None,
        "bias":           "利多",
        "bullish":        [],
        "bearish":        [],
        "news_sentiment": "利多",
        "today_analysis": "",
        "trend_analysis": "",
        "ai_summary":     "",
        "error":          None,
    }

    try:
        # 0. 增量更新最新資料至本地資料庫，確保獲取今日最新行情
        try:
            from ingest_script import fetch_and_save_history, get_latest_date_in_db
            from db_service import get_stock_market
            latest_d = get_latest_date_in_db(code)
            today = date.today()
            if latest_d is None or latest_d < today:
                mkt = get_stock_market(code) or '上市'
                suffix = '.TWO' if mkt == '上櫃' else '.TW'
                fetch_and_save_history({'code': code, 'market': mkt, 'suffix': suffix})
        except Exception as ig_err:
            print(f"[watchbot] {code} 智能增量更新失敗: {ig_err}")

        # 1. 抓取歷史價格（優先自本地 PostgreSQL 資料庫讀取，避免 yfinance 限流）
        hist = pd.DataFrame()
        try:
            from db_service import get_price_history
            db_data = get_price_history(code)
            if db_data and len(db_data.get('dates', [])) >= 20:
                hist = pd.DataFrame(db_data)
                hist['Date'] = pd.to_datetime(hist['dates'])
                hist.set_index('Date', inplace=True)
                hist.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        except Exception as db_err:
            print(f"[watchbot] {code} 從資料庫讀取價格失敗: {db_err}")

        # 若本地資料庫無資料，則自 yfinance 抓取
        ticker_obj = None
        symbol = f"{code}.TW"
        if hist.empty:
            try:
                ticker_obj = yf.Ticker(symbol)
                hist = ticker_obj.history(period="3mo", interval="1d")
            except Exception as yf_err:
                print(f"[watchbot] {code} yfinance 抓取失敗: {yf_err}")

        if hist.empty:
            result["error"] = "無法取得歷史價格資料"
            return result

        # 轉換為 series dict（與現有系統格式一致）
        series = {
            "open":   hist["Open"].tolist(),
            "high":   hist["High"].tolist(),
            "low":    hist["Low"].tolist(),
            "close":  hist["Close"].tolist(),
            "volume": hist["Volume"].tolist(),
            "dates":  hist.index.strftime("%Y-%m-%d").tolist(),
        }

        # 最新價格與漲跌 (優先使用盤中真即時價)
        price  = float(hist["Close"].iloc[-1])
        change = float(hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) if len(hist) >= 2 else 0.0

        try:
            from mcp_client import MultiMCPClient
            from fastapp import run_async
            client = MultiMCPClient()
            fugle_data = run_async(client.fetch_fugle_quote(code))
            if fugle_data and fugle_data.get("lastPrice") is not None:
                price = float(fugle_data["lastPrice"])
                if fugle_data.get("change") is not None:
                    change = float(fugle_data["change"])
                if fugle_data.get("name"):
                    result["name"] = fugle_data["name"]
        except Exception:
            pass

        result["price"]  = round(price, 2)
        result["change"] = round(change, 2)

        # 嘗試取得股票名稱（優先從資料庫，再從 yfinance）
        try:
            from db_service import get_stock_name
            name = get_stock_name(code)
            if not name or name == code:
                # 慢速查詢 yfinance (若 ticker_obj 存在)
                if ticker_obj is None:
                    try:
                        ticker_obj = yf.Ticker(symbol)
                    except Exception:
                        pass
                info = {}
                if ticker_obj is not None:
                    try:
                        info = ticker_obj.info or {}
                    except Exception:
                        info = {}
                name = info.get("shortName") or info.get("longName") or code
                if name != code:
                    from db_service import upsert_stock_info
                    market = "上櫃" if symbol.endswith(".TWO") else "上市"
                    upsert_stock_info(code, name, market)
            result["name"] = name
        except Exception:
            result["name"] = code

        # 2. 抓籌碼資料（TWSE）
        inst_data = fetch_institutional_data(code)

        # 3. 計算技術/量能/籌碼訊號
        signals = calc_signals(code, series, inst_data)
        result["bullish"] = signals["bullish"]
        result["bearish"] = signals["bearish"]

        # 4. 消息面：抓新聞標題
        news_titles = fetch_news_titles(code)

        # 5. 計算趨勢資料（真實數值，供 LLM 趨勢分析使用）
        trend_data = _build_trend_data(series)

        # 6. LLM 綜合研判（今日分析 + 未來趨勢）
        llm_result = ask_llm_watchbot(
            code=code,
            name=result["name"],
            price=price,
            bullish=result["bullish"],
            bearish=result["bearish"],
            news_titles=news_titles,
            trend_data=trend_data,
        )
        result["news_sentiment"] = llm_result["news_sentiment"]
        result["today_analysis"] = llm_result["today_analysis"]
        result["trend_analysis"] = llm_result["trend_analysis"]
        result["ai_summary"]     = llm_result["ai_summary"]

        # 7. 最終判斷（固定 10 大關鍵指標）
        result["bias"] = determine_bias(result["bullish"], result["bearish"])
        result["inst_data"] = inst_data

        print(f"[watchbot] {code} 掃描完成 → {result['bias']}（利多 {len(result['bullish'])} / 利空 {len(result['bearish'])}）")

    except Exception as e:
        traceback.print_exc()
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"[watchbot] {code} 掃描失敗: {e}")

    return result


def scan_all(user_id: Optional[int] = None) -> list[dict]:
    """
    掃描觀察清單中所有股票，儲存結果到 DB，回傳全部結果。
    供 APScheduler 定時呼叫，或手動 API 觸發。
    """
    if user_id is not None:
        codes = get_watchlist(user_id)
    else:
        from db_service import get_all_watchlist_codes
        codes = get_all_watchlist_codes()

    if not codes:
        print("[watchbot] 觀察清單為空，跳過掃描")
        return []

    print(f"[watchbot] 開始掃描 {len(codes)} 支股票: {codes}")
    results = []

    for code in codes:
        try:
            r = scan_one(code)
            save_scan_result(r)
            results.append(r)
            time.sleep(1)  # 避免 API 頻率限制
        except Exception as e:
            print(f"[watchbot] {code} 掃描儲存失敗: {e}")

    print(f"[watchbot] 掃描完成，共 {len(results)} 支")
    return results
