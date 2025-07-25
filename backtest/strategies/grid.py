import asyncio
import json
import time
import backtrader as bt
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
from utils import utils

SYMBOL = "NLY"

# --- The Grid Strategy implemented for backtrader ---
class GridStrategyBT(bt.Strategy):
    params = (
        # --- Basic Grid Parameters ---
        ('symbol', ""),
        ('base_price', 10),
        ('lower_bound', 500.0),
        ('upper_bound', 600.0),
        
        # --- Position Sizing Parameters ---
        ('position_sizing_mode', 'fixed'),  # 'fixed' or 'pyramid'
        ('base_cost', 500),             # Base shares for fixed mode, or starting shares for pyramid mode
        ('position_sizing_ratio', 1.2),  # For pyramid: e.g., 1.2 for 20% increase per grid further from start
                                         # e.g., 0.8 for 20% decrease per grid further from start
        
        # --- Price Spacing Parameters ---
        ('spacing_mode', 'fixed'),      # 'fixed' or 'dynamic'
        ('spacing_ratio', 0.05),         # For dynamic: e.g., 1.05 for 5% increase in spacing per grid further from start
        
        # --- Management Parameters ---
        ('max_buy_grids', 1),
        ('max_sell_grids', 1),
        ('start_buy', 100),
        ('do_optimize', False),
        ('num_when_optimize', 1),
        ('space_diff', 0.01)
    )


    def __init__(self):
        self.data_open = self.datas[0].open
        self.data_close = self.datas[0].close
        self.data_high = self.datas[0].high
        self.data_low = self.datas[0].low
        
        self.today = ""
        self.api = MockApi(bt_api=self, symbol=self.p.symbol)
        strategy_id = f"GRID_{self.p.symbol}_{self.p.base_cost}_{self.p.space_diff}"
        self.grid = GridStrategy(self.api, strategy_id, self.p.symbol, 
                                 self.p.base_price, self.p.lower_bound, self.p.upper_bound,
                                 self.p.base_cost, self.p.space_diff, self.p.spacing_ratio, self.p.position_sizing_ratio,
                                 do_optimize=self.p.do_optimize, num_when_optimize=self.p.num_when_optimize,
                                 data_file=f"data/mock/strategies/grid/{self.p.symbol}.json")


    def log(self, txt, dt=None, level=0):
        """ Logging function for this strategy"""
        dt = dt or self.datas[0].datetime.date(0)
        if level in [1]:
            print(f'{dt.isoformat()} - {txt}')


    def start(self):
        """Called once at the beginning of the backtest."""
        self.log('Strategy Starting...')
        
        start_price = self.data_open[-1 * (len(self)-1)]
        # 初始建仓
        if self.p.start_buy:
            self.buy(size=self.p.start_buy, price=start_price)

        self.grid.InitStrategy(start_price, self.p.start_buy, self.broker.get_cash()-self.p.start_buy*start_price)


    # 建仓单成交提交平仓单，平仓单成交无动作。
    # 成交价可能与限价有很大的差别，实际处理中应该以实际成交价作为网格中心线。
    def notify_order(self, order):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        asyncio.get_event_loop().run_until_complete(self.grid.update_order_status(common.buildGridOrder(order)))


    # 在执行bar数据之前调用。第一条bar的处理前不会执行next_open，也就是说需要在start中初始化一次。
    def next_open(self):
        self.log(f"Recive Price: {self.data_low[0]} - {self.data_high[0]}")
        # 可以每天做一次统计
        # 如果是新的一天，使用开盘价更新网格核心区，并将当前未完成的平仓单转为历史订单
        dt = self.datas[0].datetime.datetime(0)  # 当前 bar 的 datetime 对象
        date_str = dt.strftime('%Y-%m-%d')       # 格式化为 '2025-06-26'
        # if self.today == "":
        #     self.today = date_str
        if self.today != date_str:
            self.today = date_str
            summy_str = self.grid.daily_summy(date_str)
            self.log(f"{self.broker.get_value():>8.2f} Cash: {round(self.broker.get_cash(), 2):>8.2f}, Pos: {self.position.size:>3}, " + summy_str, level=1)
    
    # 当前bar的数据执行之后才会调用next()
    def next(self):
        """
        Called on each bar of data.
        Primary role here is to dynamically maintain the grid orders.
        """
        pass
      
    def stop(self):
        self.grid.DoStop()
        self.log(f"{self.broker.getvalue():.2f} Cash: {self.broker.get_cash():.2f} Position: {self.position.size}", level=1)
        

