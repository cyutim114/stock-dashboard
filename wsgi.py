import sys
from pathlib import Path
from dotenv import load_dotenv

# 1. 載入 .env 環境變數
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# 2. 匯入 Flask 實例與初始化函式 (已移除不存在的 create_watchbot_tables)
from fastapp import (
    app, 
    start_background_loop, 
    _watchbot_available, 
    global_mcp_client, 
    run_async
)

# 3. 執行原先在 __main__ 裡的背景初始化邏輯
start_background_loop()

# 4. Waitress 啟動進入點
if __name__ == "__main__":
    from waitress import serve
    print("股Friend (Waitress) 正在啟動，接聽連接埠 5001...")
    serve(app, host="127.0.0.1", port=5001, threads=4)