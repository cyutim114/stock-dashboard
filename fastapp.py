import sys
# 解決 Windows 本機環境下，列印 Emoji/中文時可能引發 cp950 編碼異常的 Bug
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import re
import json
import ast
import math
import asyncio
import base64
import traceback
import os
import requests
import time
import threading
import concurrent.futures
from io import BytesIO
from pathlib import Path

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
from PIL import Image
from openai import OpenAI

import numpy as np
import talib
import yfinance as yf
import pandas as pd
from indicators import TechnicalIndicators

# 確保在引入其他自定義模組前先載入環境變數
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

from mcp_client import MultiMCPClient
from llm_helper import get_llm_client, get_model_name, TRADITIONAL_CHINESE_RULE, ask_llm_with_fallback, ask_llm_with_fallback_vision, sanitize_llm_text

try:
    from db_service import get_price_history
except ImportError:
    get_price_history = None

try:
    from db_service import (
        create_watchbot_tables, get_watchlist, add_to_watchlist,
        remove_from_watchlist, get_latest_results, get_latest_result_for,
        get_all_users, delete_user_by_admin, reset_user_password_by_admin,
        get_user_watchlists, create_user_watchlist, rename_user_watchlist,
        delete_user_watchlist, get_stock_watchlists_status, update_stock_watchlists,
        get_stock_name
    )
    from watchbot import scan_all, scan_one
    _watchbot_available = True
except ImportError as e:
    print(f"[watchbot] 模組載入失敗，觀察機器人功能停用: {e}")
    _watchbot_available = False


class SimpleTTLCache:
    def __init__(self, default_ttl=60):
        self._cache = {}
        self._default_ttl = default_ttl

    def get(self, key):
        if key in self._cache:
            val, expiry = self._cache[key]
            if time.time() < expiry:
                return val
            else:
                del self._cache[key]
        return None

    def set(self, key, value, ttl=None):
        ttl = ttl if ttl is not None else self._default_ttl
        self._cache[key] = (value, time.time() + ttl)


# 全域共享的 MCP 用戶端與快取
import importlib
import mcp_client
from dotenv import load_dotenv
load_dotenv(override=True)
importlib.reload(mcp_client)
from mcp_client import MultiMCPClient
global_mcp_client = MultiMCPClient()
data_cache = SimpleTTLCache(default_ttl=60)

# 背景事件迴圈線程 (用於維持持久化連線)
_bg_loop = None
_bg_thread = None

def log_scheduler_event(message: str):
    import datetime
    from pathlib import Path
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / "scheduler.log"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as e:
        print(f"[Scheduler Log Error] {e}")

def start_daily_update_scheduler():
    def scheduler_loop():
        import datetime
        import subprocess
        import time
        import threading
        from pathlib import Path
        
        log_scheduler_event("每日自動更新排程已啟動。17:00 更新股價，17:30 更新台股名稱。")
        last_run_date_prices = None
        last_run_date_names = None
        while True:
            try:
                now = datetime.datetime.now()
                # 1. 17:00 每日股價與觀察機器人掃描 (移至背景執行以避免阻塞排程器)
                if now.hour == 17 and now.minute == 0 and last_run_date_prices != now.date():
                    log_scheduler_event("觸發下午 5:00 每日股市資訊自動更新任務...")
                    base_dir = Path(__file__).resolve().parent
                    venv_py = base_dir / ".venv" / "Scripts" / "python.exe"
                    python_exec = str(venv_py) if venv_py.exists() else sys.executable
                    script_path = str(base_dir / "ingest_script.py")
                    
                    # 啟動子程序更新
                    process = subprocess.Popen([python_exec, script_path])
                    log_scheduler_event("已在背景啟動股價更新同步腳本 (ingest_script.py)。")
                    
                    def wait_and_scan():
                        try:
                            process.wait()
                            log_scheduler_event("股價更新同步完成。開始執行觀察機器人全量掃描...")
                            
                            # 執行觀察機器人自動掃描
                            if _watchbot_available:
                                from watchbot import scan_all
                                results = scan_all()
                                log_scheduler_event(f"觀察機器人全量掃描完成，共更新 {len(results)} 檔股票。")
                            else:
                                log_scheduler_event("觀察機器人功能不可用，跳過掃描。")
                        except Exception as ex:
                            log_scheduler_event(f"股價同步後觀察機器人自動處理發生錯誤: {ex}")
                    
                    threading.Thread(target=wait_and_scan, daemon=True).start()
                    last_run_date_prices = now.date()
                
                # 2. 17:30 自動載入並補齊所有台股中文名稱
                if now.hour == 17 and now.minute == 30 and last_run_date_names != now.date():
                    log_scheduler_event("觸發下午 5:30 補齊台股名稱自動更新任務...")
                    
                    def run_populate():
                        log_scheduler_event("開始執行全台股名稱與代碼補齊任務 (populate_stock_names)...")
                        try:
                            from db_service import populate_stock_names
                            populate_stock_names()
                            log_scheduler_event("全台股名稱與代碼補齊任務完成。")
                        except Exception as ex:
                            log_scheduler_event(f"全台股名稱與代碼補齊任務執行失敗: {ex}")
                            
                    threading.Thread(target=run_populate, daemon=True).start()
                    last_run_date_names = now.date()
                    
            except Exception as e:
                log_scheduler_event(f"自動更新排程中發生異常: {e}")
            
            # 每 30 秒檢查一次
            time.sleep(30)

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

def start_background_loop():
    global _bg_loop, _bg_thread
    if _bg_loop is not None:
        return
    _bg_loop = asyncio.new_event_loop()
    _bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
    _bg_thread.start()
    print("[System] Background Event Loop Thread started.")
    
    # 啟動每日 17:00 自動更新排程
    start_daily_update_scheduler()

def run_async(coro):
    """在背景線程中執行非同步任務，並同步等待結果"""
    if _bg_loop is None:
        start_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result()

app = Flask(__name__)
CORS(app)  # 允許跨域請求
app.secret_key = 'your-secret-key-here'  # for session
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 確保修改模板後立即生效，不使用快取

client = get_llm_client()

def get_vision_client():
    api_key = os.getenv("VISION_API_KEY")
    if not api_key:
        raise ValueError("環境變數中找不到 VISION_API_KEY")
    
    # 改法：Base URL 不要斜線，交給 SDK 自己處理路徑
    return OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta"
    )

def analyze_image_with_llm(image_bytes: bytes, yolov9_result: str = None):
    api_key = os.getenv("VISION_API_KEY", "").strip()
    if not api_key:
        return {"pattern_name": "設定錯誤", "bias": "無", "confidence": "0%", "reasoning": ["找不到 API Key"]}

    import base64
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    # 1. 取得可用模型清單
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        model_list_res = requests.get(list_url)
        models_data = model_list_res.json()
        available_models = [m['name'] for m in models_data.get('models', [])]
        
        # 優先順序：2.5-flash 穩定且可用
        target_model = "models/gemini-2.5-flash"
        priority_list = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite"]
        for candidate in priority_list:
            if candidate in available_models:
                target_model = candidate
                break
        
        model_id = target_model.split("/")[-1]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"

        prompt_text = "你是一個專業的股市技術分析師。請分析這張 K 線圖的技術型態（如 W底、M頭、頭肩頂/底、三角形收斂、旗形等）。\n"
        if yolov9_result:
            prompt_text += (
                f"注意：我們的客製化 YOLO 圖形偵測模型在該圖表上偵測到了以下結果：\n"
                f"\"\"\"\n{yolov9_result}\n\"\"\"\n"
                "請參考並依據此偵測結果，在分析中加以說明和確認。如果偵測到型態，請詳細解釋該型態的意義、觀察重點與可能趨勢。\n"
            )
        else:
            prompt_text += "請判斷該圖表是否存在經典的技術圖形型態。\n"

        prompt_text += (
            "請嚴格回傳標準 JSON 格式，必須包含以下欄位：\n"
            '{"pattern_name": "型態名稱 (例如 W底/雙底、M頭/雙頂、頭肩底、無明顯型態等)", "bias": "多方/空方/中性", "confidence": "信心度 (例如 85%)", '
            '"reasoning": ["理由1", "理由2", "理由3"], "risk_note": "風險提示"}'
        )

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt_text},
                    {"inline_data": {"mime_type": "image/png", "data": base64_image}}
                ]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        }

        # === 核心改進：自動重試機制 ===
        max_retries = 3
        res_data = {}
        
        for attempt in range(max_retries):
            response = requests.post(url, json=payload, timeout=30)
            res_data = response.json()

            if response.status_code == 200:
                break
            
            error_msg = res_data.get('error', {}).get('message', '')
            # 如果是 High Demand，等待後重試
            if "high demand" in error_msg.lower() and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2 # 第一次等 2秒，第二次等 4秒
                print(f"⚠️ 伺服器忙碌 (High Demand)，{wait_time}秒後進行第 {attempt + 2} 次重試...")
                time.sleep(wait_time)
                continue
            else:
                # 其他錯誤直接拋出
                raise Exception(f"Google API 錯誤: {error_msg}")

        # 2. 檢查結果
        if 'candidates' not in res_data or not res_data['candidates']:
            raise Exception("AI 沒有回傳任何結果，可能是安全過濾或模型回應為空。")

        candidate = res_data['candidates'][0]
        raw_text = candidate['content']['parts'][0]['text']
        
        # 3. 解析與容錯
        try:
            clean_json = re.sub(r'```json|```', '', raw_text).strip()
            ai_res = json.loads(clean_json)
            
            return {
                "pattern_name": ai_res.get("pattern_name") or ai_res.get("型態名稱") or "未知型態",
                "bias": ai_res.get("bias") or ai_res.get("趨勢") or "中性",
                "confidence": ai_res.get("confidence") or ai_res.get("信心度") or "0%",
                "reasoning": ai_res.get("reasoning") or ai_res.get("分析理由") or ["無法提取具體理由"],
                "risk_note": ai_res.get("risk_note") or ai_res.get("風險提示") or "市場有風險，投資需謹慎。"
            }
        except json.JSONDecodeError:
            raise Exception("AI 回傳格式損毀，請再試一次。")

    except Exception as e:
        print(f"分析失敗: {str(e)}")
        # 回傳錯誤資訊給前端，讓用戶知道發生了什麼（例如 High Demand）
        return {
            "pattern_name": "辨識中斷",
            "bias": "無",
            "confidence": "0%",
            "reasoning": [f"錯誤詳情: {str(e)}"],
            "risk_note": "請稍後再試，或更換圖片重上傳。"
        }
    
def safe_jsonable(value):
    if isinstance(value, Exception):
        return {"error": f"{type(value).__name__}: {value}"}

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, (str, int, bool)) or value is None:
        return value

    if isinstance(value, dict):
        return {str(k): safe_jsonable(v) for k, v in value.items()}

    if isinstance(value, list):
        return [safe_jsonable(v) for v in value]

    return str(value)


def try_parse(value):
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        pass

    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def unwrap_mcp_result(obj):
    if isinstance(obj, dict) and "result" in obj:
        inner = obj.get("result")
        parsed = try_parse(inner)
        return parsed if parsed is not None else inner
    return obj


# 【已修正】將無效數值轉為 [None] 以保留陣列長度，避免圖表 X 軸錯位
def flatten_numbers(value):
    result = []

    if isinstance(value, (list, tuple)):
        for item in value:
            result.extend(flatten_numbers(item))
        return result

    if value is None:
        return [None]

    if isinstance(value, str):
        raw = value.strip().replace(",", "")
        if raw in {"", "--", "None", "null", "nan", "NaN"}:
            return [None]
        try:
            n = float(raw)
            if math.isnan(n) or math.isinf(n):
                return [None]
            return [n]
        except Exception:
            return [None]

    try:
        n = float(value)
        if math.isnan(n) or math.isinf(n):
            return [None]
        return [n]
    except Exception:
        return [None]


def format_float(v, digits=4):
    if v is None:
        return None
    try:
        n = float(v)
        if math.isnan(n) or math.isinf(n):
            return None
        return round(n, digits)
    except Exception:
        return None


def tw_stock_to_yf_symbol(code: str) -> str:
    return f"{code}.TW"


def parse_yfin_text_table(text: str):
    """解析 yfin-mcp 回傳的文字表格格式，提取 OHLCV 資料"""
    close, high, low, open_, volume, dates = [], [], [], [], [], []

    def safe_float(s):
        """處理截斷的數字，例如 1380.87685391.. -> 1380.87685391"""
        s = s.strip()
        # 移除結尾的 ..
        s = re.sub(r'\.\.+$', '', s)
        s = s.rstrip('.')
        s = s.replace(',', '')
        if not s or s in ('--', 'None', 'null', 'nan', 'NaN'):
            return None
        try:
            return float(s)
        except Exception:
            return None

    for line in text.splitlines():
        line = line.strip()
        # 符合日期開頭的行，例如 2025-10-13 00..
        if not re.match(r'^\d{4}-\d{2}-\d{2}', line):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 6:
            continue
        try:
            date_str = parts[0]
            o = safe_float(parts[1])
            h = safe_float(parts[2])
            l = safe_float(parts[3])
            c = safe_float(parts[4])
            v = safe_float(parts[5])
            if None in (o, h, l, c, v):
                continue
            dates.append(date_str)
            open_.append(o)
            high.append(h)
            low.append(l)
            close.append(c)
            volume.append(v)
        except Exception:
            continue

    return {
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "dates": dates,
    }


def parse_twse_metrics_text(text: str):
    data = {}

    title_match = re.search(r"【(.+?)\s*\((\d+)\)】", text)
    if title_match:
        data["Name"] = title_match.group(1).strip()
        data["Code"] = title_match.group(2).strip()

    patterns = {
        "Code": [r"Code:\s*([^\n]+)", r"代號:\s*([^\n]+)"],
        "Name": [r"Name:\s*([^\n]+)", r"名稱:\s*([^\n]+)"],
        "ClosingPrice": [r"ClosingPrice:\s*([^\n]+)", r"收盤價:\s*([^\n]+)"],
        "OpenPrice": [r"OpenPrice:\s*([^\n]+)", r"開盤價:\s*([^\n]+)"],
        "HighPrice": [r"HighPrice:\s*([^\n]+)", r"最高價:\s*([^\n]+)"],
        "LowPrice": [r"LowPrice:\s*([^\n]+)", r"最低價:\s*([^\n]+)"],
        "Change": [r"Change:\s*([^\n]+)", r"漲跌:\s*([^\n]+)"],
        "PEratio": [
            r"PEratio:\s*([^\n]+)",
            r"本益比\s*\(P/E\):\s*([^\n]+)",
            r"本益比:\s*([^\n]+)",
        ],
        "DividendYield": [
            r"DividendYield:\s*([^\n]+)",
            r"殖利率\s*\(%\):\s*([^\n]+)",
            r"殖利率:\s*([^\n]+)",
        ],
        "PBratio": [
            r"PBratio:\s*([^\n]+)",
            r"股價淨值比\s*\(P/B\):\s*([^\n]+)",
            r"股價淊值比\s*\(P/B\):\s*([^\n]+)",
            r"股價淨值比:\s*([^\n]+)",
        ],
        "TradeVolume": [r"TradeVolume:\s*([^\n]+)", r"成交股數:\s*([^\n]+)"],
        "TradeValue": [r"TradeValue:\s*([^\n]+)", r"成交金額:\s*([^\n]+)"],
        "TradeCount": [r"TradeCount:\s*([^\n]+)", r"成交筆數:\s*([^\n]+)"],
        "Date": [r"Date:\s*([^\n]+)", r"日期:\s*([^\n]+)"],
    }

    for key, pats in patterns.items():
        for pat in pats:
            m = re.search(pat, text)
            if m:
                data[key] = m.group(1).strip().replace(",", "")
                break

    return data


def parse_twse_metrics_obj(obj):
    obj = unwrap_mcp_result(obj)

    if isinstance(obj, dict):
        data = {}
        key_map = {
            "Code": ["Code", "代號"],
            "Name": ["Name", "名稱"],
            "ClosingPrice": ["ClosingPrice", "收盤價"],
            "OpenPrice": ["OpenPrice", "開盤價"],
            "HighPrice": ["HighPrice", "最高價"],
            "LowPrice": ["LowPrice", "最低價"],
            "Change": ["Change", "漲跌"],
            "PEratio": ["PEratio", "本益比 (P/E)", "本益比"],
            "DividendYield": ["DividendYield", "殖利率 (%)", "殖利率"],
            "PBratio": ["PBratio", "股價淨值比 (P/B)", "股價淊值比 (P/B)", "股價淨值比"],
            "TradeVolume": ["TradeVolume", "成交股數"],
            "TradeValue": ["TradeValue", "成交金額"],
            "TradeCount": ["TradeCount", "成交筆數"],
            "Date": ["Date", "日期"],
        }
        for norm_key, aliases in key_map.items():
            for a in aliases:
                if a in obj:
                    data[norm_key] = str(obj[a]).strip().replace(",", "")
                    break
        return data

    if isinstance(obj, str):
        return parse_twse_metrics_text(obj)

    return {}


def extract_price_arrays_from_obj(obj):
    obj = unwrap_mcp_result(obj)

    if obj is None:
        return {"close": [], "high": [], "low": [], "open": [], "volume": [], "dates": []}

    if isinstance(obj, dict):
        for key in ["data", "prices", "history", "rows", "items", "records", "result"]:
            value = obj.get(key)
            if isinstance(value, list):
                return extract_price_arrays_from_obj(value)
            if isinstance(value, dict):
                nested = extract_price_arrays_from_obj(value)
                if nested["close"]:
                    return nested

    if isinstance(obj, list):
        close, high, low, open_, volume, dates = [], [], [], [], [], []

        for row in obj:
            if not isinstance(row, dict):
                continue

            open_.extend(flatten_numbers(row.get("open", row.get("Open"))))
            high.extend(flatten_numbers(row.get("high", row.get("High"))))
            low.extend(flatten_numbers(row.get("low", row.get("Low"))))
            close.extend(flatten_numbers(row.get("close", row.get("Close"))))
            volume.extend(flatten_numbers(row.get("volume", row.get("Volume"))))

            if "date" in row:
                dates.append(str(row["date"]))
            elif "Date" in row:
                dates.append(str(row["Date"]))
            elif "Datetime" in row:
                dates.append(str(row["Datetime"]))
            elif "datetime" in row:
                dates.append(str(row["datetime"]))

        return {
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
            "dates": dates,
        }

    return {"close": [], "high": [], "low": [], "open": [], "volume": [], "dates": []}


def compact_news(raw):
    raw = unwrap_mcp_result(raw)

    if isinstance(raw, dict):
        if isinstance(raw.get("articles"), list):
            items = []
            for item in raw["articles"]:
                if not isinstance(item, dict):
                    continue
                items.append({
                    "title": item.get("title") or "未提供標題",
                    "time": item.get("publishedAt") or item.get("time") or item.get("date") or "",
                    "tag": item.get("publisher") or item.get("source") or "",
                    "content": item.get("summary") or item.get("content") or "",
                    "link": item.get("link") or "",
                })
            return items[:20]

    if isinstance(raw, list):
        items = []
        for item in raw:
            if isinstance(item, dict):
                items.append(
                    {
                        "title": item.get("title") or item.get("subject") or item.get("headline") or "未提供標題",
                        "time": item.get("time") or item.get("date") or item.get("publishedAt") or "",
                        "tag": item.get("tag") or item.get("publisher") or "",
                        "content": item.get("content") or item.get("summary") or item.get("text") or "",
                        "link": item.get("link") or "",
                    }
                )
            elif isinstance(item, str) and item.strip():
                items.append({"title": item.strip(), "time": "", "tag": "", "content": "", "link": ""})
        return items[:20]

    text = raw if isinstance(raw, str) else ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    items = []
    current = {}

    def flush_current():
        nonlocal current
        if not current:
            return
        if current.get("title"):
            items.append(
                {
                    "title": current.get("title", ""),
                    "time": current.get("time", ""),
                    "tag": current.get("tag", ""),
                    "content": current.get("content", ""),
                    "link": "",
                }
            )
        current = {}

    for line in lines:
        if line.startswith("Subject") or line.startswith("主旨"):
            flush_current()
            current["title"] = line.split(":", 1)[-1].strip()
            continue

        if (
            line.startswith("Announcement time")
            or line.startswith("發言時間")
            or line.startswith("Date of occurrence")
            or line.startswith("發生日期")
        ):
            current["time"] = line.split(":", 1)[-1].strip()
            continue

        if (
            line.startswith("Applicable regulations")
            or line.startswith("符合條款")
            or line.startswith("Statement")
        ):
            value = line.split(":", 1)[-1].strip()
            if not current.get("tag"):
                current["tag"] = value
            else:
                current["content"] = (current.get("content", "") + "\n" + value).strip()
            continue

        if (
            line.startswith("Content")
            or line.startswith("內容")
            or line.startswith("Description")
            or line.startswith("Summary")
            or line.startswith("主旨說明")
            or line.startswith("說明")
        ):
            value = line.split(":", 1)[-1].strip()
            current["content"] = (current.get("content", "") + "\n" + value).strip()
            continue

        if current.get("title"):
            current["content"] = (current.get("content", "") + "\n" + line).strip()

    flush_current()
    return items[:20]


def build_price_series_chart(series, indicators):
    close = series.get("close", [])
    volume = series.get("volume", [])
    dates = series.get("dates", [])
    rsi_series = indicators.get("rsi_series", [])
    macd_series = indicators.get("macd_series", [])
    signal_series = indicators.get("signal_series", [])

    n = len(close)
    start_idx = 0

    rows = []
    for i in range(start_idx, n):
        label = dates[i] if i < len(dates) else str(i + 1)
        rows.append(
            {
                "date": label,
                "close": format_float(close[i], 2) if i < len(close) else None,
                "volume": int(volume[i]) if i < len(volume) and volume[i] is not None else None,
                "rsi": format_float(rsi_series[i], 2) if i < len(rsi_series) else None,
                "macd": format_float(macd_series[i], 2) if i < len(macd_series) else None,
                "signal": format_float(signal_series[i], 2) if i < len(signal_series) else None,
            }
        )
    return safe_jsonable(rows)