def get_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    
    json_data_str = """
    [
        {"Open": 57.82, "High": 58.05, "Low": 57.80, "Close": 58.00, "Timestamp": "2025-06-26 09:30:00-04:00"},
        {"Open": 58.00, "High": 58.35, "Low": 57.95, "Close": 58.30, "Timestamp": "2025-06-26 09:30:05-04:00"},
        {"Open": 58.31, "High": 58.40, "Low": 58.10, "Close": 58.15, "Timestamp": "2025-06-26 09:30:10-04:00"},
        {"Open": 58.15, "High": 58.25, "Low": 57.60, "Close": 57.65, "Timestamp": "2025-06-26 09:30:15-04:00"},
        {"Open": 57.65, "High": 57.70, "Low": 57.45, "Close": 57.50, "Timestamp": "2025-06-26 09:30:20-04:00"},
        {"Open": 57.50, "High": 58.05, "Low": 57.48, "Close": 58.00, "Timestamp": "2025-06-26 09:30:25-04:00"},
        {"Open": 58.00, "High": 58.10, "Low": 57.90, "Close": 57.95, "Timestamp": "2025-06-26 09:30:30-04:00"},
        {"Open": 57.95, "High": 58.60, "Low": 57.90, "Close": 58.55, "Timestamp": "2025-06-26 09:30:35-04:00"},
        {"Open": 58.55, "High": 58.65, "Low": 57.10, "Close": 57.15, "Timestamp": "2025-06-26 09:30:40-04:00"},
        {"Open": 57.15, "High": 57.20, "Low": 56.80, "Close": 56.90, "Timestamp": "2025-06-26 09:30:45-04:00"},
        {"Open": 56.90, "High": 57.05, "Low": 56.45, "Close": 56.50, "Timestamp": "2025-06-26 09:30:50-04:00"},
        {"Open": 56.50, "High": 57.55, "Low": 56.48, "Close": 57.50, "Timestamp": "2025-06-26 09:30:55-04:00"},
        {"Open": 57.50, "High": 58.05, "Low": 57.45, "Close": 58.00, "Timestamp": "2025-06-26 09:31:00-04:00"},
        {"Open": 58.00, "High": 59.10, "Low": 57.95, "Close": 59.05, "Timestamp": "2025-06-26 09:30:05-04:00"},
        {"Open": 59.05, "High": 59.20, "Low": 58.85, "Close": 58.90, "Timestamp": "2025-06-26 09:30:10-04:00"}
    ]
    """
    data_list = json.loads(json_data_str)
    df = pd.DataFrame(data_list)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df.set_index('Timestamp', inplace=True)
    # return df
    # all_data = data_list
    data_list = []
    
    all_data = pd.DataFrame(data_list)
    api = IBapi()
    api.connect()
    mockApi = MockApi(ib_api=api)
    end_dates = utils.get_us_trading_days(start_date, end_date)
    for date in end_dates:
        valid_end_date = date+" 17:00:00 us/eastern"
        # 1 secs, 5 secs, 10 secs, 15 secs, 30 secs, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins, 20 mins, 30 mins, 1 hour, 2 hours, 3 hours, 4 hours, 8 hours, 1 day, 1W, 1M
        data = asyncio.get_event_loop().run_until_complete(mockApi.get_historical_data(symbol, end_date_time=valid_end_date, duration_str="1 D", bar_size_setting="5 secs"))
        if not data.empty:
            all_data = pd.concat([all_data, data])
        else:
            print(f"{symbol} {end_date} failed.")
    # print(f" {all_data[:4]}")
    # all_data.set_index('Timestamp', inplace=True)
    api.disconnect()
    return all_data



