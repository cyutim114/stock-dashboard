import os
import re
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIError, AuthenticationError, BadRequestError

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gemini-1.5-flash")

TRADITIONAL_CHINESE_RULE = (
    "所有回覆一律使用繁體中文。"
    "禁止使用簡體中文。"
    "請使用台灣常用的繁體中文詞彙與語氣。"
    "若輸出含有任何簡體中文，請先自行改寫為繁體中文後再輸出。"
    "只輸出最終回答，不要輸出思考過程、推理過程、分析過程或 <think> 標籤。"
    "請使用純文字自然段落回覆，嚴格禁止輸出 Markdown 粗體符號（如 ** 或 *）、井字標題（如 ##）等符號，直接以清晰文字呈現標題與內文。"
    "【極致精簡與核心重點】：回答務必精簡扼要、直指核心，只回答最關鍵且最重要的核心結論與重要數據，嚴禁長篇大論、客套寒暄、贅字或無意義鋪陳，讓使用者一眼就能掌握核心重點。"
)


def sanitize_llm_text(text: str) -> str:
    if not text:
        return ""
    # 1. 移除成對的 <think>...</think> 標籤與內部思考內容
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 2. 移除未閉合的 <think> 到結尾或換行的殘留內容 (模型流式或截斷時的思考內容)
    text = re.sub(r"<think>[\s\S]*?(?=\n\n[^\n]|\Z)", "", text, flags=re.IGNORECASE)
    # 3. 移除殘留的任何 <think> 或 </think> 單獨標籤
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    # 4. 移除 Markdown 粗體/斜體星號（例如 **文字** 或 *文字*）
    text = re.sub(r"\*{1,3}", "", text)
    # 5. 移除標題開頭的井字號（例如 ## 標題 -> 標題）
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    # 6. 移除行首標記如 - 或 >
    text = re.sub(r"(?m)^>\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def get_llm_client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY 沒有設定，請檢查 .env")
    return OpenAI(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL,
    )


def get_model_name() -> str:
    return OPENAI_MODEL


def convert_combined_tools_to_openai_tools(
    combined_tools: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    tools = []
    for tool in combined_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["prefixed_name"],
                    "description": f"[{tool['server'].upper()}] {tool.get('description', '')}",
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
        )
    return tools


# 只列區 Google Gemini 真實存在且支援圖片輸入的模型
FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash-8b",
]


def ask_llm_with_fallback(messages: list, temperature: float = 0.3, max_tokens: int | None = None) -> str:
    """自動在遭遇 429 配額不足時切換備用模型進行呼叫"""
    client = get_llm_client()
    preferred_model = get_model_name()
    candidates = [preferred_model] + [m for m in FALLBACK_MODELS if m != preferred_model]
    
    last_error = None
    for model in candidates:
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            return sanitize_llm_text(content)
        except RateLimitError as rle:
            last_error = rle
            continue
        except Exception as ex:
            if "429" in str(ex) or "RESOURCE_EXHAUSTED" in str(ex) or "Quota exceeded" in str(ex):
                last_error = ex
                continue
            raise ex
            
    if last_error:
        raise last_error
    return "模型無回應。"


def ask_llm_with_fallback_vision(messages: list, temperature: float = 0.2) -> str:
    """支援圖片輸入的 fallback，自動在遇到 429 時切換備用模型"""
    client = get_llm_client()
    preferred_model = get_model_name()
    candidates = [preferred_model] + [m for m in FALLBACK_MODELS if m != preferred_model]

    last_error = None
    for model in candidates:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            content = response.choices[0].message.content or ""
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
            return content
        except RateLimitError as rle:
            last_error = rle
            continue
        except Exception as ex:
            if "429" in str(ex) or "RESOURCE_EXHAUSTED" in str(ex) or "Quota exceeded" in str(ex):
                last_error = ex
                continue
            raise ex

    if last_error:
        raise last_error
    return "{}"


def ask_llm_text(
    user_prompt: str,
    system_prompt: str = "",
    temperature: float = 0.3,
    model: str | None = None,
) -> Dict[str, Any]:
    try:
        final_system_prompt = (
            (system_prompt or "")
            + TRADITIONAL_CHINESE_RULE
        )
        messages = [
            {"role": "system", "content": final_system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        content = ask_llm_with_fallback(messages, temperature=temperature)
        return {
            "ok": True,
            "content": content,
        }

    except RateLimitError as e:
        return {"ok": False, "content": f"配額不足：{e}"}
    except AuthenticationError as e:
        return {"ok": False, "content": f"驗證失敗：{e}"}
    except APIError as e:
        return {"ok": False, "content": f"API 錯誤：{e}"}
    except Exception as e:
        return {"ok": False, "content": f"LLM 呼叫失敗：{type(e).__name__}: {e}"}


def ask_llm_with_tools(
    user_query: str,
    openai_tools: List[Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        client = get_llm_client()

        system_prompt = (
            "你是一個台灣股市助理。"
            "TWSE 工具負責取得台股原始資料，例如股價、財報、公告、大盤資訊。"
            "yfinance 工具負責取得歷史 OHLC 與延伸市場資料。"
            "TA-Lib 工具負責計算技術指標，例如 RSI、MACD、KD、MA。"
            + TRADITIONAL_CHINESE_RULE
            + "若需要即時或查詢型資料，不可自行猜測。"
        )

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query},
            ],
            tools=openai_tools,
            tool_choice="auto",
            temperature=0.2,
        )

        return {"ok": True, "mode": "tools", "message": response.choices[0].message}

    except BadRequestError:
        try:
            client = get_llm_client()

            fallback_prompt = (
                "你是一個台灣股市助理。"
                + TRADITIONAL_CHINESE_RULE
                + "如果缺少即時資料，請明確說明需要查詢工具。"
            )

            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": fallback_prompt},
                    {"role": "user", "content": user_query},
                ],
                temperature=0.2,
            )

            return {"ok": True, "mode": "text_only", "message": response.choices[0].message}
        except Exception as e:
            return {
                "ok": False,
                "error_message": f"LLM fallback 失敗：{type(e).__name__}: {e}",
            }

    except RateLimitError as e:
        return {"ok": False, "error_message": f"配額不足：{e}"}
    except AuthenticationError as e:
        return {"ok": False, "error_message": f"驗證失敗：{e}"}
    except APIError as e:
        return {"ok": False, "error_message": f"API 錯誤：{e}"}
    except Exception as e:
        return {"ok": False, "error_message": f"LLM 呼叫失敗：{type(e).__name__}: {e}"}


def ask_llm_final(
    user_query: str,
    assistant_tool_calls: list,
    tool_messages: list,
) -> Dict[str, Any]:
    try:
        client = get_llm_client()

        system_prompt = (
            "你是一個台灣股市助理。"
            "請根據工具查詢結果，以精簡扼要的方式整理成清楚易懂的重點回答，只保留關鍵數據與重要結論。"
            + TRADITIONAL_CHINESE_RULE
            + "如果資料不足，請直接說明。"
            "不要編造未查到的資訊。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": "", "tool_calls": assistant_tool_calls},
        ]
        messages.extend(tool_messages)

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
        )

        content = response.choices[0].message.content or "模型沒有回傳內容。"
        return {
            "ok": True,
            "content": sanitize_llm_text(content),
        }

    except Exception as e:
        joined = "\n\n".join([m["content"] for m in tool_messages])
        return {
            "ok": False,
            "content": sanitize_llm_text(
                f"[LLM 最終整理失敗]\n\n{joined}\n\n錯誤：{type(e).__name__}: {e}"
            ),
        }