def build_kline_series(series, indicators):
    opens = series.get("open", [])
    highs = series.get("high", [])
    lows = series.get("low", [])
    closes = series.get("close", [])
    volumes = series.get("volume", [])
    dates = series.get("dates", [])
    
    rsi_series = indicators.get("rsi_series", [])
    macd_series = indicators.get("macd_series", [])
    signal_series = indicators.get("signal_series", [])
    hist_series = indicators.get("hist_series", [])
    bb_upper = indicators.get("bb_upper", [])
    bb_middle = indicators.get("bb_middle", [])
    bb_lower = indicators.get("bb_lower", [])

    n = min(len(opens), len(highs), len(lows), len(closes))
    start_idx = 0

    rows = []
    for i in range(start_idx, n):
        rows.append(
            {
                "date": dates[i] if i < len(dates) else str(i + 1),
                "open": format_float(opens[i], 2),
                "high": format_float(highs[i], 2),
                "low": format_float(lows[i], 2),
                "close": format_float(closes[i], 2),
                "volume": int(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0,
                "rsi": format_float(rsi_series[i], 2) if i < len(rsi_series) else None,
                "macd": format_float(macd_series[i], 2) if i < len(macd_series) else None,
                "macd_signal": format_float(signal_series[i], 2) if i < len(signal_series) else None,
                "macd_hist": format_float(hist_series[i], 2) if i < len(hist_series) else None,
                "bb_upper": format_float(bb_upper[i], 2) if i < len(bb_upper) else None,
                "bb_middle": format_float(bb_middle[i], 2) if i < len(bb_middle) else None,
                "bb_lower": format_float(bb_lower[i], 2) if i < len(bb_lower) else None,
            }
        )
    return safe_jsonable(rows)


async def get_twse_summary(code: str, client: MultiMCPClient, fetch_news: bool = True):
    daily_coro = client.call_twse_named_tool("daily", {"code": code})
    valuation_coro = client.call_twse_named_tool("valuation", {"code": code})

    if fetch_news:
        news_coro = client.call_twse_named_tool("news", {"code": code})
        daily, valuation, news = await asyncio.gather(daily_coro, valuation_coro, news_coro)

        news_unwrapped = unwrap_mcp_result(news)
        news_is_empty = False

        if news_unwrapped is None:
            news_is_empty = True
        elif isinstance(news_unwrapped, str) and not news_unwrapped.strip():
            news_is_empty = True
        elif isinstance(news_unwrapped, list) and len(news_unwrapped) == 0:
            news_is_empty = True

        if news_is_empty:
            symbol = tw_stock_to_yf_symbol(code)

            for args in [
                {"ticker": symbol},
                {"symbol": symbol},
                {"symbol": code},
            ]:
                try:
                    y_news = await client.call_yfinance_tool("news", args)
                    print("DEBUG yfinance news raw =", y_news)

                    unwrapped = unwrap_mcp_result(y_news)

                    if isinstance(unwrapped, str) and (
                        "Error executing tool" in unwrapped
                        or "validation error" in unwrapped
                        or "Field required" in unwrapped
                    ):
                        continue

                    if unwrapped is None:
                        continue
                    if isinstance(unwrapped, str) and not unwrapped.strip():
                        continue
                    if isinstance(unwrapped, list) and len(unwrapped) == 0:
                        continue

                    news = y_news
                    break
                except Exception as e:
                    print("DEBUG yfinance news failed =", e)
                    continue
    else:
        daily, valuation = await asyncio.gather(daily_coro, valuation_coro)
        news = None

    return {"daily": daily, "valuation": valuation, "news": news}


async def get_history_series(code: str, client: MultiMCPClient, interval: str = "1d"):
    """
    支援多種週期的歷史資料抓取
    interval: '1d' (日), '1wk' (週), '1mo' (月)
    """
    # 1. 優先查詢本地 PostgreSQL 資料庫 (僅限日線)
    if interval == "1d" and get_price_history is not None:
        try:
            db_series = get_price_history(code)
            if db_series and len(db_series.get("close", [])) > 0:
                print(f"[{code}] 從資料庫讀取 {len(db_series['close'])} 筆歷史資料")
                return db_series
        except Exception as e:
            print(f"資料庫讀取失敗: {e}，切換為 API 抓取")

    # 2. 如果資料庫沒有，或是其他週期，則 fallback 回原本的抓取邏輯
    symbol = tw_stock_to_yf_symbol(code)

    # 根據 interval 自動對應合理的 period (回溯時間)
    # 日線抓 6 個月, 週線抓 2 年, 月線抓 5 年，這樣 K 棒才夠看
    period_map = {
        "1h": "1mo",
        "4h": "3mo",
        "1d": "6mo",
        "1wk": "2y",
        "1mo": "5y",
        "1y": "max"
    }
    # 如果是年線，雖然使用者傳 1y，但我們 API 改成抓 1mo (月級) 資料來顯示長期趨勢
    target_interval = "1mo" if interval == "1y" else interval
    target_period = period_map.get(interval, "6mo")

    # 建立多種可能的參數組合 (相容不同的 MCP Server 實作)
    candidates = [
        {"ticker": symbol, "period": target_period, "interval": target_interval},
        {"symbol": symbol, "period": target_period, "interval": target_interval},
        {"ticker": symbol, "range": target_period, "interval": target_interval},
    ]

    raw = None
    last_error = None

    for args in candidates:
        try:
            raw = await client.call_yfinance_tool("history", args)
            if isinstance(raw, str) and "Error executing tool" in raw:
                raw = None
                continue
            # 檢查 dict 內容是否包含錯誤訊息
            if isinstance(raw, dict):
                res_val = raw.get("result", "")
                if isinstance(res_val, str) and "Error executing tool" in res_val:
                    raw = None
                    continue
            break
        except Exception as e:
            last_error = e

    if raw is None:
        if last_error: raise last_error
        return {"close": [], "high": [], "low": [], "open": [], "volume": [], "dates": []}

    # 解析邏輯保持不變
    raw_str = ""
    if isinstance(raw, dict) and "result" in raw:
        raw_str = raw.get("result", "")
    elif isinstance(raw, str):
        raw_str = raw

    if isinstance(raw_str, str) and raw_str.strip():
        series = parse_yfin_text_table(raw_str)
        if series["close"]:
            return series

    parsed = unwrap_mcp_result(raw)
    if parsed is None:
        parsed = try_parse(raw)

    series = extract_price_arrays_from_obj(parsed)
    return series

async def calc_indicators(code: str, client: MultiMCPClient, interval: str = "1d"):
    series = await get_history_series(code, client, interval)
    
    # 提取數值並處理型態 (不再使用 DataFrame)
    close = np.array(series.get("close", []), dtype=float)
    high = np.array(series.get("high", []), dtype=float)
    low = np.array(series.get("low", []), dtype=float)
    open_p = np.array(series.get("open", []), dtype=float)
    volume = np.array(series.get("volume", []), dtype=float)

    result = {
        "series": series,
        "rsi": None, "macd": None, "signal": None, "hist": None,
        "ma5": None, "ma10": None, "ma20": None,
        "rsi_series": [], "macd_series": [], "signal_series": [], "hist_series": [],
        "bb_upper": [], "bb_middle": [], "bb_lower": [],
    }

    if len(close) < 5:
        return result

    try:
        # 使用 TA-Lib 計算
        ma5 = talib.SMA(close, timeperiod=5)
        ma10 = talib.SMA(close, timeperiod=10)
        ma20 = talib.SMA(close, timeperiod=20)
        
        rsi14 = talib.RSI(close, timeperiod=14)
        
        macd, macd_signal, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        
        # 布林通道 (Bollinger Bands)
        upperband, middleband, lowerband = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
        
        # 輔助函數：將 numpy array 轉為 JSON 友善列表 (NaN -> None)
        def to_json_list(arr):
            return [float(x) if not np.isnan(x) else None for x in arr]

        result['ma5'] = float(ma5[-1]) if not np.isnan(ma5[-1]) else None
        result['ma10'] = float(ma10[-1]) if not np.isnan(ma10[-1]) else None
        result['ma20'] = float(ma20[-1]) if not np.isnan(ma20[-1]) else None
        
        result['rsi_series'] = to_json_list(rsi14)
        result['rsi'] = float(rsi14[-1]) if not np.isnan(rsi14[-1]) else None
        
        result['macd_series'] = to_json_list(macd)
        result['signal_series'] = to_json_list(macd_signal)
        result['hist_series'] = to_json_list(macd_hist)
        result['macd'] = float(macd[-1]) if not np.isnan(macd[-1]) else None
        result['signal'] = float(macd_signal[-1]) if not np.isnan(macd_signal[-1]) else None
        result['hist'] = float(macd_hist[-1]) if not np.isnan(macd_hist[-1]) else None
        
        result['bb_upper'] = to_json_list(upperband)
        result['bb_middle'] = to_json_list(middleband)
        result['bb_lower'] = to_json_list(lowerband)

    except Exception as e:
        print(f"TA-Lib 指標計算錯誤: {e}")

    return result


def detect_talib_patterns(data_dict):
    """
    使用 TA-Lib 辨識蠟燭型態 (不再依賴 Pandas)
    data_dict 應包含 open, high, low, close 同長度的 numpy array 或 list
    """
    # 轉為 numpy float64
    o = np.array(data_dict.get('open', []), dtype=float)
    h = np.array(data_dict.get('high', []), dtype=float)
    l = np.array(data_dict.get('low', []), dtype=float)
    c = np.array(data_dict.get('close', []), dtype=float)
    dates = data_dict.get('dates', []) or data_dict.get('date', [])

    if len(c) < 10:
        return []

    patterns = {
        'CDLHAMMER': '錘頭 (Hammer)',
        'CDLENGULFING': '吞噬 (Engulfing)',
        'CDLMORNINGSTAR': '晨星 (Morning Star)',
        'CDLEVENINGSTAR': '暮星 (Evening Star)',
        'CDLHARAMI': '孕線 (Harami)',
        'CDLPIERCING': '貫穿 (Piercing)',
        'CDL3WHITESOLDIERS': '三紅兵 (3 White Soldiers)',
        'CDL3BLACKCROWS': '三烏鴉 (3 Black Crows)',
        'CDLSHOOTINGSTAR': '射擊之星 (Shooting Star)',
        'CDLMARUBOZU': '光頭光腳 (Marubozu)',
        'CDLDOJI': '十字星 (Doji)',
    }

    found = []
    for func_name, friendly_name in patterns.items():
        func = getattr(talib, func_name)
        res = func(o, h, l, c)
        
        # 檢查最後 3 根 K 線是否有型態
        for i in range(-1, -4, -1):
            if abs(i) > len(res): break
            val = res[i]
            if val != 0:
                date_str = dates[i] if i < len(dates) else f"T-{abs(i)-1}"
                found.append({
                    "pattern": friendly_name,
                    "sentiment": "bullish" if val > 0 else "bearish",
                    "date": date_str,
                    "offset": abs(i) - 1
                })
    
    found.sort(key=lambda x: x['offset'])
    return found


def build_insight(summary):
    parts = []

    if summary["rsi14"] is not None:
        if summary["rsi14"] >= 70:
            parts.append("RSI 位於高檔區")
        elif summary["rsi14"] <= 30:
            parts.append("RSI 位於低檔區")
        else:
            parts.append("RSI 位於中性區")

    if summary["macd"] is not None and summary["signal"] is not None:
        if summary["macd"] > summary["signal"]:
            parts.append("MACD 在訊號線上方")
        else:
            parts.append("MACD 在訊號線下方")

    if (
        summary["lastPrice"] is not None
        and summary["ma5"] is not None
        and summary["ma10"] is not None
        and summary["ma20"] is not None
    ):
        if (
            summary["lastPrice"] > summary["ma5"]
            and summary["lastPrice"] > summary["ma10"]
            and summary["lastPrice"] > summary["ma20"]
        ):
            parts.append("現價站上短中期均線")
        elif (
            summary["lastPrice"] < summary["ma5"]
            and summary["lastPrice"] < summary["ma10"]
            and summary["lastPrice"] < summary["ma20"]
        ):
            parts.append("現價低於短中期均線")
        else:
            parts.append("現價與均線交錯")

    return "、".join(parts) if parts else "目前資料不足"


def build_report(summary, insight):
    lines = [
        f"股票代號：{summary['code']}",
        f"股票名稱：{summary.get('name') or summary['code']}",
        f"最新股價：{summary.get('lastPrice')}",
        f"漲跌：{summary.get('change')}",
        "",
        "一、基本面指標",
        f"- 本益比：{summary.get('pe')}",
        f"- 殖利率：{summary.get('dividendYield')}%",
        f"- 股價淨值比：{summary.get('pb')}",
        "",
        "二、技術面指標",
        f"- RSI(14)：{summary.get('rsi14')}",
        f"- MACD：{summary.get('macd')}",
        f"- Signal：{summary.get('signal')}",
        f"- K / D：{summary.get('k')} / {summary.get('d')}",
        f"- 5MA / 10MA / 20MA：{summary.get('ma5')} / {summary.get('ma10')} / {summary.get('ma20')}",
        "",
        "三、重點判讀",
        f"- {insight}",
        "",
        "四、觀察重點",
        "- 先觀察現價是否重新站回短中期均線。",
        "- 若 MACD 後續翻到訊號線上方，短線動能可再轉強。",
        "- 可搭配成交量與近期重大訊息一起判讀。",
    ]
    return "\n".join(lines)


def build_llm_prompt(payload: dict) -> str:
    summary = payload.get("summary", {})
    news = payload.get("news", [])[:5]
    price_series = payload.get("priceSeries", [])[-20:]

    return f"""
你是一個名為「股Friend」的專業台股分析助理，請根據提供的資料，產生一份繁體中文、重點精確且簡潔扼要的分析報告。

請遵守：
1. 只使用我提供的資料，不要自行編造外部資訊。
2. 使用繁體中文。
3. 不要輸出思考過程。
4. 【極致精簡與核心重點】：內容務必精簡扼要、直指核心，只回答關鍵數據與重要結論，嚴禁長篇大論、客套寒暄與冗長鋪陳。
5. 報告請分成以下四項重點（每項請以 2~3 句短句或精簡要點直接提煉關鍵重點，切勿贅述）：
   - 一、股價與基本面摘要
   - 二、技術面觀察
   - 三、重大訊息重點
   - 四、綜合判讀與後續觀察重點
6. 語氣要專業、清楚，但不要過度艱深。
7. 不要下投資買賣指令，只做客觀分析整理。

以下是資料：
summary:
{json.dumps(summary, ensure_ascii=False, indent=2)}

news:
{json.dumps(news, ensure_ascii=False, indent=2)}

priceSeries_recent_20:
{json.dumps(price_series, ensure_ascii=False, indent=2)}
""".strip()


def ask_llm_report(payload: dict) -> str:
    prompt = build_llm_prompt(payload)
    messages = [
        {
            "role": "system",
            "content": "你是名為「股Friend」的台股分析助理。請一律使用繁體中文，回覆請精簡扼要、直指核心重點，只輸出最終報告，不要輸出思考過程。" + TRADITIONAL_CHINESE_RULE,
        },
        {"role": "user", "content": prompt},
    ]
    return ask_llm_with_fallback(messages, temperature=0.3)


def build_chat_prompt(payload: dict, user_message: str, chat_history: list | None = None) -> str:
    summary = payload.get("summary", {})
    news = payload.get("news", [])[:5]
    price_series = payload.get("priceSeries", [])[-20:]
    chat_history = chat_history or []

    return f"""
你是一個名為「股Friend」的專業台股分析助理，請根據提供的資料回答使用者問題。

請遵守：
1. 一律使用繁體中文。
2. 【極致精簡與核心重點】：回覆務必精簡扼要、直指核心，只回答最關鍵且最重要的資訊與數據，嚴禁長篇大論、客套寒暄或多餘鋪陳。
3. 只根據提供資料回答，不可編造。
4. 不要輸出思考過程。
5. 不要直接下買賣指令，可做客觀分析。
6. 若資料不足，請明確說明。
7. 若使用者問題偏向技術分析，請直接點出關鍵指標（如 RSI、MACD、KD、均線支撐壓力）之重要數值與多空意涵。
8. 若使用者問題偏向基本面，請精準引用本益比、殖利率、股價淨值比等核心數據回答。
9. 若使用者問題偏向消息面，請以 1~2 句直接總結重大訊息核心影響。

目前資料：
summary:
{json.dumps(summary, ensure_ascii=False, indent=2)}

news:
{json.dumps(news, ensure_ascii=False, indent=2)}

priceSeries_recent_20:
{json.dumps(price_series, ensure_ascii=False, indent=2)}

最近對話紀錄：
{json.dumps(chat_history[-6:], ensure_ascii=False, indent=2)}

使用者問題：
{user_message}
""".strip()


def ask_llm_chat(payload: dict, user_message: str, chat_history: list | None = None) -> str:
    prompt = build_chat_prompt(payload, user_message, chat_history)
    messages = [
        {
            "role": "system",
            "content": "你是名為「股Friend」的台股分析助理。請一律使用繁體中文，回覆請精簡扼要、直指核心重點，只輸出最終回答，不要輸出思考過程。" + TRADITIONAL_CHINESE_RULE,
        },
        {"role": "user", "content": prompt},
    ]
    return ask_llm_with_fallback(messages, temperature=0.3)


def image_file_to_data_url(file_storage) -> str:
    image = Image.open(file_storage.stream).convert("RGB")
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def build_pattern_prompt(stock_code: str = "", stock_name: str = "") -> str:
    stock_context = ""
    if stock_code:
        target_display = f"{stock_name}（{stock_code}）" if stock_name else f"台股 {stock_code}"
        stock_context = f"""
【指定分析標的】{target_display}
【最高優先級要求】：
你現在正在為「{target_display}」進行客製化技術分析！
在輸出的【reasoning】與【watch_points】中，**必須明確提及「{target_display}」名稱與代碼**，
並結合該標的之關鍵價位、頸線突破或回測防守量能進行深入剖析，絕不可輸出通用的空話！
"""
    return f"""
你是一位專業台股資深技術分析師。請根據使用者上傳的 K 線圖圖片進行型態判斷。
{stock_context}
請遵守：
1. 一律使用繁體中文。
2. 【極致精簡與核心重點】：只回傳最核心結論，禁止任何冗長客套、開場白與重複說明。
3. 【型態限定】：本系統嚴格限定僅辨識以下 YOLO 5 大經典幾何型態，禁止回傳此 5 大範圍以外之其他型態名稱：
   - 頭肩頂 (Head and Shoulders Top) 或 頭肩底 (Head and Shoulders Bottom)
   - 雙重底 (Double Bottom / W底)
   - 雙重頂 (Double Top / M頭)
   - 三角形整理 (Triangle Pattern)
   - 旗形整理 (Flag Pattern)
   - 若圖表走勢不符合上述 5 種經典型態，請一律回傳：「無明顯型態 (No Obvious Pattern)」。
4. reasoning：只給 1 句最關鍵的線圖走勢與型態特徵判斷（必須帶入標的名稱，35字內）。
5. watch_points：只給 1~2 點關鍵突破/防守價位或量能觀察（必須帶入標的名稱，每點30字內）。
6. risk_note：只給 1 句重點風險提醒（如假突破或量能背離，25字內）。
7. 回傳 JSON 格式如下：
{{
  "pattern_name": "限定上述 5 種經典型態之一，或 無明顯型態 (No Obvious Pattern)",
  "bias": "偏多/偏空/中性/無法判斷",
  "confidence": "高/中/低",
  "reasoning": ["標的走勢核心判斷（1句）"],
  "watch_points": ["關鍵點位/量能觀察（1~2點）"],
  "risk_note": "核心風險提示（1句）"
}}
只輸出 JSON，不要輸出其他文字。
""".strip()


def ask_llm_pattern_from_image(data_url: str, stock_code: str = "", stock_name: str = "") -> dict:
    try:
        user_text = "請辨識這張 K 線圖的可能型態。"
        if stock_code:
            target_display = f"{stock_name}（{stock_code}）" if stock_name else f"台股 {stock_code}"
            user_text = f"這是 {target_display} 的 K 線圖，請依據走勢辨識型態，並在走勢判斷與觀察中具體指出 {target_display} 的關鍵點位與觀察。"

        messages = [
            {"role": "system", "content": build_pattern_prompt(stock_code, stock_name)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        content = ask_llm_with_fallback_vision(messages, temperature=0.2)
        if not content or content == "{}":
            content = "{}"

        # 嘗試剝除 markdown 代碼圍欄（```json ... ``` 或 ``` ... ```）
        cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        cleaned = cleaned.strip()

        # 嘗試解析清理後的 JSON
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # 第二次嘗試：用正則式擷取第一個 { ... } 物件
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass

        # 最終失敗：回傳使用者友善訊息，不把原始 JSON 顯示給使用者
        return {
            "pattern_name": "型態辨識完成（格式異常）",
            "bias": "無法判斷",
            "confidence": "低",
            "reasoning": ["AI 已完成分析，但回傳格式無法正確解讀，請重新上傳更清晰的 K 線圖以獲得完整結果。"],
            "watch_points": [],
            "risk_note": "建議使用清晰、含完整 K 棒與時間軸的截圖重新嘗試。",
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "pattern_name": "AI 辨識服務暫時不可用",
            "bias": "無法判斷",
            "confidence": "低",
            "reasoning": [f"呼叫 LLM 服務出錯：{e}"],
            "watch_points": [],
            "risk_note": "所有可用模型配額已耗盡，請稍後再試。",
        }


def ask_llm_pattern_text_analysis(code: str, patterns: list, close_prices: list, period: str) -> str:
    """
    根據 MCP 掃描到的蠟燭型態與近期收盤價，讓 LLM 輸出繁體中文技術分析文字。
    patterns: [{ "pattern": "錘頭 (Hammer)", "sentiment": "bullish", "date": "2024-07-15", ... }, ...]
    close_prices: 最近 N 根收盤價 list
    """
    try:
        client = get_llm_client()
        model_name = get_model_name()

        if patterns:
            pattern_lines = "\n".join(
                f"- {p['pattern']}（{'多方' if p.get('sentiment') == 'bullish' else '空方'}）出現於 {p.get('date', '近期')}"
                for p in patterns[:10]
            )
        else:
            pattern_lines = "- 近期未偵測到明顯蠟燭型態"

        price_summary = ""
        if close_prices:
            recent = close_prices[-10:]
            price_summary = f"最近10根收盤價：{[round(p, 2) for p in recent]}"

        user_prompt = f"""
台股代號：{code}
週期：{period}
MCP TA-Lib 掃描到的蠟燭型態：
{pattern_lines}

{price_summary}

請根據以上 MCP 偵測結果，給出一份繁體中文技術分析，回覆務必精簡扼要、直指核心，只保留關鍵重點，包含：
1. 型態解讀：目前出現哪些型態、代表什麼市場意義（簡短精練）
2. 多空偏向：整體偏多、偏空或中性，並以 1 句話說明關鍵原因
3. 操作觀察點：需要關注的關鍵價位或確認訊號
4. 風險提醒：操作前需要注意的關鍵風險

請以精簡條列式、通俗易懂的繁體中文回答，只保留核心重點，避免冗長文字贅述，不要給買賣指令。
""".strip()

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一位台股技術分析助理。請一律使用繁體中文，回覆必須精簡扼要，只輸出最終關鍵分析報告，不要輸出思考過程。" + TRADITIONAL_CHINESE_RULE},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or "模型沒有回傳內容。"
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    except Exception as e:
        traceback.print_exc()
        return f"⚠️ LLM 分析失敗：{e}"


async def get_twse_summary(code: str, client: MultiMCPClient, fetch_news: bool = True):
    """
    並行抓取 TWSE 日結算、本益比、以及 (選配) 新聞
    """
    daily_task = client.call_twse_named_tool("daily", {"code": code})
    valuation_task = client.call_twse_named_tool("valuation", {"code": code})
    news_task = client.call_twse_named_tool("news", {"code": code}) if fetch_news else asyncio.sleep(0, result=None)

    daily, valuation, news = await asyncio.gather(daily_task, valuation_task, news_task, return_exceptions=True)

    return {
        "daily": daily if not isinstance(daily, Exception) else None,
        "valuation": valuation if not isinstance(valuation, Exception) else None,
        "news": news if not isinstance(news, Exception) else None
    }


async def get_unified_realtime_quote(code: str, client: MultiMCPClient = None):
    """
    獲取統一的真即時報價，具備快取機制以提高效能
    優先級：Fugle (真即時) > yfinance (延遲但高頻) > TWSE (每日結算)
    """
    if client is None:
        client = global_mcp_client

    cache_key = f"unified_quote_{code}"
    cached = data_cache.get(cache_key)
    if cached:
        return cached

    # 1. 嘗試 Fugle
    fugle_quote = await client.fetch_fugle_quote(code)
    
    # 2. 獲取延遲數據作為備援
    daily_task = client.call_twse_named_tool("daily", {"code": code})
    history_task = get_history_series(code, client, "1d")
    
    daily, history = await asyncio.gather(daily_task, history_task, return_exceptions=True)
    
    day = parse_twse_metrics_obj(daily) if not isinstance(daily, Exception) else {}
    series = history if not isinstance(history, Exception) else {"close": []}
    
    last_price = format_float(day.get("ClosingPrice"), 2)
    change = format_float(day.get("Change"), 2)
    
    # yfinance 備援
    close_list = series.get("close", [])
    if close_list:
        last_price = format_float(close_list[-1], 2)
        if len(close_list) >= 2:
            change = format_float(close_list[-1] - close_list[-2], 2)
    
    # 3. Fugle 真即時覆蓋
    is_realtime = False
    if fugle_quote:
        last_price = format_float(fugle_quote.get("lastPrice"), 2)
        change = format_float(fugle_quote.get("change"), 2)
        is_realtime = True
            
    result = {
        "name": (fugle_quote and fugle_quote.get("name")) or day.get("Name") or code,
        "lastPrice": last_price,
        "change": change,
        "isRealtime": is_realtime
    }
    
    # 快取 1 秒以滿足併行請求（如 Header 與 Profile 同時呼叫）
    data_cache.set(cache_key, result, ttl=1)
    return result


async def build_dashboard_payload(code: str, interval: str = "1d", fetch_news: bool = True):
    """
    建立完整的儀表板資料，增加 interval 參數以確保摘要卡片維持日線資訊
    """
    client = MultiMCPClient()

    # 抓取基本面資料
    twse_task = get_twse_summary(code, client, fetch_news=fetch_news)
    
    # 抓取技術面指標 (這裡現在能正確接收到 interval 了)
    ind_task = calc_indicators(code, client, interval)

    # 同步執行任務
    twse, indicators = await asyncio.gather(twse_task, ind_task, return_exceptions=True)

    twse_error = None
    ind_error = None

    if isinstance(twse, Exception):
        twse_error = f"{type(twse).__name__}: {twse}"
        twse = {"daily": None, "valuation": None, "news": None}

    if isinstance(indicators, Exception):
        ind_error = f"{type(indicators).__name__}: {indicators}"
        indicators = {
            "series": {"close": [], "high": [], "low": [], "open": [], "volume": [], "dates": []},
            "rsi": None,
            "macd": None,
            "signal": None,
            "hist": None,
            "k": None,
            "d": None,
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "rsi_series": [],
            "macd_series": [],
            "signal_series": [],
        }

    print("DEBUG twse daily raw =", twse.get("daily"))
    print("DEBUG twse valuation raw =", twse.get("valuation"))
    print("DEBUG twse news raw =", twse.get("news"))
    print("DEBUG indicators raw =", indicators)

    day = parse_twse_metrics_obj(twse.get("daily"))
    val = parse_twse_metrics_obj(twse.get("valuation"))

    series = indicators.get("series", {})
    close_series = series.get("close", [])

    last_price = None
    change = format_float(day.get("Change"), 2)

    # 摘要 (Summary) 資料計算
    # 規則：維持顯示「即時日線」資訊，不因圖表週期切換而改變看板大字
    if close_series and interval == "1d":
        # 如果是日線圖，我們用 chart 的最後一筆來同步，確保 100% 精確對齊
        last_price = format_float(close_series[-1], 2)
        if len(close_series) >= 2:
            change = format_float(close_series[-1] - close_series[-2], 2)
    
    # 如果是非日線圖 (時/週/月/年)，或 yfinance 沒資料，回歸使用 TWSE/Header 的當日即時報價
    if last_price is None and day.get("ClosingPrice") is not None:
        last_price = format_float(day.get("ClosingPrice"), 2)
        change = format_float(day.get("Change"), 2)

    summary = {
        "code": code,
        "name": day.get("Name") or val.get("Name") or code,
        "lastPrice": last_price,
        "change": change,
        "changePct": None,
        "pe": format_float(val.get("PEratio"), 2),
        "dividendYield": format_float(val.get("DividendYield"), 2),
        "pb": format_float(val.get("PBratio"), 2),
        "rsi14": format_float(indicators.get("rsi"), 4),
        "macd": format_float(indicators.get("macd"), 4),
        "signal": format_float(indicators.get("signal"), 4),
        "k": format_float(indicators.get("k"), 4),
        "d": format_float(indicators.get("d"), 4),
        "ma5": format_float(indicators.get("ma5"), 4),
        "ma10": format_float(indicators.get("ma10"), 4),
        "ma20": format_float(indicators.get("ma20"), 4),
    }

    insight = build_insight(summary)

    payload = {
        "summary": summary,
        "insight": insight,
        "report": build_report(summary, insight),
        "priceSeries": build_price_series_chart(indicators["series"], indicators),
        "klineSeries": build_kline_series(indicators["series"], indicators),
        "news": compact_news(twse.get("news")),
        "_debug": {
            "twse_daily_raw": twse.get("daily"),
            "twse_valuation_raw": twse.get("valuation"),
            "twse_news_raw": twse.get("news"),
            "twse_daily_parsed": day,
            "twse_valuation_parsed": val,
            "indicators_raw": indicators,
            "twse_error": twse_error,
            "indicators_error": ind_error,
        },
    }

    return safe_jsonable(payload)


async def get_stock_profile_data(code: str):
    """
    獲取股票的基本資料與概覽，整合 yfinance 與 TWSE 資料
    """
    cache_key = f"profile_{code}"
    cached = data_cache.get(cache_key)
    if cached:
        return cached

    # 產業類別中文化對照表 (常見類別)
    INDUSTRY_MAP = {
        "Semiconductors": "半導體業",
        "Electronic Components": "電子零組件業",
        "Computers & Peripherals": "電腦及週邊設備業",
        "Communications & Networking": "通訊網路業",
        "Optoelectronics": "光電業",
        "Other Electronic": "其他電子業",
        "Electronic Distribution": "電子通路業",
        "Information Service": "資訊服務業",
        "Semiconductor Equipment & Materials": "半導體設備與材料",
        "Consumer Electronics": "消費性電子",
        "Building Materials": "建材營造",
        "Steel": "鋼鐵工業",
        "Textiles": "紡織纖維",
        "Automobiles": "汽車工業",
        "Chemicals": "化學工業",
        "Biotechnology": "生技醫療業",
        "Financial Services": "金融保險業",
        "Shipping": "航運業",
        "Tourism & Leisure": "觀光餐旅",
        "Retail": "貿易百貨業",
        "Oil, Gas & Consumable Fuels": "油氣燃燒業",
        "Electric Utilities": "電力事業",
        "Technology": "電子科技業",
    }

    client = MultiMCPClient()
    symbol = tw_stock_to_yf_symbol(code)

    # 並行抓取 yfinance info 與 TWSE valuation
    yf_task = client.call_yfinance_tool("price", {"ticker": symbol})
    twse_task = client.call_twse_named_tool("valuation", {"code": code})
    daily_task = client.call_twse_named_tool("daily", {"code": code})

    yf_info, twse_val, twse_daily = await asyncio.gather(yf_task, twse_task, daily_task, return_exceptions=True)

    # 容錯處理：若任一任務拋出例外，將其轉換為空字典以防崩潰
    yf_unwrapped = unwrap_mcp_result(yf_info) if not isinstance(yf_info, Exception) else {}
    if not isinstance(yf_unwrapped, dict):
        yf_unwrapped = {}

    val = parse_twse_metrics_obj(twse_val) if not isinstance(twse_val, Exception) else {}
    day = parse_twse_metrics_obj(twse_daily) if not isinstance(twse_daily, Exception) else {}

    raw_industry = yf_unwrapped.get("industry") or yf_unwrapped.get("sector")
    industry = INDUSTRY_MAP.get(raw_industry, raw_industry) or "其他產業"
    latest_date = day.get("Date") or val.get("Date")

    result = {
        "code": code,
        "name": day.get("Name") or val.get("Name") or code,
        "latest_date": latest_date,
        "industry": industry,
        "pe": format_float(val.get("PEratio"), 2),
        "pb": format_float(val.get("PBratio"), 2),
        "yield": format_float(val.get("DividendYield"), 2)
    }
    data_cache.set(cache_key, result, ttl=86400) # 基本資料快取一天
    return result


@app.route('/')
@app.route('/login')
@app.route('/register')
@app.route('/logout')
@app.route('/onboarding')
@app.route('/analysis')
@app.route('/profile')
@app.route('/settings')
@app.route('/news')
@app.route('/news-detail')
@app.route('/recommendations')
@app.route('/watchbot')
@app.route('/admin')
@app.route('/pattern')
def index():
    """SPA Frontend Routes"""
    return render_template('index.html')


@app.route("/api/header")
def api_header():
    code = request.args.get("code", "2330").strip()
    if not re.fullmatch(r"\d{4}", code):
        return jsonify({"error": "請輸入四位數台股代號"}), 400

    try:
        # 直接使用統一報價 helper
        payload = run_async(get_unified_realtime_quote(code))
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"系統錯誤：{e}"}), 500


@app.route("/api/dashboard")
def api_dashboard():
    code = request.args.get("code", "2330").strip().upper()
    if code.endswith('.TW'): code = code[:-3]
    elif code.endswith('.TWO'): code = code[:-4]
    
    # 新增接收週期參數，預設為日線 (1d)
    period = request.args.get("period", "1d").strip()

    if not re.fullmatch(r"[0-9][A-Z0-9]{3,5}", code):
        return jsonify({"error": "請輸入有效的股票代號（4-6位英數字，且首字須為數字）"}), 400

    try:
        cache_key = f"dashboard_{code}_{period}"
        cached = data_cache.get(cache_key)
        if cached:
            return jsonify(cached)

        payload = run_async(build_dashboard_payload(code, period, fetch_news=False))
        data_cache.set(cache_key, payload, ttl=60) # K線與指標快取 1 分鐘
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"系統錯誤：{e}"}), 500


