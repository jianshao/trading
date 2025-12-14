import asyncio
import json
import logging
import time
import traceback
from typing import Any, Dict, List
import backtrader as bt
from matplotlib import pyplot as plt
import pandas as pd
import os, sys

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '../..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi
from backtest.common import common
from backtest.common.mockapi import MockApi
from strategy.grid import GridStrategy
from strategy.order_manager import OrderManager
from utils import utils
from utils.logger_manager import LoggerManager
from strategy.real_time_data_processer import RealTimeDataProcessor
from backtest.common.view import visualize_results

choose_bar_size = 30  # in seconds
choose_total_cost = 20000

# --- The Grid Strategy implemented for backtrader ---
class GridStrategyBT(bt.Strategy):
    params = (
        # --- Basic Grid Parameters ---
        ('symbol', 'QQQ'),
        ('unique_tag', '1'),
        ('total_cost', choose_total_cost),
        ('grid_count', 20),
        ('retention_fund_ratio', 0.2),
        ('strategy_when_bear', 'nothing'),
        
        ('api', None)
    )


    def __init__(self):
        self.data_open = self.datas[0].open
        self.data_close = self.datas[0].close
        self.data_high = self.datas[0].high
        self.data_low = self.datas[0].low
        
        # 增加一个标志位，确保 InitStrategy 只运行一次
        self.strategy_initialized = False
        self.atr  = bt.indicators.ATR(self.datas[1], period=14)
        # self.ema5 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=5)
        self.ema12 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=12)
        self.ema20 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=20)
        self.ema26 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=26)
        self.macd = bt.indicators.MACD(self.datas[1], 
                                       period_me1=12,  # 快线周期
                                       period_me2=26,  # 慢线周期
                                       period_signal=9  # 信号线周期
                                      )
        # 直接访问DIF、DEA、Hist
        self.dif = self.macd.macd
        self.dea = self.macd.signal
        # self.hist = self.macd.histo
        
        self.loop = asyncio.get_event_loop() 
        
        self.month = 0
        self.today = ""
        self.api = MockApi(bt_api=self, ib_api=self.p.api, symbol=self.p.symbol)
        strategy_id = f"GRID_{self.p.unique_tag}_{self.p.symbol}"
        om = OrderManager(self.api, None, send_kafka=False)
        processor = RealTimeDataProcessor(self.api)
        log_configs = {
            "order": "logs/order.log",
            "app": "logs/app.log"
        }
        LoggerManager.init(log_configs, level=logging.INFO, realtime_data_processor=processor)
        
        defaults = {
            "symbol": self.p.symbol,
            "unique_tag": 1,
            "total_cost": self.p.total_cost,
            "grid_count": self.p.grid_count,
            "retention_fund_ratio": self.p.retention_fund_ratio,
            "strategy_when_bear": self.p.strategy_when_bear,
            "data_file": f"data/mock/strategies/grid/{self.p.symbol}.json",
            # 【核心】注入适配器
            "real_data_processor": processor
        }
        self.grid = GridStrategy(om, strategy_id, **defaults)
        self.runtimes = {}


    def log(self, txt, dt=None, level=0):
        """ Logging function for this strategy"""
        dt = dt or self.datas[0].datetime.date(0)
        if level in [1]:
            print(f'{dt.isoformat()} - {txt}')


    def start(self):
        """Called once at the beginning of the backtest."""
        self.log('Strategy Starting...', level=1)

        # self.loop.run_until_complete(self.grid.InitStrategy(self.broker.get_cash()))
        

    # 建仓单成交提交平仓单，平仓单成交无动作。
    # 成交价可能与限价有很大的差别，实际处理中应该以实际成交价作为网格中心线。
    def notify_order(self, order):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        self.log('notify order...', level=0)
        self.loop.run_until_complete(self.grid.update_order_status(common.buildGridOrder(order)))

    # 在执行bar数据之前调用。第一条bar的处理前不会执行next_open，也就是说需要在start中初始化一次。
    def next_open(self):
        # 1. 【预热检查】数据太少时，指标计算不出来，会报错或返回 NaN
        # 你的策略至少需要 MA20 和 ATR(14)，建议至少等 20 个 bar
        # 如果你用了 MA60，这里必须写 60
        if pd.isna(self.ema26[0]):
            return
        
        self.log(f"Recive Price: {self.data_low[0]} - {self.data_high[0]}", level=0)
        # 可以每天做一次统计
        # 如果是新的一天，使用开盘价更新网格核心区，并将当前未完成的平仓单转为历史订单
        date_str = self.datas[0].datetime.datetime(0).strftime('%Y-%m-%d')  # 当前 bar 的 datetime 对象
        month = round(self.datas[0].datetime.date(0).month)
        if self.today != date_str:
            self.today = date_str
            
            # 2. 【延迟初始化】在第一个有效 bar 进行初始化
            if not self.strategy_initialized:
                self.log("Initializing Grid Strategy...")
                
                # 强制同步执行
                self.loop.run_until_complete(self.grid.InitStrategy(self.broker.get_cash()))
                self.strategy_initialized = True
                
            else:
                # 每天都调用 rebalance。
                # runtime 传 None，因为回测中没有外部文件动态修改参数的需求
                # 初始化当天不需要再做下面的 rebalance 检查，直接返回即可
                self.runtimes = self.loop.run_until_complete(self.grid.rebalance(self.runtimes))
            
            # 统计每日的资金仓位变化
            summy = self.grid.DailySummary(date_str)
            # 每个月显示一条数据
            if month != self.month:
                self.month = month
                params = summy.params
                summy_str = f"Profit: {summy.profits:.2f}, Completed: {params.get('completed_count', 0)}, Pending: Buy({params.get('pending_buy_count', 0)}), Sell({params.get('pending_sell_count', 0)})"
                self.log(f"{self.broker.get_value():>8.2f} Cash: {round(summy.cash, 2):>8.2f}, Pos: {summy.position:>3}, " + summy_str, level=1)
        
    # 当前bar的数据执行之后才会调用next()
    def next(self):
        """
        Called on each bar of data.
        Primary role here is to dynamically maintain the grid orders.
        """
        pass
      
    def stop(self):
        # self.loop.run_until_complete(self.grid.DoStop())
        self.log(f"{self.broker.getvalue():.2f} Cash: {self.broker.get_cash():.2f} Position: {self.position.size}", level=1)
        

