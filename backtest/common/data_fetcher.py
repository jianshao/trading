
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import pandas as pd
import pytz
import datetime

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        
# 假设这些是你项目中已有的引用
from apis.ibkr import IBapi
from backtest.common.mockapi import MockApi

# IBKR API 的一些限制常数 (Bar Size -> 建议的最大 Duration)
# 格式: {BarSizeString: (DurationString, TimeDelta)}
# 注意：这些是经验值，用于避免 pacing violation 或请求过大
IB_LIMITS = {
    "1 sec":  ("1800 S", timedelta(seconds=1800)),
    "5 secs": ("3600 S", timedelta(seconds=3600)),
    "10 secs":("14400 S", timedelta(hours=4)),
    "15 secs":("14400 S", timedelta(hours=4)),
    "30 secs":("1 D", timedelta(days=1)),
    "1 min":  ("10 D", timedelta(days=10)), # 虽然可以更长，但分块获取更稳健
    "2 mins": ("20 D", timedelta(days=20)),
    "3 mins": ("1 M", timedelta(days=30)),
    "5 mins": ("1 M", timedelta(days=30)),
    "10 mins":("1 M", timedelta(days=30)),
    "15 mins":("1 M", timedelta(days=30)),
    "30 mins":("1 M", timedelta(days=30)),
    "1 hour": ("1 Y", timedelta(days=365)),
    "1 day":  ("1 Y", timedelta(days=365)),
}


# 默认限制
DEFAULT_LIMIT = ("1 M", timedelta(days=30))

class DataFetcher:
    def __init__(self, api: IBapi):
        # 这里包装 MockApi 或直接用 IBapi，取决于你的架构
        self.mock_api = MockApi(ib_api=api)
        
    async def fetch_historical_data(
        self, 
        symbol: str, 
        start_date: Union[str, datetime.datetime], 
        end_date: Union[str, datetime.datetime], 
        bar_size_setting: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = True, 
        timezone: str = "US/Eastern"
    ) -> pd.DataFrame:
        """
        通用历史数据获取函数，统一使用 UTC 格式以避免时区解析错误。
        """
        # 1. 初始化时区对象
        tz = pytz.timezone(timezone)
        
        # 统一处理 start_date
        if isinstance(start_date, str):
            start_dt = pd.to_datetime(start_date).tz_localize(None)
            start_dt = tz.localize(start_dt)
        else:
            start_dt = start_date if start_date.tzinfo else tz.localize(start_date)

        # 统一处理 end_date
        if isinstance(end_date, str):
            end_dt = pd.to_datetime(end_date).tz_localize(None)
            end_dt = tz.localize(end_dt)
        else:
            end_dt = end_date if end_date.tzinfo else tz.localize(end_date)
            
        # 2. 获取该 bar_size 对应的最佳请求时长
        duration_str, duration_delta = IB_LIMITS.get(bar_size_setting, DEFAULT_LIMIT)
        
        all_dfs = []
        current_end = end_dt
        
        print(f"Start fetching {symbol} from {start_dt} to {end_dt} using UTC format...")
        start_time_perf = time.time()

        # 3. 循环分页获取 (从后向前)
        while current_end > start_dt:
            # =========================================================
            # 修改核心：统一转换为 UTC 格式，移除 symbol == "VXN" 的判断
            # =========================================================
            
            # A. 确保 current_end 是带时区的
            if current_end.tzinfo is None:
                current_end = tz.localize(current_end)
            
            # B. 转换为 UTC 时间对象
            end_dt_utc = current_end.astimezone(pytz.utc)
            
            # C. 格式化为 IB 专用的 UTC 字符串格式 (中间是短横线，不带时区名)
            # 格式示例: "20230501-14:00:00"
            end_time_str = end_dt_utc.strftime("%Y%m%d-%H:%M:%S")
            
            # =========================================================

            # 调用 API
            try:
                chunk_df = await self.mock_api.get_historical_data(
                    symbol=symbol,
                    end_date_time=end_time_str, # 传入 UTC 字符串
                    duration_str=duration_str,
                    bar_size_setting=bar_size_setting,
                    what_to_show=what_to_show,
                    use_rth=use_rth
                )
            except Exception as e:
                print(f"Error fetching chunk ending {end_time_str}: {e}")
                chunk_df = pd.DataFrame()

            if not chunk_df.empty:
                # 假设返回的数据索引是 naive datetime (不带时区) 或者是 UTC
                # 我们需要将其标准化为用户请求的目标时区 (self.timezone) 以便后续处理
                
                # 如果索引没有时区，IB通常返回的是请求时区或UTC，这里假设我们需要将其对齐
                if chunk_df.index.tz is None:
                    # 注意：如果 IB 返回的是当地时间，这里直接 localize
                    # 如果 IB 在 UTC 模式下返回的是 UTC 时间，这里应该先 localize utc 再 convert
                    # 稳妥起见，假设 mock_api 处理后返回的是 naive local time
                    chunk_df.index = chunk_df.index.tz_localize(timezone)
                else:
                    # 如果已经有时区，转换为目标时区
                    chunk_df.index = chunk_df.index.tz_convert(timezone)
                
                # 截取有效区间
                chunk_df = chunk_df[chunk_df.index > start_dt]
                
                if not chunk_df.empty:
                    all_dfs.append(chunk_df)
                    
                    # 更新下一次循环的结束时间
                    min_date = chunk_df.index.min()
                    
                    # 逻辑保护：防止死循环
                    if min_date >= current_end:
                        current_end -= duration_delta
                    else:
                        current_end = min_date
                else:
                    break
            else:
                # 无数据，向前推进
                current_end -= duration_delta
            
            await asyncio.sleep(0.1)

        # 4. 合并数据
        if not all_dfs:
            print(f"No data found for {symbol}")
            return pd.DataFrame()

        final_df = pd.concat(all_dfs)
        final_df = final_df[~final_df.index.duplicated(keep='first')]
        final_df.sort_index(inplace=True)
        
        elapsed = time.time() - start_time_perf
        print(f"Fetch completed for {symbol}: {len(final_df)} bars in {elapsed:.2f}s")
        
        return final_df

    @staticmethod
    def resample_data(df: pd.DataFrame, target_rule: str) -> pd.DataFrame:
        """
        将数据重采样为新的周期
        target_rule: pandas offset aliases, e.g., '1min', '5min', '1H', '1D'
        """
        if df.empty:
            return df

        agg_dict = {
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }
        
        # 保留可能存在的额外列 (如 average, barCount)
        if 'average' in df.columns:
            agg_dict['average'] = 'mean'
        if 'barCount' in df.columns:
            agg_dict['barCount'] = 'sum'
            
        resampled_df = df.resample(target_rule).agg(agg_dict).dropna()
        return resampled_df