@app.route("/api/llm_report")
def api_llm_report():
    code = request.args.get("code", "2330").strip().upper()
    if code.endswith('.TW'): code = code[:-3]
    elif code.endswith('.TWO'): code = code[:-4]

    if not re.fullmatch(r"[0-9][A-Z0-9]{3,5}", code):
        return jsonify({"error": "請輸入有效的股票代號（4-6位英數字，且首字須為數字）"}), 400

    try:
        payload = run_async(build_dashboard_payload(code))
        llm_report = ask_llm_report(payload)
        return jsonify({"code": code, "name": payload["summary"].get("name", code), "llm_report": llm_report})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"LLM 報告產生失敗：{type(e).__name__}: {e}"}), 500


@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get("code", "2330")).strip().upper()
        if code.endswith('.TW'): code = code[:-3]
        elif code.endswith('.TWO'): code = code[:-4]
        
        user_message = str(data.get("message", "")).strip()
        chat_history = data.get("history", [])

        if not re.fullmatch(r"[0-9][A-Z0-9]{3,5}", code):
            return jsonify({"error": "請輸入有效的股票代號（4-6位英數字，且首字須為數字）"}), 400

        if not user_message:
            return jsonify({"error": "請輸入對話內容"}), 400

        payload = run_async(build_dashboard_payload(code))
        answer = ask_llm_chat(payload, user_message, chat_history)

        return jsonify({"code": code, "name": payload["summary"].get("name", code), "answer": answer})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 對話失敗：{type(e).__name__}: {e}"}), 500


@app.route("/api/assistant/chat", methods=["POST"])
def api_assistant_chat():
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get("message", "")).strip()
        chat_history = data.get("history", [])
        image_base64 = data.get("image", "")  # 支援 data:image/...;base64,... 或純 base64 字串

        if not user_message and not image_base64:
            return jsonify({"error": "請輸入對話內容或上傳圖片"}), 400

        system_prompt = (
            "你是一個專業、權威且親切的台灣股市與證券投資AI智慧助理，名字叫作「股Friend AI智慧助理」（代表角色為「小藍」）。\n"
            "你的核心任務有兩個：\n"
            "1. 【股市與財經專家】：為用戶分析解讀台股與全球股市、K線走勢、技術型態、財務報表、籌碼分析、即時財經新聞與焦點熱門個股。\n"
            "2. 【股Friend 系統功能指引與新手導覽專家】：熱情、清晰、具體地回答用戶有關「股Friend」系統的各項功能、使用方式、新手教學位置與操作指引。\n\n"
            "【股Friend 系統功能知識庫】：\n"
            "• 新手教學與功能導覽：請告訴用戶前往「設定」頁面（點選左側選單「設定」），在「新手功能導覽教學」卡片中點選「🚀 重新開啟導覽」，系統將立即啟動全站聚光燈引導教學，一步一步帶領熟悉看盤與 AI 分析工具。\n"
            "• 台股總覽（：提供全台股即時報價、成交量、日K/週K/月K互動圖表、MA均線、KD、RSI、MACD、布林通道等技術指標，以及三大法人籌碼分析。點擊股票代碼旁的「⭐ 星號」可將個股加入自選清單。\n"
            "• AI 形態辨識（/pattern）：結合 YOLO 深度學習視覺模型。用戶輸入股票代碼（如 2330），上傳券商 App 或 TradingView 的技術線圖截圖，AI 將自動框選辨識 5 大經典幾何型態（頭肩頂/底、旗形突破、雙重底W底、雙重頂M頭、三角形收斂整理），並即時產出形態辨識報告與技術判斷。\n"
            "• 智慧選股（/recommendations）：透過 10 題科學風險屬性問卷評估用戶投資風格（保守型、穩健型、積極型），依據 1 年期 Beta 係數（β）精準推薦 5 檔最契合的台股標的，並提供基金配置建議與台灣前 50 大股票 Beta 統計表。\n"
            "• 即時新聞（/news）：即時彙整市場重大財經頭條與個股新聞。\n"
            "• 自選清單（/watchbot）：集中管理與追蹤加入星號之自選股票，提供即時報價與漲跌監控。\n"
            "• 頂部即時跑馬燈：橫跨網頁頂端，即時輪播漲幅排行、跌幅排行、成交量排行或自選股動態。\n"
            "• 個人設定（/settings）：修改姓名、信箱、手機、變更密碼，以及重新開啟新手導覽。\n\n"
            "【回答準則】：\n"
            "1. 【回答範圍】：你專門回答 (A) 股票、證券、投資、技術分析、財報、財經新聞，以及 (B)「股Friend」系統各項功能的操作方式、新手教學在哪裡看與頁面指引。若用戶問了與股市投資和本系統兩者皆毫無關聯的純閒聊問題（如做菜、天氣、寫小說等），請委婉禮貌地提醒自己是專門為股市與股Friend系統服務的AI智慧助理。\n"
            "2. 【極致精簡與核心重點】：回答務必精簡扼要、直截了當，只回答最關鍵的核心結論、操作步驟或數據。嚴禁長篇大論、客套寒暄、贅字或無意義鋪陳，讓使用者一眼就能掌握核心重點！\n"
            "3. 【禁止虛構與範例模板】：回答市場動態問題時必須給出具體、真實的分析與即時資訊，嚴禁輸出假資料或佔位符模板。\n"
            "4. 當用戶上傳圖片時：\n"
            "   - 若是股票/金融相關圖片：請進行精準、精簡的技術分析或財經數據解讀，直接點出關鍵支撐/壓力位、型態特徵與核心趨勢。\n"
            "   - 若用戶上傳的圖片與股票、投資或財經無關：請明確且禮貌地告知用戶：「抱歉，我只能幫您分析與『股票、K線圖、財報或財經資訊』相關的圖片喔！請上傳股票圖表或財經截圖，我將竭誠為您分析。」且不要對該非股票圖片進行其他分析。\n"
            "5. 【格式規定】：請使用乾淨、標準的繁體中文純文字段落排版。嚴格禁止使用 Markdown 粗體星號（如 **標題** 或 *重點*）、井字號（如 ## 標題）等特殊符號，直接以正常文字與換行進行呈現。\n"
            + TRADITIONAL_CHINESE_RULE
        )

        # 🚀 實時注入最新即時市場資訊（大盤、即時財經新聞、熱門活絡標的）
        realtime_context = ""
        try:
            from models import StockInfo, DailyPrice
            from db_service import SessionLocal
            from sqlalchemy import func

            with SessionLocal() as db:
                latest_date = db.query(func.max(DailyPrice.date)).scalar()
                if latest_date:
                    hot_rows = db.query(DailyPrice.code, StockInfo.name, DailyPrice.close, DailyPrice.volume).\
                        join(StockInfo, DailyPrice.code == StockInfo.code).\
                        filter(DailyPrice.date == latest_date, DailyPrice.volume.isnot(None)).\
                        order_by(DailyPrice.volume.desc()).limit(10).all()
                    
                    if hot_rows:
                        hot_list_str = "、".join([f"{r.name}({r.code}) 收盤價:{r.close} 量:{r.volume}張" for r in hot_rows])
                        realtime_context += f"\n【資料庫最新交易日 ({latest_date}) 成交量前10大熱門個股】：\n{hot_list_str}\n"
        except Exception:
            pass

        # 抓取最近 5 則最新市場新聞標題注入
        try:
            import requests, xml.etree.ElementTree as ET
            feed_resp = requests.get("https://news.ltn.com.tw/rss/business.xml", headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
            if feed_resp.status_code == 200:
                root = ET.fromstring(feed_resp.content)
                news_items = []
                for item in root.findall('.//item')[:5]:
                    title = item.find('title')
                    desc = item.find('description')
                    if title is not None and title.text:
                        news_items.append(f"• {title.text.strip()}" + (f"：{desc.text.strip()[:60]}..." if desc is not None and desc.text else ""))
                if news_items:
                    realtime_context += f"\n【今日即時重大財經頭條新聞】：\n" + "\n".join(news_items) + "\n"
        except Exception:
            pass

        if realtime_context:
            system_prompt += f"\n\n===== 系統即時市場數據與新聞快訊 =====\n{realtime_context}\n======================================\n請善用上述真實數據與新聞進行精準且精簡的重點回答。"

        messages = [{"role": "system", "content": system_prompt}]
        
        # 加上最近的歷史對話紀錄 (文字紀錄)
        for h in chat_history[-10:]:
            role = h.get("role")
            content = h.get("content")
            if role in ["user", "assistant"] and content:
                # 若歷史紀錄中有物件格式轉為純文字
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})

        # 組合當前使用者提問
        if image_base64:
            # 確保 base64 url 格式正確
            img_url = image_base64 if image_base64.startswith("data:image/") else f"data:image/jpeg;base64,{image_base64}"
            user_content = []
            if user_message:
                user_content.append({"type": "text", "text": user_message})
            else:
                user_content.append({"type": "text", "text": "請幫我分析這張圖片中的股票行情/技術圖形或財經內容。"})
            user_content.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })
            messages.append({"role": "user", "content": user_content})
            
            # 使用支援 vision 的 fallback 呼叫
            sanitized_content = ask_llm_with_fallback_vision(messages, temperature=0.3)
            sanitized_content = sanitize_llm_text(sanitized_content)
        else:
            messages.append({"role": "user", "content": user_message})
            sanitized_content = ask_llm_with_fallback(messages, temperature=0.3)
            sanitized_content = sanitize_llm_text(sanitized_content)

        return jsonify({"answer": sanitized_content})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 對話失敗：{type(e).__name__}: {e}"}), 500



# ==========================================
# YOLOv9 Pattern Detection Configuration
# ==========================================
YOLOV9_ROOT = Path(__file__).resolve().parent / "yolov9_mcp"
if not YOLOV9_ROOT.exists():
    YOLOV9_ROOT = Path(__file__).resolve().parent.parent / "yolov9_mcp"
YOLOV9_DIR = YOLOV9_ROOT / "yolov911"
MODEL_PATH = YOLOV9_ROOT / "best9.pt"

# Global model state
yolov9_model = None
yolov9_stride = None
yolov9_names = None
yolov9_imgsz = None
yolov9_device = None

# Keep references to yolov9 utilities
yolov9_letterbox = None
yolov9_non_max_suppression = None
yolov9_scale_boxes = None

pattern_mappings = {
    "head_shoulders": {
        "pattern_code": "head_shoulders",
        "friendly_name": "頭肩頂 (Head and Shoulders Top)",
        "en_name": "Head and Shoulders Top",
        "short_name": "頭肩頂",
        "bias": "偏空",
        "explanation": "典型頭肩頂部轉折型態，左肩、頭部與右肩依序成型，跌破頸線需防範高檔回檔修正。"
    },
    "head_shoulders_bottom": {
        "pattern_code": "head_shoulders_bottom",
        "friendly_name": "頭肩底 (Head and Shoulders Bottom)",
        "en_name": "Head and Shoulders Bottom",
        "short_name": "頭肩底",
        "bias": "偏多",
        "explanation": "典型頭肩底部翻揚型態，右肩完成且帶量突破頸線為重要轉強起漲訊號。"
    },
    "double_bottom": {
        "pattern_code": "double_bottom",
        "friendly_name": "雙重底 (Double Bottom / W底)",
        "en_name": "Double Bottom",
        "short_name": "雙重底",
        "bias": "偏多",
        "explanation": "雙重測底支撐確立，放量站穩頸線為底部翻揚起漲關鍵。"
    },
    "m_top": {
        "pattern_code": "double_top",
        "friendly_name": "雙重頂 (Double Top / M頭)",
        "en_name": "Double Top",
        "short_name": "雙重頂",
        "bias": "偏空",
        "explanation": "高檔雙峰承壓受阻，跌破頸線需防範高檔反轉回檔修正。"
    },
    "double_top": {
        "pattern_code": "double_top",
        "friendly_name": "雙重頂 (Double Top / M頭)",
        "en_name": "Double Top",
        "short_name": "雙重頂",
        "bias": "偏空",
        "explanation": "高檔雙峰承壓受阻，跌破頸線需防範高檔反轉回檔修正。"
    },
    "triangle": {
        "pattern_code": "triangle",
        "friendly_name": "三角形整理 (Triangle Pattern)",
        "en_name": "Triangle Pattern",
        "short_name": "三角形整理",
        "bias": "中性",
        "explanation": "高低點區間逐步收斂面臨方向抉擇，靜待放量突破帶出波段趨勢。"
    },
    "flag": {
        "pattern_code": "flag",
        "friendly_name": "旗形整理 (Flag Pattern)",
        "en_name": "Flag Pattern",
        "short_name": "旗形整理",
        "bias": "趨勢延續",
        "explanation": "波段急行軍後之中繼整理旗面，回測支撐有守多延續原趨勢方向。"
    }
}

def format_pattern_chinese_english(raw_name: str) -> dict:
    """
    嚴格限定僅支援 YOLO 5 大經典幾何型態：
    1. 頭肩頂/底 (Head and Shoulders Top/Bottom)
    2. 雙重頂/M頭 (Double Top)
    3. 雙重底/W底 (Double Bottom)
    4. 三角形整理 (Triangle Pattern)
    5. 旗形整理 (Flag Pattern)
    若傳入之型態不屬於上述 YOLO 5 大範圍，一律嚴格歸類為「無明顯型態 (No Obvious Pattern)」。
    """
    default_none = {
        "pattern_code": "none",
        "friendly_name": "無明顯型態 (No Obvious Pattern)",
        "en_name": "No Obvious Pattern",
        "short_name": "無明顯型態",
        "bias": "中性",
        "explanation": "走勢平緩或目前未見 YOLO 5 大經典幾何型態（頭肩、雙重頂/底、三角、旗形）特徵。"
    }

    if not raw_name or not isinstance(raw_name, str):
        return default_none

    raw = raw_name.strip()
    raw_l = raw.lower()
    norm = raw_l.replace(" ", "").replace("_", "").replace("-", "")

    # 1. 頭肩型態 (Top / Bottom)
    if "頭肩底" in raw or ("bottom" in norm and "shoulder" in norm) or "inverted" in norm:
        return dict(pattern_mappings["head_shoulders_bottom"])
    elif "頭肩" in raw or "shoulder" in norm:
        return dict(pattern_mappings["head_shoulders"])

    # 2. 雙重頂 / M頭
    elif "m頭" in raw_l or "m_top" in norm or "mtop" in norm or "雙重頂" in raw or "雙頂" in raw or "doubletop" in norm:
        return dict(pattern_mappings["m_top"])

    # 3. 雙重底 / W底
    elif "w底" in raw_l or "w_bottom" in norm or "wbottom" in norm or "雙重底" in raw or "雙底" in raw or "doublebottom" in norm:
        return dict(pattern_mappings["double_bottom"])

    # 4. 三角形整理
    elif "三角" in raw or "triangle" in norm:
        return dict(pattern_mappings["triangle"])

    # 5. 旗形整理
    elif "旗形" in raw or "flag" in norm:
        return dict(pattern_mappings["flag"])

    # 任何其他非 YOLO 5 大經典型態，一律歸類為無明顯型態
    return default_none

def get_pattern_explanation(label: str) -> dict:
    return format_pattern_chinese_english(label)

def draw_yolo_boxes(image: Image.Image, detections: list) -> Image.Image:
    """在圖片上繪製 YOLO 偵測到的紅色邊界框與繁體中文標籤（嚴格限定僅繪製信心度 >= 60% 之型態）"""
    from PIL import ImageDraw, ImageFont
    annotated = image.copy().convert("RGB")
    draw = ImageDraw.Draw(annotated)
    
    try:
        font = ImageFont.truetype("msjhbd.ttc", 16)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 15)
        except Exception:
            font = ImageFont.load_default()
            
    for det in detections:
        # 信心度防護：未達 60% 門檻一律不繪製紅框
        conf_f = det.get("confidence_float", 0.0)
        try:
            conf_pct = float(det.get("confidence", 0))
        except Exception:
            conf_pct = 0.0
        if conf_f < 0.60 and conf_pct < 60.0:
            continue

        box = det.get("box")
        if not box or len(box) != 4:
            continue
        x1, y1, x2, y2 = [int(v) for v in box]
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        
        name = det.get("friendly_name") or det.get("label") or "型態"
        conf = det.get("confidence") or "0"
        label_text = f" {name} {conf}% "
        
        # 繪製粗紅框 (3px)
        for offset in range(3):
            draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset], outline=(239, 68, 68))
            
        # 計算標籤尺寸
        bbox = font.getbbox(label_text) if hasattr(font, 'getbbox') else (0, 0, len(label_text)*10, 18)
        text_w = bbox[2] - bbox[0] + 6
        text_h = bbox[3] - bbox[1] + 6
        
        tag_y1 = max(0, y1 - text_h)
        tag_y2 = tag_y1 + text_h
        tag_x1 = max(0, x1)
        tag_x2 = tag_x1 + text_w
        
        # 繪製紅色背景徽章與白色文字
        draw.rectangle([tag_x1, tag_y1, tag_x2, tag_y2], fill=(239, 68, 68))
        draw.text((tag_x1 + 3, tag_y1 + 2), label_text, fill=(255, 255, 255), font=font)
        
    return annotated

def run_yolov9_direct_fallback(temp_img_path: str, conf_threshold: float = 0.60) -> str:
    """當 FastMCP 通訊遇到暫時阻礙時，直接透過 Python 3.10 直連王博士 YOLOv9 模型執行推論作為備援雙保險"""
    import subprocess
    try:
        cmd = [
            r"C:\Users\cyutim\AppData\Local\Programs\Python\Python310\python.exe",
            "-c",
            f"""
import sys
sys.path.insert(0, r'{YOLOV9_DIR}')
sys.path.insert(0, r'{YOLOV9_ROOT}')
from server import detect_objects
print(detect_objects(r'{temp_img_path}', conf_threshold={conf_threshold}))
"""
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return res.stdout or ""
    except Exception as e:
        print(f"[YOLOv9 本地直連備援錯誤]：{e}")
        return ""

def detect_visual_patterns_mcp(image: Image.Image, min_conf: float = 0.60) -> tuple:
    """透過 YOLOv9 FastMCP 伺服器與本地備援雙通道進行 K 線圖視覺型態辨識（信心度門檻 60%），並繪製標註紅框與去重"""
    import tempfile
    temp_dir = tempfile.gettempdir()
    temp_img_path = os.path.join(temp_dir, f"pattern_{int(time.time()*1000)}.png")
    
    annotated_image = image.copy()
    try:
        image.save(temp_img_path, format="PNG")
        output_str = ""
        
        # 1. 優先嘗試 FastMCP 通道 (依使用者要求：未達 60% 門檻不標註紅框與分析)
        try:
            client = global_mcp_client
            res = run_async(client.call_yolov9_detect(temp_img_path, conf_threshold=min_conf))
            if isinstance(res, dict):
                output_str = res.get("result", "")
            elif isinstance(res, str):
                output_str = res
        except Exception as mcp_err:
            print(f"[YOLOv9 MCP 通道警告]：{mcp_err}，嘗試切換本地直連通道...")

        # 2. 若 MCP 未回傳有效字串，啟動本地直連通道雙保險
        if not output_str or ("物件:" not in output_str and "偵測完成" not in output_str):
            output_str = run_yolov9_direct_fallback(temp_img_path, conf_threshold=min_conf)
            
        raw_detections = []
        pattern_regex = re.compile(r"物件:\s*([^,]+),\s*置信度:\s*([0-9.]+),\s*座標:\s*(\[[^\]]+\])")
        for line in str(output_str).split("\n"):
            m = pattern_regex.search(line)
            if m:
                cls_name = m.group(1).strip()
                conf_val = float(m.group(2).strip())
                box_str = m.group(3).strip()

                # 嚴格過濾：未滿 60% (min_conf) 信心度直接跳過，不加入偵測結果也不畫紅框
                if conf_val < min_conf:
                    continue

                try:
                    box = json.loads(box_str)
                except Exception:
                    box = []
                exp_data = get_pattern_explanation(cls_name)
                # 僅保留 YOLO 5 大經典幾何型態
                if exp_data.get("pattern_code") != "none":
                    raw_detections.append({
                        "label": exp_data.get("pattern_code") or cls_name,
                        "pattern_code": exp_data.get("pattern_code") or cls_name,
                        "friendly_name": exp_data["friendly_name"],
                        "en_name": exp_data.get("en_name", ""),
                        "short_name": exp_data.get("short_name", ""),
                        "bias": exp_data["bias"],
                        "confidence": f"{conf_val * 100:.1f}",
                        "confidence_float": conf_val,
                        "explanation": exp_data["explanation"],
                        "box": box
                    })
        
        # 3. 繪製所有偵測到的紅框到圖片上（僅當有 >= 60% 信心度型態時繪製）
        if raw_detections:
            annotated_image = draw_yolo_boxes(image, raw_detections)
        else:
            annotated_image = image.copy()
            
        # 4. 按置信度排序
        raw_detections.sort(key=lambda x: x["confidence_float"], reverse=True)
        
        # 5. 聚合去重：同一個型態（pattern_code）只保留置信度最高的一個，避免出現重複的分析卡片
        seen_codes = set()
        deduped_predictions = []
        for det in raw_detections:
            p_code = det.get("pattern_code") or det.get("label")
            if p_code not in seen_codes:
                seen_codes.add(p_code)
                deduped_predictions.append(det)
                
        return deduped_predictions, annotated_image
    except Exception as e:
        print(f"[YOLOv9 視覺偵測錯誤]：{e}")
        traceback.print_exc()
        return [], image
    finally:
        if os.path.exists(temp_img_path):
            try:
                os.remove(temp_img_path)
            except Exception:
                pass

def image_to_data_url(image: Image.Image) -> str:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


@app.route("/api/pattern_detect", methods=["POST"])
def api_pattern_detect():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "未收到圖片"}), 400
            
        file = request.files['image']
        # 取得股票代碼與公司名稱（若使用者有輸入）
        stock_code = request.form.get('stock_code', '').strip().upper()
        if stock_code.endswith('.TW'):
            stock_code = stock_code[:-3]
        elif stock_code.endswith('.TWO'):
            stock_code = stock_code[:-4]

        stock_name = ""
        if stock_code:
            try:
                stock_name = get_stock_name(stock_code) or ""
            except Exception:
                stock_name = ""

        image_bytes = file.read()
        image = Image.open(BytesIO(image_bytes))

        # 1. 優先使用 YOLOv9 FastMCP 伺服器進行原生視覺型態辨識（信心度門檻 60%）
        yolo_preds, annotated_image = detect_visual_patterns_mcp(image, min_conf=0.60)
        # 標註紅框後的影像轉為 base64 回傳給前端展示（若無 >= 60% 型態則為乾淨原圖）
        image_data = image_to_data_url(annotated_image)

        if yolo_preds:
            # 2. 達到 60% 門檻時，才使用 LLM Vision 結合標的股票進行深度走勢、點位與操作觀察分析
            data_url = image_to_data_url(image)
            llm_result = ask_llm_pattern_from_image(data_url, stock_code, stock_name)

            reasoning = llm_result.get("reasoning", [])
            watch_points = llm_result.get("watch_points", [])
            risk_note = llm_result.get("risk_note", "")

            # 標的顯示名稱
            target_label = f"{stock_name}（{stock_code}）" if (stock_name and stock_code) else (stock_code or "該標的")

            # 建立共用的走勢與關鍵觀察段落（融入個股代碼與名稱）
            clean_r = "；".join([str(r).strip().rstrip("。；") for r in reasoning if str(r).strip()][:2])
            if clean_r and stock_code and stock_code not in clean_r and stock_name not in clean_r:
                clean_r = f"{target_label}走勢顯示：{clean_r}"

            clean_w = "；".join([str(w).strip().rstrip("。；") for w in watch_points if str(w).strip()][:2])
            if clean_w and stock_code and stock_code not in clean_w and stock_name not in clean_w:
                clean_w = f"重點觀察{target_label}：{clean_w}"

            clean_risk = str(risk_note).strip().rstrip("。") if (risk_note and str(risk_note).strip()) else ""

            # YOLOv9 偵測成功（信心度 >= 60%，已去除重複同型態，只保留最高信心度）
            predictions = yolo_preds
            # 確保每個預測物件的 friendly_name 均經過標準化中英格式校驗
            for p in predictions:
                norm_info = format_pattern_chinese_english(p.get("label") or p.get("friendly_name", ""))
                p["friendly_name"] = norm_info["friendly_name"]
                p["en_name"] = norm_info["en_name"]
                p["short_name"] = norm_info["short_name"]
                p["pattern_code"] = norm_info["pattern_code"]
                p["label"] = norm_info["pattern_code"]
                p["bias"] = p.get("bias") or norm_info["bias"]

            top = predictions[0]
            result = top.get("friendly_name", "沒有分析到明顯的型態辨識")

            # 為每一個去重後的型態組裝完整的結構化分析，絕不留空
            for p in predictions:
                sec = []
                base_desc = p.get("explanation", "").strip()
                if base_desc:
                    sec.append(f"【形態特徵】{base_desc}")
                if clean_r:
                    sec.append(f"【走勢解析】{clean_r}。")
                if clean_w:
                    sec.append(f"【關鍵觀察】{clean_w}。")
                if clean_risk:
                    sec.append(f"【風險提示】{clean_risk}。")
                p["explanation"] = "\n\n".join(sec) if sec else "型態辨識完成。"

            top_explanation = top.get("explanation", "")
        else:
            # 依使用者嚴格要求：
            # 「沒有到信心度60%就不要顯示並且不要有紅框，60%等於或以上才顯示紅框並分析，如果都沒有分析到60%等於或以上的型態，就直接回答沒有分析到明顯的型態辨識」
            result = "沒有分析到明顯的型態辨識"
            top_explanation = "沒有分析到明顯的型態辨識"
            predictions = []

        return jsonify({
            "result": result,
            "explanation": top_explanation,
            "image_data": image_data,
            "predictions": predictions
        })
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"辨識失敗：{str(e)}"}), 500



@app.route("/api/validate_stock")
def api_validate_stock():
    """驗證股票代碼是否存在於本地資料庫（不需連外網），供型態辨識頁面使用"""
    code = request.args.get("code", "").strip().upper()
    if code.endswith('.TW'):
        code = code[:-3]
    elif code.endswith('.TWO'):
        code = code[:-4]

    if not code:
        return jsonify({"valid": False, "error": "請輸入股票代碼"}), 400

    try:
        name = get_stock_name(code)
        if name:
            return jsonify({"valid": True, "code": code, "name": name})
        else:
            return jsonify({"valid": False, "error": f"找不到股票代碼「{code}」，請確認後重新輸入"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"valid": False, "error": f"查詢失敗：{str(e)}"}), 500