async def get_data(symbol: str, api: IBapi, start_date: str, end_date: str, bar_size_setting: str) -> pd.DataFrame:
    
    # json_data_str = """
    # [
    #     {"Open": 57.82, "High": 58.05, "Low": 57.80, "Close": 58.00, "Timestamp": "2025-06-26 09:30:00-04:00"},
    #     {"Open": 58.00, "High": 58.35, "Low": 57.95, "Close": 58.30, "Timestamp": "2025-06-26 09:30:05-04:00"},
    #     {"Open": 58.31, "High": 58.40, "Low": 58.10, "Close": 58.15, "Timestamp": "2025-06-26 09:30:10-04:00"},
    #     {"Open": 58.15, "High": 58.25, "Low": 57.60, "Close": 57.65, "Timestamp": "2025-06-26 09:30:15-04:00"},
    #     {"Open": 57.65, "High": 57.70, "Low": 57.45, "Close": 57.50, "Timestamp": "2025-06-26 09:30:20-04:00"},
    #     {"Open": 57.50, "High": 58.05, "Low": 57.48, "Close": 58.00, "Timestamp": "2025-06-26 09:30:25-04:00"},
    #     {"Open": 58.00, "High": 58.10, "Low": 57.90, "Close": 57.95, "Timestamp": "2025-06-26 09:30:30-04:00"},
    #     {"Open": 57.95, "High": 58.60, "Low": 57.90, "Close": 58.55, "Timestamp": "2025-06-26 09:30:35-04:00"},
    #     {"Open": 58.55, "High": 58.65, "Low": 57.10, "Close": 57.15, "Timestamp": "2025-06-26 09:30:40-04:00"},
    #     {"Open": 57.15, "High": 57.20, "Low": 56.80, "Close": 56.90, "Timestamp": "2025-06-26 09:30:45-04:00"},
    #     {"Open": 56.90, "High": 57.05, "Low": 56.45, "Close": 56.50, "Timestamp": "2025-06-26 09:30:50-04:00"},
    #     {"Open": 56.50, "High": 57.55, "Low": 56.48, "Close": 57.50, "Timestamp": "2025-06-26 09:30:55-04:00"},
    #     {"Open": 57.50, "High": 58.05, "Low": 57.45, "Close": 58.00, "Timestamp": "2025-06-26 09:31:00-04:00"},
    #     {"Open": 58.00, "High": 59.10, "Low": 57.95, "Close": 59.05, "Timestamp": "2025-06-26 09:30:05-04:00"},
    #     {"Open": 59.05, "High": 59.20, "Low": 58.85, "Close": 58.90, "Timestamp": "2025-06-26 09:30:10-04:00"}
    # ]
    # """
    # data_list = json.loads(json_data_str)
    # df = pd.DataFrame(data_list)
    # df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    # df.set_index('Timestamp', inplace=True)
    # return df
    # all_data = data_list
    data_list = []
    
    all_data = pd.DataFrame(data_list)
    # api = IBapi(port=7496, client_id=4)
    # await api.connectAsync()
    mockApi = MockApi(ib_api=api)
    end_dates = utils.get_us_trading_days(start_date, end_date)
    count, index, start_time = len(end_dates), 0, time.time()

    day_lines = pd.DataFrame([])
    for date in end_dates:
        valid_end_date = date+" 17:00:00 us/eastern"
        # 1 secs, 5 secs, 10 secs, 15 secs, 30 secs, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins, 20 mins, 30 mins, 1 hour, 2 hours, 3 hours, 4 hours, 8 hours, 1 day, 1W, 1M
        data = await mockApi.get_historical_data(symbol, end_date_time=valid_end_date, duration_str="1 D", bar_size_setting=bar_size_setting)
        if not data.empty:
            all_data = pd.concat([all_data, data])
            
            day_data = {
                "Timestamp": date, 
                "Open": data.iloc[0]['Open'], 
                "High": data['High'].max(), 
                "Low": data['Low'].min(), 
                "Close": data.iloc[-1]['Close'], 
                "Volume": data['Volume'].sum(), 
                "average": data['Close'].mean(), 
                "barCount": data['barCount'].sum()
            }
            day_line = pd.DataFrame([day_data])
            day_lines = pd.concat([day_lines, day_line], ignore_index=True)
        else:
            print(f"{symbol} {valid_end_date} failed.")
        
        if (index + 1) % 50 == 0:
            print(f"{symbol} data fetch progress: {index}/{count} completed. {time.time() - start_time:.2f} seconds elapsed.")
        index += 1
        # await asyncio.sleep(1)
        
    # 2. 处理日线数据 (day_lines)
    if not day_lines.empty:
        # 将字符串 "2023-01-04" 转换为 datetime 对象
        day_lines['Timestamp'] = pd.to_datetime(day_lines['Timestamp'])
        # 设置索引
        day_lines.set_index('Timestamp', inplace=True)
        # 排序
        day_lines.sort_index(inplace=True)
    # print(f" {all_data[:4]}")
    # all_data.set_index('Timestamp', inplace=True)
    # api.disconnect()
    return [all_data, day_lines]