# === 缓存管理与业务逻辑 ===

# 全局缓存 (也可以放在类里)
DATA_CACHE: Dict[str, pd.DataFrame] = {}

async def get_universal_data(
    symbol: str, 
    api: IBapi, 
    start_date: str, 
    end_date: str, 
    bar_size: str = "1 min", # 支持 "1 min", "5 mins", "1 day" 等 IB 格式
    use_cache: bool = True
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    对外暴露的高级接口：
    1. 自动处理缓存
    2. 返回 主数据 DataFrame 和 日线 DataFrame (兼容原代码逻辑)
    """
    fetcher = DataFetcher(api)
    
    # 构造缓存 Key
    cache_key = f"{symbol}_{start_date}_{end_date}_{bar_size}"
    
    if use_cache and cache_key in DATA_CACHE:
        print(f"Hit Cache for {cache_key}")
        df = DATA_CACHE[cache_key]
    else:
        # 优化：
        # 如果用户要 "5 mins" 数据，我们不再像原代码那样傻傻地取 "30 secs" 再聚合。
        # 我们直接取 "5 mins" (API支持)。
        # 只有当用户请求非标准周期 (如 "7 mins") 时才需要取基础周期聚合。
        
        # 这里假设 bar_size 是 IB 标准格式，直接请求
        df = await fetcher.fetch_historical_data(
            symbol, start_date, end_date, bar_size_setting=bar_size
        )
        
        if use_cache:
            DATA_CACHE[cache_key] = df

    # 生成日线数据 (兼容原逻辑)
    # 原代码手动计算，这里用 Resample，极快
    if not df.empty:
        day_lines = DataFetcher.resample_data(df, '1D') # 1 Day
        # 添加原代码中的 Timestamp 列（如果需要与老代码兼容）
        # day_lines['Timestamp'] = day_lines.index
    else:
        day_lines = pd.DataFrame()

    return df, day_lines

# === 使用示例 (兼容原代码逻辑) ===

if __name__ == "__main__":
    # 模拟环境
    mock_api_instance = None # 你的 IBapi 实例
    
    async def main():
        symbol = "NVDA"
        start = "2023-01-01"
        end = "2023-01-10"
        
        # 1. 获取主数据 (例如 1分钟线)
        df_1min, df_daily = await get_universal_data(symbol, mock_api_instance, start, end, "1 min")
        
        print("Intraday Data Head:")
        print(df_1min.head())
        print("Daily Data Head:")
        print(df_daily.head())
        
        # 2. 如果你需要像原代码那样获取 VXN，单独调用即可，解耦
        # vxn_df, _ = await get_universal_data("VXN", mock_api_instance, start, end, "1 day")

    # asyncio.run(main())