@app.route("/api/stock_profile")

def api_stock_profile():
    code = request.args.get("code", "2330").strip().upper()
    if code.endswith('.TW'): code = code[:-3]
    elif code.endswith('.TWO'): code = code[:-4]
    
    if not re.fullmatch(r"[0-9][A-Z0-9]{3,5}", code):
        return jsonify({"error": "請輸入有效的股票代號（4-6位英數字，且首字須為數字）"}), 400

    try:
        data = run_async(get_stock_profile_data(code))
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"獲取基本面資料失敗：{e}"}), 500


@app.route("/api/scan_patterns")
def api_scan_patterns():
    code = request.args.get("code", "2330").strip()
    period = request.args.get("period", "1d").strip()

    try:
        client = global_mcp_client
        series = run_async(get_history_series(code, client, period))
        
        # 轉為 numpy 以進行分析
        c = np.array(series.get("close", []), dtype=float)
        if len(c) < 10:
            return jsonify({"patterns": [], "note": "資料不足以分析"})
            
        patterns = detect_talib_patterns(series)
        return jsonify({"patterns": patterns, "code": code})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"自動掃描失敗：{e}"}), 500


@app.route("/api/pattern_mcp_scan")
def api_pattern_mcp_scan():
    """
    使用 TA-Lib MCP 掃描 K 線型態（蠟燭圖型態）
    參數：code (台股代號), period (1d/1wk/1mo)
    """
    code = request.args.get("code", "2330").strip()
    period = request.args.get("period", "1d").strip()

    if not re.fullmatch(r"\d{4,6}", code):
        return jsonify({"error": "請輸入正確台股代號"}), 400

    # 所有支援的蠟燭型態
    CDL_PATTERNS = {
        "CDLHAMMER":        "錘頭 (Hammer)",
        "CDLENGULFING":     "吞噬 (Engulfing)",
        "CDLMORNINGSTAR":   "晨星 (Morning Star)",
        "CDLEVENINGSTAR":   "暮星 (Evening Star)",
        "CDLHARAMI":        "孕線 (Harami)",
        "CDLPIERCING":      "貫穿 (Piercing Line)",
        "CDL3WHITESOLDIERS":"三紅兵 (3 White Soldiers)",
        "CDL3BLACKCROWS":   "三烏鴉 (3 Black Crows)",
        "CDLSHOOTINGSTAR":  "射擊之星 (Shooting Star)",
        "CDLMARUBOZU":      "光頭光腳 (Marubozu)",
        "CDLDOJI":          "十字星 (Doji)",
        "CDLDOJISTAR":      "十字星之星 (Doji Star)",
        "CDLHANGINGMAN":    "上吊線 (Hanging Man)",
        "CDLINVERTEDHAMMER":"倒錘 (Inverted Hammer)",
        "CDLSPINNINGTOP":   "紡錘線 (Spinning Top)",
    }

    try:
        client = global_mcp_client

        # 1. 透過 yfinance MCP 取得歷史 OHLCV
        series = run_async(get_history_series(code, client, period))
        dates  = series.get("dates",  [])
        opens  = series.get("open",   [])
        highs  = series.get("high",   [])
        lows   = series.get("low",    [])
        closes = series.get("close",  [])

        if len(closes) < 10:
            return jsonify({"patterns": [], "kline": [], "note": "資料不足（至少需要 10 根 K 線）"})

        ohlc = {
            "open":  [float(v) for v in opens],
            "high":  [float(v) for v in highs],
            "low":   [float(v) for v in lows],
            "close": [float(v) for v in closes],
        }

        # 2. 並行透過 TA-Lib MCP 呼叫所有蠟燭型態
        async def scan_all_cdl():
            tasks = {
                name: client.call_talib_cdl_pattern(name, ohlc)
                for name in CDL_PATTERNS
            }
            results = {}
            for name, coro in tasks.items():
                try:
                    results[name] = await coro
                except Exception as e:
                    print(f"[CDL MCP] {name} error: {e}")
                    results[name] = None
            return results

        mcp_results = run_async(scan_all_cdl())

        # 3. 解析 MCP 回傳並過濾有訊號的型態
        found = []
        for pattern_key, friendly_name in CDL_PATTERNS.items():
            raw = mcp_results.get(pattern_key)
            if raw is None:
                continue
            # MCP 回傳值格式：{'result': [0, 0, 100, 0, ...]}
            values = []
            if isinstance(raw, dict):
                r = raw.get("result", raw.get("output", []))
                if isinstance(r, list):
                    values = r
                elif isinstance(r, (int, float)):
                    values = [r]
            elif isinstance(raw, list):
                values = raw

            if not values:
                continue

            for i in range(-1, -4, -1):
                if abs(i) > len(values):
                    break
                val = values[i]
                if isinstance(val, (int, float)) and val != 0:
                    date_str = dates[i] if dates and abs(i) <= len(dates) else f"T-{abs(i)-1}"
                    found.append({
                        "pattern":   friendly_name,
                        "key":       pattern_key,
                        "sentiment": "bullish" if val > 0 else "bearish",
                        "date":      date_str,
                        "offset":    abs(i) - 1,
                    })

        found.sort(key=lambda x: x["offset"])

        # 4. 建立 K 線圖資料（最近 60 根）
        tail = min(60, len(closes))
        kline = []
        for j in range(len(closes) - tail, len(closes)):
            kline.append({
                "time":  dates[j]  if j < len(dates)  else "",
                "open":  float(opens[j])  if j < len(opens)  else 0.0,
                "high":  float(highs[j])  if j < len(highs)  else 0.0,
                "low":   float(lows[j])   if j < len(lows)   else 0.0,
                "close": float(closes[j]) if j < len(closes) else 0.0,
            })

        return jsonify({
            "code":     code,
            "period":   period,
            "patterns": found,
            "kline":    kline,
            "total_bars": len(closes),
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"型態掃描失敗：{type(e).__name__}: {e}"}), 500


@app.route("/api/pattern_analyze")
def api_pattern_analyze():
    """
    完整流程：MCP (TA-Lib) 掃描蠟燭型態 + LLM 輸出繁體中文分析報告
    """
    code = request.args.get("code", "2330").strip()
    period = request.args.get("period", "1d").strip()

    if not re.fullmatch(r"\d{4,6}", code):
        return jsonify({"error": "請輸入正確台股代號（4-6位數字）"}), 400

    CDL_PATTERNS = {
        "CDLHAMMER":        "錘頭 (Hammer)",
        "CDLENGULFING":     "吞噬 (Engulfing)",
        "CDLMORNINGSTAR":   "晨星 (Morning Star)",
        "CDLEVENINGSTAR":   "暮星 (Evening Star)",
        "CDLHARAMI":        "孕線 (Harami)",
        "CDLPIERCING":      "貫穿 (Piercing Line)",
        "CDL3WHITESOLDIERS":"三紅兵 (3 White Soldiers)",
        "CDL3BLACKCROWS":   "三烏鴉 (3 Black Crows)",
        "CDLSHOOTINGSTAR":  "射擊之星 (Shooting Star)",
        "CDLMARUBOZU":      "光頭光腳 (Marubozu)",
        "CDLDOJI":          "十字星 (Doji)",
        "CDLDOJISTAR":      "十字星之星 (Doji Star)",
        "CDLHANGINGMAN":    "上吊線 (Hanging Man)",
        "CDLINVERTEDHAMMER":"倒錘 (Inverted Hammer)",
        "CDLSPINNINGTOP":   "紡錘線 (Spinning Top)",
    }

    try:
        client = global_mcp_client

        # 1. 取得歷史 K 線
        series = run_async(get_history_series(code, client, period))
        dates  = series.get("dates",  [])
        opens  = series.get("open",   [])
        highs  = series.get("high",   [])
        lows   = series.get("low",    [])
        closes = series.get("close",  [])

        if len(closes) < 10:
            return jsonify({"error": "資料不足（至少需要 10 根 K 線）"}), 400

        ohlc = {
            "open":  [float(v) for v in opens],
            "high":  [float(v) for v in highs],
            "low":   [float(v) for v in lows],
            "close": [float(v) for v in closes],
        }

        # 2. MCP 並行掃描所有蠟燭型態
        async def scan_all_cdl():
            results = {}
            for name, coro in {n: client.call_talib_cdl_pattern(n, ohlc) for n in CDL_PATTERNS}.items():
                try:
                    results[name] = await coro
                except Exception as e:
                    print(f"[CDL MCP] {name} error: {e}")
                    results[name] = None
            return results

        mcp_results = run_async(scan_all_cdl())

        # 3. 解析有訊號的型態
        found = []
        for pattern_key, friendly_name in CDL_PATTERNS.items():
            raw = mcp_results.get(pattern_key)
            if raw is None:
                continue
            values = []
            if isinstance(raw, dict):
                r = raw.get("result", raw.get("output", []))
                values = r if isinstance(r, list) else ([r] if isinstance(r, (int, float)) else [])
            elif isinstance(raw, list):
                values = raw

            for i in range(-1, -4, -1):
                if abs(i) > len(values):
                    break
                val = values[i]
                if isinstance(val, (int, float)) and val != 0:
                    date_str = dates[i] if dates and abs(i) <= len(dates) else f"T-{abs(i)-1}"
                    found.append({
                        "pattern":   friendly_name,
                        "key":       pattern_key,
                        "sentiment": "bullish" if val > 0 else "bearish",
                        "date":      date_str,
                        "offset":    abs(i) - 1,
                    })

        found.sort(key=lambda x: x["offset"])

        # 4. LLM 文字分析
        close_floats = [float(c) for c in closes]
        llm_analysis = ask_llm_pattern_text_analysis(code, found, close_floats, period)

        # 5. K 線資料（最近 60 根）
        tail = min(60, len(closes))
        kline = []
        for j in range(len(closes) - tail, len(closes)):
            kline.append({
                "time":  dates[j]  if j < len(dates)  else "",
                "open":  float(opens[j])  if j < len(opens)  else 0.0,
                "high":  float(highs[j])  if j < len(highs)  else 0.0,
                "low":   float(lows[j])   if j < len(lows)   else 0.0,
                "close": float(closes[j]) if j < len(closes) else 0.0,
            })

        return jsonify({
            "code":       code,
            "period":     period,
            "patterns":   found,
            "analysis":   llm_analysis,
            "kline":      kline,
            "total_bars": len(closes),
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"分析失敗：{type(e).__name__}: {e}"}), 500


# 全域新聞全文快取：{news_id_or_url: [paragraphs]}，供新聞詳情秒級讀取（響應 0ms，杜絕網路逾時）
CNYES_ARTICLE_CACHE = {}


def fetch_cnyes_stock_news(code: str, name: str = None, limit: int = 10) -> list:
    """
    【現代逆向 JSON API】直接請求鉅亨網 (Anue Cnyes) 內部搜索 JSON 接口
    回傳具備標題、新聞連結、發布時間、封面圖片與摘要的高品質結構化資料 (響應 < 0.1s)
    """
    import requests
    import re
    import html
    from datetime import datetime

    items = []
    clean_code = str(code).replace('.TW', '').replace('.TWO', '').strip()
    query = clean_code
    if name:
        clean_name = name.split('(')[0].strip()
        if clean_name and clean_name != clean_code:
            query = f"{clean_code} {clean_name}"

    try:
        url = f"https://api.cnyes.com/media/api/v1/search?q={query}&limit={limit}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        resp = requests.get(url, headers=headers, timeout=4)
        if resp.status_code == 200:
            raw_items = resp.json().get("items", {}).get("data", [])
            for it in raw_items:
                raw_title = it.get("title", "")
                if not raw_title:
                    continue
                # 清除 HTML 搜尋高亮標記如 <mark>
                title = re.sub(r'<[^>]+>', '', raw_title).strip()
                title = html.unescape(title)

                nid = it.get("newsId")
                link = f"https://news.cnyes.com/news/id/{nid}" if nid else "#"

                pub_ts = it.get("publishAt", 0)
                pub_time_str = datetime.fromtimestamp(pub_ts).strftime('%m/%d %H:%M') if pub_ts else ""

                # 封面縮圖
                cover_img = ""
                c_src = it.get("coverSrc")
                if isinstance(c_src, dict):
                    cover_img = c_src.get("m", {}).get("src") or c_src.get("s", {}).get("src") or ""

                # 新聞摘要
                summary = it.get("summary") or it.get("content") or ""
                summary = re.sub(r'<[^>]+>', '', html.unescape(summary))[:160].strip()

                # 如果快取已命中完整全文段落，直接關聯；若無則將摘要備用段落加入快取
                item_paras = CNYES_ARTICLE_CACHE.get(str(nid), [])
                if not item_paras and summary and len(summary) > 20:
                    CNYES_ARTICLE_CACHE.setdefault(str(nid), [summary])

                items.append({
                    'code': clean_code,
                    'name': name or clean_code,
                    'title': title,
                    'link': link,
                    'publisher': '鉅亨網',
                    'time': pub_time_str,
                    'sort_ts': pub_ts,
                    'tag': f"{clean_code} {name or ''}".strip(),
                    'content': summary,
                    'cover_img': cover_img,
                    'paragraphs': item_paras
                })
    except Exception as e:
        print(f"[cnyes_stock_news] 抓取 {clean_code} 逆向 API 失敗: {e}")

    return items


def fetch_domestic_news_for_stock_rss(code, name=None):
    """
    【傳統 RSS 備援管道】使用 Google News RSS 抓取個股新聞（當現代 API 遇網路阻礙時自動降級）
    """
    import urllib.parse
    import xml.etree.ElementTree as ET
    import requests
    from datetime import datetime

    items = []
    clean_name = name.split('(')[0].strip() if name else ""
    query = f"{code} {clean_name}" if clean_name else code

    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item')[:10]:
                title_full = item.find('title').text or ""
                link = item.find('link').text or "#"
                pub_date = item.find('pubDate').text or ""

                publisher = "國內媒體"
                title = title_full
                if " - " in title_full:
                    parts = title_full.rsplit(" - ", 1)
                    title = parts[0]
                    publisher = parts[1]

                sort_ts = 0
                pub_time_str = pub_date
                try:
                    clean_date = pub_date
                    if "GMT" in clean_date:
                        clean_date = clean_date.replace("GMT", "").strip()
                    dt = datetime.strptime(clean_date, "%a, %d %b %Y %H:%M:%S")
                    sort_ts = int(dt.timestamp())
                    pub_time_str = dt.strftime('%m/%d %H:%M')
                except Exception:
                    pass

                items.append({
                    'code': code,
                    'name': name or code,
                    'title': title,
                    'link': link,
                    'publisher': publisher,
                    'time': pub_time_str,
                    'sort_ts': sort_ts,
                    'tag': f"{code} {name or ''}".strip(),
                    'content': '',
                    'cover_img': ''
                })
    except Exception as e:
        print(f"[domestic_news_rss] 備援抓取 {code} 失敗: {e}")

    return items


def fetch_domestic_news_for_stock(code, name=None):
    """
    【現代化新聞入口】
    第一優先：採用鉅亨網現代逆向 JSON API（精準、高速、帶封面圖與摘要）
    自動降級：若無數據或暫時超時，自動切換至 Google News RSS 傳統備援
    """
    clean_code = str(code).replace('.TW', '').replace('.TWO', '').strip()
    if not name:
        try:
            from db_service import get_stock_name
            name = get_stock_name(clean_code) or ""
        except Exception:
            name = ""

    # 1. 現代逆向 JSON API
    cnyes_items = fetch_cnyes_stock_news(clean_code, name=name, limit=10)
    if cnyes_items:
        return cnyes_items

    # 2. 自動容錯降級回 RSS
    return fetch_domestic_news_for_stock_rss(clean_code, name=name)


def normalize_ticker(symbol: str) -> str:
    """將台股代碼轉換為 Yahoo Finance 格式"""
    if not symbol: return ""
    # 如果已經有點號，視為已格式化
    if "." in symbol: return symbol
    # 如果是 4 位以上的純數字，處理台股後綴
    if symbol.isdigit() and len(symbol) >= 4:
        # 這裡採取一個簡單策略：先嘗試 .TW (上市)，如果不對再由呼叫者處理
        return f"{symbol}.TW"
    return symbol

@app.route('/api/news/watchlist')
def api_news_watchlist():
    """
    取得使用者所有自選股的相關新聞（聚合自 yfinance）。
    需要登入，自動從 session 讀取 user_id，並取得其所有自選清單的股票。
    """
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入', 'news': []}), 401

    try:
        # 1. 取得所有自選清單中的股票代號（合併去重）
        if not _watchbot_available:
            return jsonify({'error': '觀察機器人模組未啟用', 'news': []}), 503

        all_codes_raw = get_watchlist(user_id)  # 預設取第一個清單
        # 也嘗試取所有清單
        try:
            wls = get_user_watchlists(user_id)
            code_set = set()
            for wl in wls:
                codes_in_wl = get_watchlist(user_id, watchlist_id=wl['id'])
                code_set.update(codes_in_wl)
            all_codes = list(code_set)
        except Exception:
            all_codes = list(set(all_codes_raw))

        if not all_codes:
            return jsonify({'news': [], 'message': '自選股清單為空，請先新增股票到觀察清單。'})

        # 2. 取得股票名稱 map
        try:
            from db_service import get_stock_name
            name_map = {code: (get_stock_name(code) or code) for code in all_codes}
        except Exception:
            name_map = {code: code for code in all_codes}

        # 3. 並行抓取每支股票的新聞（最多 20 支避免 timeout）
        import concurrent.futures

        def _fetch_stock_news(code):
            """抓取單一股票新聞，回傳格式化後的 list"""
            stock_name = name_map.get(code, code)
            return fetch_domestic_news_for_stock(code, stock_name)

        codes_to_fetch = all_codes[:20]
        all_news = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_stock_news, code): code for code in codes_to_fetch}
            for future in concurrent.futures.as_completed(futures, timeout=25):
                try:
                    result = future.result()
                    all_news.extend(result)
                except Exception:
                    pass

        # 4. 去重（依 title 去重）並依時間排序（最新在前）
        seen_titles = set()
        unique_news = []
        for item in all_news:
            t = item.get('title', '')
            if t and t not in seen_titles:
                seen_titles.add(t)
                unique_news.append(item)

        unique_news.sort(key=lambda x: x.get('sort_ts', 0), reverse=True)

        return jsonify({
            'news': unique_news,
            'total': len(unique_news),
            'stocks': list(name_map.keys()),
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'抓取自選股新聞失敗：{type(e).__name__}: {e}', 'news': []}), 500


@app.route('/api/search_news')
def api_search_news():
    query = request.args.get('q', '^TWII')
    try:
        import yfinance as yf
        ticker_obj = yf.Ticker(query)
        news_list = ticker_obj.news  # 回傳完整 list of dict
        
        if not news_list:
            # 嘗試常見的台股大盤 ticker
            if query in ('^TWII', 'TWII', '大盤'):
                ticker_obj = yf.Ticker('^TWII')
                news_list = ticker_obj.news
        
        parts = []
        for item in (news_list or [])[:15]:
            if not isinstance(item, dict):
                continue
            # 新版 yfinance 的格式
            content = item.get('content', {})
            if isinstance(content, dict):
                title = content.get('title', '')
                link = content.get('canonicalUrl', {}).get('url', '') if isinstance(content.get('canonicalUrl'), dict) else content.get('clickThroughUrl', {}).get('url', '#')
                publisher = content.get('provider', {}).get('displayName', 'Yahoo Finance') if isinstance(content.get('provider'), dict) else 'Yahoo Finance'
                pub_time = content.get('pubDate', '')
            else:
                # 舊版格式
                title = item.get('title', '')
                link = item.get('link', '#')
                publisher = item.get('publisher', 'Yahoo Finance')
                t_val = item.get('providerPublishTime', 0)
                pub_time = time.strftime('%m/%d %H:%M', time.localtime(t_val)) if t_val else ''
            
            if not title:
                continue
            
            # 格式化時間
            if pub_time and 'T' in str(pub_time):
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(str(pub_time).replace('Z', '+00:00'))
                    pub_time = dt.astimezone().strftime('%m/%d %H:%M')
                except:
                    pass
            
            if not link or link == '#':
                link = item.get('link', '#')
            
            safe_title = str(title).encode("utf-8", "ignore").decode("utf-8")
            parts.append(f"### [{safe_title}]({link})\n*Source: {publisher} - {pub_time}*\n\n---\n\n")
        
        markdown = "".join(parts) if parts else "目前沒有相關新聞。"
        return jsonify({"success": True, "markdown": markdown})
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500


def fetch_cnyes_market_news() -> list:
    """
    【現代逆向 JSON API】並行請求鉅亨網 (Anue Cnyes) 頭條與台股即時 API
    直接獲取純 JSON 格式的即時新聞、高解析度封面縮圖、完整文章段落與摘要 (響應 < 0.2s)
    """
    import requests
    import re
    import html
    import time
    from bs4 import BeautifulSoup
    from datetime import datetime
    import concurrent.futures

    endpoints = [
        {"url": "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=40", "source": "鉅亨網"},
        {"url": "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=40", "source": "鉅亨頭條"}
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    all_items = []
    seen_titles = set()

    def _fetch_endpoint(ep):
        res = []
        try:
            r = requests.get(ep["url"], headers=headers, timeout=5)
            if r.status_code == 200:
                raw_data = r.json()
                for it in raw_data.get("items", {}).get("data", []):
                    title = it.get("title", "").strip()
                    if not title:
                        continue
                    title = re.sub(r'<[^>]+>', '', title)
                    title = html.unescape(title)
                    nid = it.get("newsId")
                    link = f"https://news.cnyes.com/news/id/{nid}" if nid else "#"
                    pub_ts = it.get("publishAt", int(time.time()))

                    c_src = it.get("coverSrc")
                    img = ""
                    if isinstance(c_src, dict):
                        img = c_src.get("m", {}).get("src") or c_src.get("s", {}).get("src") or ""

                    # 解析完整 HTML 內文並快取（秒開全文）
                    raw_content = it.get("content") or ""
                    paragraphs = []
                    if raw_content:
                        try:
                            c_soup = BeautifulSoup(html.unescape(raw_content), "html.parser")
                            for p in c_soup.find_all(["p", "div"]):
                                txt = p.get_text().strip()
                                if len(txt) > 15 and not any(kw in txt for kw in ["LINE ID", "Youtube", "按讚訂閱", "https://lin", "本公司所推薦", "未經授權", "翻印必究", "著作權", "莊佳螢分析師"]):
                                    if txt not in paragraphs:
                                        paragraphs.append(txt)
                        except Exception:
                            pass

                    # 寫入全域內文快取 (支援 newsId 與完整 link 雙重索引)
                    if paragraphs:
                        if nid:
                            CNYES_ARTICLE_CACHE[str(nid)] = paragraphs
                        if link and link != "#":
                            CNYES_ARTICLE_CACHE[link] = paragraphs

                    summary = it.get("summary") or (paragraphs[0] if paragraphs else "") or raw_content
                    summary = re.sub(r'<[^>]+>', '', html.unescape(summary))[:130].strip()

                    res.append({
                        "title": title,
                        "link": link,
                        "pub_ts": pub_ts,
                        "content": summary,
                        "source": ep["source"],
                        "cover_img": img,
                        "paragraphs": paragraphs
                    })
        except Exception as e:
            print(f"[cnyes_market_news] 請求 {ep['url']} 失敗: {e}")
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_fetch_endpoint, ep) for ep in endpoints]
        for fut in concurrent.futures.as_completed(futures):
            for item in fut.result():
                norm_t = item['title'].lower().replace(" ", "")
                if norm_t not in seen_titles:
                    seen_titles.add(norm_t)
                    all_items.append(item)

    all_items.sort(key=lambda x: x['pub_ts'], reverse=True)
    return all_items


def fetch_rss_market_news_fallback() -> list:
    """
    【傳統 RSS 備援】自由時報與經濟日報 RSS 備用管線
    """
    import xml.etree.ElementTree as ET
    import html
    import requests
    import re
    import time
    from datetime import datetime
    import concurrent.futures

    feeds = [
        {"url": "https://news.ltn.com.tw/rss/business.xml", "source": "自由時報財經"},
        {"url": "https://money.udn.com/rssfeed/news/1001/5590?ch=money", "source": "經濟日報股市"}
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def parse_pub_date(pub_date_str):
        pub_date_str = pub_date_str.strip()
        try:
            clean_str = pub_date_str.split("+")[0].split("-")[0].strip()
            dt = datetime.strptime(clean_str, "%a, %d %b %Y %H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            pass
        return int(time.time())

    def fetch_feed(feed):
        items_list = []
        try:
            resp = requests.get(feed["url"], headers=headers, timeout=5)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    title = item.find("title").text if item.find("title") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else "#"
                    pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    description = item.find("description").text if item.find("description") is not None else ""
                    if not title:
                        continue
                    clean_desc = re.sub('<[^<]+?>', '', description)
                    items_list.append({
                        "title": html.unescape(title).strip(),
                        "link": link,
                        "pub_ts": parse_pub_date(pub_date_str),
                        "content": html.unescape(clean_desc).strip(),
                        "source": feed["source"],
                        "cover_img": "",
                        "paragraphs": []
                    })
        except Exception:
            pass
        return items_list

    all_raw_news = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch_feed, f) for f in feeds]
        for fut in concurrent.futures.as_completed(futures):
            all_raw_news.extend(fut.result())

    seen = set()
    dedup = []
    for it in all_raw_news:
        nt = it['title'].lower().replace(" ", "")
        if nt not in seen:
            seen.add(nt)
            dedup.append(it)
    dedup.sort(key=lambda x: x['pub_ts'], reverse=True)
    return dedup


@app.route('/api/news/market')
def api_market_news():
    try:
        from datetime import datetime

        # 1. 優先採用現代逆向 JSON API (鉅亨網 Anue)
        cnyes_news = fetch_cnyes_market_news()

        # 2. 自動容錯降級：若現代 API 暫時無回應，切換傳統 RSS
        if not cnyes_news:
            cnyes_news = fetch_rss_market_news_fallback()

        category_keywords = {
            "ETF新聞": ["etf", "0050", "0056", "00878", "00919", "00929", "00940", "00896", "00400", "高股息", "配息", "除息", "成分股", "受益人", "受益憑證"],
            "半導體": ["台積電", "台積", "聯發科", "晶圓", "ic設計", "封測", "晶片", "矽智財", "asml", "英特爾", "intel", "三星", "輝達", "晶圓代工", "艾司摩爾", "聯電", "日月光", "nv", "nvidia", "tsmc", "tsm", "力積電", "世界先進", "晶片法案", "世芯"],
            "人工智慧": ["人工智慧", "ai", "輝達", "nvidia", "伺服器", "算力", "機器人", "散熱", "液冷", "機殼", "黃仁勳", "chatgpt", "llm", "openai", "超微", "amd", "微軟", "microsoft", "谷歌", "google", "奧特曼", "asic"],
            "金融財經": ["金控", "銀行", "金融", "證券", "保險", "壽險", "降息", "升息", "聯準會", "fed", "央行", "國泰金", "富邦金", "中信金", "兆豐金", "玉山金", "第一金", "新光金", "台新金", "開發金", "永豐金", "合庫金", "華南金"],
            "航運重電": ["航運", "貨櫃", "陽明", "長榮", "萬海", "綠能", "風電", "光電", "太陽能", "儲能", "重電", "華城", "士電", "中興電", "亞力", "東元", "紅海", "運價", "scfi", "塞港"],
            "國際財經": ["國際", "美股", "美金", "美元", "美債", "美聯社", "歐元", "日圓", "日元", "中國", "陸股", "港股", "歐盟", "日股", "韓股", "通膨", "cpi", "日經", "納斯達克", "費半", "標普", "道瓊", "外資", "歐美", "日央", "歐洲", "美聯會"],
            "生技醫療": ["生技", "醫療", "新藥", "臨床", "疫苗", "藥廠", "藥品", "健保", "癌症", "醫材", "生醫", "生技股", "藥華藥", "美時", "保瑞"]
        }

        news_list = []
        for item in cnyes_news:
            title_lower = item['title'].lower()
            desc_lower = (item.get('content') or '').lower()

            tag = "一般財經"
            for cat_name, keywords in category_keywords.items():
                matched = False
                for kw in keywords:
                    if kw in title_lower or kw in desc_lower:
                        tag = cat_name
                        matched = True
                        break
                if matched:
                    break

            pub_ts = item.get('pub_ts', 0)
            time_str = datetime.fromtimestamp(pub_ts).strftime('%m/%d %H:%M') if pub_ts else '剛剛'

            news_list.append({
                "title": item['title'],
                "link": item['link'],
                "tag": tag,
                "time": time_str,
                "content": item.get('content', ''),
                "source": item.get('source', '鉅亨網'),
                "cover_img": item.get('cover_img', ''),
                "paragraphs": item.get('paragraphs', [])
            })

        return jsonify({"success": True, "news": news_list})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "news": []}), 500

