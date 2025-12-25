import asyncio
import logging
import backtrader as bt
import pandas as pd
import os, sys

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '../..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from backtest.common import data_fetcher
from backtest.common.mockapi import MockApi
from strategy.grid import GridStrategy
from common.order_manager import OrderManager
from common.logger_manager import LoggerManager
from common.real_time_data_processer import RealTimeDataProcessor

choose_bar_size = 30  # in seconds
choose_total_cost = 20000

# --- The Grid Strategy implemented for backtrader ---
class GridStrategyBT(bt.Strategy):
    params = (
        # --- Basic Grid Parameters ---
        ('symbol', 'QQQ'),
        ('unique_tag', '1'),
        ('total_cost', choose_total_cost),
        ('retention_fund_ratio', 0.2),
        
        ('api', None)
    )


    def __init__(self):
        self.data_open = self.datas[0].open
        self.data_close = self.datas[0].close
        self.data_high = self.datas[0].high
        self.data_low = self.datas[0].low
        self.vxn = self.datas[2].open
        
        # 增加一个标志位，确保 InitStrategy 只运行一次
        self.strategy_initialized = False
        self.atr  = bt.indicators.ATR(self.datas[1], period=14)
        # self.ema5 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=5)
        self.ema12 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=12)
        self.ema20 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=20)
        self.ema26 = bt.indicators.ExponentialMovingAverage(self.datas[1], period=26)
        self.macd = bt.indicators.MACDHisto(self.datas[1], 
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
            "retention_fund_ratio": self.p.retention_fund_ratio,
            "data_file": f"data/mock/strategies/grid/{self.p.symbol}.json",
            # 【核心】注入适配器
            "real_data_processor": processor
        }
        self.grid = GridStrategy(om, strategy_id, **defaults)
        self.grid.is_running = True
        self.runtimes = {}
        self.not_today_count = 0
        self.days_total = 0


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
        self.loop.run_until_complete(self.grid.update_order_status(data_fetcher.buildGridOrder(order)))

    # 在执行bar数据之前调用。第一条bar的处理前不会执行next_open，也就是说需要在start中初始化一次。
    def next_open(self):
        # 1. 【预热检查】数据太少时，指标计算不出来，会报错或返回 NaN
        # 你的策略至少需要 MA20 和 ATR(14)，建议至少等 20 个 bar
        # 如果你用了 MA60，这里必须写 60
        if pd.isna(self.ema26[0]):
            return
        
        LoggerManager.Debug("app", strategy=f"backtrader", event=f"next", content=f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        self.log(f"Recive Price: {self.data_low[0]} - {self.data_high[0]}", level=0)
        # 可以每天做一次统计
        # 如果是新的一天，使用开盘价更新网格核心区，并将当前未完成的平仓单转为历史订单
        date_str = self.datas[0].datetime.datetime(0).strftime('%Y-%m-%d')  # 当前 bar 的 datetime 对象
        month = round(self.datas[0].datetime.date(0).month/6)
        if self.today != date_str:
            self.today = date_str
            
            self.runtimes = self.loop.run_until_complete(self.grid.rebalance(self.runtimes))
            
            # 统计每日的资金仓位变化
            self.days_total += 1
            if self.grid.not_today:
                self.not_today_count += 1
            summy = self.grid.DailySummary(date_str)
            # 每个月显示一条数据
            if month != self.month:
                self.month = month
                params = summy.params
                summy_str = f"Profit: {summy.profits:.2f}, Completed: {params.get('completed_count', 0)}, Pending: Buy({params.get('pending_buy_count', 0)}), Sell({params.get('pending_sell_count', 0)})"
                self.log(f"{self.broker.get_value():>8.2f} Cash: {round(summy.cash, 2):>8.2f}, Pos: {summy.position:>3}, " + summy_str + f", {self.not_today_count}/{self.days_total}", level=1)
            LoggerManager.Debug("app", strategy=f"backtrader", event=f"next_open", content=f"<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<")
        
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
        
