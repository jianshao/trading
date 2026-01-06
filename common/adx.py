from typing import Optional
import pandas as pd
import numpy as np
import backtrader as bt
import pandas as pd

def get_backtrader_indicators(data_df: pd.DataFrame, indicator_class, **kwargs) -> pd.DataFrame:
    """
    使用 backtrader 引擎来计算指定指标的值。

    Args:
        data_df (pd.DataFrame): 包含 OHLCV 数据的 DataFrame。
        indicator_class: 要计算的 backtrader 指标类 (例如, bt.indicators.ADX)。
        **kwargs: 传递给指标的参数 (例如, period=14)。

    Returns:
        pd.DataFrame: 包含指标线(lines)计算结果的 DataFrame。
    """
    class IndicatorCalculatorStrategy(bt.Strategy):
        def __init__(self):
            # 在策略中实例化指标
            self.indicator = indicator_class(**kwargs)
            
        def next(self):
            # 不需要任何交易逻辑
            pass
            
    cerebro = bt.Cerebro()
    data_feed = bt.feeds.PandasData(dataname=data_df)
    cerebro.adddata(data_feed)
    cerebro.addstrategy(IndicatorCalculatorStrategy)
    
    # 运行回测（实际上只是为了计算指标）
    results = cerebro.run()
    strat = results[0]
    
    # 从指标的 lines 中提取数据
    indicator_data = {}
    for i, line_name in enumerate(strat.indicator.lines.getlinealiases()):
        line_data = strat.indicator.lines[i].array
        indicator_data[line_name] = line_data
        
    return pd.DataFrame(indicator_data, index=data_df.index)

# --- 如何在你的 API 或分析脚本中使用 ---
if __name__ == '__main__':
    # 假设你已经通过 IBapi 获取了 historical_df
    # historical_df = await api.get_historical_data(...) 
    
    # 模拟获取的数据
    sample_data = { ... } # (使用之前的样本数据)
    dates = pd.date_range(start='2023-01-01', periods=100)
    historical_df = pd.DataFrame(...) # (创建样本DataFrame)

    # 使用 backtrader 计算 ADX
    adx_results_bt = get_backtrader_indicators(
        historical_df, 
        bt.indicators.ADX, 
        period=14
    )
    
    if adx_results_bt is not None:
        print("ADX calculated by backtrader engine:")
        # backtrader 的 ADX 指标包含 'adx', 'pdi' (+DI), 'mdi' (-DI)
        print(adx_results_bt.tail())
        
        # 获取最新的 ADX 值
        latest_adx_bt = adx_results_bt['adx'].iloc[-1]
        print(f"\nLatest ADX from backtrader: {latest_adx_bt:.2f}")

import pandas as pd
import talib # 需要安装 TA-Lib

def calculate_adx_with_talib(data_df: pd.DataFrame, period: int = 14) -> Optional[pd.DataFrame]:
    if not all(col in data_df.columns for col in ['High', 'Low', 'Close']):
        return None
        
    try:
        adx = talib.ADX(data_df['High'], data_df['Low'], data_df['Close'], timeperiod=period)
        plus_di = talib.PLUS_DI(data_df['High'], data_df['Low'], data_df['Close'], timeperiod=period)
        minus_di = talib.MINUS_DI(data_df['High'], data_df['Low'], data_df['Close'], timeperiod=period)
        
        return pd.DataFrame({
            f'ADX_{period}': adx,
            f'DMP_{period}': plus_di,
            f'DMN_{period}': minus_di
        }, index=data_df.index)
    except Exception as e:
        print(f"Error calculating ADX with TA-Lib: {e}")
        return None
