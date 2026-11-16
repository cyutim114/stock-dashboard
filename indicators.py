"""
技術指標計算模塊 - Technical Indicators Module
支持價格型、量能型、價量結合型指標
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any


class TechnicalIndicators:
    """技術指標計算類"""
    
    @staticmethod
    def calculate_ma(data: pd.Series, period: int) -> pd.Series:
        """
        計算移動平均線 (Moving Average)
        :param data: 價格數據
        :param period: 周期
        :return: MA 值
        """
        return data.rolling(window=period).mean()
    
    @staticmethod
    def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, pd.Series]:
        """
        計算 MACD (Moving Average Convergence Divergence)
        :param data: 價格數據
        :param fast: 快速 EMA 周期
        :param slow: 慢速 EMA 周期
        :param signal: 信號線 EMA 周期
        :return: MACD、信號線、柱狀圖
        """
        ema_fast = data.ewm(span=fast).mean()
        ema_slow = data.ewm(span=slow).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal).mean()
        histogram = macd - signal_line
        
        return {
            'macd': macd,
            'signal': signal_line,
            'histogram': histogram
        }
    
    @staticmethod
    def calculate_rsi(data: pd.Series, period: int = 14) -> pd.Series:
        """
        計算 RSI (Relative Strength Index)
        :param data: 價格數據
        :param period: 周期
        :return: RSI 值 (0-100)
        """
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_kd(data: pd.DataFrame, period: int = 14) -> Dict[str, pd.Series]:
        """
        計算 KD 指標 (隨機指標 Stochastic Oscillator)
        :param data: DataFrame 包含 'High', 'Low', 'Close'
        :param period: 周期
        :return: K 值和 D 值
        """
        lowest_low = data['Low'].rolling(window=period).min()
        highest_high = data['High'].rolling(window=period).max()
        
        # 計算 RSV (Raw Stochastic Value)
        rsv = 100 * (data['Close'] - lowest_low) / (highest_high - lowest_low)
        rsv = rsv.fillna(0)
        
        # 計算 K 值（RSV的3日平滑）
        k = rsv.rolling(window=3).mean()
        
        # 計算 D 值（K值的3日平滑）
        d = k.rolling(window=3).mean()
        
        return {
            'k': k,
            'd': d
        }
    
    @staticmethod
    def calculate_bollinger_bands(data: pd.Series, period: int = 20, std_dev: float = 2.0) -> Dict[str, pd.Series]:
        """
        計算布林通道 (Bollinger Bands)
        :param data: 價格數據
        :param period: 周期
        :param std_dev: 標準差倍數
        :return: 上軌、中軌、下軌
        """
        sma = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        
        upper_band = sma + (std_dev * std)
        lower_band = sma - (std_dev * std)
        
        return {
            'upper': upper_band,
            'middle': sma,
            'lower': lower_band
        }
    
    @staticmethod
    def calculate_bias(data: pd.Series, period: int = 20) -> pd.Series:
        """
        計算 BIAS (乖離率)
        BIAS = (當日收盤價 - N日均線) / N日均線 * 100%
        :param data: 價格數據
        :param period: 周期
        :return: BIAS 值
        """
        ma = data.rolling(window=period).mean()
        bias = ((data - ma) / ma) * 100
        
        return bias
    
    @staticmethod
    def calculate_arbr(data: pd.DataFrame, period: int = 26) -> Dict[str, pd.Series]:
        """
        計算 ARBR (人氣指標和買賣意願指標)
        AR = (H - O) 之和 / (O - L) 之和 * 100
        BR = (H - CY) 之和 / (CY - L) 之和 * 100
        :param data: DataFrame 包含 'Open', 'High', 'Low', 'Close'
        :param period: 周期
        :return: AR 和 BR 指標
        """
        ho = data['High'] - data['Open']
        ol = data['Open'] - data['Low']
        
        ar = (ho.rolling(window=period).sum() / ol.rolling(window=period).sum()) * 100
        ar = ar.replace([np.inf, -np.inf], 0).fillna(0)
        
        # BR 計算
        hcy = data['High'] - data['Close'].shift(1)  # yesterdays close
        cyl = data['Close'].shift(1) - data['Low']
        
        br = (hcy.rolling(window=period).sum() / cyl.rolling(window=period).sum()) * 100
        br = br.replace([np.inf, -np.inf], 0).fillna(0)
        
        return {
            'ar': ar,
            'br': br
        }
    
    # 量能型指標
    
    @staticmethod
    def calculate_volume_ma(data: pd.Series, period: int = 20) -> pd.Series:
        """
        計算成交量均線 (Volume Moving Average)
        :param data: 成交量數據
        :param period: 周期
        :return: VMA 值
        """
        return data.rolling(window=period).mean()
    
    @staticmethod
    def calculate_volume_ratio(data: pd.Series, period: int = 5) -> pd.Series:
        """
        計算量比 (Volume Ratio)
        當日成交量 / 過去N天平均成交量
        :param data: 成交量數據
        :param period: 周期
        :return: 量比值
        """
        avg_volume = data.rolling(window=period).mean()
        volume_ratio = data / avg_volume
        
        return volume_ratio
    
    # 價量結合型指標
    
    @staticmethod
    def calculate_obv(data: pd.DataFrame) -> pd.Series:
        """
        計算 OBV (On-Balance Volume)
        :param data: DataFrame 包含 'Close' 和 'Volume'
        :return: OBV 值
        """
        obv = pd.Series(0, index=data.index)
        
        for i in range(1, len(data)):
            if data['Close'].iloc[i] > data['Close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] + data['Volume'].iloc[i]
            elif data['Close'].iloc[i] < data['Close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] - data['Volume'].iloc[i]
            else:
                obv.iloc[i] = obv.iloc[i-1]
        
        return obv
    
    @staticmethod
    def calculate_vwap(data: pd.DataFrame) -> pd.Series:
        """
        計算 VWAP (Volume Weighted Average Price)
        VWAP = Σ(P * V) / Σ(V)
        :param data: DataFrame 包含 'High', 'Low', 'Close', 'Volume'
        :return: VWAP 值
        """
        # 使用 High、Low、Close 的平均值作為價格
        tp = (data['High'] + data['Low'] + data['Close']) / 3
        
        # 計算累積 PV 和 V
        cumsum_pv = (tp * data['Volume']).cumsum()
        cumsum_v = data['Volume'].cumsum()
        
        vwap = cumsum_pv / cumsum_v
        
        return vwap
    
    @staticmethod
    def calculate_price_volume_divergence(data: pd.DataFrame, period: int = 20) -> Dict[str, Any]:
        """
        計算量價背離 (Price-Volume Divergence)
        檢查價格和成交量的趨勢是否相反
        :param data: DataFrame 包含 'Close' 和 'Volume'
        :param period: 周期
        :return: 背離信號
        """
        # 計算價格趨勢
        price_ma = data['Close'].rolling(window=period).mean()
        price_trend = data['Close'] - price_ma
        
        # 計算成交量趨勢
        volume_ma = data['Volume'].rolling(window=period).mean()
        volume_trend = data['Volume'] - volume_ma
        
        # 檢測背離（價格上升但成交量下降，或反之）
        divergence = pd.Series(0, index=data.index)
        
        for i in range(1, len(data)):
            # 價格上升但成交量下降 - 頂部背離
            if price_trend.iloc[i] > 0 and volume_trend.iloc[i] < 0:
                divergence.iloc[i] = -1  # 負背離
            # 價格下降但成交量上升 - 底部背離
            elif price_trend.iloc[i] < 0 and volume_trend.iloc[i] > 0:
                divergence.iloc[i] = 1   # 正背離
        
        return {
            'divergence': divergence,
            'price_trend': price_trend,
            'volume_trend': volume_trend
        }
    
    @staticmethod
    def calculate_all_indicators(data: pd.DataFrame) -> Dict[str, Any]:
        """
        計算所有技術指標
        :param data: DataFrame 包含 'Open', 'High', 'Low', 'Close', 'Volume'
        :return: 包含所有指標的字典
        """
        indicators = {}
        
        try:
            # 價格型指標
            indicators['ma_5'] = TechnicalIndicators.calculate_ma(data['Close'], 5)
            indicators['ma_10'] = TechnicalIndicators.calculate_ma(data['Close'], 10)
            indicators['ma_20'] = TechnicalIndicators.calculate_ma(data['Close'], 20)
            indicators['ma_50'] = TechnicalIndicators.calculate_ma(data['Close'], 50)
            
            macd = TechnicalIndicators.calculate_macd(data['Close'])
            indicators['macd'] = macd['macd']
            indicators['macd_signal'] = macd['signal']
            indicators['macd_histogram'] = macd['histogram']
            
            indicators['rsi'] = TechnicalIndicators.calculate_rsi(data['Close'], 14)
            
            kd = TechnicalIndicators.calculate_kd(data, 14)
            indicators['kd_k'] = kd['k']
            indicators['kd_d'] = kd['d']
            
            bb = TechnicalIndicators.calculate_bollinger_bands(data['Close'], 20, 2.0)
            indicators['bb_upper'] = bb['upper']
            indicators['bb_middle'] = bb['middle']
            indicators['bb_lower'] = bb['lower']
            
            indicators['bias'] = TechnicalIndicators.calculate_bias(data['Close'], 20)
            
            arbr = TechnicalIndicators.calculate_arbr(data, 26)
            indicators['ar'] = arbr['ar']
            indicators['br'] = arbr['br']
            
            # 量能型指標
            indicators['vma_5'] = TechnicalIndicators.calculate_volume_ma(data['Volume'], 5)
            indicators['vma_20'] = TechnicalIndicators.calculate_volume_ma(data['Volume'], 20)
            indicators['volume_ratio'] = TechnicalIndicators.calculate_volume_ratio(data['Volume'], 5)
            
            # 價量結合型指標
            indicators['obv'] = TechnicalIndicators.calculate_obv(data)
            indicators['vwap'] = TechnicalIndicators.calculate_vwap(data)
            
            pvd = TechnicalIndicators.calculate_price_volume_divergence(data, 20)
            indicators['price_volume_divergence'] = pvd['divergence']
            
        except Exception as e:
            print(f"Error calculating indicators: {e}")
        
        return indicators


def format_indicators_for_json(indicators: Dict[str, Any], data: pd.DataFrame) -> Dict[str, List[float]]:
    """
    將指標轉換為 JSON 格式
    :param indicators: 指標字典
    :param data: 原始數據
    :return: JSON 格式的指標
    """
    result = {}
    
    for key, value in indicators.items():
        if isinstance(value, pd.Series):
            # 只返回最後 100 行的數據
            result[key] = value.tail(100).values.tolist()
    
    return result
