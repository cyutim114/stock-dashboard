"""計算股票Beta係數的模組"""

import numpy as np
import pandas as pd
import yfinance as yf
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta


class BetaCalculator:
    """計算Beta係數"""

    def __init__(self, market_index: str = "^TWII", period: str = "1y"):
        """
        初始化Beta計算器
        
        Args:
            market_index: 市場指數代號，預設為台灣加權指數 ^TWII
            period: 計算期間，預設為1年
        """
        self.market_index = market_index
        self.period = period
        self.market_data = None
        self.market_returns = None
        self.load_market_data()

    def load_market_data(self):
        """載入市場指數數據"""
        try:
            index_data = yf.download(
                self.market_index,
                period=self.period,
                progress=False
            )
            if index_data.empty:
                raise ValueError(f"無法獲取 {self.market_index} 的數據")
            
            # 存儲原始數據用於對齐
            self.market_data = index_data
            
            # 處理不同的數據格式
            if isinstance(index_data, pd.DataFrame):
                if 'Adj Close' in index_data.columns:
                    close_prices = index_data['Adj Close']
                elif 'Close' in index_data.columns:
                    close_prices = index_data['Close']
                else:
                    raise ValueError(f"無法找到價格列，可用列: {list(index_data.columns)}")
            else:
                close_prices = index_data
            
            # 計算日報酬率
            self.market_returns = close_prices.pct_change().dropna()
        except Exception as e:
            print(f"載入市場數據失敗: {e}")
            self.market_returns = None

    def calculate_beta(self, stock_code: str) -> Optional[float]:
        """
        計算單支股票的Beta係數
        
        Formula: β = cov(r_stock, r_market) / var(r_market)
        
        Args:
            stock_code: 股票代號（yfinance格式，例如 "2330.TW"）
        
        Returns:
            Beta係數，如果計算失敗則返回None
        """
        if self.market_returns is None or len(self.market_returns) < 30:
            return None

        try:
            # 下載股票數據
            stock_data = yf.download(
                stock_code,
                period=self.period,
                progress=False
            )
            
            if stock_data.empty:
                return None
            
            # 處理不同的數據格式
            if isinstance(stock_data, pd.DataFrame):
                if 'Adj Close' in stock_data.columns:
                    close_prices = stock_data['Adj Close']
                elif 'Close' in stock_data.columns:
                    close_prices = stock_data['Close']
                else:
                    return None
            else:
                close_prices = stock_data
            
            # 計算日報酬率
            stock_returns = close_prices.pct_change().dropna()
            
            # 對齐時間序列
            common_dates = self.market_returns.index.intersection(stock_returns.index)
            if len(common_dates) < 30:
                return None
            
            # 使用reindex來確保對齐
            market_ret = self.market_returns.reindex(common_dates)
            stock_ret = stock_returns.reindex(common_dates)
            
            # 移除任何NaN值
            mask = ~(np.isnan(market_ret) | np.isnan(stock_ret))
            market_ret = market_ret[mask]
            stock_ret = stock_ret[mask]
            
            if len(market_ret) < 30:
                return None
            
            # 轉換為NumPy數組以便計算
            market_arr = np.asarray(market_ret).flatten()
            stock_arr = np.asarray(stock_ret).flatten()
            
            # 使用NumPy的polyfit計算Beta（線性回歸）
            # polyfit返回係數，最高度優先
            # 對於1次多項式，polyfit返回[slope, intercept]
            coefficients = np.polyfit(market_arr, stock_arr, 1)
            slope = coefficients[0]
            
            return float(slope) if not np.isnan(slope) else None
        
        except Exception as e:
            print(f"計算 {stock_code} 的Beta失敗: {e}")
            return None

    def calculate_batch_beta_and_prices(self, stock_codes: List[str]) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[float]]]:
        """
        批量計算多支股票的Beta係數與最新價格 (優化版)
        
        Args:
            stock_codes: 股票代號列表 (yfinance格式，例如 "2330.TW")
            
        Returns:
            Tuple[betas_dict, prices_dict]
        """
        betas = {}
        prices = {}
        
        if self.market_returns is None or len(self.market_returns) < 30:
            return {code: None for code in stock_codes}, {code: None for code in stock_codes}
            
        try:
            # 一次性下載所有股票的歷史數據
            stock_data = yf.download(
                stock_codes,
                period=self.period,
                progress=False,
                group_by='ticker'
            )
            
            if stock_data.empty:
                return {code: None for code in stock_codes}, {code: None for code in stock_codes}
                
            for code in stock_codes:
                try:
                    # 獲取該股票的數據
                    if len(stock_codes) == 1:
                        ticker_df = stock_data
                    else:
                        if code in stock_data.columns.levels[0]:
                            ticker_df = stock_data[code]
                        else:
                            betas[code] = None
                            prices[code] = None
                            continue
                    
                    # 提取價格列
                    close_col = 'Adj Close' if 'Adj Close' in ticker_df.columns else 'Close'
                    prices_series = ticker_df[close_col]
                    if isinstance(prices_series, pd.DataFrame):
                        prices_series = prices_series.iloc[:, 0]
                    prices_series = prices_series.dropna()
                    
                    # 獲取最新價格
                    if not prices_series.empty:
                        prices[code] = float(prices_series.iloc[-1])
                    else:
                        prices[code] = None
                        
                    # 計算日報酬率
                    stock_returns = prices_series.pct_change().dropna()
                    
                    # 對齐時間序列
                    common_dates = self.market_returns.index.intersection(stock_returns.index)
                    if len(common_dates) < 30:
                        betas[code] = None
                        continue
                        
                    market_ret = self.market_returns.reindex(common_dates)
                    stock_ret = stock_returns.reindex(common_dates)
                    
                    m_arr = np.asarray(market_ret).flatten()
                    s_arr = np.asarray(stock_ret).flatten()
                    
                    mask = ~(np.isnan(m_arr) | np.isnan(s_arr))
                    m_arr = m_arr[mask]
                    s_arr = s_arr[mask]
                    
                    if len(m_arr) < 30:
                        betas[code] = None
                        continue
                        
                    coefficients = np.polyfit(m_arr, s_arr, 1)
                    slope = coefficients[0]
                    
                    betas[code] = float(slope) if not np.isnan(slope) else None
                except Exception as e:
                    print(f"計算 {code} 失敗: {e}")
                    betas[code] = None
                    prices[code] = None
        except Exception as e:
            print(f"批量下載數據失敗: {e}")
            for code in stock_codes:
                betas[code] = None
                prices[code] = None
                
        return betas, prices

    def calculate_batch_beta(self, stock_codes: List[str]) -> Dict[str, Optional[float]]:
        """
        批量計算多支股票的Beta係數 (已優化為單次下載)
        
        Args:
            stock_codes: 股票代號列表
        
        Returns:
            {股票代號: Beta係數} 的字典
        """
        betas, _ = self.calculate_batch_beta_and_prices(stock_codes)
        return betas

    def classify_stocks(
        self,
        stock_betas: Dict[str, Optional[float]],
        tolerance: float = 0.001
    ) -> Dict[str, List[str]]:
        """
        根據Beta係數分類股票
        
        Args:
            stock_betas: {股票代號: Beta係數} 的字典
            tolerance: 對於Beta=1的容差（預設±0.001，僅包含0.999-1.001）
        
        Returns:
            {分類: [股票代號列表]} 的字典
        """
        conservative = []  # Beta < 1（低波動 / 保守型，含 Beta < 0）
        balanced = []      # Beta ≈ 1（穩健型）
        aggressive = []    # Beta > 1（積極型）

        for code, beta in stock_betas.items():
            if beta is None:
                continue
            
            if beta < 1:
                conservative.append((code, beta))
            elif abs(beta - 1) <= tolerance:
                balanced.append((code, beta))
            else:
                aggressive.append((code, beta))

        # 按Beta值排序
        conservative.sort(key=lambda x: x[1])
        balanced.sort(key=lambda x: x[1])
        aggressive.sort(key=lambda x: x[1], reverse=True)

        return {
            "conservative": [
                {"code": code, "beta": round(beta, 3)} 
                for code, beta in conservative
            ],
            "balanced": [
                {"code": code, "beta": round(beta, 3)} 
                for code, beta in balanced
            ],
            "aggressive": [
                {"code": code, "beta": round(beta, 3)} 
                for code, beta in aggressive
            ]
        }


def get_recommended_stocks(
    risk_profile: str,
    stock_list: List[str],
    count: int = 5
) -> List[Dict]:
    """
    根據風險屬性推薦股票
    
    Args:
        risk_profile: 風險屬性 ("保守型", "穩健型", "積極型")
        stock_list: 股票代號列表
        count: 推薦數量
    
    Returns:
        推薦股票列表
    """
    calculator = BetaCalculator()
    
    # 轉換股票代號格式
    tw_stocks = [f"{code}.TW" for code in stock_list]
    
    # 計算所有股票的Beta
    betas = calculator.calculate_batch_beta(tw_stocks)
    
    # 分類股票
    classified = calculator.classify_stocks(betas)
    
    # 根據風險屬性推薦
    if risk_profile == "保守型":
        recommendations = classified["conservative"]
    elif risk_profile == "穩健型":
        recommendations = classified["balanced"]
    else:  # 積極型
        recommendations = classified["aggressive"]
    
    return recommendations[:count]
