import os
import json
import asyncio
import traceback
import httpx
import contextlib
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

import contextlib
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client, StdioServerParameters

load_dotenv()


def _make_subprocess_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class MCPClientError(RuntimeError):
    pass


class MultiMCPClient:
    def __init__(self):
        self.twse_url = (os.getenv("TWSE_MCP_URL", "") or "").strip()
        self.twse_fallback_url = (os.getenv("TWSE_MCP_FALLBACK_URL", "") or "").strip()

        self.twse_tool_daily = (os.getenv("TWSE_TOOL_DAILY", "") or "").strip()
        self.twse_tool_valuation = (os.getenv("TWSE_TOOL_VALUATION", "") or "").strip()
        self.twse_tool_news = (os.getenv("TWSE_TOOL_NEWS", "") or "").strip()

        self.talib_command = (os.getenv("TALIB_MCP_COMMAND", "") or "").strip()
        self.talib_args = self._split_args(os.getenv("TALIB_MCP_ARGS", ""))

        self.yfinance_command = (os.getenv("YFINANCE_MCP_COMMAND", "") or "").strip()
        self.yfinance_args = self._split_args(os.getenv("YFINANCE_MCP_ARGS", ""))
        self.yfinance_tool_price = (os.getenv("YFINANCE_TOOL_PRICE", "get_stock_info") or "").strip()
        self.yfinance_tool_history = (os.getenv("YFINANCE_TOOL_HISTORY", "get_historical_stock_prices") or "").strip()
        self.yfinance_tool_news = (os.getenv("YFINANCE_TOOL_NEWS", "get_yahoo_finance_news") or "").strip()

        # YOLOv9 MCP 定義
        import sys
        default_python = r"C:\Users\cyutim\AppData\Local\Programs\Python\Python310\python.exe"
        if not os.path.exists(default_python):
            default_python = sys.executable
        self.yolov9_command = (os.getenv("YOLOV9_MCP_COMMAND", "") or default_python).strip()
        yolov9_args_env = os.getenv("YOLOV9_MCP_ARGS", "").strip()
        if yolov9_args_env:
            if os.path.exists(yolov9_args_env):
                self.yolov9_args = [yolov9_args_env]
            else:
                self.yolov9_args = self._split_args(yolov9_args_env)
        else:
            local_server = Path(__file__).resolve().parent / "yolov9_mcp" / "server.py"
            default_server = str(local_server) if local_server.exists() else r"C:\Users\cyutim\claude code\yolov9_mcp\server.py"
            self.yolov9_args = [default_server]

        # Fugle API 定義
        self.fugle_token = (os.getenv("FUGLE_API_TOKEN", "") or "").strip()

        # 持久化連線狀態
        self._exit_stack = contextlib.AsyncExitStack()
        self._sessions: Dict[str, ClientSession] = {} # target -> session
        self._is_connected = False

    @staticmethod
    def _split_args(arg_string: str) -> list[str]:
        arg_string = (arg_string or "").strip()
        if not arg_string:
            return []
        import shlex
        try:
            return shlex.split(arg_string, posix=False)
        except Exception:
            return [x for x in arg_string.split(" ") if x.strip()]

    def _twse_urls(self) -> list[str]:
        urls = []
        for u in [self.twse_url, self.twse_fallback_url]:
            if u and u not in urls:
                urls.append(u)
        return urls

    async def list_twse_tools(self) -> List[Dict[str, Any]]:
        urls = self._twse_urls()
        if not urls:
            raise MCPClientError("TWSE_MCP_URL 未設定")

        last_error = None
        for url in urls:
            try:
                print(f"[TWSE MCP] list_tools connect => {url}")
                async with streamable_http_client(url) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return self._format_tools(result)
            except Exception as e:
                last_error = e
                traceback.print_exc()
                print(f"[TWSE MCP] list_tools failed => {url}")

        raise MCPClientError(
            f"TWSE MCP list_tools 失敗: {type(last_error).__name__}: {last_error} | tried={urls}"
        ) from last_error

    async def list_stdio_tools(self, target: str) -> List[Dict[str, Any]]:
        command, args, server_name = self._resolve_stdio_target(target)

        if not command:
            raise MCPClientError(f"{server_name} command 未設定")

        try:
            print(f"[{server_name}] command => {command}")
            print(f"[{server_name}] args => {args}")
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=os.environ.copy(),
            )

            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return self._format_tools(result)
        except Exception as e:
            traceback.print_exc()
            raise MCPClientError(
                f"{server_name} list_tools 失敗: {type(e).__name__}: {e}"
            ) from e

    def _resolve_stdio_target(self, target: str):
        if target == "talib":
            return self.talib_command, self.talib_args, "TA-Lib MCP"
        if target == "yfinance":
            return self.yfinance_command, self.yfinance_args, "yfinance MCP"
        if target == "yolov9":
            return self.yolov9_command, self.yolov9_args, "WongKinYiu-YOLOv9-Server"
        raise MCPClientError(f"未知的 stdio target: {target}")

    def _format_tools(self, result: Any) -> List[Dict[str, Any]]:
        tools = []
        for tool in getattr(result, "tools", []):
            tools.append({
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "inputSchema": getattr(tool, "inputSchema", None),
            })
        return tools

    async def connect(self):
        """建立所有持久化連線"""
        if self._is_connected:
            return
        
        # 1. 連線 TWSE (HTTP Stream)
        urls = self._twse_urls()
        if urls:
            url = urls[0]
            try:
                print(f"[TWSE MCP] Initializing persistent connection => {url}")
                # streamable_http_client returns a context manager that yields (read, write, control)
                ctx = streamable_http_client(url)
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(ctx)
                
                session_ctx = ClientSession(read_stream, write_stream)
                session = await self._exit_stack.enter_async_context(session_ctx)
                
                await session.initialize()
                self._sessions["twse"] = session
            except Exception as e:
                print(f"[TWSE MCP] Persistent connection failed: {e}")
                traceback.print_exc()

        # 2. 連線 TA-Lib (Stdio)
        if self.talib_command:
            try:
                print(f"[TA-Lib MCP] Initializing persistent process => {self.talib_command}")
                server_params = StdioServerParameters(command=self.talib_command, args=self.talib_args, env=_make_subprocess_env())
                read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_client(server_params))
                session = await self._exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self._sessions["talib"] = session
            except Exception as e:
                print(f"[TA-Lib MCP] Persistent connection failed: {e}")

        # 3. 連線 yfinance (Stdio)
        if self.yfinance_command:
            try:
                print(f"[yfinance MCP] Initializing persistent process => {self.yfinance_command}")
                server_params = StdioServerParameters(command=self.yfinance_command, args=self.yfinance_args, env=_make_subprocess_env())
                read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_client(server_params))
                session = await self._exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self._sessions["yfinance"] = session
            except Exception as e:
                print(f"[yfinance MCP] Persistent connection failed: {e}")

        # 4. 連線 YOLOv9 (Stdio)
        if self.yolov9_command:
            try:
                print(f"[YOLOv9 MCP] Initializing persistent process => {self.yolov9_command} {self.yolov9_args}")
                server_params = StdioServerParameters(command=self.yolov9_command, args=self.yolov9_args, env=_make_subprocess_env())
                read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_client(server_params))
                session = await self._exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self._sessions["yolov9"] = session
            except Exception as e:
                print(f"[YOLOv9 MCP] Persistent connection failed: {e}")

        self._is_connected = True

    async def disconnect(self):
        """關閉所有持久化連線"""
        await self._exit_stack.aclose()
        self._sessions = {}
        self._is_connected = False
        print("[MultiMCPClient] All connections closed.")

    async def call_twse_named_tool(self, env_tool_name: str, tool_args: Dict[str, Any]) -> Any:
        tool_name = {
            "daily": self.twse_tool_daily,
            "valuation": self.twse_tool_valuation,
            "news": self.twse_tool_news,
        }.get(env_tool_name, "")

        if not tool_name:
            raise MCPClientError(f"TWSE tool name 未設定：{env_tool_name}")

        args = dict(tool_args)
        if "code" in args and tool_name in ["get_stock_daily_trading", "get_stock_valuation_ratios", "get_daily_day_trading_targets"]:
            args["stock_no"] = args.pop("code")

        return await self._call_twse_tool(tool_name, args)

    async def _call_twse_tool_once(self, url: str, tool_name: str, tool_args: Dict[str, Any]) -> Any:
        print(f"[TWSE MCP] [One-off] connect => {url}")
        async with streamable_http_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, tool_args)
                return self._extract_tool_result(result)

    async def _call_twse_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> Any:
        session = self._sessions.get("twse")
        if not session:
            # Fallback to one-off if not connected
            urls = self._twse_urls()
            if not urls: raise MCPClientError("TWSE_MCP_URL 未設定")
            return await self._call_twse_tool_once(urls[0], tool_name, tool_args)
        
        print(f"[TWSE MCP] [Persistent] tool => {tool_name}")
        result = await session.call_tool(tool_name, tool_args)
        return self._extract_tool_result(result)

    async def call_talib_tool(self, logical_name: str, tool_args: Dict[str, Any]) -> Any:
        """
        使用 talib_compute_indicator 統一介面呼叫 TA-Lib MCP。
        logical_name: rsi / macd / sma / stoch
        tool_args: fastapp.py 傳入的原始參數
        """
        # 對應 TA-Lib 指標名稱
        indicator_map = {
            "rsi": "RSI",
            "macd": "MACD",
            "sma": "SMA",
            "stoch": "STOCH",
        }
        indicator = indicator_map.get(logical_name, logical_name.upper())

        # 轉換參數格式為 talib_compute_indicator 所需格式
        # inputs: {"close": [...]} 或 {"high": [...], "low": [...], "close": [...]}
        # parameters: {"timeperiod": 14} 等
        inputs = {}
        parameters = {}

        if logical_name == "rsi":
            inputs = {"close": tool_args.get("real", [])}
            parameters = {"timeperiod": tool_args.get("timeperiod", 14)}

        elif logical_name == "macd":
            inputs = {"close": tool_args.get("real", [])}
            parameters = {
                "fastperiod": tool_args.get("fastperiod", 12),
                "slowperiod": tool_args.get("slowperiod", 26),
                "signalperiod": tool_args.get("signalperiod", 9),
            }

        elif logical_name == "sma":
            inputs = {"close": tool_args.get("real", [])}
            parameters = {"timeperiod": tool_args.get("timeperiod", 5)}

        elif logical_name == "stoch":
            inputs = {
                "high": tool_args.get("high", []),
                "low": tool_args.get("low", []),
                "close": tool_args.get("close", []),
            }
            parameters = {}

        talib_args = {
            "indicator": indicator,
            "inputs": inputs,
            "parameters": parameters,
        }

        print(f"[TA-Lib MCP] logical => {logical_name}, indicator => {indicator}")

        return await self._call_stdio_tool(
            command=self.talib_command,
            args=self.talib_args,
            tool_name="talib_compute_indicator",
            tool_args=talib_args,
            server_name="TA-Lib MCP",
        )

    async def call_talib_cdl_pattern(self, pattern_name: str, ohlc: Dict[str, Any]) -> Any:
        """
        通過 TA-Lib MCP 呼叫單一蠟燭型態辨識
        pattern_name: 'CDLHAMMER', 'CDLENGULFING', 'CDLMORNINGSTAR' 等
        ohlc: {open: [...], high: [...], low: [...], close: [...]}
        """
        talib_args = {
            "indicator": pattern_name,
            "inputs": {
                "open":  ohlc.get("open",  []),
                "high":  ohlc.get("high",  []),
                "low":   ohlc.get("low",   []),
                "close": ohlc.get("close", []),
            },
            "parameters": {}
        }
        print(f"[TA-Lib MCP CDL] indicator => {pattern_name}")
        return await self._call_stdio_tool(
            command=self.talib_command,
            args=self.talib_args,
            tool_name="talib_compute_indicator",
            tool_args=talib_args,
            server_name="TA-Lib MCP",
        )


    async def call_yfinance_tool(self, logical_name: str, tool_args: Dict[str, Any]) -> Any:
        tool_name = {
            "price": self.yfinance_tool_price,
            "history": self.yfinance_tool_history,
            "news": self.yfinance_tool_news,
        }.get(logical_name, logical_name)

        return await self._call_stdio_tool(
            command=self.yfinance_command,
            args=self.yfinance_args,
            tool_name=tool_name,
            tool_args=tool_args,
            server_name="yfinance MCP",
        )

    async def call_yolov9_detect(self, image_path: str, conf_threshold: float = 0.25, iou_threshold: float = 0.45) -> Any:
        """
        呼叫 YOLOv9 MCP 進行 K 線圖型態識別
        """
        return await self._call_stdio_tool(
            command=self.yolov9_command,
            args=self.yolov9_args,
            tool_name="detect_objects",
            tool_args={
                "image_path": image_path,
                "conf_threshold": conf_threshold,
                "iou_threshold": iou_threshold
            },
            server_name="WongKinYiu-YOLOv9-Server"
        )


    async def _call_stdio_tool(
        self,
        command: str,
        args: list[str],
        tool_name: str,
        tool_args: Dict[str, Any],
        server_name: str,
    ) -> Any:
        # 優先尋找持久化 Session（依 server_name 精準判定，避免 command 同為 python.exe 時發生鍵值碰撞）
        session_key = None
        if "YOLO" in server_name:
            session_key = "yolov9"
        elif "TA-Lib" in server_name:
            session_key = "talib"
        elif "yfinance" in server_name:
            session_key = "yfinance"
        session = self._sessions.get(session_key) if session_key else None

        if session:
            print(f"[{server_name}] [Persistent] tool => {tool_name}")
            result = await session.call_tool(tool_name, tool_args)
            return self._extract_tool_result(result)

        # Fallback to one-off
        if not command:
            raise MCPClientError(f"{server_name} command 未設定")

        try:
            print(f"[{server_name}] [One-off] tool => {tool_name}")
            server_params = StdioServerParameters(command=command, args=args, env=_make_subprocess_env())
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, tool_args)
                    return self._extract_tool_result(result)
        except Exception as e:
            traceback.print_exc()
            raise MCPClientError(
                f"{server_name} 呼叫失敗: {type(e).__name__}: {e} | tool={tool_name}"
            ) from e

    async def fetch_fugle_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        獲取即時報價：優先 Fugle API (若有 token)，若無或失敗則自動切換至 TWSE MIS 官方盤中行情與 yfinance。
        """
        clean_symbol = str(symbol).strip().upper()
        if clean_symbol.endswith(".TW"):
            clean_symbol = clean_symbol[:-3]
        elif clean_symbol.endswith(".TWO"):
            clean_symbol = clean_symbol[:-4]

        # 1. 嘗試 Fugle API (若有 token)
        if self.fugle_token:
            url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{clean_symbol}"
            headers = {"X-API-KEY": self.fugle_token}
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        total = data.get("total") or {}
                        bids = data.get("bids") or []
                        asks = data.get("asks") or []
                        last_price = data.get("lastPrice") or data.get("closePrice")
                        prev_close = data.get("previousClose") or data.get("referencePrice")
                        
                        return {
                            "name": data.get("name"),
                            "symbol": clean_symbol,
                            "lastPrice": float(last_price) if last_price is not None else None,
                            "price": float(last_price) if last_price is not None else None,
                            "change": float(data.get("change")) if data.get("change") is not None else 0.0,
                            "changePercent": float(data.get("changePercent")) if data.get("changePercent") is not None else 0.0,
                            "open": float(data.get("openPrice")) if data.get("openPrice") is not None else None,
                            "high": float(data.get("highPrice")) if data.get("highPrice") is not None else None,
                            "low": float(data.get("lowPrice")) if data.get("lowPrice") is not None else None,
                            "prevClose": float(prev_close) if prev_close is not None else None,
                            "volume": int(total.get("tradeVolume", 0)),
                            "buy": float(bids[0]["price"]) if bids and bids[0].get("price") is not None else None,
                            "sell": float(asks[0]["price"]) if asks and asks[0].get("price") is not None else None,
                            "isRealtime": True
                        }
            except Exception as e:
                logger.debug(f"[Fugle API] Request failed: {e}")

        # 2. 自動切換 TWSE MIS 官方盤中行情
        mis_quotes = await self.fetch_realtime_quotes_batch([clean_symbol])
        if clean_symbol in mis_quotes:
            return mis_quotes[clean_symbol]

        # 3. 備援：yfinance fast_info
        try:
            import yfinance as yf
            for sym in [f"{clean_symbol}.TW", f"{clean_symbol}.TWO"]:
                t = yf.Ticker(sym)
                fi = t.fast_info
                lp = getattr(fi, "last_price", None)
                if lp is not None and lp > 0:
                    pc = getattr(fi, "previous_close", None)
                    chg = round(float(lp) - float(pc), 2) if pc else 0.0
                    chg_pct = round((chg / float(pc)) * 100, 2) if pc else 0.0
                    vol = getattr(fi, "last_volume", 0) or 0
                    # 統一以「股數 (shares)」為標準口徑，供前端 formatStockColumnCell 統一除以 1000 顯示張數
                    vol_shares = int(vol)
                    return {
                        "name": clean_symbol,
                        "symbol": clean_symbol,
                        "lastPrice": round(float(lp), 2),
                        "price": round(float(lp), 2),
                        "change": chg,
                        "changePercent": chg_pct,
                        "open": round(float(getattr(fi, "open", lp)), 2),
                        "high": round(float(getattr(fi, "day_high", lp)), 2),
                        "low": round(float(getattr(fi, "day_low", lp)), 2),
                        "volume": vol_shares,
                        "prevClose": round(float(pc), 2) if pc else None,
                        "buy": round(float(lp) - 0.05, 2),
                        "sell": round(float(lp) + 0.05, 2),
                        "isRealtime": True
                    }
        except Exception as e:
            logger.debug(f"[yfinance quote] Failed for {clean_symbol}: {e}")

        return None

    async def fetch_realtime_quotes_batch(self, symbols: list[str]) -> Dict[str, Dict[str, Any]]:
        """
        批次從 TWSE MIS 系統獲取即時盤中報價（支援上市 TSE 與上櫃 OTC，單次請求毫秒級回應）
        """
        if not symbols:
            return {}

        import re
        clean_codes = []
        for s in symbols:
            c = re.sub(r"\D", "", str(s)).strip()
            if c:
                clean_codes.append(c)

        if not clean_codes:
            return {}

        ex_chs = []
        for c in clean_codes:
            ex_chs.append(f"tse_{c}.tw")
            ex_chs.append(f"otc_{c}.tw")

        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" + "|".join(ex_chs) + "&json=1&delay=0"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res: Dict[str, Dict[str, Any]] = {}

        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("msgArray", []):
                        c = item.get("c")
                        if not c:
                            continue
                        z = item.get("z")
                        y = item.get("y")
                        prev_close = float(y) if y and y != "-" else None

                        last_price = None
                        if z and z != "-":
                            try: last_price = float(z)
                            except Exception: pass
                        elif item.get("a") and item.get("a") != "-":
                            try: last_price = float(item.get("a").split("_")[0])
                            except Exception: pass
                        elif item.get("b") and item.get("b") != "-":
                            try: last_price = float(item.get("b").split("_")[0])
                            except Exception: pass
                        elif prev_close is not None:
                            last_price = prev_close

                        if last_price is None:
                            continue

                        change = round(last_price - prev_close, 2) if prev_close else 0.0
                        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

                        v = item.get("v", "0")
                        vol = int(v) if v and v != "-" else 0
                        # TWSE MIS 的 v 欄位是「累積成交量 (張數)」，
                        # 系統內部資料模型（如 daily_price、/api/stocks）統一使用「股數 (shares)」，
                        # 前端 formatStockColumnCell 渲染成交量(張) 時會除以 1000，
                        # 因此將張數乘上 1000 轉為股數，確保自選清單與台股總覽口徑完全一致。
                        vol_shares = vol * 1000

                        o = float(item.get("o")) if item.get("o") and item.get("o") != "-" else last_price
                        h = float(item.get("h")) if item.get("h") and item.get("h") != "-" else last_price
                        l = float(item.get("l")) if item.get("l") and item.get("l") != "-" else last_price

                        buy_val = None
                        if item.get("b") and item.get("b") != "-":
                            try: buy_val = float(item.get("b").split("_")[0])
                            except Exception: pass

                        sell_val = None
                        if item.get("a") and item.get("a") != "-":
                            try: sell_val = float(item.get("a").split("_")[0])
                            except Exception: pass

                        res[c] = {
                            "name": item.get("n"),
                            "symbol": c,
                            "lastPrice": last_price,
                            "price": last_price,
                            "change": change,
                            "changePercent": change_pct,
                            "open": o,
                            "high": h,
                            "low": l,
                            "volume": vol_shares,
                            "prevClose": prev_close,
                            "buy": buy_val or (round(last_price - 0.05, 2)),
                            "sell": sell_val or (round(last_price + 0.05, 2)),
                            "isRealtime": True
                        }
        except Exception as e:
            logger.debug(f"[TWSE MIS batch] Request failed: {e}")

        return res

    def _extract_tool_result(self, result: Any) -> Any:
        if result is None:
            return None

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        content = getattr(result, "content", None)
        if content is not None:
            if len(content) == 1:
                item = content[0]
                text = getattr(item, "text", None)
                if text is not None:
                    return self._parse_text_payload(text)

            texts = []
            for item in content:
                text = getattr(item, "text", None)
                if text is not None:
                    texts.append(text)

            if texts:
                if len(texts) == 1:
                    return self._parse_text_payload(texts[0])
                return [self._parse_text_payload(t) for t in texts]

        return str(result)

    def _parse_text_payload(self, text: str) -> Any:
        text = (text or "").strip()
        if not text:
            return ""

        try:
            return json.loads(text)
        except Exception:
            return text