def run_single_backtest(api, symbol, data_feed, strategy_class, params):
    """Helper function to run a backtest for a single strategy and return its returns."""
    cerebro = bt.Cerebro(stdstats=False)
    data = bt.feeds.PandasData(dataname=data_feed[0], name="data")
    cerebro.adddata(data)
    data = bt.feeds.PandasData(dataname=data_feed[1], name="data_DAILY")
    cerebro.adddata(data)
    default = {
        "api": api,
        "symbol": symbol,
        "grid_count": 20,
        "retention_fund_ratio": 0.2,
        "strategy_when_bear": "nothing"
    }
    default.update(params)
    cerebro.addstrategy(strategy_class, **default)
    
    cerebro.broker.setcash(20000.0)
    cerebro.broker.setcommission(commission=0.005)
    
    # Add TimeReturn analyzer to record portfolio value over time
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='time_return')
    
    print(f"Running backtest for params: {params.get('strategy_id', 'Unknown')}")
    results = cerebro.run(cheat_on_open=True)
    
    # Extract the daily portfolio values
    returns_analyzer = results[0].analyzers.time_return.get_analysis()
    # returns_analyzer is a dict where keys are dates and values are returns
    # We need to convert it to a Series of portfolio values
    
    initial_value = cerebro.broker.startingcash
    portfolio_values = pd.Series(returns_analyzer).cumsum() + 1 # Cumulative returns
    portfolio_values = portfolio_values * initial_value
    # Add the starting point
    start_date = data_feed[0].index[0].to_pydatetime().date()
    portfolio_values[start_date] = initial_value
    portfolio_values.sort_index(inplace=True)

    return portfolio_values    

