import os
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, BigInteger, UniqueConstraint, Index, Text, text, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, synonym

# 載入環境變數
load_dotenv()

# 資料庫連線字串，預設連線到 localhost:5432
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_db")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class StockInfo(Base):
    __tablename__ = "stock_info"

    code = Column(String(10), primary_key=True, index=True, comment="股票代號")
    name = Column(String(100), nullable=False, comment="股票名稱")
    market = Column(String(10), nullable=True, comment="市場別 (上市/上櫃)")
    industry = Column(String(50), nullable=True, comment="產業類別")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="最後更新時間")


class DailyPrice(Base):
    __tablename__ = "daily_price"

    # TimescaleDB 不需要自動遞增的 primary key，直接使用 (code, date) 作為唯一鍵
    code = Column(String(10), primary_key=True, index=True, comment="股票代號")
    date = Column(Date, primary_key=True, index=True, comment="交易日期")
    
    open = Column(Float, nullable=True, comment="開盤價")
    high = Column(Float, nullable=True, comment="最高價")
    low = Column(Float, nullable=True, comment="最低價")
    close = Column(Float, nullable=True, comment="收盤價")
    volume = Column(BigInteger, nullable=True, comment="成交量")

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 確保同一天同一檔股票只有一筆資料
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
    )



class User(Base):
    __tablename__ = "users"

    user_id       = Column("user_id", Integer, primary_key=True, autoincrement=True, comment="使用者ID")
    id            = synonym("user_id")
    username      = Column(String(50), nullable=False, unique=True, comment="使用者名稱")
    email         = Column(String(100), nullable=False, unique=True, comment="電子郵件")
    password_hash = Column(String(255), nullable=False, comment="加密密碼")
    name          = Column(String(100), default="", comment="真實姓名")
    phone         = Column(String(20), default="", comment="手機號碼")
    occupation    = Column(String(50), default="", comment="職業")
    is_admin      = Column(Boolean, default=False, comment="是否為管理員")
    created_at    = Column(DateTime, default=datetime.utcnow, comment="註冊時間")


class UserWatchlist(Base):
    __tablename__ = "user_watchlists"

    watchlist_id = Column("watchlist_id", Integer, primary_key=True, autoincrement=True, comment="自選清單ID")
    id           = synonym("watchlist_id")
    user_id      = Column(Integer, nullable=False, comment="使用者ID")
    name         = Column(String(50), nullable=False, comment="清單名稱")
    created_at   = Column(DateTime, default=datetime.utcnow, comment="建立時間")

    __table_args__ = (
        UniqueConstraint('user_id', 'name', name='uix_user_watchlist_name'),
    )


class Watchlist(Base):
    __tablename__ = "watchlist"

    item_id      = Column("item_id", Integer, primary_key=True, autoincrement=True, comment="自選項目ID")
    id           = synonym("item_id")
    user_id      = Column(Integer, nullable=False, comment="使用者ID")
    watchlist_id = Column(Integer, nullable=True, comment="清單ID")
    code         = Column(String(10), nullable=False, comment="股票代號")
    sort_order   = Column(Integer, default=0, nullable=True, comment="排序順序")
    added_at     = Column(DateTime, default=datetime.utcnow, comment="加入時間")

    __table_args__ = (
        UniqueConstraint('user_id', 'watchlist_id', 'code', name='uix_user_watchlist_code'),
    )



class WatchResult(Base):
    __tablename__ = "watch_results"

    result_id  = Column("result_id", Integer, primary_key=True, autoincrement=True, comment="掃描結果ID")
    id         = synonym("result_id")
    code       = Column(String(10), nullable=False, index=True, comment="股票代號")
    name       = Column(String(50), nullable=True, comment="股票名稱")
    price      = Column(Float, nullable=True, comment="掃描時股價")
    change     = Column(Float, nullable=True, comment="漲跌")
    bias       = Column(String(10), nullable=True, comment="偏多/偏空/中性")
    bullish    = Column(JSONB, nullable=True, comment="利多訊號列表")
    bearish    = Column(JSONB, nullable=True, comment="利空訊號列表")
    news_sentiment = Column(String(10), nullable=True, comment="消息面情緒")
    ai_summary = Column(Text, nullable=True, comment="LLM 研判文字")
    inst_data  = Column(JSONB, nullable=True, comment="籌碼面數據")
    scanned_at = Column(DateTime, default=datetime.utcnow, comment="掃描時間")



class UserRiskProfile(Base):
    """儲存使用者風險屬性評估問卷結果"""
    __tablename__ = "user_risk_profiles"

    profile_id = Column("profile_id", Integer, primary_key=True, autoincrement=True, comment="風險評估記錄ID")
    id         = synonym("profile_id")
    user_id    = Column(Integer, nullable=False, unique=True, index=True, comment="使用者 ID")
    score      = Column(Integer, nullable=False, comment="問卷總得分")
    profile    = Column(String(10), nullable=False, comment="風險屬性（保守型/穩健型/積極型）")
    answers    = Column(JSONB, nullable=True, comment="各題答案 JSON")
    taken_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="測驗時間")


def init_db():
    """初始化資料庫並設定 TimescaleDB"""
    print("正在建立資料表...")
    Base.metadata.create_all(bind=engine)
    
    # 嘗試建立 TimescaleDB 擴充套件與 Hypertable (如果沒有裝 TimescaleDB，就使用原生 PostgreSQL)
    with engine.begin() as conn:
        try:
            print("嘗試啟用 TimescaleDB extension...")
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"))
            
            # 檢查是否已經是 hypertable
            check_query = text("""
                SELECT count(*) 
                FROM timescaledb_information.hypertables 
                WHERE hypertable_name = 'daily_price';
            """)
            res = conn.execute(check_query).scalar()
            
            if res == 0:
                print("將 daily_price 轉換為 Hypertable...")
                conn.execute(text("SELECT create_hypertable('daily_price', 'date', migrate_data => true);"))
            else:
                print("daily_price 已經是 Hypertable。")
        except Exception as e:
            print("[Warning] 未偵測到 TimescaleDB 擴充套件，將使用標準 PostgreSQL 資料表格式。")
            print("對於 500 萬筆以內的股票資料，原生 PostgreSQL 效能已經非常足夠！")
            
    print("資料庫初始化完成！")

if __name__ == "__main__":
    init_db()
