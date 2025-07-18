import asyncio
import json
import random
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional
import backtrader as bt
import pandas as pd
import numpy as np
import datetime
import os, sys

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '../..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi
from backtest.common.mockapi import MockApi
from strategy.grid import GridStrategy
from utils import utils

completed_count = 0
total_pnl = 0
position = 0
total_cost = 0
last_price = 0

class LiveGrid:
    def __init__(self, order):
        self.open_order = order
        self.close_order = None

# --- The Grid Strategy implemented for backtrader ---
class GridStrategyBT(bt.Strategy):
    params = (
        # --- Basic Grid Parameters ---
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
        ('start_buy', 100)
    )


    def __init__(self):
        self.data_open = self.datas[0].open
        self.data_close = self.datas[0].close
        self.data_high = self.datas[0].high
        self.data_low = self.datas[0].low

        # --- NEW: Dynamic Grid Generation ---
        # This now holds tuples of (price, shares_at_this_price)
        # We will generate separate lists for potential buy grids and sell grids
        self.buy_grid_definitions = {} # price -> shares
        self.sell_grid_definitions = {} # price -> shares
        self.grid_definitions = {}

        # --- State Management (mostly unchanged, but now uses dynamic shares) ---
        # 运行时数据
        self.historical_orders = {}
        self.open_orders = {}
        self.close_orders = {}
        
        # 统计数据
        self.pending_sell_count = 0
        self.pending_buy_count = 0
        self.historical_completed_count = 0
        self.historical_completed_profit = 0
        
        self.today = ""
        self.inited = False


    def _generate_dynamic_grids(self, curr_market_price):
        """
        NEW: Generates grid levels and their corresponding share sizes based on strategy parameters.
        This handles fixed, pyramid, and dynamic spacing logic.
        """
        # Determine the starting point for dynamic/pyramid calculations
        start_price = round(curr_market_price * 0.998, 2)
        # start_price = 57.5
        if start_price is None or not (self.p.lower_bound <= start_price <= self.p.upper_bound):
            start_price = (self.p.lower_bound + self.p.upper_bound) / 2.0
        base_spacing = round(start_price * 0.015, 2)
        # base_spacing = 0.5
        base_shares = int(self.p.base_cost/curr_market_price) # self.p.base_shares
        # base_shares = 10
        
        # --- Generate grid levels below the start_price (for BUYS) ---
        current_price = start_price
        current_spacing = base_spacing
        current_shares = base_shares
        
        self.grid_definitions = {}
        next_buy_price = current_price
        while True:
            if next_buy_price < self.p.lower_bound:
                break # Stop if we go below the lower bound

            # Round price to a sensible precision, e.g., 2-4 decimal places
            next_buy_price = round(next_buy_price, 2)
            
            self.grid_definitions[next_buy_price] = {"shares": int(current_shares), "status": 0}

            # Update for next iteration
            next_buy_price -= current_spacing
            if self.p.spacing_mode == 'dynamic':
                current_spacing = round(current_spacing * (1 - self.p.spacing_ratio), 2)
            if self.p.position_sizing_mode == 'pyramid':
                current_shares = round(current_shares * (1 - self.p.position_sizing_ratio), 2)

        # --- Generate grid levels above the start_price (for SELLS) ---
        current_spacing = base_spacing
        current_shares = base_shares
        next_sell_price = start_price + base_spacing
        while True:
            if next_sell_price > self.p.upper_bound:
                break # Stop if we go above the upper bound

            next_sell_price = round(next_sell_price, 2)
            
            self.grid_definitions[next_sell_price] = {"shares": int(current_shares), "status": 0}

            # Update for next iteration
            next_sell_price += current_spacing
            if self.p.spacing_mode == 'dynamic':
                current_spacing = round((1 + self.p.spacing_ratio) * current_spacing, 2)
            if self.p.position_sizing_mode == 'pyramid':
                current_shares = round(current_shares * (1 + self.p.position_sizing_ratio), 2)


    def log(self, txt, dt=None, level=0):
        """ Logging function for this strategy"""
        dt = dt or self.datas[0].datetime.date(0)
        if level in [1]:
            print(f'{dt.isoformat()} - {txt}')


    def _init_before_day(self, current_market_price):
        # # 计算并生成网格列表
        self._generate_dynamic_grids(current_market_price) # New method to generate grids based on params
        
        self.log(f"Strategy Initialized with Dynamic Grids.", level=1)
        self.log(f"  Grids (price: shares): { {round(k,2):v for k,v in self.grid_definitions.items()} }", level=1)
        
        # 使用开盘价刷新网格
        self.maintain_active_grid_orders(current_market_price)

    def start(self):
        """Called once at the beginning of the backtest."""
        self.log('Strategy Starting...')
        
        # 初始建仓
        if self.p.start_buy:
            self.buy(size=self.p.start_buy)
        
        start_price = self.data_open[-1 * (len(self)-1)]
        self._init_before_day(start_price)

        
    def grid_sell(self, price: float, size: float, purpose = "OPEN"):
        # 持仓量过少时不提交卖单
        # local_pending_sell_count = self.pending_sell_count
        # while (self.position.size < local_pending_sell_count):
        #     # 先取出历史订单中所有的卖单
        #     sell_orders = {ref: grid for ref, grid in self.historical_orders.items() if grid.open_order.issell()}
        #     if sell_orders:
        #         # 找出卖单中卖出价最高的那个
        #         max_ref = max(sell_orders, key=lambda ref: sell_orders[ref].open_order.price)
        #         # 将找到的卖单执行取消
        #         grid = self.historical_orders[max_ref]
        #         self.cancel(grid.open_order)
        #         local_pending_sell_count -= abs(grid.open_order.size)
        #     else:
        #         self.log(f"place sell error: position expect {size} but {self.position.size} and no pending sell orders.", level=1)
        #         return None
        self.pending_sell_count += abs(size)
        return self.sell(price=price, size=size, exectype=bt.Order.Limit)

    def grid_buy(self, price, size):
        cost = round(price * size, 2)
        # while cost > self.broker.get_cash():
        #     buy_orders = {ref: grid for ref, grid in self.historical_orders.items() if grid.open_order.isbuy()}
        #     if buy_orders:
        #         # 找出卖单中卖出价最高的那个
        #         min_ref = min(buy_orders, key=lambda ref: buy_orders[ref].open_order.price)
        #         # 将找到的卖单执行取消
        #         grid = self.historical_orders[min_ref]
        #         self.cancel(grid.open_order)
        #         cost -= abs(round(grid.open_order.size * grid.open_order.price, 2))
        #     else:
        #         self.log(f"place buy error: cash expect {round(price * size, 2)} but {self.broker.get_cash()} and no pending buy orders.", level=1)
        #         return None
        return self.buy(price=price, size=size, exectype=bt.Order.Limit)
      
    # 建仓单成交提交平仓单，平仓单成交无动作。
    # 成交价可能与限价有很大的差别，实际处理中应该以实际成交价作为网格中心线。
    def notify_order(self, order):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        if order.status in [order.Submitted, order.Accepted]:
            return
        
        global completed_count, total_pnl, position, total_cost
        # 卖出时shares是负数，买入时是正数
        price, shares, ref = round(order.executed.price, 2), order.executed.size, order.ref
        if ref not in self.open_orders.keys() and ref not in self.close_orders.keys():
            self.log(f"unknown order ref: {ref}.")
            return 
        
        if order.status in [order.Completed]:
            # self.log(f"Order Completed: {ref} {price} {shares}")
            position += shares
            total_cost = round(total_cost + shares * price, 2)
            # 历史订单不需要重新提交新订单
            if ref in self.historical_orders.keys():
                grid = self.historical_orders[ref]
                profit = abs(round((price - grid.open_order.executed.price)*shares, 2))
                self.log(f"history order completed: {ref} {price} {shares} win {profit}")
                completed_count += 1
                total_pnl += profit
                
                if order.issell():
                    self.pending_sell_count -= abs(shares)
                
                self.historical_completed_count += 1
                self.historical_completed_profit += abs(profit)
                del self.historical_orders[ref]
                return 
          
            if order.isbuy():
                self.log(f'BUY EXECUTED, Price: {price:.2f}, Qty: {shares:.0f}')
            
                potential_sell_targets = sorted([p for p in self.grid_definitions.keys() if p > price])
                if not potential_sell_targets:
                    self.log(f"Warning: No sell grid defined above buy price {price:.2f}. Cannot place closing order.")
                    return
                sell_target = potential_sell_targets[0] # The next grid level up
                if ref in self.open_orders.keys():
                    # 建仓单成交，提交平仓单
                    # if 1 <= random.randint(1, 100) <= 10:
                    #     shares -= 1
                    orders = self.close_orders
                    sell_order = self.grid_sell(price=sell_target, size=shares, purpose="CLOSE")
                    if sell_order:
                        self.log(f"Place SELL order, Price: {sell_target:.2f}, Qty: {shares}")
                        grid = self.open_orders[ref]
                        grid.close_open = sell_order
                        orders[sell_order.ref] = grid
                    
                    del self.open_orders[ref]
                elif ref in self.close_orders.keys():
                    profit = abs(round((sell_target-price)*shares, 2))
                    total_pnl += profit
                    completed_count += 1
                    self.log(f"GRID CYCLE (SELL-BUY) COMPLETED for grid @{price} - @{sell_target:.2f} {profit}")
                    orders = self.open_orders
                    
                    sell_order = self.grid_sell(price=sell_target, size=shares)
                    if sell_order:
                        self.log(f"Place SELL order, Price: {sell_target:.2f}, Qty: {shares}")
                        orders[sell_order.ref] = LiveGrid(sell_order)
                        
                    del self.close_orders[ref]

            elif order.issell():
                self.pending_sell_count -= abs(shares)
                self.log(f'SELL EXECUTED, Price: {price:.2f}, Qty: {shares:.0f}')
                potential_buy_targets = sorted([p for p in self.grid_definitions.keys() if p < price], reverse=True)
                if not potential_buy_targets:
                    self.log(f"Warning: No buy grid defined below sell price {price:.2f}. Cannot place closing order.")
                    return
                buy_target = potential_buy_targets[0] # The next grid level down
                if ref in self.open_orders.keys():
                    # 建仓单成交，提交平仓单
                    orders = self.close_orders
                    buy_order = self.grid_buy(price=buy_target, size=abs(shares))
                    if buy_order:
                        self.log(f"Place BUY order, Price: {buy_target:.2f}, Qty: {abs(shares)}")
                        grid = self.open_orders[ref]
                        grid.close_order = buy_order
                        orders[buy_order.ref] = grid
                    
                    del self.open_orders[ref]
                elif ref in self.close_orders.keys():
                    profit = abs(round((buy_target-price)*shares, 2))
                    total_pnl += profit
                    completed_count += 1
                    self.log(f"GRID CYCLE (BUY-SELL) COMPLETED for grid @{buy_target:.2f} {profit}")
                    orders = self.open_orders
                    buy_order = self.grid_buy(price=buy_target, size=abs(shares))
                    if buy_order:
                        self.log(f"Place BUY order, Price: {buy_target:.2f}, Qty: {abs(shares)}")
                        orders[buy_order.ref] = LiveGrid(buy_order)
                    
                    del self.close_orders[ref]
                
            self.maintain_active_grid_orders(price)

        elif order.status in [order.Canceled, order.Margin, order.Rejected, order.Expired]:
            # self.log(f'Order Canceled/Margin/Rejected/Expired: {order.status} Ref {order.ref} {order.price:.2f} {order.size}')
            if order.issell():
                self.pending_sell_count -= abs(order.size)
            if ref in self.open_orders.keys():
                del self.open_orders[ref]
            if ref in self.close_orders.keys():
                del self.close_orders[ref]


    # 在执行bar数据之前调用。第一条bar的处理前不会执行next_open，也就是说需要在start中初始化一次。
    def next_open(self):
        # self.log(f"Recive Price: {self.data_low[0]} - {self.data_high[0]}")
        
        # 可以每天做一次统计
        # 如果是新的一天，使用开盘价更新网格核心区，并将当前未完成的平仓单转为历史订单
        dt = self.datas[0].datetime.datetime(0)  # 当前 bar 的 datetime 对象
        date_str = dt.strftime('%Y-%m-%d')       # 格式化为 '2025-06-26'
        if self.today == "":
            self.today = date_str
        if self.today != date_str:
            # self.log(f" New Day: {date_str}", force=True)
            self.today = date_str
            # self._init_before_day(current_market_price, date_str)
            time.sleep(1)
            
            cash, curr_position = self.broker.get_cash(), self.position.size
            pending_sell, pending_buy, pending_sell_cost, pending_buy_cost = 0, 0, 0, 0
            pending_buy_order_count_map = {}
            pending_sell_order_count_map = {}
            for ref, grid in self.close_orders.items():
                order = grid.open_order
                if order.issell():
                    if order.price not in pending_buy_order_count_map.keys():
                        pending_buy_order_count_map[order.price] = 0
                    pending_buy_order_count_map[order.price] += abs(order.size)
                    pending_sell += abs(order.size)
                    pending_sell_cost += abs(round(order.size * order.price, 2))
                else:
                    if order.price not in pending_sell_order_count_map.keys():
                        pending_sell_order_count_map[order.price] = 0
                    pending_sell_order_count_map[order.price] += abs(order.size)
                    pending_buy += abs(order.size)
                    pending_buy_cost += abs(round(order.size * order.price, 2))
            # self.log(f"Value: {self.broker.get_value():>9.2f} Cash: {round(cash, 2):>9.2f}, Position: {curr_position:>3}, Pending: Sell({pending_sell:>3}) Cost({round(pending_sell_cost, 2):>8.2f}), Buy({pending_buy:>3}) Cost({round(pending_buy_cost, 2):>8.2f})", force=True)
            self.log(f"{self.broker.get_value():>8.2f} Cash: {round(cash, 2):>8.2f}, Pos: {curr_position:>3}, Completed: {completed_count:>3}, {total_pnl:>7.2f} Pending: Buy({pending_sell:>3}, {round(pending_sell_cost, 2):>8.2f}), Sell({pending_buy:>3}, {round(pending_buy_cost, 2):>8.2f})", level=1)
            # self.log(f"Pending Order Count: {len(self.historical_orders.keys())}, Position: {self.position.size}", force=True)
            
            if date_str == "2024-03-01":
                self.log(f"pending buy : { {round(k,2):v for k,v in pending_buy_order_count_map.items()} }", level=1)
                self.log(f"pending sell: { {round(k,2):v for k,v in pending_sell_order_count_map.items()} }", level=1)
            
    
    # 当前bar的数据执行之后才会调用next()
    def next(self):
        """
        Called on each bar of data.
        Primary role here is to dynamically maintain the grid orders.
        """
        # 更新最后收盘价
        global last_price
        last_price = self.data_close[0]
        

    def stop(self):
        self.log(f"{self.broker.getvalue():.2f} Cash: {self.broker.get_cash():.2f} Position: {self.position.size}", level=1)

    # 只会提交建仓单，或取消建仓单。维护网格核心区（即当前成交价的上下各一个网格，不包括当前网格）。
    def maintain_active_grid_orders(self, current_price):
        """
        MODIFIED: The logic remains the same, but it now uses the dynamically generated
        buy_grid_definitions and sell_grid_definitions for placing orders.
        """
        self.log(f"Maintain Grid: {current_price}")
        # --- Determine target price levels ---
        # 取当前价格的低一网格
        target_buy_levels = sorted(
                [price for price in self.grid_definitions.keys() if price < current_price],
                reverse=True
            )[0]
        
        # 取当前价格的高一网格
        target_sell_levels = sorted(
                [price for price in self.grid_definitions.keys() if price > current_price]
            )[0]
        
        for price, info in self.grid_definitions.items():
            if target_buy_levels <= price <= target_sell_levels:
                if info["status"] == 1:
                    continue

                # 原本失活状态的网格上一定没有建仓单
                self.grid_definitions[price]["status"] = 1
                order = None
                if price < current_price:
                    order = self.grid_buy(price=price, size=info["shares"])
                    self.log(f"Price {price:.2f} is in core buy zone and free. Placing BUY order for {info} {order.ref} shares.")
                elif price > current_price:
                    order = self.grid_sell(price=price, size=info["shares"])
                    self.log(f"Price {price:.2f} is in core sell zone and free. Placing SELL order for {info} {order.ref} shares.", level=0)
                if order:
                    self.open_orders[order.ref] = LiveGrid(order)
            else:
                # 原本处于激活状态的网格，取消其上的建仓单
                if info["status"] == 1:
                    self.grid_definitions[price]["status"] = 0
                    open_orders = self.open_orders.copy()
                    for ref, grid in open_orders.items():
                        if price == grid.open_order.price:
                            self.cancel(grid.open_order)
                            self.log(f" cancel order {ref} {price}")
                            del self.open_orders[ref]
        self.log(f"opening orders: { {round(grid.open_order.price,2): grid.open_order.isbuy() for ref, grid in self.open_orders.items()} }", level=0)