def get_data_from_cache_or_fetch(symbol: str, api: IBapi, start_date: str, end_date: str, bar_size: str, cache: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    key = f"{symbol}_{start_date}_{end_date}_{bar_size}"
    if key in cache:
        return cache[key]

    key_30 = f"{symbol}_{start_date}_{end_date}_{30}"
    if key_30 not in cache:
        df = asyncio.get_event_loop().run_until_complete(get_data(symbol, api, start_date, end_date, "30 secs"))
        cache[key_30] = df
    else:
        df = cache[key_30]
    if bar_size == 5:
        return df

    # df.index = pd.to_datetime(df.index)  # 确保 DatetimeIndex
    df_data = df[0].resample(f"{bar_size}s").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open"])   # 去掉没有数据的K线
    cache[key] = [df_data, df[1]]

    return cache[key]

def run():
    periods_list = [
        ["2020-01-01", "2025-01-01"], # 主验证周期
        # ["2022-01-01", "2023-01-01"], # 趋势下跌压力测试
        # ["2020-02-01", "2020-06-01"], # 极端波动压力测试 
        # ["2018-09-01", "2019-02-01"]  # 流动性枯竭压力测试
    ]
    params_map = {
        # "NLY": [
        #     {"grid_count": 20, "strategy_when_bear": "clear"},
        #     {"grid_count": 20, "strategy_when_bear": "stop"},
        #     {"grid_count": 30, "strategy_when_bear": "clear"},
        #     {"grid_count": 30, "strategy_when_bear": "stop"},
        #     {"grid_count": 40, "strategy_when_bear": "clear"},
        #     {"grid_count": 40, "strategy_when_bear": "stop"},
        #     {"grid_count": 60, "strategy_when_bear": "clear"},
        #     {"grid_count": 60, "strategy_when_bear": "stop"},
        # ],
        "TQQQ": [
            {"grid_count": 20, "retention_fund_ratio": 0, "strategy_when_bear": "nothing"},
            # {"grid_count": 20, "retention_fund_ratio": 0.1, "strategy_when_bear": "nothing"},
            # {"grid_count": 20, "retention_fund_ratio": 0.2, "strategy_when_bear": "nothing"},
            # {"grid_count": 20, "retention_fund_ratio": 0.3, "strategy_when_bear": "nothing"},
            {"grid_count": 20, "retention_fund_ratio": 0, "strategy_when_bear": "stop"},
            # {"grid_count": 20, "retention_fund_ratio": 0.1, "strategy_when_bear": "stop"},
            # {"grid_count": 20, "retention_fund_ratio": 0.2, "strategy_when_bear": "stop"},
            # {"grid_count": 20, "retention_fund_ratio": 0.3, "strategy_when_bear": "stop"},
            {"grid_count": 20, "retention_fund_ratio": 0, "strategy_when_bear": "clear"},
            # {"grid_count": 20, "retention_fund_ratio": 0.1, "strategy_when_bear": "clear"},
            # {"grid_count": 20, "retention_fund_ratio": 0.2, "strategy_when_bear": "clear"},
            # {"grid_count": 20, "retention_fund_ratio": 0.3, "strategy_when_bear": "clear"},
            # {"grid_count": 20, "strategy_when_bear": "stop"},
            # {"grid_count": 30, "strategy_when_bear": "clear"},
            # {"grid_count": 30, "strategy_when_bear": "stop"},
            # {"grid_count": 40, "strategy_when_bear": "clear"},
            # {"grid_count": 40, "strategy_when_bear": "stop"},
            # {"grid_count": 60, "strategy_when_bear": "clear"},
            # {"grid_count": 60, "strategy_when_bear": "stop"},
        ]
    }
    try:
        api: IBapi = IBapi(port=7496, client_id=13)
        api.connect()
        
        cache = {}
        all_portfolio_values = {}
        for period in periods_list:
            start_date, end_date = period[0], period[1]
            for symbol, params_list in params_map.items():
                print(f"\n\n########## Testing {symbol}: {start_date}  {end_date} ##########")
                df = get_data_from_cache_or_fetch(symbol, api, start_date, end_date, choose_bar_size, cache)
                print(f"Total len: {len(df[0])}")
                for idx, param in enumerate(params_list):
                    portfolio_series = run_single_backtest(api, symbol, df, GridStrategyBT, param)
                    strategy_id = f"{symbol}_{idx}"
                    all_portfolio_values[strategy_id] = portfolio_series
                # visualize_results(df)
        # --- 4. Plot all equity curves on one chart using matplotlib ---
        plt.figure(figsize=(15, 8))
        colors = ['blue', 'green', 'red', 'cyan', 'magenta', 'yellow', 'black', 'white']
        idx = 0
        for strategy_id, values in all_portfolio_values.items():
            plt.plot(values.index, values, color=colors[idx], label=f'{colors[idx]}: Equity Curve - {strategy_id}')
            idx += 1
            
        plt.title('Strategy Equity Curve Comparison')
        plt.xlabel('Date')
        plt.ylabel('Portfolio Value ($)')
        plt.legend()
        plt.grid(True)
        plt.show()
    except Exception as e:
        print(f"error: {e}")
        traceback.print_exc()
    finally:
        if api.isConnected():
            api.disconnect()


# --- Main Backtesting Setup ---
if __name__ == '__main__':
    run()