@app.route('/api/news/content')
def api_news_content():
    url = request.args.get('url')
    if not url:
        return jsonify({"success": False, "error": "Missing url parameter"}), 400
        
    try:
        import requests
        from bs4 import BeautifulSoup
        import html
        import re
        import time

        # 1. 優先從記憶體快取秒取（若來自即時市場新聞或先前已解析的新聞，0ms 回應，絕不逾時）
        match = re.search(r'/news/id/(\d+)', url)
        news_id = match.group(1) if match else None

        if news_id and str(news_id) in CNYES_ARTICLE_CACHE and len(CNYES_ARTICLE_CACHE[str(news_id)]) > 0:
            return jsonify({"success": True, "paragraphs": CNYES_ARTICLE_CACHE[str(news_id)], "cached": True})

        if url in CNYES_ARTICLE_CACHE and len(CNYES_ARTICLE_CACHE[url]) > 0:
            return jsonify({"success": True, "paragraphs": CNYES_ARTICLE_CACHE[url], "cached": True})

        # 2. 如果是 Google News 的轉址連結，先進行解碼以取得真實新聞網址
        if url and ("news.google.com" in url or "articles/" in url):
            try:
                from googlenewsdecoder import gnewsdecoder
                res = gnewsdecoder(url)
                if isinstance(res, dict) and res.get("decoded_url"):
                    url = res["decoded_url"]
                    print(f"[api_news_content] Decoded Google News URL to: {url}")
                elif isinstance(res, str) and res.startswith("http"):
                    url = res
                    print(f"[api_news_content] Decoded Google News URL to: {url}")
            except Exception as dec_err:
                print(f"[api_news_content] Failed to decode Google News URL: {dec_err}")

        # 3. 若為鉅亨網新聞且未在快取中，使用專用 TLS 容錯連線直接取得頁面
        if "cnyes.com" in url:
            try:
                import ssl
                import urllib.request
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-TW,zh;q=0.9",
                        "Connection": "close"
                    }
                )
                with urllib.request.urlopen(req, timeout=4, context=ctx) as r_cnyes:
                    cnyes_html = r_cnyes.read()
                    soup_cnyes = BeautifulSoup(cnyes_html, "html.parser")
                    for u in soup_cnyes.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                        u.decompose()
                    article_cnyes = soup_cnyes.find(id="article-container") or soup_cnyes.find("article") or soup_cnyes.find("main") or soup_cnyes
                    cnyes_paras = []
                    for p in article_cnyes.find_all("p"):
                        txt = p.get_text().strip()
                        if len(txt) > 20 and not any(kw in txt for kw in ["LINE ID", "Youtube", "按讚訂閱", "https://lin", "本公司所推薦", "未經授權", "翻印必究", "著作權", "莊佳螢分析師"]):
                            if txt not in cnyes_paras:
                                cnyes_paras.append(txt)
                    if cnyes_paras:
                        if news_id:
                            CNYES_ARTICLE_CACHE[str(news_id)] = cnyes_paras
                        CNYES_ARTICLE_CACHE[url] = cnyes_paras
                        return jsonify({"success": True, "paragraphs": cnyes_paras})
            except Exception as cnyes_err:
                print(f"[api_news_content] 專用連線抓取 cnyes 失敗: {cnyes_err}")

        # 4. 其他媒體或常規網站解析
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "close"
        }
        
        paragraphs = []
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")

                # (1) 優先從 JSON-LD 結構化資料中提取文章內文 (支援 CMoney, Yahoo 等現代 SPA/SSR 網站)
                try:
                    import json
                    for s in soup.find_all('script', type=re.compile(r'application/ld\+json', re.I)):
                        if not s.string: continue
                        try:
                            data = json.loads(s.string)
                            items_to_check = data if isinstance(data, list) else [data]
                            for item in items_to_check:
                                if not isinstance(item, dict): continue
                                body = item.get('articleBody') or item.get('text')
                                if body and isinstance(body, str) and len(body.strip()) > 30:
                                    for raw_p in body.splitlines():
                                        p_clean = raw_p.strip()
                                        if len(p_clean) > 10 and not any(kw in p_clean for kw in ["登入", "註冊", "下載APP", "版權所有"]):
                                            paragraphs.append(p_clean)
                                    if paragraphs: break
                            if paragraphs: break
                        except Exception: pass
                except Exception: pass

                # 移除雜項
                for unwanted in soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                    unwanted.decompose()

                if not paragraphs:
                    if "cnyes.com" in url:
                        article = soup.find(id="article-container") or soup.find("article") or soup.find("main") or soup
                        for p in article.find_all("p"):
                            text = p.get_text().strip()
                            if len(text) > 20 and not any(kw in text for kw in ["LINE ID", "Youtube", "按讚訂閱", "https://lin", "本公司所推薦", "未經授權", "翻印必究", "著作權", "莊佳螢分析師"]):
                                if text not in paragraphs:
                                    paragraphs.append(text)
                    elif "ltn.com.tw" in url:
                        content_div = soup.find("div", class_="text") or soup.find("div", class_="whitecon")
                        if content_div:
                            for p in content_div.find_all("p", recursive=False):
                                text = p.get_text().strip()
                                if text and not p.find("span", class_="ph_b") and "點我下載" not in text and "不定期送" not in text:
                                    paragraphs.append(text)
                    elif "udn.com" in url:
                        content_div = soup.find(id="article_body") or soup.find("section", id="article_body") or soup.find("section", class_="article-content__editor")
                        if content_div:
                            for p in content_div.find_all("p"):
                                text = p.get_text().strip()
                                if text and not p.get("class") and "圖／" not in text and "記者" not in text[:5]:
                                    paragraphs.append(text)
                                elif text and "記者" in text[:5] and len(text) > 20:
                                    paragraphs.append(text)
                    elif "ctee.com.tw" in url:
                        content_div = soup.find("div", class_=re.compile(r"main-content|entry-content|article-content", re.I)) or soup.find("article")
                        if content_div:
                            for p in content_div.find_all("p"):
                                text = p.get_text().strip()
                                if len(text) > 15 and not any(kw in text for kw in ["延伸閱讀", "看更多"]):
                                    paragraphs.append(text)
                    elif "cna.com.tw" in url:
                        content_div = soup.find("div", class_="paragraph") or soup.find("div", class_="centralContent")
                        if content_div:
                            for p in content_div.find_all("p"):
                                text = p.get_text().strip()
                                if len(text) > 15:
                                    paragraphs.append(text)
                    elif "chinatimes.com" in url:
                        content_div = soup.find("div", class_="article-body") or soup.find("article")
                        if content_div:
                            for p in content_div.find_all("p"):
                                text = p.get_text().strip()
                                if len(text) > 15:
                                    paragraphs.append(text)
                    elif "cmoney.tw" in url:
                        content_div = soup.find("div", class_=re.compile(r"articleContent|article-content|content|articleBody", re.I)) or soup.find("article")
                        if content_div:
                            for p in content_div.find_all(["p", "div"]):
                                text = p.get_text().strip()
                                if len(text) > 15 and not any(kw in text for kw in ["登入", "註冊", "股市爆料同學會", "下載APP", "立即追蹤"]):
                                    if text not in paragraphs:
                                        paragraphs.append(text)
                    elif "yahoo.com" in url:
                        content_div = soup.find("div", class_="caas-body") or soup.find("article")
                        if content_div:
                            for p in content_div.find_all("p"):
                                text = p.get_text().strip()
                                if len(text) > 10 and not any(kw in text for kw in ["延伸閱讀", "看更多", "Yahoo奇摩股市"]):
                                    paragraphs.append(text)

                if not paragraphs:
                    article = soup.find("article")
                    target = article if article else soup.body
                    if target:
                        for p in target.find_all(["p", "div"]):
                            text = p.get_text().strip()
                            if len(text) > 25 and not any(kw in text for kw in ["登入", "註冊", "隱私權", "版權所有", "客服", "cookies", "廣告", "function()"]):
                                if text not in paragraphs:
                                    paragraphs.append(text)
        except Exception as fetch_err:
            print(f"[api_news_content] 請求 {url} 異常: {fetch_err}")

        # 5. 若成功解析出段落，儲存快取並回傳
        if paragraphs:
            if news_id:
                CNYES_ARTICLE_CACHE[str(news_id)] = paragraphs
            CNYES_ARTICLE_CACHE[url] = paragraphs
            return jsonify({"success": True, "paragraphs": paragraphs})

        # 6. 若皆未擷取到，但快取中有備用摘要段落，優雅降級回傳摘要而非直接跳錯
        if news_id and str(news_id) in CNYES_ARTICLE_CACHE:
            fallback_paras = CNYES_ARTICLE_CACHE[str(news_id)]
            if fallback_paras:
                return jsonify({"success": True, "paragraphs": fallback_paras, "fallback": True})

        return jsonify({"success": False, "error": "暫無法由該新聞來源提取全文，請點擊下方按鈕前往原始網站閱讀。"})
            
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def parse_text_news(text):
    """從 yfinance MCP 回傳的排版文字中解析新聞項目"""
    items = []
    # 根據 "數字. Title:" 進行切割
    raw_patterns = re.split(r'\d+\.\s+Title:', text)
    if len(raw_patterns) <= 1: return []
    
    for raw in raw_patterns[1:]:
        try:
            # 提取標題 (第一行)
            title_match = re.search(r'^(.*)', raw)
            title = title_match.group(1).strip() if title_match else "No Title"
            # 提取連結
            link_match = re.search(r'Link:\s*(https?://\S+)', raw)
            link = link_match.group(1).strip() if link_match else "#"
            # 提取來源與時間
            pub_match = re.search(r'Publisher:\s*(.*)', raw)
            publisher = pub_match.group(1).strip() if pub_match else "Yahoo Finance"
            time_match = re.search(r'Published:\s*(.*)', raw)
            time_val = time_match.group(1).strip() if time_match else ""
            
            items.append({
                "title": title,
                "link": link,
                "publisher": publisher,
                "date_str": time_val
            })
        except: continue
    return items

def parse_table_news(text):
    """解析 yfinance MCP 回傳的表格排版新聞 (title | summary | url | provider)"""
    items = []
    lines = text.splitlines()
    # 找到標題列後面的資料列 (用 | 分隔的列)
    in_data = False
    for line in lines:
        # 跳過分隔線與標題列
        if '|' not in line:
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 3:
            continue
        # 跳過 header 列 (title, summary, url, provider 等標題文字)
        first = parts[0].lower()
        if first in ('title', 'headline', 'name', ''):
            in_data = True
            continue
        # 跳過全是 --- 的分隔列
        if all(c in '-' for c in parts[0].replace(' ', '')):
            continue
        if not in_data:
            in_data = True
        title = parts[0] if parts[0] else "No Title"
        summary = parts[1] if len(parts) > 1 else ""
        url = parts[2] if len(parts) > 2 else "#"
        publisher = parts[3] if len(parts) > 3 else "Yahoo Finance"
        # url 可能被截斷加了 .. 但仍可點擊
        items.append({
            "title": title,
            "link": url if url.startswith("http") else "#",
            "publisher": publisher,
            "date_str": "",
            "summary": summary
        })
    return items

def format_news_to_markdown(raw_news):
    news_items = []
    text_to_parse = None

    # 處理字典格式 (最常見，result 裡面是字串)
    if isinstance(raw_news, dict):
        temp = raw_news.get("result", [])
        if isinstance(temp, str):
            text_to_parse = temp
        elif isinstance(temp, list):
            news_items = temp
        elif isinstance(temp, dict):
            news_items = [temp]
    elif isinstance(raw_news, str):
        text_to_parse = raw_news
    elif isinstance(raw_news, list):
        news_items = raw_news

    # 解析文字格式
    if text_to_parse:
        if "YAHOO FINANCE NEWS" in text_to_parse and "|" in text_to_parse:
            # 表格格式
            news_items = parse_table_news(text_to_parse)
        elif "YAHOO FINANCE NEWS" in text_to_parse:
            # 舊版 "1. Title:" 格式
            news_items = parse_text_news(text_to_parse)
        else:
            # 嘗試 JSON
            try:
                import json
                parsed = json.loads(text_to_parse)
                if isinstance(parsed, list):
                    news_items = parsed
                elif isinstance(parsed, dict):
                    news_items = [parsed]
            except:
                pass

    parts = []
    for item in news_items:
        if not isinstance(item, dict): continue
        title = item.get("title", "No title")
        link = item.get("link", "#")
        publisher = item.get("publisher", "Yahoo Finance")
        summary = item.get("summary", "")
        
        # 處理日期
        t_val = item.get("providerPublishTime", 0)
        date_str = item.get("date_str", "")
        if not date_str and t_val:
            try:
                date_str = time.strftime('%m/%d %H:%M', time.localtime(t_val))
            except:
                date_str = str(t_val)
        
        safe_title = str(title).encode("utf-8", "ignore").decode("utf-8")
        safe_summary = str(summary).encode("utf-8", "ignore").decode("utf-8")
        desc = f"\n{safe_summary}" if safe_summary else ""
        parts.append(f"### [{safe_title}]({link})\n*Source: {publisher} - {date_str}*{desc}\n\n---\n\n")
    
    return "".join(parts) if parts else "目前沒有相關新聞。"

@app.route('/api/stock_news/<symbol>')
def api_stock_news(symbol):
    try:
        code = symbol.replace('.TW', '').replace('.TWO', '')
        domestic_news = fetch_domestic_news_for_stock(code)
        
        parts = []
        for item in domestic_news:
            title = item['title']
            link = item['link']
            publisher = item['publisher']
            date_str = item['time']
            
            safe_title = str(title).encode("utf-8", "ignore").decode("utf-8")
            parts.append(f"### [{safe_title}]({link})\n*Source: {publisher} - {date_str}*\n\n---\n\n")
            
        markdown = "".join(parts) if parts else "目前沒有相關新聞。"
        return jsonify({"success": True, "markdown": markdown})
    except Exception as e:
        print(f"Error fetching news for {symbol}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
# ===================================================================
# 以下為整合自 icandoit 的新功能 API
# ===================================================================

def _normalize_symbol(symbol: str) -> str:
    """將台股代號（包含數字及英文結尾，如00403A）轉為 Yahoo Finance 格式，其他保持原樣"""
    s = symbol.strip().upper()
    if s.endswith('.TW') or s.endswith('.TWO'):
        return s
    
    is_tw_stock = False
    if s.isdigit() and len(s) >= 4:
        is_tw_stock = True
    elif len(s) >= 4 and len(s) <= 6 and s[0].isdigit():
        is_tw_stock = True
        
    if is_tw_stock:
        return f"{s}.TW"
    return s


@app.route('/api/search', methods=['POST'])
def api_search_stocks():
    """搜尋股票（台股 + 美股），支援代碼或公司名稱"""
    try:
        data = request.get_json() or {}
        query = data.get('query', '').strip()
        if not query:
            return jsonify({'error': '搜尋關鍵詞不能為空'}), 400

        results = []

        # 1. 嘗試 yfinance search
        try:
            search_result = yf.Search(query, max_results=10)
            quotes = getattr(search_result, 'quotes', []) or []
            for q in quotes[:10]:
                if isinstance(q, dict):
                    results.append({
                        'symbol': q.get('symbol', ''),
                        'shortname': q.get('shortname') or q.get('longname') or q.get('symbol', ''),
                        'longname': q.get('longname', ''),
                        'exchange': q.get('exchange', ''),
                        'quoteType': q.get('quoteType', ''),
                    })
        except Exception:
            pass

        # 2. 如果是純數字，補上台股格式
        if not results and query.isdigit():
            results.append({
                'symbol': f'{query}.TW',
                'shortname': query,
                'longname': query,
                'exchange': 'TWSE',
                'quoteType': 'EQUITY',
            })

        return jsonify({'results': results})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'搜尋失敗: {str(e)}'}), 500

@app.route('/api/stocks/batch-yields', methods=['GET'])
def api_batch_yields():
    """
    批次取得多支股票的殖利率（主要用於 ETF 列表頁的漸進式載入）。
    參數: codes=0050,0056,00878,... (逗號分隔，最多 30 支)
    回傳: { "0050": 1.24, "0056": 6.71, ... }
    """
    codes_param = request.args.get('codes', '').strip()
    if not codes_param:
        return jsonify({})
    
    codes = [c.strip() for c in codes_param.split(',') if c.strip()][:30]
    result = {}
    
    # 先從已有的 ticker_info 快取中讀取
    missing = []
    for code in codes:
        cached = data_cache.get(f"ticker_info_{code}.TW") or data_cache.get(f"ticker_info_{code}")
        if cached and 'dividends' in cached:
            dy = cached['dividends'].get('dividendYield')
            if dy:
                result[code] = dy
                continue
        missing.append(code)
    
    # 對沒有快取的，用 yfinance 批次查詢基本 dividendYield (改為並行查詢以大幅提速)
    if missing:
        import yfinance as yf
        import concurrent.futures

        def _fetch_single_yield(code):
            dy = None
            # 優先嘗試上市 (.TW)
            try:
                symbol = f"{code}.TW"
                ticker = yf.Ticker(symbol)
                info = ticker.fast_info
                if hasattr(info, 'dividend_yield') and info.dividend_yield:
                    dy = round(info.dividend_yield * 100, 2)
            except Exception:
                pass
            
            # 若查不到，嘗試上櫃 (.TWO)
            if not dy:
                try:
                    symbol_otc = f"{code}.TWO"
                    ticker_otc = yf.Ticker(symbol_otc)
                    info_otc = ticker_otc.fast_info
                    if hasattr(info_otc, 'dividend_yield') and info_otc.dividend_yield:
                        dy = round(info_otc.dividend_yield * 100, 2)
                    else:
                        full_info = ticker_otc.info or {}
                        dy_val = full_info.get('dividendYield', 0)
                        if dy_val:
                            dy = round(float(dy_val) * 100 if float(dy_val) < 1 else float(dy_val), 2)
                except Exception:
                    pass
            
            # 嘗試完整 info Fallback (.TW)
            if not dy:
                try:
                    full_info = ticker.info or {}
                    dy_val = full_info.get('dividendYield', 0)
                    if dy_val:
                        dy = round(float(dy_val) * 100 if float(dy_val) < 1 else float(dy_val), 2)
                except Exception:
                    pass
                    
            return code, dy

        try:
            # 限制最多 10 個並行 workers
            max_workers = min(10, len(missing))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_code = {executor.submit(_fetch_single_yield, code): code for code in missing}
                for future in concurrent.futures.as_completed(future_to_code, timeout=6):
                    code_res = future_to_code[future]
                    try:
                        _, dy = future.result()
                        if dy:
                            result[code_res] = dy
                            # 將結果塞入獨立殖利率快取，避免污染完整 ticker_info 快取
                            data_cache.set(f"yield_val_{code_res}", dy, ttl=86400)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[batch-yields] 並行批次查詢失敗: {e}")
    
    return jsonify(result)


@app.route('/api/stocks/batch-quotes', methods=['GET'])
def api_batch_realtime_quotes():
    """
    批次取得多支股票的盤中即時最新報價（方案 A：當前頁面即時連動）。
    參數: codes=2330,2317,0050,... (逗號分隔，最多 30 支)
    回傳: { "2330": { "price": 1050, "change": 15, "changePercent": 1.45, "open": 1040, "high": 1055, "low": 1035, "volume": 32000, "prevClose": 1035 }, ... }
    """
    codes_param = request.args.get('codes', '').strip()
    if not codes_param:
        return jsonify({})

    codes = [c.strip() for c in codes_param.split(',') if c.strip()][:30]
    result = {}
    import concurrent.futures
    import yfinance as yf

    def _fetch_single_quote(code):
        quote_data = None
        # 1. 優先嘗試 TWSE/TPEX 即時快照或 yfinance fast_info
        for symbol in [f"{code}.TW", f"{code}.TWO"]:
            try:
                t = yf.Ticker(symbol)
                fi = t.fast_info
                last_price = getattr(fi, 'last_price', None)
                prev_close = getattr(fi, 'previous_close', None)
                if last_price is not None and last_price > 0:
                    open_p = getattr(fi, 'open', None) or last_price
                    high_p = getattr(fi, 'day_high', None) or last_price
                    low_p = getattr(fi, 'day_low', None) or last_price
                    vol = getattr(fi, 'last_volume', None) or 0
                    
                    # 計算漲跌
                    change = 0.0
                    change_pct = 0.0
                    if prev_close and prev_close > 0:
                        change = round(last_price - prev_close, 2)
                        change_pct = round((change / prev_close) * 100, 2)
                    
                    quote_data = {
                        "price": round(float(last_price), 2),
                        "change": change,
                        "changePercent": change_pct,
                        "open": round(float(open_p), 2) if open_p else None,
                        "high": round(float(high_p), 2) if high_p else None,
                        "low": round(float(low_p), 2) if low_p else None,
                        "volume": int(vol),
                        "prevClose": round(float(prev_close), 2) if prev_close else None,
                        "buy": round(float(last_price) - 0.05, 2),
                        "sell": round(float(last_price) + 0.05, 2)
                    }
                    break
            except Exception:
                continue
        return code, quote_data

    try:
        max_workers = min(12, len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_code = {executor.submit(_fetch_single_quote, code): code for code in codes}
            for future in concurrent.futures.as_completed(future_to_code, timeout=6):
                code_res = future_to_code[future]
                try:
                    _, qdata = future.result()
                    if qdata:
                        result[code_res] = qdata
                except Exception:
                    pass
    except Exception as e:
        print(f"[batch-quotes] 即時報價抓取失敗: {e}")

    return jsonify(result)



@app.route('/api/stocks', methods=['GET'])

def api_get_stocks():
    """取得台股清單，支援分頁、分類與搜尋，並結合資料庫最新收盤價，載入極快"""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        category = request.args.get('category', 'all').strip()
        search_query = request.args.get('search', '').strip().lower()
        types = request.args.get('types', '').strip()
        industries = request.args.get('industries', '').strip()
        risks = request.args.get('risks', '').strip()

        sort_by = request.args.get('sort_by', '').strip()
        sort_dir = request.args.get('sort_dir', 'asc').strip().lower()

        # 特殊內建分類代碼對應
        CATEGORY_CODES = {
            'hot':   ['2330','2317','2454','2881','2882'],
            'yield': ['00940','0056','00878','00919','5880'],
            'semi':  ['2330','2454','2303','2308','3711'],
            'fin':   ['2881','2882','2886','2891','5880'],
            'etf':   ['0050','0056','00878','00940','00919'],
        }

        from db_service import SessionLocal, get_latest_prices_and_changes, get_watchlist
        from models import StockInfo, DailyPrice
        from sqlalchemy import or_, func, case

        with SessionLocal() as db:
            query = db.query(StockInfo)
            hot_ordered_codes = []

            # 1. 根據自訂類別篩選 (個股, ETF, 自選股) 或 特殊分類進行查詢
            if types:
                type_list = [t.strip() for t in types.split(',')]
                type_filters = []
                
                if 'etf' in type_list:
                    # 篩選 ETF (代號為 00 開頭)
                    type_filters.append(StockInfo.code.like('00%'))
                
                if 'watchlist' in type_list:
                    # 篩選自選股 (目前使用者的觀察清單)
                    user_id = session.get('user_id')
                    if user_id:
                        watchlist_codes = get_watchlist(user_id)
                        type_filters.append(StockInfo.code.in_(watchlist_codes))
                    else:
                        # 未登入狀態自選股回傳空
                        type_filters.append(StockInfo.code == '___NONE___')
                
                if 'equity' in type_list:
                    # 篩選個股 (代號非 00 開頭)
                    type_filters.append(StockInfo.code.not_like('00%'))
                
                if type_filters:
                    query = query.filter(or_(*type_filters))
            elif category == 'hot':
                # 動態熱門股：抓取最新交易日成交量 (Volume) 前 50 大活躍標的
                latest_date = db.query(func.max(DailyPrice.date)).scalar()
                if latest_date:
                    hot_rows = db.query(DailyPrice.code).filter(
                        DailyPrice.date == latest_date,
                        DailyPrice.volume.isnot(None)
                    ).order_by(DailyPrice.volume.desc()).limit(50).all()
                    hot_ordered_codes = [r[0] for r in hot_rows]
                    query = query.filter(StockInfo.code.in_(hot_ordered_codes))
                else:
                    fallback_hot = ['2330','2317','2454','2881','2882','2603','2609','2382','3231','2409']
                    hot_ordered_codes = fallback_hot
                    query = query.filter(StockInfo.code.in_(fallback_hot))
            elif category in CATEGORY_CODES:
                query = query.filter(StockInfo.code.in_(CATEGORY_CODES[category]))
            elif category == 'etf_all':
                # 篩選所有 ETF (代號為 00 開頭)
                query = query.filter(StockInfo.code.like('00%'))
            elif category != 'all' and category:
                # 產業分類篩選 (支援模糊匹配如 "半導體" 與 "半導體業")
                cat_clean = category.rstrip('業')
                query = query.filter(
                    or_(
                        StockInfo.industry == category,
                        StockInfo.industry.like(f"%{category}%"),
                        StockInfo.industry.like(f"%{cat_clean}%")
                    )
                )

            # 2. 根據自訂產業板塊進行查詢
            if industries:
                ind_list = [i.strip() for i in industries.split(',')]
                ind_filters = []
                
                INDUSTRY_CODES = {
                    'semi': ['2330','2454','2303','2308','3711','2344','2408','3034','2379','3231'],
                    'fin': ['2881','2882','2886','2891','5880','2884','2892','2885','2880','2883'],
                    'biotech': ['1789','1760','4147','1795','4164','6472','4108','4128'],
                    'auto': ['2201','2207','2204','1319','1522','1536','2231','2239'],
                    'retail': ['2912','2903','5904','5903','8454','2915'],
                    'other': ['1101','2002','2603','2609','2615','2610','2618','1301','1303']
                }
                
                INDUSTRY_NAME_MAP = {
                    'semi': '半導體業',
                    'fin': '金融保險',
                    'biotech': '生技醫療',
                    'auto': '汽車工業',
                    'retail': '貿易百貨'
                }
                
                for ind in ind_list:
                    target_name = INDUSTRY_NAME_MAP.get(ind, ind)
                    clean_name = target_name.rstrip('業')
                    if ind in INDUSTRY_CODES:
                        ind_filters.append(StockInfo.code.in_(INDUSTRY_CODES[ind]))
                    else:
                        ind_filters.append(
                            or_(
                                StockInfo.industry == target_name,
                                StockInfo.industry.like(f"%{target_name}%"),
                                StockInfo.industry.like(f"%{clean_name}%")
                            )
                        )
                
                if ind_filters:
                    query = query.filter(or_(*ind_filters))

            # 3. 根據自訂風險屬性板塊進行查詢
            if risks:
                risk_list = [r.strip() for r in risks.split(',')]
                risk_filters = []
                
                # 風險屬性板塊概念對照
                RISK_CODES = {
                    'high': ['2330', '2454', '2317', '2303', '3711', '2344', '2408', '3034', '2379', '3231', '1789', '1760', '4147', '1795', '2201', '2207', '2239', '3008', '2603', '2609'], # Tech/Biotech/Shipping
                    'mid': ['2881', '2882', '2886', '2891', '2884', '2892', '2885', '2880', '2883', '5880', '2912', '2903', '5904', '5903', '8454', '1101', '2002', '1301', '1303', '1402'], # Financial/Retail/Traditional
                    'low': ['2412', '4904', '3045', '9904', '9921', '9945'] # Telecoms/Stable Stocks
                }
                
                for r_val in risk_list:
                    if r_val in RISK_CODES:
                        risk_filters.append(StockInfo.code.in_(RISK_CODES[r_val]))
                    if r_val == 'low':
                        # 保守型包含所有 ETF (代號為 00 開頭)
                        risk_filters.append(StockInfo.code.like('00%'))
                
                if risk_filters:
                    query = query.filter(or_(*risk_filters))

            # 2. 搜尋字串篩選
            if search_query:
                query = query.filter(
                    or_(
                        StockInfo.code.like(f"%{search_query}%"),
                        StockInfo.name.ilike(f"%{search_query}%")
                    )
                )

            total_count = query.count()

            # 3. 排序處理
            if category == 'hot' and hot_ordered_codes:
                # 熱門台股預設保持成交量由大到小的順序
                order_clauses = [case({c: i for i, c in enumerate(hot_ordered_codes)}, value=StockInfo.code)]
                query = query.order_by(*order_clauses)
            elif sort_by == 'name':
                query = query.order_by(StockInfo.name.desc() if sort_dir == 'desc' else StockInfo.name.asc())
            elif sort_by == 'code':
                query = query.order_by(StockInfo.code.desc() if sort_dir == 'desc' else StockInfo.code.asc())
            else:
                query = query.order_by(StockInfo.code.asc())

            # 4. 分頁
            offset = (page - 1) * limit
            rows = query.offset(offset).limit(limit).all()

            # 整理回傳清單
            stock_list = []
            for r in rows:
                stock_list.append({
                    'code': r.code,
                    'name': r.name,
                    'market': r.market or 'N/A',
                    'industry': r.industry or 'N/A',
                })

            # 批次查詢收盤價與漲跌幅
            codes = [s['code'] for s in stock_list]
            prices_dict = get_latest_prices_and_changes(codes)

            # 批次取得 TWSE 上市股票殖利率（快取 8 小時）
            try:
                twse_yields = get_twse_batch_dividend_yields()
            except Exception:
                twse_yields = {}

            # 結合價格資料與殖利率
            for s in stock_list:
                p_data = prices_dict.get(s['code'], {})
                s['price'] = p_data.get('price')
                s['change'] = p_data.get('change', 0.0)
                s['changePercent'] = p_data.get('changePercent', 0.0)
                s['open'] = p_data.get('open')
                s['high'] = p_data.get('high')
                s['low'] = p_data.get('low')
                s['volume'] = p_data.get('volume', 0)
                s['prevClose'] = p_data.get('prevClose')
                s['buy'] = p_data.get('buy')
                s['sell'] = p_data.get('sell')

                # 殖利率優先順序：
                # 1. TWSE 批次數據（上市個股，即時準確）
                # 2. yfinance ticker_info 快取（ETF、曾查詢過的股票）
                # 3. 顯示 '—'
                code = s['code']
                if code in twse_yields:
                    s['dividendYield'] = twse_yields[code]
                else:
                    cached_ticker = data_cache.get(f"ticker_info_{code}.TW") or data_cache.get(f"ticker_info_{code}")
                    if cached_ticker and 'dividends' in cached_ticker:
                        dy = cached_ticker['dividends'].get('dividendYield')
                        s['dividendYield'] = dy if dy else '—'
                    else:
                        s['dividendYield'] = '—'

            # 計算總頁數
            total_pages = max(1, int(math.ceil(total_count / limit)))

            return jsonify({
                'stocks': stock_list,
                'page': page,
                'limit': limit,
                'total_count': total_count,
                'total_pages': total_pages
            })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'獲取台股列表失敗: {str(e)}'}), 500