def get_data(symbol: str) -> pd.DataFrame:
    
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
    end_dates = utils.get_us_trading_days("2024-01-01", "2024-01-03")
    for end_date in end_dates:
        valid_end_date = end_date+" 17:00:00 us/eastern"
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
    cerebro.addstrategy(GridStrategyBT, 
                        position_sizing_mode='pyramid',
                        base_cost=500,
                        position_sizing_ratio=0.00, # Increase shares by 5% each grid down
                        spacing_mode='fixed',
                        spacing_ratio=0.00, # Increase spacing by 5% each grid
                        lower_bound=30.0,
                        upper_bound=99.0,
                        start_buy=200)

    # 3. Create a Data Feed (using your provided test data)
    df = get_data("TQQQ")
    # print(f"{df}")
    print(f"Total len: {len(df)}")
    
    # Convert to backtrader data feed
    data_feed = bt.feeds.PandasData(dataname=df, name="TQQQ")
    cerebro.adddata(data_feed)

    # 4. Set our desired cash and commission
    cerebro.broker.setcash(40000.0)
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
    print(f"Cash: {results.broker.getcash():.2f}, Position: {results.position.size}, Cost: {results.position.price:.2f} LastPrice: {last_price:.2f}")
    print(f"Cash: , Position: {position}, Cost: {total_cost:.2f}")
    print(f"CompletedCount: {completed_count}, Profit: {total_pnl:.2f}, AvgProfit: {total_pnl/completed_count:.2f}")

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