# --- Main Backtesting Setup ---
if __name__ == '__main__':
    # 1. Create a Cerebro engine
    cerebro = bt.Cerebro()

    params = [
        {"symbol":"WU", "cash": 20000, "position_sizing_mode": "fixed", "position_sizing_ratio": 0.05, "base_cost": 500, "spacing_mode": "fixed", "spacing_ratio": 0.05, "lower_bound": 0, "upper_bound": 10, "start_buy": 100}, # classic base
        {"symbol":"WU", "cash": 20000, "position_sizing_mode": "fixed", "position_sizing_ratio": 0.05, "base_cost": 500, "spacing_mode": "fixed", "spacing_ratio": 0.05, "lower_bound": 0, "upper_bound": 10, "start_buy": 100}, # classic base
    ]
    
    # for param in params:
        # 2. Add the strategy
    symbol = "TQQQ"
    cerebro.addstrategy(GridStrategyBT, 
                        symbol=symbol,
                        base_price=60,
                        base_cost=1000,
                        position_sizing_ratio=0.00, # Increase shares by 5% each grid down
                        spacing_ratio=0.00, # Increase spacing by 5% each grid
                        lower_bound=34,
                        upper_bound=93,
                        start_buy=1000,
                        do_optimize=True,
                        num_when_optimize=3,
                        space_diff=0.01)

    # 3. Create a Data Feed (using your provided test data)
    df = get_data(symbol, "2024-01-01", "2025-07-01")
    # print(f"{df}")
    print(f"Total len: {len(df)}")
    
    # Convert to backtrader data feed
    data_feed = bt.feeds.PandasData(dataname=df, name=symbol)
    cerebro.adddata(data_feed)

    # 4. Set our desired cash and commission
    cerebro.broker.setcash(94000.0)
    # Example: 0.005 per share, with a minimum of $1.00 per trade
    cerebro.broker.setcommission(commission=0.005)
    cerebro.broker.setcommission(interest=0.0)
    # cerebro.broker.setcommission(commission=0.0) # For no commission test

    # 5. Add Analyzers
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade_analyzer')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe_ratio', timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SQN, _name='sqn')
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='returns')


    # 6. Run over everything
    before = cerebro.broker.getvalue()
    results = cerebro.run(cheat_on_open=True)[0]
    final = cerebro.broker.getvalue()
    
    print("\n---- Cash ----")
    print(f"Init: {before:.2f} Final: {results.broker.get_value():.2f} NetProfit: {final - before:.2f}")
    print(f"Cash: {results.broker.getcash():.2f}, Position: {results.position.size}, Cost: {results.position.price:.2f}")

    # 7. Print out the final result
    strat = results
    trade_analysis = strat.analyzers.trade_analyzer.get_analysis()

    print("\n--- Trade Analysis ---")
    if trade_analysis:
        # print(f"Total Closed Trades: {trade_analysis.total.closed}")
        # print(f"Total Closed Trades: {completed_count}")
        # print(f"Total Net PNL: ${trade_analysis.pnl.net.total:.2f}")
        # total_cost是包含了盈利的。
        # print(f"Average PNL per Trade: ${trade_analysis.pnl.net.average:.2f}")
        # print(f"Winning Trades: {trade_analysis.won.total}, Losing Trades: {trade_analysis.lost.total}")
        # print(f"Win Rate: {trade_analysis.won.total / trade_analysis.total.closed * 100:.2f}%" if trade_analysis.total.closed > 0 else "N/A")
        # print(f"Max Consecutive Winning: {trade_analysis.streak.won.longest}")
        # print(f"Max Consecutive Losing: {trade_analysis.streak.lost.longest}")
        pass
    
    returns = strat.analyzers.returns.get_analysis()
    if returns:
        count = returns.get('rtot', 0)
        # print(f'累计收益率: {returns}')
        
    sqn_analysis = strat.analyzers.sqn.get_analysis()
    if sqn_analysis:
        print("\n--- System Quality Number (SQN) ---")
        print(f"SQN: {sqn_analysis.sqn:.2f}")
        print(f"Trades for SQN: {sqn_analysis.trades}")

    drawdown_analysis = strat.analyzers.drawdown.get_analysis()
    if drawdown_analysis:
        print("\n--- Drawdown Analysis ---")
        print(f"Max Drawdown: {drawdown_analysis.max.drawdown:.2f}%")
        print(f"Max Money Drawdown: ${drawdown_analysis.max.moneydown:.2f}")

    # 8. Plot the results
    # cerebro.plot(style='candlestick', barup='green', bardown='red')