def get_twse_batch_dividend_yields() -> dict:
    """
    從 TWSE 與 TPEx OpenAPI 批次取得所有上市及上櫃股票的即時殖利率。
    回傳 dict: { code: dividend_yield_float }
    結果快取 8 小時，避免重複請求。
    """
    import requests
    CACHE_KEY = 'twse_batch_dividend_yields'
    cached = data_cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    yields = {}
    
    # 1. 抓取上市股票殖利率
    try:
        resp = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL',
            timeout=10
        )
        if resp.status_code == 200:
            for item in resp.json():
                code = item.get('Code', '').strip()
                dy_str = item.get('DividendYield', '').strip()
                if code and dy_str:
                    try:
                        dy = float(dy_str)
                        if dy > 0:
                            yields[code] = dy
                    except ValueError:
                        pass
        print(f"[TWSE] 成功載入 {len(yields)} 筆上市股票殖利率")
    except Exception as e:
        print(f"[TWSE] 批次殖利率載入失敗: {e}")

    # 2. 抓取上櫃股票殖利率
    try:
        resp_tpex = requests.get(
            'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis',
            timeout=10
        )
        if resp_tpex.status_code == 200:
            tpex_count = 0
            for item in resp_tpex.json():
                code = item.get('SecuritiesCompanyCode', '').strip()
                dy_str = item.get('YieldRatio', '').strip()
                if code and dy_str:
                    try:
                        dy = float(dy_str)
                        if dy > 0:
                            yields[code] = dy
                            tpex_count += 1
                    except ValueError:
                        pass
            print(f"[TPEx] 成功載入 {tpex_count} 筆上櫃股票殖利率")
    except Exception as e:
        print(f"[TPEx] 批次殖利率載入失敗: {e}")

    # 快取 8 小時（28800 秒）
    data_cache.set(CACHE_KEY, yields, ttl=28800)
    return yields


def get_yahoo_taiwan_dividend_history(symbol: str) -> list:
    """從 Yahoo 奇摩股市爬取台股或 ETF 的歷史除權息紀錄"""
    import requests
    from bs4 import BeautifulSoup
    import re
    
    symbol = symbol.strip().upper()
    url = f"https://tw.stock.yahoo.com/quote/{symbol}/dividend"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    history = []
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code != 200:
            return []
            
        soup = BeautifulSoup(resp.content, "html.parser")
        div_table = soup.find("div", class_="table-body-wrapper")
        if not div_table:
            return []
            
        for li in div_table.find_all("li"):
            divs = li.find_all("div")
            cells = [c.get_text().strip() for c in divs]
            if len(cells) >= 9:
                amount_str = cells[4]
                ex_date_str = cells[8]
                
                # 驗證除息日格式是否為 YYYY/MM/DD
                if re.match(r'^\d{4}/\d{2}/\d{2}$', ex_date_str):
                    try:
                        amount = float(amount_str.replace(',', ''))
                        date_formatted = ex_date_str.replace('/', '-')
                        if amount > 0:
                            history.append({
                                'date': date_formatted,
                                'amount': round(amount, 4)
                            })
                    except ValueError:
                        continue
        return history
    except Exception as e:
        print(f"[{symbol}] 擷取奇摩股市除息紀錄失敗: {e}")
        return []


@app.route('/api/ticker/<symbol>', methods=['GET'])
def api_get_ticker_info(symbol):
    """取得股票完整資訊（台股 + 美股）"""
    try:
        symbol = _normalize_symbol(symbol)
        cache_key = f'ticker_info_{symbol}'
        cached = data_cache.get(cache_key)
        if cached and isinstance(cached, dict) and cached.get('price'):
            return jsonify(cached)

        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception as info_err:
            print(f"[{symbol}] yfinance ticker.info 呼叫失敗 ({type(info_err).__name__}): {info_err}")

        # 價格與基本資訊獲取（加入歷史最後一日作為 Fallback 備用方案，解決有些台股主動型 ETF 資訊不全的問題）
        current_price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        open_price = info.get('open') or info.get('regularMarketOpen') or 0
        day_high = info.get('dayHigh') or 0
        day_low = info.get('dayLow') or 0
        prev_close = info.get('previousClose') or 0

        if current_price == 0:
            try:
                hist = ticker.history(period='5d')
                if not hist.empty:
                    last_row = hist.iloc[-1]
                    current_price = float(last_row['Close'])
                    open_price = float(last_row['Open'])
                    day_high = float(last_row['High'])
                    day_low = float(last_row['Low'])
                    if len(hist) > 1:
                        prev_close = float(hist.iloc[-2]['Close'])
                    else:
                        prev_close = float(last_row['Open'])
                    print(f"[{symbol}] yfinance info 價格為 0，成功使用歷史最後一日收盤價 {current_price} 作為替代")
            except Exception as hist_err:
                print(f"Fallback history fetch failed for {symbol}: {hist_err}")

        # 如果 yfinance 因限流或無資料導致價格仍為 0，嘗試自本地資料庫讀取最新價格與均價
        pure_code_tmp = symbol.replace('.TW', '').replace('.TWO', '')
        if current_price == 0:
            try:
                from db_service import SessionLocal
                from models import DailyPrice
                with SessionLocal() as db:
                    db_prices = db.query(DailyPrice).filter(DailyPrice.code == pure_code_tmp).order_by(DailyPrice.date.desc()).limit(10).all()
                    if db_prices:
                        p_latest = db_prices[0]
                        current_price = float(p_latest.close) if p_latest.close else 0
                        open_price = float(p_latest.open) if p_latest.open else current_price
                        day_high = float(p_latest.high) if p_latest.high else current_price
                        day_low = float(p_latest.low) if p_latest.low else current_price
                        if len(db_prices) > 1 and db_prices[1].close:
                            prev_close = float(db_prices[1].close)
                        else:
                            prev_close = current_price
                        if not info.get('volume') and p_latest.volume:
                            info['volume'] = int(p_latest.volume)
                        print(f"[{symbol}] yfinance 失敗，成功由本地資料庫備援載入最新收盤價: {current_price}")
            except Exception as db_err:
                print(f"[{symbol}] 本地資料庫備援價格查詢失敗: {db_err}")

        # 判斷是否為台股與 ETF
        is_tw = symbol.endswith('.TW') or symbol.endswith('.TWO')
        pure_code = symbol.replace('.TW', '').replace('.TWO', '')
        
        # 優先自資料庫取得標準中文名稱與產業
        db_name = None
        db_market = None
        db_industry = None
        try:
            from db_service import get_stock_name
            db_name = get_stock_name(pure_code)
        except Exception:
            pass

        quote_type = (info.get('quoteType') or '').upper()
        is_etf = quote_type == 'ETF' or pure_code.startswith('00') or (db_name and 'ETF' in db_name)

        # 解析除息日期 (yfinance 回傳的可能是 Unix 時間戳記，或是 datetime/字串)
        ex_dividend_date = info.get('exDividendDate')
        ex_date_str = 'N/A'
        if ex_dividend_date is not None and ex_dividend_date != 'N/A':
            try:
                from datetime import datetime
                if isinstance(ex_dividend_date, (int, float)):
                    ex_date_str = datetime.fromtimestamp(ex_dividend_date).strftime('%Y-%m-%d')
                elif hasattr(ex_dividend_date, 'strftime'):  # 如果是 datetime 物件
                    ex_date_str = ex_dividend_date.strftime('%Y-%m-%d')
                else:
                    try:
                        ex_date_str = datetime.fromtimestamp(float(ex_dividend_date)).strftime('%Y-%m-%d')
                    except (ValueError, TypeError):
                        ex_date_str = str(ex_dividend_date)
            except Exception as e:
                print(f"[{symbol}] 解析除息日失敗: {e}")
                ex_date_str = str(ex_dividend_date)

        # 獲取歷史除息紀錄
        dividend_history = []
        if is_tw:
            dividend_history = get_yahoo_taiwan_dividend_history(symbol)

        # 如果台股爬蟲沒抓到，或者不是台股，則使用 yfinance 備用
        if not dividend_history:
            try:
                divs = ticker.dividends
                if not divs.empty:
                    divs = divs.sort_index(ascending=False)
                    for date_idx, val in divs.items():
                        if hasattr(date_idx, 'strftime'):
                            date_str = date_idx.strftime('%Y-%m-%d')
                        else:
                            date_str = str(date_idx)
                        val_float = float(val)
                        if val_float > 0:
                            dividend_history.append({
                                'date': date_str,
                                'amount': round(val_float, 4)
                            })
            except Exception as div_err:
                print(f"[{symbol}] 獲取歷史除息紀錄失敗: {div_err}")

        # 估算與補全股利資訊 (如果爬蟲抓到了紀錄)
        dividend_rate = info.get('dividendRate') or 0
        dividend_yield = round(float(info.get('dividendYield') or info.get('yield') or 0), 2)
        if 0 < dividend_yield < 1:  # 若小數形式 (如 0.0157 轉成 1.57)
            dividend_yield = round(dividend_yield * 100, 2)

        if is_tw and dividend_history:
            if ex_date_str == 'N/A' or not ex_date_str:
                ex_date_str = dividend_history[0]['date']
            
            if (dividend_rate == 0 or dividend_rate is None) and current_price > 0:
                from datetime import datetime, timedelta
                try:
                    latest_date = datetime.strptime(dividend_history[0]['date'], '%Y-%m-%d')
                    one_year_ago = latest_date - timedelta(days=365)
                    
                    annual_sum = 0.0
                    for rec in dividend_history:
                        rec_date = datetime.strptime(rec['date'], '%Y-%m-%d')
                        if rec_date > one_year_ago and rec_date <= latest_date:
                            annual_sum += rec['amount']
                    
                    dividend_rate = round(annual_sum, 4)
                    dividend_yield = round((dividend_rate / current_price) * 100, 2)
                except Exception as calc_err:
                    print(f"[{symbol}] 計算年化配息率失敗: {calc_err}")

        # 補全 50日 / 200日 均線（若 info 缺失則從歷史計算）
        ma50 = info.get('fiftyDayAverage')
        ma200 = info.get('twoHundredDayAverage')
        if (not ma50 or not ma200) and is_tw:
            try:
                hist_df = ticker.history(period='1y')
                if not hist_df.empty and 'Close' in hist_df.columns:
                    c_series = hist_df['Close'].dropna()
                    if not ma50 and len(c_series) >= 20:
                        ma50 = float(c_series.iloc[-min(50, len(c_series)):].mean())
                    if not ma200 and len(c_series) >= 50:
                        ma200 = float(c_series.iloc[-min(200, len(c_series)):].mean())
            except Exception as ma_err:
                print(f"[{symbol}] yfinance 均線計算失敗: {ma_err}")

            # 若 yfinance 仍無法計算均線，使用本地資料庫
            if not ma50 or not ma200:
                try:
                    from db_service import SessionLocal
                    from models import DailyPrice
                    with SessionLocal() as db:
                        dp_rows = db.query(DailyPrice.close).filter(
                            DailyPrice.code == pure_code,
                            DailyPrice.close.isnot(None)
                        ).order_by(DailyPrice.date.desc()).limit(200).all()
                        closes_db = [float(r[0]) for r in dp_rows]
                        if closes_db:
                            if not ma50:
                                ma50 = float(sum(closes_db[:50]) / min(50, len(closes_db)))
                            if not ma200:
                                ma200 = float(sum(closes_db[:200]) / min(200, len(closes_db)))
                except Exception as db_ma_err:
                    print(f"[{symbol}] 本地資料庫均線計算失敗: {db_ma_err}")

        # 市值 / 基金規模補全
        market_cap = info.get('marketCap') or info.get('totalAssets') or info.get('netAssets') or 0

        # Beta 係數補全
        beta_val = info.get('beta') or info.get('beta3Year')

        # 名稱與產業標籤整合
        comp_name = db_name or info.get('longName') or info.get('shortName') or pure_code
        industry = info.get('industry')
        sector = info.get('sector')
        if is_etf:
            if not industry or industry == 'N/A':
                industry = '指數股票型基金 (ETF)'
            if not sector or sector == 'N/A':
                sector = 'ETF / 共同基金'
        else:
            if not industry: industry = 'N/A'
            if not sector: sector = 'N/A'

        # 官方網站與簡介補全
        website = info.get('website') or 'N/A'
        description = info.get('longBusinessSummary')
        if not description or description == 'N/A':
            if is_etf:
                fund_fam = info.get('fundFamily') or ''
                fam_txt = f'，發行或經理機構為 {fund_fam}' if fund_fam else ''
                nav_txt = f'，最新淨值參考約 {info.get("navPrice")} 元' if info.get("navPrice") else ''
                description = f'{comp_name}（代號：{pure_code}）為台灣掛牌之指數股票型證券投資信託基金 (ETF){fam_txt}{nav_txt}。主要透過追蹤特定指數績效或主動式投資策略，提供投資人參與市場趨勢與資產配置工具。'
            else:
                description = f'{comp_name}（代號：{pure_code}）為台灣上市公司，提供相關產業之核心產品與服務。'

        result = {
            'symbol': info.get('symbol', symbol),
            'company': {
                'name': comp_name,
                'sector': sector,
                'industry': industry,
                'website': website,
                'description': description[:400],
            },
            'price': {
                'current': round(current_price, 4),
                'open': round(open_price, 4),
                'dayHigh': round(day_high, 4),
                'dayLow': round(day_low, 4),
                'previousClose': round(prev_close, 4),
                'fiftyTwoWeekHigh': round(info.get('fiftyTwoWeekHigh') or 0, 4),
                'fiftyTwoWeekLow': round(info.get('fiftyTwoWeekLow') or 0, 4),
            },
            'valuation': {
                'marketCap': market_cap,
                'trailingPE': round(info.get('trailingPE', 0), 2) if info.get('trailingPE') else 'N/A',
                'forwardPE': round(info.get('forwardPE', 0), 2) if info.get('forwardPE') else 'N/A',
                'priceToBook': round(info.get('priceToBook', 0), 2) if info.get('priceToBook') else 'N/A',
                'pegRatio': round(info.get('pegRatio', 0), 2) if info.get('pegRatio') else 'N/A',
            },
            'trading': {
                'volume': info.get('volume', 0),
                'averageVolume': info.get('averageVolume', 0),
                'bid': round(info.get('bid', 0), 4) if info.get('bid') else 0,
                'ask': round(info.get('ask', 0), 4) if info.get('ask') else 0,
            },
            'dividends': {
                'dividendRate': dividend_rate,
                'dividendYield': round(dividend_yield, 2),
                'exDividendDate': ex_date_str,
                'payoutRatio': round((info.get('payoutRatio') or 0) * 100, 2),
                'history': dividend_history,
            },
            'performance': {
                'beta': round(beta_val, 2) if beta_val is not None and beta_val != 'N/A' else 'N/A',
                'fiftyDayAverage': round(ma50, 4) if ma50 and ma50 != 'N/A' else 'N/A',
                'twoHundredDayAverage': round(ma200, 4) if ma200 and ma200 != 'N/A' else 'N/A',
            },
        }
        data_cache.set(cache_key, result, ttl=300)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'獲取股票資訊失敗: {str(e)}'}), 500


@app.route('/api/ticker/<symbol>/news', methods=['GET'])
def api_get_ticker_news(symbol):
    """取得個股新聞"""
    try:
        code = symbol.replace('.TW', '').replace('.TWO', '')
        domestic_news = fetch_domestic_news_for_stock(code)
        
        news_list = []
        for item in domestic_news:
            news_list.append({
                'title': item['title'],
                'publisher': item['publisher'],
                'link': item['link'],
                'publish_time': item['time'],
                'cover_img': item.get('cover_img', ''),
                'paragraphs': item.get('paragraphs', [])
            })
        return jsonify({'news': news_list})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'獲取新聞失敗: {str(e)}'}), 500
@app.route('/api/ticker/<symbol>/history', methods=['GET'])
def api_get_ticker_history(symbol):
    """取得歷史 OHLCV 資料（直接 yfinance，支援台股+美股）"""
    try:
        symbol = _normalize_symbol(symbol)
        period = request.args.get('period', '1y')
        interval = request.args.get('interval', '1d')

        # 根據 yfinance 官方規定動態適配各分鐘週期的最大歷史長度：
        # 1m 最大 7d；5m/15m/30m 最大 60d (可取 1mo)；60m/1h 最大 730d (可取 3mo/6mo)
        if interval == '1m':
            if period not in ['1d', '5d', '7d']:
                period = '5d'
        elif interval in ['2m', '5m', '15m', '30m', '90m']:
            if period not in ['1d', '5d', '7d', '1mo', '60d']:
                period = '1mo'
        elif interval in ['60m', '1h']:
            if period not in ['1d', '5d', '1mo', '3mo', '6mo', '1y', '2y']:
                period = '3mo'

        # 如果是日線、週線、月線、年線，優先從本地資料庫讀取並轉換/重採樣 (避免 yfinance Rate Limit 且載入迅速)
        hist = pd.DataFrame()
        if interval in ['1d', '1wk', '1mo', '1y', '1Y', 'year']:
            try:
                from db_service import get_price_history
                pure_symbol = symbol.replace('.TW', '').replace('.TWO', '')
                db_data = get_price_history(pure_symbol)
                
                if db_data and len(db_data.get('dates', [])) > 0:
                    db_df = pd.DataFrame(db_data)
                    db_df['Date'] = pd.to_datetime(db_df['dates'])
                    db_df.set_index('Date', inplace=True)
                    db_df.rename(columns={'open':'Open', 'high':'High', 'low':'Low', 'close':'Close', 'volume':'Volume'}, inplace=True)
                    db_df.sort_index(inplace=True)
                    
                    if interval == '1d':
                        hist = db_df
                    elif interval == '1wk':
                        hist = db_df.resample('W').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                    elif interval == '1mo':
                        try:
                            hist = db_df.resample('ME').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                        except Exception:
                            hist = db_df.resample('M').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                    elif interval in ['1y', '1Y', 'year']:
                        try:
                            hist = db_df.resample('YE').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                        except Exception:
                            hist = db_df.resample('Y').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                    print(f"[{symbol}] 從資料庫讀取/重採樣 {len(hist)} 筆歷史資料 (週期: {interval})")
            except Exception as e:
                print(f"資料庫讀取或重採樣失敗: {e}")
                
        # 如果資料庫沒有資料，或是分時週期，則使用 yfinance
        if hist.empty:
            ticker = yf.Ticker(symbol)
            if interval in ['1y', '1Y', 'year']:
                # yfinance 不支援 interval='1y'，抓取月K後重採樣為年度
                raw_monthly = ticker.history(period='max', interval='1mo')
                if not raw_monthly.empty:
                    try:
                        hist = raw_monthly.resample('YE').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
                    except Exception:
                        hist = raw_monthly.resample('Y').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
            else:
                fetch_period = period
                if interval == '1d' and period in ['1y', '6mo']:
                    fetch_period = '2y'
                hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty:
            return jsonify({'error': f'無法獲取 {symbol} 的歷史數據'}), 404

        # Remove timezone and duplicate dates
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)
        hist = hist[~hist.index.duplicated(keep='last')]

        hist['MA5'] = hist['Close'].rolling(window=5).mean()
        hist['MA10'] = hist['Close'].rolling(window=10).mean()
        hist['MA20'] = hist['Close'].rolling(window=20).mean()
        hist['MA60'] = hist['Close'].rolling(window=60).mean()
        hist['MA240'] = hist['Close'].rolling(window=240).mean()

        # Bollinger Bands
        hist['BB_middle'] = hist['MA20']
        std = hist['Close'].rolling(window=20).std()
        hist['BB_upper'] = hist['BB_middle'] + (std * 2)
        hist['BB_lower'] = hist['BB_middle'] - (std * 2)

        # MACD
        exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
        exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
        hist['MACD'] = exp1 - exp2
        hist['MACD_signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
        hist['MACD_hist'] = hist['MACD'] - hist['MACD_signal']

        # ======================================================
        # 一目均衡表 (Ichimoku Cloud)
        # ======================================================
        # 轉換線 (Tenkan-sen): (9日最高 + 9日最低) / 2
        tenkan_high = hist['High'].rolling(window=9).max()
        tenkan_low  = hist['Low'].rolling(window=9).min()
        hist['Ichimoku_tenkan'] = (tenkan_high + tenkan_low) / 2

        # 基準線 (Kijun-sen): (26日最高 + 26日最低) / 2
        kijun_high = hist['High'].rolling(window=26).max()
        kijun_low  = hist['Low'].rolling(window=26).min()
        hist['Ichimoku_kijun'] = (kijun_high + kijun_low) / 2

        # 先行帶A (Senkou Span A): (轉換線 + 基準線) / 2，前移26期
        senkou_a = (hist['Ichimoku_tenkan'] + hist['Ichimoku_kijun']) / 2
        hist['Ichimoku_senkou_a'] = senkou_a.shift(26)

        # 先行帶B (Senkou Span B): (52日最高 + 52日最低) / 2，前移26期
        span_b_high = hist['High'].rolling(window=52).max()
        span_b_low  = hist['Low'].rolling(window=52).min()
        senkou_b = (span_b_high + span_b_low) / 2
        hist['Ichimoku_senkou_b'] = senkou_b.shift(26)

        # 遲行帶 (Chikou Span): 收盤價後移26期
        hist['Ichimoku_chikou'] = hist['Close'].shift(-26)

        data_points = []
        for index, row in hist.iterrows():
            # 處理時區轉換：若為分鐘/分時線，確保轉為台北時間 (UTC+8) 的時間戳
            if interval in ['1m', '2m', '5m', '15m', '30m', '60m', '90m', '1h']:
                if index.tzinfo is not None:
                    dt_local = index.tz_convert('Asia/Taipei').tz_localize(None)
                    timestamp = int(dt_local.timestamp())
                else:
                    timestamp = int(index.timestamp()) + (8 * 3600)
            else:
                timestamp = int(index.timestamp())

            data_points.append({
                'time': timestamp,
                'open': float(row['Open']) if pd.notna(row['Open']) else None,
                'high': float(row['High']) if pd.notna(row['High']) else None,
                'low': float(row['Low']) if pd.notna(row['Low']) else None,
                'close': float(row['Close']) if pd.notna(row['Close']) else None,
                'volume': int(row['Volume']) if pd.notna(row['Volume']) else None,
                'ma5': float(row['MA5']) if pd.notna(row['MA5']) else None,
                'ma10': float(row['MA10']) if pd.notna(row['MA10']) else None,
                'ma20': float(row['MA20']) if pd.notna(row['MA20']) else None,
                'ma60': float(row['MA60']) if pd.notna(row['MA60']) else None,
                'ma240': float(row['MA240']) if pd.notna(row['MA240']) else None,
                'bb_upper': float(row['BB_upper']) if pd.notna(row['BB_upper']) else None,
                'bb_middle': float(row['BB_middle']) if pd.notna(row['BB_middle']) else None,
                'bb_lower': float(row['BB_lower']) if pd.notna(row['BB_lower']) else None,
                'macd': float(row['MACD']) if pd.notna(row['MACD']) else None,
                'macd_signal': float(row['MACD_signal']) if pd.notna(row['MACD_signal']) else None,
                'macd_hist': float(row['MACD_hist']) if pd.notna(row['MACD_hist']) else None,
                # 一目均衡表
                'ichi_tenkan':  float(row['Ichimoku_tenkan'])  if pd.notna(row['Ichimoku_tenkan'])  else None,
                'ichi_kijun':   float(row['Ichimoku_kijun'])   if pd.notna(row['Ichimoku_kijun'])   else None,
                'ichi_senkou_a':float(row['Ichimoku_senkou_a']) if pd.notna(row['Ichimoku_senkou_a']) else None,
                'ichi_senkou_b':float(row['Ichimoku_senkou_b']) if pd.notna(row['Ichimoku_senkou_b']) else None,
                'ichi_chikou':  float(row['Ichimoku_chikou'])  if pd.notna(row['Ichimoku_chikou'])  else None,
            })

        response = {
            'symbol': symbol,
            'period': period,
            'interval': interval,
            'data': data_points
        }
        return jsonify(response)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'獲取歷史數據失敗: {str(e)}'}), 500


# ============================================================================
# 🦉 快速 AI 個股趨勢分析專用 API（支援本地 DB 極速運算 + LLM + 15分鐘記憶體快取）
# ============================================================================
_TREND_CACHE = {}
_TREND_CACHE_LOCK = threading.Lock()

@app.route('/api/stock/<symbol>/trend_analysis', methods=['GET', 'POST'])
def api_stock_trend_analysis(symbol):
    """
    極速 AI 個股趨勢分析：
    1. 優先查閱記憶體快取（15分鐘有效），點擊幾乎 0 秒立即顯示。
    2. 直接自本地 PostgreSQL 讀取行情（<0.05秒），計算 MA5, MA20, MA60 與短線走勢，跳過耗時的外部 MCP 與新聞爬取。
    3. 送出精準 LLM 提示詞取得結構化 JSON 趨勢研判（~1.5秒）。
    4. 具備自動降級機制，LLM 異常時自動依量化指標產出專業分析，保證 100% 穩定輸出。
    """
    t0 = time.time()
    code = symbol.replace('.TW', '').replace('.TWO', '').strip().upper()
    now = time.time()

    # 1. 檢查快取
    with _TREND_CACHE_LOCK:
        cached = _TREND_CACHE.get(code)
        if cached and (now - cached.get('ts', 0) < 900):  # 15分鐘快取
            res = dict(cached['data'])
            res['cached'] = True
            res['elapsed'] = round(now - t0, 3)
            return jsonify(res)

    try:
        from db_service import get_price_history, get_stock_name
        name = get_stock_name(code) or code
        db_data = get_price_history(code) if get_price_history else None

        df = None
        if db_data and len(db_data.get('dates', [])) > 0:
            df = pd.DataFrame(db_data)
            df['close'] = pd.to_numeric(df.get('close', df.get('Close')), errors='coerce')
            df['volume'] = pd.to_numeric(df.get('volume', df.get('Volume')), errors='coerce')
            df.dropna(subset=['close'], inplace=True)

        if df is None or len(df) < 5:
            # 備援 yfinance
            try:
                ticker = yf.Ticker(f"{code}.TW")
                hist = ticker.history(period="1mo")
                if not hist.empty:
                    df = pd.DataFrame({
                        'close': hist['Close'],
                        'volume': hist['Volume']
                    })
            except Exception:
                pass

        if df is None or len(df) < 5:
            return jsonify({
                'code': code,
                'name': name,
                'data': {
                    'recent_week': ['目前暫無足夠歷史交易數據進行技術均線運算。'],
                    'future': ['建議觀察成交量變化與市場題材發酵。'],
                    'warning': '資料不足，請注意操作風險。'
                },
                'cached': False,
                'elapsed': round(time.time() - t0, 3)
            })

        close = float(df['close'].iloc[-1])
        ma5 = float(df['close'].rolling(5).mean().iloc[-1])
        ma20 = float(df['close'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else ma5
        ma60 = float(df['close'].rolling(60).mean().iloc[-1]) if len(df) >= 60 else ma20
        pct_5d = float((close - df['close'].iloc[-5]) / df['close'].iloc[-5] * 100)
        vol_5d = float(df['volume'].iloc[-5:].mean()) / 1000 if len(df) >= 5 else 0.0

        ma_status = []
        if close >= ma5: ma_status.append('站上5日線')
        else: ma_status.append('跌破5日線')
        if close >= ma20: ma_status.append('站上月線')
        else: ma_status.append('跌破月線')
        if ma5 >= ma20: ma_status.append('短均多頭')
        else: ma_status.append('短均偏空')

        # 準備 Rule-based 降級資料
        if pct_5d > 0:
            fallback_week = [
                f"近一週上漲 {pct_5d:+.2f}%，短線多頭動能延續。",
                f"收盤價穩居月線（約 {ma20:.1f} 元）之上，技術結構偏多。"
            ]
            fallback_future = [
                f"若成交量能維持，短線有望向上挑戰波段高點。",
                f"回檔留意月線 {ma20:.1f} 元支撐防守力道。"
            ]
            fallback_warning = "短線漲多可能面臨正乖離拉回，追高請嚴設停損點。"
        else:
            fallback_week = [
                f"近一週回檔 {pct_5d:+.2f}%，短線處於技術面修正格局。",
                f"目前在月線（約 {ma20:.1f} 元）附近震盪消化賣壓。"
            ]
            fallback_future = [
                f"若能於季線 {ma60:.1f} 元附近量縮守穩，有望迎來技術性反彈。",
                f"後續觀察買盤回流時機與均線重整狀態。"
            ]
            fallback_warning = "處於整理偏弱格局，避免過早摸底，靜待止跌訊號。"

        analysis_result = {
            'recent_week': fallback_week,
            'future': fallback_future,
            'warning': fallback_warning
        }

        # 呼叫快速精簡 LLM
        try:
            prompt = f"""請針對台股 {code}（{name}）進行精簡的 AI 趨勢分析。
【關鍵技術數據】：
- 最新收盤價：{close} 元
- 近5日累計漲跌幅：{pct_5d:+.2f}%
- 均線現況：MA5={ma5:.1f}, MA20={ma20:.1f}, MA60={ma60:.1f}
- 均線多空：{', '.join(ma_status)}
- 5日均量：約 {vol_5d:.1f} 張

請嚴格遵守：
1. 一律使用繁體中文。
2. 回覆必須是合法的純 JSON 字串，嚴格禁止 markdown 代碼塊標記（如 ```json）、禁止思考過程或額外解說。
3. JSON 格式如下：
{{
  "recent_week": ["近一週技術均線或價格動能觀察1（25字內）", "短線支撐壓力觀察2（25字內）"],
  "future": ["中長期型態或箱型突破預測1（25字內）", "關鍵支撐或目標價位預測2（25字內）"],
  "warning": "針對近期熱度、乖離追高或短線風險提醒（40字內，不含「提醒：」字樣）"
}}"""

            messages = [
                {'role': 'system', 'content': '你是一位資深台股量化與技術分析助理，請只回傳純 JSON 字串。' + TRADITIONAL_CHINESE_RULE},
                {'role': 'user', 'content': prompt}
            ]
            resp_str = ask_llm_with_fallback(messages, temperature=0.2, max_tokens=300)
            clean_str = resp_str.strip()
            if "```" in clean_str:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_str, re.IGNORECASE)
                if m:
                    clean_str = m.group(1).strip()
            start_idx = clean_str.find('{')
            end_idx = clean_str.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_str = clean_str[start_idx:end_idx+1]

            parsed = json.loads(clean_str)
            if isinstance(parsed, dict) and 'recent_week' in parsed and 'future' in parsed:
                analysis_result = parsed
        except Exception as llm_err:
            print(f"[TrendAnalysis] LLM 呼叫異常或解析失敗 ({llm_err})，採用規則引擎備援")

        result_payload = {
            'code': code,
            'name': name,
            'data': analysis_result,
            'cached': False,
            'elapsed': round(time.time() - t0, 3)
        }

        with _TREND_CACHE_LOCK:
            _TREND_CACHE[code] = {
                'ts': now,
                'data': result_payload
            }

        return jsonify(result_payload)

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': code,
            'name': code,
            'data': {
                'recent_week': ['目前短線多頭氣勢仍在。', '向上突破需要成交量配合。'],
                'future': ['目前處於基本面支撐格局。', '注意未來成交量能否跟上。'],
                'warning': '該個股目前處於區間震盪，請投資人注意操作風險。'
            },
            'error': str(e)
        }), 200


@app.route('/api/ticker/<symbol>/indicators', methods=['GET'])
def api_get_ticker_indicators(symbol):
    """取得豐富技術指標（BIAS, ARBR, OBV, VWAP 等）"""
    try:
        symbol = _normalize_symbol(symbol)
        period = request.args.get('period', '1y')

        hist = pd.DataFrame()
        try:
            from db_service import get_price_history
            pure_symbol = symbol.replace('.TW', '')
            db_data = get_price_history(pure_symbol)
            if db_data and len(db_data['dates']) > 0:
                hist = pd.DataFrame(db_data)
                hist['Date'] = pd.to_datetime(hist['dates'])
                hist.set_index('Date', inplace=True)
                hist.rename(columns={'open':'Open', 'high':'High', 'low':'Low', 'close':'Close', 'volume':'Volume'}, inplace=True)
        except Exception as e:
            pass

        if hist.empty:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period)

        if hist.empty:
            return jsonify({'error': f'無法獲取 {symbol} 的歷史數據'}), 404

        indicators = TechnicalIndicators.calculate_all_indicators(hist)
        dates = hist.index.strftime('%Y-%m-%d').tolist()
        n = len(dates)

        def to_list(series):
            return [float(x) if pd.notna(x) else None for x in series.tail(n)]

        response = {
            'symbol': symbol,
            'period': period,
            'dates': dates,
            'indicators': {
                '📈 價格型指標': {
                    '移動平均線 (MA)': {
                        'MA5': to_list(indicators['ma_5']),
                        'MA10': to_list(indicators['ma_10']),
                        'MA20': to_list(indicators['ma_20']),
                        'MA50': to_list(indicators['ma_50']),
                    },
                    'MACD': {
                        'macd': to_list(indicators['macd']),
                        'signal': to_list(indicators['macd_signal']),
                        'histogram': to_list(indicators['macd_histogram']),
                    },
                    'RSI': {'rsi': to_list(indicators['rsi'])},
                    'KD': {'k': to_list(indicators['kd_k']), 'd': to_list(indicators['kd_d'])},
                    '布林通道 (Bollinger Bands)': {
                        'upper': to_list(indicators['bb_upper']),
                        'middle': to_list(indicators['bb_middle']),
                        'lower': to_list(indicators['bb_lower']),
                    },
                    'BIAS': {'bias': to_list(indicators['bias'])},
                    'ARBR': {'ar': to_list(indicators['ar']), 'br': to_list(indicators['br'])},
                },
                '📊 量能型指標': {
                    '成交量均線 (VMA)': {
                        'vma5': to_list(indicators['vma_5']),
                        'vma20': to_list(indicators['vma_20']),
                    },
                    '量比': {'volume_ratio': to_list(indicators['volume_ratio'])},
                },
                '🔗 價量結合型指標': {
                    'OBV': {'obv': to_list(indicators['obv'])},
                    'VWAP': {'vwap': to_list(indicators['vwap'])},
                    '量價背離': {'divergence': [int(x) if pd.notna(x) else None for x in indicators['price_volume_divergence'].tail(n)]},
                },
            }
        }
        return jsonify(response)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'獲取技術指標失敗: {str(e)}'}), 500


@app.route('/api/compare', methods=['POST'])
def api_compare_tickers():
    """多股票歷史績效比較"""
    try:
        data = request.get_json() or {}
        symbols = data.get('symbols', [])
        period = data.get('period', '1y')

        if not symbols:
            return jsonify({'error': '請至少提供一個股票代碼'}), 400
        if len(symbols) > 5:
            return jsonify({'error': '最多只能比較 5 個股票'}), 400

        comparison_data = {}
        for sym in symbols:
            sym = _normalize_symbol(sym)
            try:
                ticker = yf.Ticker(sym)
                hist = ticker.history(period=period)
                if not hist.empty:
                    initial = hist['Close'].iloc[0]
                    final = hist['Close'].iloc[-1]
                    returns = round(((final - initial) / initial) * 100, 2)
                    comparison_data[sym] = {
                        'dates': hist.index.strftime('%Y-%m-%d').tolist(),
                        'prices': [float(x) for x in hist['Close'].tolist()],
                        'initial_price': round(float(initial), 4),
                        'final_price': round(float(final), 4),
                        'returns': returns,
                    }
            except Exception as e:
                print(f'Compare: failed for {sym}: {e}')

        if not comparison_data:
            return jsonify({'error': '無法獲取任何股票的數據'}), 404

        return jsonify({'comparison': comparison_data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'比較失敗: {str(e)}'}), 500


# ===================================================================
# 使用者帳戶與身份驗證 API
# ===================================================================

@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    """使用者註冊"""
    try:
        data = request.get_json(silent=True) or {}
        username = str(data.get('username', '')).strip()
        email = str(data.get('email', '')).strip()
        password = str(data.get('password', '')).strip()
        password_confirm = str(data.get('password_confirm', '')).strip()

        if not username or not password or not password_confirm:
            return jsonify({'error': '帳號、密碼與確認密碼皆為必填項目'}), 400

        if password != password_confirm:
            return jsonify({'error': '確認密碼與設定密碼不相符，請重新輸入！'}), 400

        if not email:
            email = f"{username}@example.com"

        if len(username) < 3 or len(username) > 20:
            return jsonify({'error': '用戶名長度必須在 3 到 20 個字元之間'}), 400

        if len(password) < 6:
            return jsonify({'error': '密碼長度必須至少 6 個字元'}), 400

        if not re.fullmatch(r'[^@]+@[^@]+\.[^@]+', email):
            return jsonify({'error': '請輸入有效的電子郵件地址'}), 400

        from db_service import create_user
        user = create_user(username, email, password)
        if not user:
            return jsonify({'error': '用戶名或電子郵件已被註冊'}), 400

        # 註冊成功後自動幫用戶登入
        session['user_id'] = user.id
        session['username'] = user.username
        session['name'] = user.name
        session['is_admin'] = user.is_admin

        return jsonify({
            'success': True,
            'message': '註冊成功！',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'name': user.name,
                'is_admin': user.is_admin
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """使用者登入"""
    try:
        data = request.get_json(silent=True) or {}
        username = str(data.get('username', '')).strip()
        password = str(data.get('password', '')).strip()

        if not username or not password:
            return jsonify({'error': '請輸入用戶名與密碼'}), 400

        from db_service import authenticate_user
        user = authenticate_user(username, password)
        if not user:
            return jsonify({'error': '用戶名或密碼錯誤'}), 401

        session['user_id'] = user.id
        session['username'] = user.username
        session['name'] = user.name
        session['is_admin'] = user.is_admin

        return jsonify({
            'success': True,
            'message': '登入成功！',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'name': user.name,
                'is_admin': user.is_admin
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """使用者登出"""
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('name', None)
    session.pop('is_admin', None)
    return jsonify({'success': True, 'message': '已成功登出'})


@app.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    """取得目前登入狀態"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'logged_in': False, 'user': None})
    
    from db_service import get_user_profile
    profile = get_user_profile(user_id)
    if not profile:
        session.clear()
        return jsonify({'logged_in': False, 'user': None})
        
    return jsonify({
        'logged_in': True,
        'user': profile
    })


@app.route('/api/auth/profile', methods=['POST'])
def api_auth_profile():
    """更新個人資料"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    try:
        data = request.get_json(silent=True) or {}
        update_data = {}
        if "name" in data:
            update_data["name"] = str(data.get("name", "")).strip()
        if "email" in data:
            email = str(data.get("email", "")).strip()
            if email and not re.fullmatch(r'[^@]+@[^@]+\.[^@]+', email):
                return jsonify({'error': '請輸入有效的電子郵件地址'}), 400
            update_data["email"] = email
        if "phone" in data:
            update_data["phone"] = str(data.get("phone", "")).strip()
        if "occupation" in data:
            update_data["occupation"] = str(data.get("occupation", ""))
        if "password" in data and data.get("password"):
            pwd = str(data.get("password", "")).strip()
            if len(pwd) < 6:
                return jsonify({'error': '新密碼長度必須至少 6 個字元'}), 400
            update_data["password"] = pwd

        from db_service import update_user_profile
        success = update_user_profile(user_id, update_data)
        if success:
            if "name" in update_data:
                session['name'] = update_data['name']
            return jsonify({'success': True, 'message': '個人資料已更新成功！'})
        else:
            return jsonify({'error': '更新失敗：電子郵件可能已被其他帳號佔用'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


# ===================================================================
# 管理後台 API
# ===================================================================

@app.route('/api/admin/users', methods=['GET'])
def api_admin_users():
    """管理員獲取所有使用者列表"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    
    # 這裡 get_all_users 本身就會驗證 admin_id 是否為管理員
    users = get_all_users(admin_id)
    if not users and not session.get('is_admin'):
        # 額外安全檢查：如果沒回傳資料且 session 也不是 admin，返回 403 拒絕存取
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403
        
    return jsonify({'success': True, 'users': users})


@app.route('/api/admin/user/<int:target_id>/reset-password', methods=['POST'])
def api_admin_reset_password(target_id):
    """管理員重設指定使用者密碼"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        new_password = str(data.get('password', '')).strip()
        if not new_password or len(new_password) < 6:
            return jsonify({'error': '密碼長度必須至少 6 個字元'}), 400
            
        success = reset_user_password_by_admin(admin_id, target_id, new_password)
        if success:
            return jsonify({'success': True, 'message': '密碼已成功重設！'})
        else:
            return jsonify({'error': '操作失敗：您可能沒有管理員權限，或該使用者不存在'}), 403
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


# ===================================================================
# 觀察機器人 API (自選清單管理)
# ===================================================================
# ===================================================================

@app.route('/api/watchlists', methods=['GET'])
def api_watchlists_get():
    """取得自選清單列表"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        wls = get_user_watchlists(user_id)
        return jsonify({'success': True, 'watchlists': wls})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlists/add', methods=['POST'])
def api_watchlists_add():
    """新增自選清單"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': '清單名稱不能為空'}), 400
            
        wl = create_user_watchlist(user_id, name)
        return jsonify({'success': True, 'watchlist': wl})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlists/<int:watchlist_id>', methods=['PUT'])
def api_watchlists_rename(watchlist_id):
    """重新命名自選清單"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': '清單名稱不能為空'}), 400
            
        rename_user_watchlist(user_id, watchlist_id, name)
        return jsonify({'success': True, 'message': '修改成功'})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlists/<int:watchlist_id>', methods=['DELETE'])
def api_watchlists_delete(watchlist_id):
    """刪除自選清單"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        delete_user_watchlist(user_id, watchlist_id)
        return jsonify({'success': True, 'message': '刪除成功'})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchbot/stock_status', methods=['GET'])
def api_watchbot_stock_status():
    """查詢某隻股票在使用者自選清單的加入狀態"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        code = request.args.get('code', '').strip().upper()
        if code.endswith('.TW'):
            code = code[:-3]
        elif code.endswith('.TWO'):
            code = code[:-4]
            
        wls = get_user_watchlists(user_id)
        selected_ids = get_stock_watchlists_status(user_id, code)
        return jsonify({
            'success': True,
            'watchlists': wls,
            'selected_watchlist_ids': selected_ids
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchbot/update_stock_watchlists', methods=['POST'])
def api_watchbot_update_stock_watchlists():
    """批次同步股票所在的自選清單"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        if code.endswith('.TW'):
            code = code[:-3]
        elif code.endswith('.TWO'):
            code = code[:-4]
            
        watchlist_ids = data.get('watchlist_ids', [])
        if not isinstance(watchlist_ids, list):
            return jsonify({'error': '無效的清單 ID 格式'}), 400
            
        update_stock_watchlists(user_id, code, [int(i) for i in watchlist_ids])
        return jsonify({'success': True, 'message': '更新自選狀態成功'})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def clean_nan(obj):
    """遞迴清理所有 NaN 與 Inf 值，轉為 None 或合法型別以避免 JSON 解析崩潰"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    return obj


@app.route('/api/watchbot/list', methods=['GET'])
def api_watchbot_list():
    """取得觀察清單 + 每支股票最新掃描結果"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
    
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        watchlist_id = request.args.get('watchlist_id', type=int)
        # 若未指定 watchlist_id，則預設讀取第一個清單
        if watchlist_id is None:
            wls = get_user_watchlists(user_id)
            if wls:
                watchlist_id = wls[0]['id']
                
        codes   = get_watchlist(user_id, watchlist_id=watchlist_id)
        results = get_latest_results()
        # 用 dict 方便 merge
        results_map = {r['code']: r for r in results}
        
        from db_service import get_stock_name, get_latest_prices_and_changes
        prices_dict = get_latest_prices_and_changes(codes)
        
        # 抓取盤中真即時行情 (優先 TWSE MIS 官方批次 / Fugle / yfinance)，確保自選股清單顯示盤中最新價格與成交量
        fugle_quotes = {}
        try:
            fugle_quotes = run_async(global_mcp_client.fetch_realtime_quotes_batch(codes)) or {}
            missing_codes = [c for c in codes if c not in fugle_quotes]
            if missing_codes:
                async def _fetch_missing():
                    tasks = [global_mcp_client.fetch_fugle_quote(c) for c in missing_codes]
                    res = await asyncio.gather(*tasks, return_exceptions=True)
                    return {c: r for c, r in zip(missing_codes, res) if isinstance(r, dict)}
                fugle_quotes.update(run_async(_fetch_missing()))
        except Exception as fq_err:
            print(f"Watchlist real-time quotes error: {fq_err}")
        
        items = []
        for code in codes:
            # 取得該股在 results_map 中的資料，或建構預設的空資料
            raw_item = results_map.get(code)
            if raw_item:
                # 建立 dict 複製品以利直接寫入
                item = dict(raw_item)
                # 過濾舊資料中可能殘留的「自營商」項目，確保總數為 9 項
                item['bullish'] = [s for s in (item.get('bullish') or []) if '自營商' not in s]
                item['bearish'] = [s for s in (item.get('bearish') or []) if '自營商' not in s]
                if item.get('bias') and item['bias'] != '尚未掃描':
                    item['bias'] = '偏多' if len(item['bullish']) >= len(item['bearish']) else '偏空'
                if item.get('news_sentiment'):
                    item['news_sentiment'] = '利空' if item['news_sentiment'] in ['負面', '利空'] else '利多'
            else:
                item = {
                    'code': code, 'name': code,
                    'price': None, 'change': None, 'bias': '尚未掃描',
                    'bullish': [], 'bearish': [],
                    'news_sentiment': None, 'ai_summary': None, 'inst_data': {}, 'scanned_at': None
                }
            
            p_data = prices_dict.get(code, {})
            fq = fugle_quotes.get(code, {})
            
            def is_valid_num(val):
                return val is not None and not (isinstance(val, float) and (math.isnan(val) or math.isinf(val)))

            # 優先使用 Fugle 盤中真即時價與成交量
            if is_valid_num(fq.get('lastPrice')):
                item['price'] = fq.get('lastPrice')
                item['change'] = fq.get('change', 0.0)
                item['changePercent'] = fq.get('changePercent', 0.0)
                item['open'] = fq.get('open') if is_valid_num(fq.get('open')) else p_data.get('open')
                item['high'] = fq.get('high') if is_valid_num(fq.get('high')) else p_data.get('high')
                item['low'] = fq.get('low') if is_valid_num(fq.get('low')) else p_data.get('low')
                item['volume'] = fq.get('volume', 0) if fq.get('volume') else (p_data.get('volume', 0) if is_valid_num(p_data.get('volume')) else 0)
                item['prevClose'] = fq.get('prevClose') if is_valid_num(fq.get('prevClose')) else p_data.get('prevClose')
                item['buy'] = fq.get('buy') if is_valid_num(fq.get('buy')) else p_data.get('buy')
                item['sell'] = fq.get('sell') if is_valid_num(fq.get('sell')) else p_data.get('sell')
            else:
                # 次之使用 daily_price 歷史報價表
                if is_valid_num(p_data.get('price')):
                    item['price'] = p_data.get('price')
                    item['change'] = p_data.get('change', 0.0)
                    item['changePercent'] = p_data.get('changePercent', 0.0)
                item['open'] = p_data.get('open') if is_valid_num(p_data.get('open')) else None
                item['high'] = p_data.get('high') if is_valid_num(p_data.get('high')) else None
                item['low'] = p_data.get('low') if is_valid_num(p_data.get('low')) else None
                item['volume'] = p_data.get('volume', 0) if is_valid_num(p_data.get('volume')) else 0
                item['prevClose'] = p_data.get('prevClose') if is_valid_num(p_data.get('prevClose')) else None
                item['buy'] = p_data.get('buy') if is_valid_num(p_data.get('buy')) else None
                item['sell'] = p_data.get('sell') if is_valid_num(p_data.get('sell')) else None

            # 補強：若仍無價格或為 NaN，嘗試透過 yfinance 即時補完價格與漲跌
            if not is_valid_num(item.get('price')):
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(f"{code}.TW")
                    hist = ticker.history(period="5d")
                    if not hist.empty:
                        last_row = hist.iloc[-1]
                        close_val = float(last_row['Close'])
                        if not math.isnan(close_val):
                            item['price'] = round(close_val, 2)
                            if is_valid_num(last_row.get('Open')): item['open'] = round(float(last_row['Open']), 2)
                            if is_valid_num(last_row.get('High')): item['high'] = round(float(last_row['High']), 2)
                            if is_valid_num(last_row.get('Low')): item['low'] = round(float(last_row['Low']), 2)
                            if is_valid_num(last_row.get('Volume')): item['volume'] = int(last_row['Volume'])
                            if len(hist) > 1:
                                prev_close = float(hist.iloc[-2]['Close'])
                                if not math.isnan(prev_close):
                                    item['change'] = round(close_val - prev_close, 2)
                                    item['prevClose'] = round(prev_close, 2)
                                    item['changePercent'] = round((item['change'] / prev_close) * 100, 2) if prev_close else 0.0
                                else:
                                    item['change'] = 0.0
                            else:
                                item['change'] = 0.0
                except Exception as ex:
                    print(f"Fallback fetch price for {code} failed: {ex}")

            # 從即時行情或資料庫 stock_info 中強制修正或載入最新中文名稱！
            if fq.get('name'):
                item['name'] = fq['name']
            else:
                db_name = get_stock_name(code)
                if db_name:
                    item['name'] = db_name
                
            items.append(item)
            
        return jsonify({'watchlist': clean_nan(items)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchbot/add', methods=['POST'])
def api_watchbot_add():
    """新增股票到觀察清單"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        # 移除可能的後綴以利比對
        if code.endswith('.TW'):
            code = code[:-3]
        elif code.endswith('.TWO'):
            code = code[:-4]
            
        if not re.fullmatch(r'[0-9][A-Z0-9]{3,5}', code):
            return jsonify({'error': '請輸入有效的股票代號（4-6位英數字，且首字須為數字）'}), 400
            
        # 取得要加入的 watchlist_ids（支援單選或多選複選）
        watchlist_ids = data.get('watchlist_ids')
        if watchlist_ids is None:
            single_wid = data.get('watchlist_id')
            if single_wid is not None:
                watchlist_ids = [int(single_wid)]
            else:
                wls = get_user_watchlists(user_id)
                if wls:
                    watchlist_ids = [wls[0]['id']]
                else:
                    return jsonify({'error': '使用者尚未建立任何自選清單'}), 400
        elif not isinstance(watchlist_ids, list) or len(watchlist_ids) == 0:
            return jsonify({'error': '請至少選擇一個自選清單'}), 400

        target_ids = [int(wid) for wid in watchlist_ids]
        current_ids = get_stock_watchlists_status(user_id, code)
        merged_ids = list(set(current_ids).union(set(target_ids)))

        # 若有新的清單要加入
        new_ids = set(target_ids) - set(current_ids)
        if not new_ids:
            return jsonify({'success': False, 'message': f'{code} 已存在於所選的所有自選清單中'})

        update_stock_watchlists(user_id, code, merged_ids)
        return jsonify({'success': True, 'message': f'{code} 已成功加入所選的自選清單！'})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchbot/remove/<code>', methods=['DELETE'])
def api_watchbot_remove(code):
    """從觀察清單刪除股票"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        code_clean = code.strip().upper()
        if code_clean.endswith('.TW'):
            code_clean = code_clean[:-3]
        elif code_clean.endswith('.TWO'):
            code_clean = code_clean[:-4]
            
        watchlist_id = request.args.get('watchlist_id', type=int)
        
        removed = remove_from_watchlist(user_id, code_clean, watchlist_id)
        if removed:
            return jsonify({'success': True, 'message': f'{code_clean} 已從觀察清單移除'})
        else:
            return jsonify({'success': False, 'message': f'{code_clean} 不在觀察清單中'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchbot/reorder', methods=['POST'])
def api_watchbot_reorder():
    """重新排序特定自選清單中的股票順序"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        data = request.get_json(silent=True) or {}
        watchlist_id = data.get('watchlist_id')
        ordered_codes = data.get('ordered_codes', [])
        
        if watchlist_id is None:
            return jsonify({'error': '未指定自選清單 ID'}), 400
        if not isinstance(ordered_codes, list):
            return jsonify({'error': '排序代號格式錯誤'}), 400
            
        from db_service import reorder_watchlist
        reorder_watchlist(user_id, int(watchlist_id), [str(c).strip().upper() for c in ordered_codes])
        return jsonify({'success': True, 'message': '自選股排序更新成功'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/market/indices', methods=['GET'])
def api_market_indices():
    """取得台股大盤雙核心指數（加權指數 TAIEX、櫃買指數 TPEX）即時數據與走勢圖點位"""
    try:
        url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw|otc_o00.tw&json=1&delay=0'
        resp = requests.get(url, timeout=4).json()
        msg_array = resp.get('msgArray', [])
        
        # 取得分時走勢折線圖點位
        tse_chart = []
        try:
            r_tse = requests.get('https://mis.twse.com.tw/stock/api/getChartOhlc.jsp?ex=tse&ch=t00.tw', timeout=3).json()
            tse_chart = [float(item['c']) for item in r_tse.get('ohlcArray', []) if item.get('c')]
        except Exception:
            pass
            
        otc_chart = []
        try:
            r_otc = requests.get('https://mis.twse.com.tw/stock/api/getChartOhlc.jsp?ex=otc&ch=o00.tw', timeout=3).json()
            otc_chart = [float(item['c']) for item in r_otc.get('ohlcArray', []) if item.get('c')]
        except Exception:
            pass
            
        indices = []
        for item in msg_array:
            code = item.get('c')
            is_tse = (code == 't00')
            name = '加權指數' if is_tse else '櫃買指數'
            eng_name = 'TAIEX' if is_tse else 'TPEX'
            
            price = float(item.get('z') or item.get('y') or 0)
            prev_close = float(item.get('y') or price)
            change = round(price - prev_close, 2)
            pct_change = round((change / prev_close) * 100, 2) if prev_close else 0.0
            open_p = float(item.get('o') or price)
            high_p = float(item.get('h') or price)
            low_p = float(item.get('l') or price)
            
            points = tse_chart if is_tse else otc_chart
            if not points:
                points = [open_p, (open_p + high_p) / 2, high_p, low_p, price]
                
            indices.append({
                'code': code,
                'name': name,
                'eng_name': eng_name,
                'price': price,
                'prev_close': prev_close,
                'change': change,
                'pct_change': pct_change,
                'open': open_p,
                'high': high_p,
                'low': low_p,
                'points': points
            })
            
        return jsonify({'success': True, 'indices': indices})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/user/<int:target_id>', methods=['DELETE'])
def api_admin_delete_user(target_id):
    """管理員刪除使用者帳戶"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
        
    if admin_id == target_id:
        return jsonify({'error': '您不能刪除自己的管理員帳戶'}), 400
        
    try:
        success = delete_user_by_admin(admin_id, target_id)
        if success:
            return jsonify({'success': True, 'message': '使用者已成功刪除！'})
        else:
            return jsonify({'error': '操作失敗：您可能沒有管理員權限，或該使用者不存在'}), 403
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/admin/stocks', methods=['GET'])
def api_admin_stocks():
    """管理員獲取台股資料庫狀態與清單（支援後端分頁與關鍵字過濾）"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    if not session.get('is_admin'):
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403

    try:
        from flask import request
        import math
        
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        search = request.args.get('search', '').strip().lower()

        from db_service import get_all_stocks_list
        stocks = get_all_stocks_list()
        
        # 統計資訊（不受搜尋影響的全局數據）
        total_count = len(stocks)
        synced_count = sum(1 for s in stocks if s['price_count'] > 0)
        unsynced_count = total_count - synced_count

        # 搜尋篩選
        if search:
            filtered_stocks = [
                s for s in stocks
                if search in s['code'].lower()
                or (s['name'] and search in s['name'].lower())
                or (s['industry'] and search in s['industry'].lower())
            ]
        else:
            filtered_stocks = stocks

        # 分頁計算
        filtered_total = len(filtered_stocks)
        total_pages = math.ceil(filtered_total / limit) if filtered_total > 0 else 1
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_stocks = filtered_stocks[start_idx:end_idx]

        return jsonify({
            'success': True,
            'stocks': paginated_stocks,
            'summary': {
                'total': total_count,
                'synced': synced_count,
                'unsynced': unsynced_count
            },
            'pagination': {
                'page': page,
                'limit': limit,
                'total_filtered': filtered_total,
                'pages': total_pages
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/admin/reload_service', methods=['POST'])
def api_admin_reload_service():
    """管理員熱重載伺服器服務 (NSSM 會在程序結束後自動拉起新程序)"""
    is_localhost = request.remote_addr in ['127.0.0.1', '::1', 'localhost']
    admin_id = session.get('user_id')
    if not is_localhost and (not admin_id or not session.get('is_admin')):
        return jsonify({'error': '權限不足'}), 403
    import os, time, threading
    def _do_exit():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({'success': True, 'message': '伺服器正在重新啟動中...'})


@app.route('/api/admin/db/trigger_daily_update', methods=['POST'])
def api_admin_db_trigger_daily_update():
    """管理員手動觸發全台股歷史股價更新任務"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    if not session.get('is_admin'):
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403

    try:
        import subprocess
        # 跨平台取得 Python 執行路徑與腳本路徑
        python_exec = sys.executable
        script_path = str(Path(__file__).parent / "ingest_script.py")
        # 在背景啟動 ingest_script.py，不阻塞 Flask
        subprocess.Popen([python_exec, script_path])
        return jsonify({
            'success': True,
            'message': '已成功手動觸發「全台股歷史股價更新」！系統已在背景啟動同步腳本，大約需要數分鐘，您可稍後重新整理列表以查看各股的更新進度。'
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/admin/stock/<code>/update', methods=['POST'])
def api_admin_stock_update(code):
    """管理員手動同步指定個股歷史股價"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    if not session.get('is_admin'):
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403

    try:
        from models import SessionLocal, StockInfo
        market = "上市"
        with SessionLocal() as db:
            stock = db.query(StockInfo).filter(StockInfo.code == code).first()
            if stock:
                market = stock.market or "上市"
                
        suffix = ".TWO" if market == "上櫃" else ".TW"
        stock_info = {
            'code': code,
            'market': market,
            'suffix': suffix
        }

        from ingest_script import fetch_and_save_history
        success = fetch_and_save_history(stock_info, period="1y") # 同步 1 年的歷史價格以節省時間

        if success:
            # 重新取得該股票最新狀態
            from db_service import get_price_history
            hist = get_price_history(code)
            price_count = len(hist['dates']) if hist else 0
            last_date = hist['dates'][-1] if hist else "無"

            return jsonify({
                'success': True,
                'message': f'個股 {code} 價格同步成功！',
                'stock': {
                    'code': code,
                    'last_date': last_date,
                    'price_count': price_count
                }
            })
        else:
            return jsonify({'error': f'個股 {code} 價格同步失敗，請確認代號是否正確。'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/admin/db/populate_names', methods=['POST'])
def api_admin_db_populate_names():
    """管理員一鍵啟動背景線程補齊台股中文名稱"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    if not session.get('is_admin'):
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403

    try:
        import threading
        from db_service import populate_stock_names
        
        # 啟動背景線程執行名稱補全，避免前端請求逾時
        threading.Thread(target=populate_stock_names).start()

        return jsonify({
            'success': True,
            'message': '已成功於背景啟動「全台股名稱與代碼補齊」任務！大約耗時 10~20 秒，請稍後重整查看。'
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/admin/db/cleanup', methods=['POST'])
def api_admin_db_cleanup():
    """管理員清理資料庫中無效的股票代號（7碼以上且無歷史股價的衍生商品）"""
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': '未登入或會話已過期'}), 401
    if not session.get('is_admin'):
        return jsonify({'error': '權限不足，僅限管理員存取'}), 403

    try:
        from db_service import cleanup_orphan_stocks
        result = cleanup_orphan_stocks()
        return jsonify({
            'success': True,
            'deleted': result['deleted'],
            'remaining': result['remaining'],
            'message': f'已成功清理 {result["deleted"]} 筆無效代號（認購/認售權證、TDR 等衍生商品），資料庫剩餘 {result["remaining"]} 筆有效股票。'
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500





@app.route('/api/watchbot/scan', methods=['POST'])
def api_watchbot_scan():
    """手動觸發全部掃描，同步等待完成後回傳所有結果（掃描中會阻塞請求）"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        results = scan_all(user_id)
        return jsonify({'success': True, 'count': len(results), 'results': results})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'掃描失敗：{e}'}), 500


# 追蹤哪些使用者正在進行背景掃描（user_id -> True/False）
_scan_running: dict = {}

@app.route('/api/watchbot/scan_background', methods=['POST'])
def api_watchbot_scan_background():
    """在背景執行掃描，立即回傳，不阻塞前端。掃完後資料自動更新至 DB。"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401

    # 避免同一使用者同時觸發多次
    if _scan_running.get(user_id):
        return jsonify({'status': 'already_running', 'message': '已有掃描在執行中，請稍候'})

    def _run(uid):
        _scan_running[uid] = True
        try:
            print(f"[AutoScan] 使用者 {uid} 登入觸發背景掃描開始")
            scan_all(uid)
            print(f"[AutoScan] 使用者 {uid} 背景掃描完成")
        except Exception as ex:
            print(f"[AutoScan] 使用者 {uid} 背景掃描失敗: {ex}")
        finally:
            _scan_running[uid] = False

    import threading
    t = threading.Thread(target=_run, args=(user_id,), daemon=True)
    t.start()
    return jsonify({'status': 'started', 'message': '背景掃描已啟動，請稍後重新整理觀察清單'})


@app.route('/api/watchbot/scan_status', methods=['GET'])
def api_watchbot_scan_status():
    """查詢目前使用者的背景掃描狀態"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入'}), 401
    running = bool(_scan_running.get(user_id, False))
    return jsonify({'running': running})


@app.route('/api/watchbot/result/<code>', methods=['GET'])
def api_watchbot_result(code):
    """取得單一股票最新掃描結果"""
    if not _watchbot_available:
        return jsonify({'error': '觀察機器人模組未啟用'}), 503
        
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登入或會話已過期，請重新登入'}), 401
        
    try:
        code_str = code.strip()
        result = get_latest_result_for(code_str)
        if not result:
            return jsonify({'error': f'{code_str} 尚無掃描結果'}), 404
        
        from db_service import get_stock_name
        db_name = get_stock_name(code_str)
        if db_name:
            result['name'] = db_name
            
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def get_raw_stock_day_all():
    cache_key = 'raw_stock_day_all'
    cached = data_cache.get(cache_key)
    if cached:
        return cached
    import requests
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=10)
        if res.status_code == 200:
            res.encoding = 'utf-8'
            data = res.json()
            data_cache.set(cache_key, data, ttl=300) # 快取 5 分鐘
            return data
    except Exception as e:
        print(f"[TWSE] 取得 STOCK_DAY_ALL 失敗: {e}")
    return None


@app.route('/api/market/ticker', methods=['GET'])
def api_get_market_ticker_data():
    """取得跑馬燈資料，支援多種排行與限制數量
    Query params:
      type: volume | yield | gainers | losers | turnover (default: volume)
      limit: 10 | 20 (default: 20)
    """
    try:
        ticker_type = request.args.get('type', 'volume').lower()
        limit = request.args.get('limit', '20')
        try:
            limit = int(limit)
        except ValueError:
            limit = 20
            
        if ticker_type not in ['volume', 'yield', 'gainers', 'losers', 'turnover']:
            ticker_type = 'volume'
            
        cache_key = f'ticker_data_{ticker_type}_{limit}'
        cached = data_cache.get(cache_key)
        if cached:
            return jsonify({'stocks': cached})
            
        data = get_raw_stock_day_all()
        if not data:
            return jsonify({'error': '無法取得市場數據'}), 500
            
        from db_service import _is_valid_stock_code
        
        parsed_stocks = []
        yields = {}
        if ticker_type == 'yield':
            yields = get_twse_batch_dividend_yields()
            
        for item in data:
            code = item.get("Code", "").strip()
            name = item.get("Name", "").strip()
            
            if not code or not name or not _is_valid_stock_code(code):
                continue
                
            try:
                price = float(item.get("ClosingPrice", 0.0)) if item.get("ClosingPrice") else None
            except (ValueError, TypeError):
                price = None
                
            try:
                change = float(item.get("Change", 0.0)) if item.get("Change") else 0.0
            except (ValueError, TypeError):
                change = 0.0
                
            # 計算排行分數/數值
            sort_val = 0.0
            if ticker_type == 'volume':
                try:
                    sort_val = int(item.get("TradeVolume", 0))
                except:
                    continue
            elif ticker_type == 'turnover':
                try:
                    sort_val = int(item.get("TradeValue", 0))
                except:
                    continue
            elif ticker_type == 'yield':
                sort_val = yields.get(code, 0.0)
                if sort_val <= 0:
                    continue
            elif ticker_type in ['gainers', 'losers']:
                if price is not None and price != change:
                    prev = price - change
                    sort_val = (change / prev) * 100 if prev > 0 else 0.0
                else:
                    sort_val = 0.0
                    
            parsed_stocks.append({
                "code": code,
                "name": name,
                "price": price,
                "change": change,
                "sort_val": sort_val
            })
            
        # 排序
        if ticker_type == 'losers':
            parsed_stocks.sort(key=lambda x: x["sort_val"])
        else:
            parsed_stocks.sort(key=lambda x: x["sort_val"], reverse=True)
            
        top_stocks = parsed_stocks[:limit]
        
        result_stocks = []
        for item in top_stocks:
            item_copy = item.copy()
            if ticker_type == 'yield':
                item_copy["yield"] = item["sort_val"]
            elif ticker_type in ['gainers', 'losers']:
                item_copy["pct"] = item["sort_val"]
            elif ticker_type == 'volume':
                item_copy["volume"] = item["sort_val"]
            elif ticker_type == 'turnover':
                item_copy["turnover"] = item["sort_val"]
                
            del item_copy["sort_val"]
            result_stocks.append(item_copy)
            
        # 快取 5 分鐘
        data_cache.set(cache_key, result_stocks, ttl=300)
        return jsonify({'stocks': result_stocks})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/market/top_volume', methods=['GET'])
def api_get_top_volume_stocks():
    """從 TWSE OpenAPI 取得成交量前 20 名個股"""
    try:
        cache_key = 'top_volume_stocks_20'
        cached = data_cache.get(cache_key)
        if cached:
            return jsonify({'stocks': cached})
            
        import requests
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=10)
        if res.status_code == 200:
            res.encoding = 'utf-8'
            data = res.json()
            
            parsed_stocks = []
            for item in data:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                
                # 排除沒有代號或名稱的資料
                if not code or not name:
                    continue
                    
                # 排除認購/認售權證等 6 碼非 00 開頭的股票
                from db_service import _is_valid_stock_code
                if not _is_valid_stock_code(code):
                    continue
                
                try:
                    vol = int(item.get("TradeVolume", 0))
                    parsed_stocks.append((vol, item))
                except (ValueError, TypeError):
                    continue
            
            # 依成交量遞減排序
            parsed_stocks.sort(key=lambda x: x[0], reverse=True)
            top_20 = parsed_stocks[:20]
            
            result_stocks = []
            for vol, item in top_20:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                
                # 解析收盤價與漲跌
                try:
                    price = float(item.get("ClosingPrice", 0.0)) if item.get("ClosingPrice") else None
                except (ValueError, TypeError):
                    price = None
                    
                try:
                    change = float(item.get("Change", 0.0)) if item.get("Change") else 0.0
                except (ValueError, TypeError):
                    change = 0.0
                
                result_stocks.append({
                    "code": code,
                    "name": name,
                    "price": price,
                    "change": change,
                    "volume": vol
                })
            
            # 快取 10 分鐘
            data_cache.set(cache_key, result_stocks, ttl=600)
            return jsonify({'stocks': result_stocks})
        else:
            return jsonify({'error': '無法取得成交量排行'}), res.status_code
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': '找不到該資源'}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': '伺服器內部錯誤'}), 500


# ===================================================================
# 🧠 智慧選股 API
# ===================================================================

# ---- 問卷題目資料 ----
_RISK_QUESTIONS = [
    {"id": 0, "title": "一、年齡", "options": ["65歲(含)以上/17歲(含)以下", "60-64歲", "56-59歲", "41-55歲", "18-40歲"]},
    {"id": 1, "title": "二、金融理財知識：您對金融市場和投資的認識？", "options": ["對金融市場一無所知", "只有一些基本知識", "瞭解基本知識並明白分散投資", "對商品及風險有進一步認識", "非常熟悉大部份金融產品"]},
    {"id": 2, "title": "三、金融投資商品的投資經驗", "options": ["1年（含）以下", "1年-3年（含）", "3年-6年（含）", "6年-10年（含）", "10年以上"]},
    {"id": 3, "title": "四、可承受的波動程度：您可接受何種程度的價格波動？", "options": ["5%以下", "10%以下", "15%以下", "20%以下", "20%以上"]},
    {"id": 4, "title": "五、投資年間：當投資商品有波動時，您可接受的持有期間？", "options": ["3個月以下", "3個月-6個月", "6個月-1年", "1年-3年", "3年以上"]},
    {"id": 5, "title": "六、財務狀況：您的家庭月收入約有多少比例可以用於投資或儲蓄？", "options": ["無", "0%-10%", "10%-30%", "30%-60%", "60%以上"]},
    {"id": 6, "title": "七、資產配置：目前投資的資產中，約有多少比例持有金融投資商品？", "options": ["無", ">0%-10%", ">10%-25%", ">25%-50%", ">50%"]},
    {"id": 7, "title": "八、金融投資商品的交易頻率？", "options": ["三年以上", "一年以上", "半年", "每季", "三個月以下"]},
    {"id": 8, "title": "九、投資目標：下列哪一項最能描述您的投資目標？", "options": ["維持本金", "穩定定期收益", "適度風險長期成長", "較高風險資本增值", "更高風險最大增值"]},
    {"id": 9, "title": "十、若非預期的事件發生時，您的備用金相當於幾個月的家庭開銷？", "options": ["無備用金", "3個月以下", "3個月到6個月", "6個月到9個月", "超過9個月"]},
]

_POPULAR_STOCKS_MAP = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電", "2382": "廣達",
    "2303": "聯電", "3711": "日月光投控", "2881": "富邦金", "2882": "國泰金", "2891": "中信金",
    "2886": "兆豐金", "2884": "玉山金", "2885": "元大金", "2892": "第一金", "2890": "永豐金",
    "2880": "華南金", "2883": "凱基金", "2887": "台新金", "2356": "英業達", "5880": "合庫金",
    "3231": "緯創", "2357": "華碩", "2324": "仁寶", "2301": "光寶科", "2408": "南亞科",
    "2409": "友達", "3481": "群創", "2603": "長榮", "2609": "陽明", "2615": "萬海",
    "1303": "南亞", "1301": "台塑", "1326": "台化", "6505": "台塑化", "1101": "台泥",
    "1102": "亞泥", "1216": "統一", "1402": "遠東新", "2002": "中鋼", "2207": "和泰車",
    "2912": "統一超", "5871": "中租-KY", "5876": "上海商銀", "6415": "矽力*-KY", "8454": "富邦媒",
    "9910": "豐泰", "3045": "台灣大", "4904": "遠傳", "2412": "中華電", "3008": "大立光",
}

def _calc_risk_profile(score: int):
    if score <= 15:
        return "保守型", "風險承受度較低，不適合投資風險太高的基金。", "低風險 (RR1) 及中低風險 (RR2) 的基金"
    elif score <= 30:
        return "穩健型", "願意承擔適量風險，以追求有潛力的報酬。", "低風險 (RR1) 至中高風險 (RR4) 的基金"
    else:
        return "積極型", "願意承擔較高風險以追求獲利，接受較高波動。", "低風險 (RR1) 至高風險 (RR5) 的任何基金"


@app.route('/api/smart-stock/questions', methods=['GET'])
def api_smart_stock_questions():
    """取得風險評估問卷題目"""
    return jsonify({'questions': _RISK_QUESTIONS, 'total': len(_RISK_QUESTIONS)})


@app.route('/api/smart-stock/result', methods=['POST'])
def api_smart_stock_result():
    """計算風險評估結果並將結果綁定帳號存入資料庫，同時推薦股票"""
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': '請先登入後再進行風險評估問卷'}), 401

        data = request.get_json(silent=True) or {}
        answers = data.get('answers', {})  # {question_id_str: score_int}

        if len(answers) != len(_RISK_QUESTIONS):
            return jsonify({'error': f'請完成所有 {len(_RISK_QUESTIONS)} 題問卷'}), 400

        total_score = sum(int(v) for v in answers.values())
        profile, desc, fund_info = _calc_risk_profile(total_score)

        # 儲存問卷結果至 PostgreSQL 資料庫 (確保綁定當前登入的 user_id)
        from db_service import save_risk_profile
        saved_ok = save_risk_profile(user_id, total_score, profile, answers)
        if not saved_ok:
            print(f'[smart-stock] ⚠️ 警告：儲存用戶 {user_id} 風險屬性到資料庫失敗！')

        # 計算 Beta 推薦股票（在背景線程執行，避免阻塞）
        recommended_stocks = []
        error_msg = None
        try:
            from beta_calculator import BetaCalculator
            calculator = BetaCalculator(period="1y")
            tw_stocks = [f"{code}.TW" for code in _POPULAR_STOCKS_MAP.keys()]
            betas, prices = calculator.calculate_batch_beta_and_prices(tw_stocks)
            classified = calculator.classify_stocks(betas)

            if profile == "保守型":
                pool = classified["conservative"]
            elif profile == "穩健型":
                pool = classified["balanced"] or classified["conservative"]
            else:
                pool = classified["aggressive"]

            for stock in pool[:5]:
                ticker_code = stock['code']
                short_code = ticker_code.replace(".TW", "")
                recommended_stocks.append({
                    'code': short_code,
                    'name': _POPULAR_STOCKS_MAP.get(short_code, short_code),
                    'beta': stock['beta'],
                    'current_price': round(prices.get(ticker_code, 0) or 0, 2),
                })
        except Exception as e:
            error_msg = f"推薦股票時出錯: {str(e)}"

        return jsonify({
            'score': total_score,
            'profile': profile,
            'desc': desc,
            'fund_info': fund_info,
            'recommended_stocks': recommended_stocks,
            'error_msg': error_msg,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/smart-stock/my-profile', methods=['GET'])
def api_smart_stock_my_profile():
    """取得目前登入使用者的既有風險屬性結果"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'profile': None})
    try:
        from db_service import get_risk_profile
        saved = get_risk_profile(user_id)
        if not saved:
            return jsonify({'profile': None})
        # 用既有的 answers 重新計算推薦股票
        answers = saved['answers']
        profile, desc, fund_info = _calc_risk_profile(saved['score'])
        recommended_stocks = []
        error_msg = None
        try:
            from beta_calculator import BetaCalculator
            calculator = BetaCalculator(period="1y")
            tw_stocks = [f"{code}.TW" for code in _POPULAR_STOCKS_MAP.keys()]
            betas, prices = calculator.calculate_batch_beta_and_prices(tw_stocks)
            classified = calculator.classify_stocks(betas)
            if profile == "保守型":
                pool = classified["conservative"]
            elif profile == "穩健型":
                pool = classified["balanced"] or classified["conservative"]
            else:
                pool = classified["aggressive"]
            for stock in pool[:5]:
                ticker_code = stock['code']
                short_code = ticker_code.replace(".TW", "")
                recommended_stocks.append({
                    'code': short_code,
                    'name': _POPULAR_STOCKS_MAP.get(short_code, short_code),
                    'beta': stock['beta'],
                    'current_price': round(prices.get(ticker_code, 0) or 0, 2),
                })
        except Exception as e:
            error_msg = f"推薦股票時出錯: {str(e)}"
        return jsonify({
            'profile': {
                'score': saved['score'],
                'profile': profile,
                'desc': desc,
                'fund_info': fund_info,
                'taken_at': saved['taken_at'],
                'recommended_stocks': recommended_stocks,
                'error_msg': error_msg,
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'伺服器內部錯誤：{e}'}), 500


@app.route('/api/smart-stock/stocks', methods=['GET'])
def api_smart_stock_stocks():
    """取得台股前50支股票的 Beta 係數與股價"""
    try:
        from beta_calculator import BetaCalculator
        calculator = BetaCalculator(period="1y")
        tw_stocks = [f"{code}.TW" for code in _POPULAR_STOCKS_MAP.keys()]
        betas, prices = calculator.calculate_batch_beta_and_prices(tw_stocks)

        stock_list = []
        for code, name in _POPULAR_STOCKS_MAP.items():
            ticker_code = f"{code}.TW"
            beta = betas.get(ticker_code)
            price = prices.get(ticker_code)

            if beta is None:
                category = "未知"
            elif beta < 1:
                category = "保守型"
            elif abs(beta - 1) <= 0.001:
                category = "穩健型"
            else:
                category = "積極型"

            stock_list.append({
                'code': code,
                'name': name,
                'price': round(price, 2) if price is not None else None,
                'beta': round(beta, 3) if beta is not None else None,
                'category': category,
            })

        return jsonify({'stocks': stock_list})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'載入股票數據失敗: {e}'}), 500


if __name__ == "__main__":
    # 在啟動時建立背景迴圈與持久化連線
    start_background_loop()

    # 建立觀察機器人資料表（若不存在）
    if _watchbot_available:
        try:
            create_watchbot_tables()
            print("✅ 觀察機器人資料表就緒")
        except Exception as e:
            print(f"⚠️ 觀察機器人資料表建立失敗: {e}")

    print("正在建立持久化 MCP 連線...")
    run_async(global_mcp_client.connect())

    print("股Friend 正在啟動：http://127.0.0.1:5001")
    app.run(host="0.0.0.0", port=5001, debug=True)

