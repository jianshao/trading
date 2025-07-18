#!/usr/bin/python3
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Dict, List, Any, Optional, Tuple, Callable
# import numpy as np
import pandas as pd

from common.account import Account, Position
# import pandas_ta as ta

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.api import BaseAPI

# --- Constants ---
PENDING_ORDERS_FILE_TPL = "{strategy_id}_pending_cycles.json" # For persisting active grid cycles

ORDER_REF_PREFIX = "gridflex_" # Changed prefix for clarity
NUM_BUY_GRIDS_BELOW_MARKET = 2
NUM_SELL_GRIDS_ABOVE_MARKET = 2

class GridOrder:
    def __init__(self, symbol, order_id, action, price, shares, status:str = ""):
        self.order_id = order_id
        self.symbol = symbol
        self.action = action
        self.lmt_price = price
        self.shares = shares
        self.status = status

class LiveGridCycle: # Renamed from LiveGridTrade for clarity, represents one full cycle attempt
    def __init__(self, strategy_id: str,
                 # For buy-first cycle
                 open_order: GridOrder,
                 open_apply_time: int,
                 open_done_time: Optional[int] = None):
        self.strategy_id = strategy_id
        
        # 建仓订单信息
        self._open_order = open_order
        self.open_apply_time = open_apply_time
        self.open_done_time = open_done_time
        self.open_fee = 0.0
        
        # 平仓订单信息
        self._close_order = None
        self.close_apply_time = None
        self.close_done_time = None
        self.close_fee = 0.0
        # 保存原始订单，取消时使用
        
        # 当前订单所处的周期：
        # OPENNING：已提交建仓单，等待成交中
        # CLOSING：建仓已成交, 已提交平仓单，等待成交中
        # DONE：平仓单已成交，周期结束
        self.status = "OPENNING"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    @classmethod
    def from_dict(cls, data: dict) -> 'LiveGridCycle':
        # Ensure all necessary keys for constructor are present or have defaults
        obj = cls(
            data['symbol'], data['strategy_id'], data.get('shares', 0), # Default shares to 0 if missing
            open_order_id=data.get('open_order_id'), open_price=data.get('open_price'),
            open_action=data.get('open_action'), open_apply_time=data.get('open_apply_time'),
            open_done_time=data.get('open_done_time'),
            close_order_id=data.get('close_order_id'), close_price=data.get('close_price'),
            close_action=data.get('close_action'), close_apply_time=data.get('close_apply_time'),
            close_done_time=data.get('open_done_time')
        )
        obj.status = data.get('status', "OPENNING")
        return obj
        
    # 建仓单成交
    def closing(self, open_order: GridOrder):
        self.open_done_time = datetime.datetime.now()
        self.close_apply_time = datetime.datetime.now()
        self._open_order = open_order
        self.open_fee = 1.05
        self.status = "CLOSING"
        
    # 平仓单成交
    def done(self, order: GridOrder):
        self._close_order = order
        self.close_done_time = datetime.datetime.now()
        self.status = "DONE"
        

class GridUnit:
    def __init__(self, buy_price: float, sell_price: float, cost_per_grid: float):
        # self.api = api
        # self.contract = contract
        # self.strategy_id = strategy_id
        self.status = "deactive"
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.cost_per_grid = cost_per_grid
        
        # 计算出需要的参数
        self.shares_per_grid = int(cost_per_grid/buy_price)
        # self.shares_per_grid = 50
        
    def is_active(self) -> bool:
        return self.status == "active"
      

class GridStrategy:
    def __init__(self, api: BaseAPI, symbol: str, start_price: float, cost_per_grid: float, proportion: float, grid_price_type: str = "classic",
                 get_order_id: Callable[[str], int] = None, data_dir: str = "data",
                 init_cash: float = 10000): # For initial setup only
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.api = api
        self.strategy_id = f"GRID_{symbol}"
        self.symbol = symbol
        
        # 策略参数
        self.start_price = start_price  # 当前市场价格
        self.cost_per_grid = cost_per_grid  # 单网格成本
        self.proportion = proportion  # 网格价差与当前市场价格的比例
        self.grid_price_type: str = grid_price_type # 网格价差类型，已知的：classic(固定价差)，tower（金字塔价差）
        self.lower_bound = 0.0
        self.upper_bound = 0.0
        self.price_spacing = 0.0
        self.primary_exchange = "NASDAQ"
        # 上层传入的获取本地关联order_id，并且关联到对应策略的方法
        self.get_order_id: Callable[[str], int] = get_order_id
        
        # 收集的历史未成交的订单
        self.pending_orders: Dict[int, LiveGridCycle] = {}
        
        # 网格以及其他运行时数据
        self.units: List[GridUnit] = []
        self.open_orders: Dict[int, LiveGridCycle] = {}
        self.close_orders: Dict[int, LiveGridCycle] = {}
        self.trade_logs: List[Dict[Any, Any]] = []
        
        self.net_profit = 0
        self.completed_count = 0
        self.total_open_cost_time = 0
        self.total_close_cost_time = 0
        
        # 策略持有的账户信息
        self.account = Account(init_cash, [Position(symbol)])

    def __str__(self):
        return (f"GridParams(ID={self.strategy_id}, Sym={self.symbol}, CurrPrice={self.start_price:.2f} Type={self.grid_price_type} Proportion={self.proportion:.2f}, "
                f"Cost={self.cost_per_grid:.2f})")
    
    # 在策略下提交订单
    def place_grid_leg_order(self, action: str, price: float, shares: int, purpose: str, 
                                    order_ref_suffix_override: Optional[str] = None) -> Optional[any]:
        
        # print(f"Placing order: {action} {self.contract.symbol} {shares} @{price}")
        
        price = round(price, 2)
        shares = round(shares, 0)

        order_id = self.get_order_id(self.strategy_id)
        
        ref_suffix = order_ref_suffix_override if order_ref_suffix_override else f"{purpose}_{action.upper()}"
        order_ref = f"{ORDER_REF_PREFIX}{self.strategy_id}_{ref_suffix}_{order_id}"

        # Make up to 3 attempts if place_limit_order returns None (submission failure)
        api_trade_obj: Optional[Any] = None
        for attempt in range(1):
            api_trade_obj = self.api.place_limit_order( # This is from BaseAPI, returns ib_insync.Trade
                self.symbol, action.upper(), float(shares), price, 
                order_ref=order_ref, order_id_to_use=order_id
            )
            if api_trade_obj and api_trade_obj.order.orderId == order_id:
                return api_trade_obj.order
                # break # Successful submission
            # print(f"  Attempt {attempt + 1}/3 to place {action} ({purpose}) OrderID {order_id} for {self.strategy_id} failed. Retrying in 1s...")
            # await asyncio.sleep(1) # Wait 1 second before retry
        
        print(f"  Failed to place {action} ({purpose}) OrderID {order_id} for {self.strategy_id} after 3 attempts. Logging failure.")
        return None
      
    
    # 根据操作动作以及目标更新订单字典
    def _update_orders_info(self, unit: GridUnit, action: str, purpose: str, order: any, old_order_id: int = None):
        # 建仓时生成一个新的订单周期数据，此时没有old_order_id
        if purpose == "OPEN":
            price = unit.sell_price if action.upper() == "SELL" else unit.buy_price
            grid_order = GridOrder(self.symbol, order.orderId, order.action, price, order.totalQuantity)
            self.open_orders[order.orderId] = LiveGridCycle(self.strategy_id, grid_order, time.time())
        else:
            # 提交平仓订单，说明已经存在建仓信息
            cycle = self.open_orders[old_order_id]
            price = unit.sell_price if action.upper() == "SELL" else unit.buy_price
            grid_order = GridOrder(self.symbol, order.orderId, order.action, price, order.totalQuantity)
            cycle.closing(grid_order)
            
            # 将原来的建仓订单周期数据转移到平仓字典
            self.close_orders[order.orderId] = cycle
            del self.open_orders[old_order_id]
    
    def buy(self, unit: GridUnit, purpose: str, old_order_id: int = None) -> Optional[any]:
        # 对应网格未激活时不能提交订单，只针对建仓订单
        if purpose == "OPEN" and not unit.is_active():
            print(f" {purpose} BUY {self.symbol} {unit.buy_price} failed: grid deactive!")
            return None
        
        # 检查账户数据
        if not self.account.place_buy(self.symbol, unit.shares_per_grid, unit.buy_price):
            return None

        order = self.place_grid_leg_order("BUY", unit.buy_price, unit.shares_per_grid, purpose)
        if order:
            self._update_orders_info(unit, "BUY", purpose, order, old_order_id)
            return order
      
    def sell(self, unit: GridUnit, purpose: str, old_order_id: int = None) -> Optional[any]:
        # 对应网格未激活时不能提交订单，只针对建仓订单
        if purpose == "OPEN" and not unit.is_active():
            print(f" {purpose} SELL {self.symbol} {unit.sell_price} failed: grid deactive!")
            return None
        
        if not self.account.place_sell(self.symbol, unit.shares_per_grid, unit.sell_price):
            return None

        order = self.place_grid_leg_order("SELL", unit.sell_price, unit.shares_per_grid, purpose)
        if order:
            self._update_orders_info(unit, "SELL", purpose, order, old_order_id)
            return order
        
    def clean_when_cancel_order(self, order_id: int):
        if order_id in self.open_orders.keys():
            # TODO: 是否日志记录??
            order = self.open_orders[order_id]._open_order
            del self.open_orders[order_id]
        elif order_id in self.close_orders.keys():
            # TODO: 是否需要人工干预？？？
            order = self.open_orders[order_id]._close_order
            del self.close_orders[order_id]
        else:
            print(f"cancel clean: unknow order id({order_id}).")
            return 

        # 更新账户数据，一定要在这里更新，否则手动取消的那些订单会被遗漏处理。
        if order.action.upper() == "BUY":
            self.account.cancel_buy(self.symbol, order.totalQuantity, order.lmtPrice)
        else:
            self.account.cancel_sell(self.symbol, order.totalQuantity, order.lmtPrice)

    
    def _log_completed_trade(self, cycle: LiveGridCycle):
        """Logs the details of a completed grid cycle to self.trade_logs."""
        log_entry = {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "shares": cycle.shares,
            "open_order_id": cycle.open_order_id,
            "open_action": cycle.open_action,
            "open_price": cycle.open_price,
            "open_apply_time": cycle.open_apply_time,
            "open_done_time": cycle.open_done_time,
            "close_order_id": cycle.close_order_id,
            "close_action": cycle.close_action,
            "close_price": cycle.close_price,
            "close_apply_time": cycle.close_apply_time,
            "close_done_time": cycle.close_done_time,
            "fee": cycle.open_fee + cycle.close_fee
        }
        
        gross_profit = abs(cycle.open_price - cycle.close_price) * cycle.shares
        log_entry["gross_profit"] = gross_profit
        log_entry["net_profit"] = round(gross_profit - cycle.open_fee - cycle.close_fee, 2)
        
        self.net_profit += log_entry["net_profit"]
        self.completed_count += 1
        self.total_open_cost_time += round((cycle.open_done_time - cycle.open_apply_time))
        self.total_close_cost_time += round((cycle.close_done_time - cycle.close_apply_time))
        self.trade_logs.append(log_entry)
        # print(f"  LOGGED CYCLE for {self.strategy_id}: Open {log_entry['open_action']} @{log_entry['open_price']:.2f}, "
        #       f"Close {log_entry['close_action']} @{log_entry['close_price']:.2f}. Net PNL: {log_entry['net_profit']:.2f}")
        return
    
    def order_status_update(self, unit: GridUnit, order: GridOrder):
        
        action = order.action # BUY or SELL
        order_id = order.order_id
        lmt_price = order.lmt_price
        # if order_status.status in ["Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"]:
        # print(f"GridEngine: OrderUpdate for {self.strategy_id}, OrderID {order_id} ({action} {self.contract.symbol}), Status: {order_status.status}, Filled: {order_status.filled}@{order_status.lastFillPrice:.2f}")

        if order.status == "Filled":
            new_action = "SELL" if action == "BUY" else "BUY"
            if order_id in self.open_orders.keys():
                # 建仓单成交，更新成交时间
                cycle = self.open_orders[order_id]
                cycle.closing(order)
                
                # 成交的是建仓单, 提交对应的平仓单
                # await self.sell("CLOSE", order_id) if action.upper() == "BUY" else await self.buy("CLOSE", order_id)
                if action.upper() == "BUY":
                    price = unit.sell_price
                    # 更新账户信息
                    self.account.buy_done(self.symbol, unit.shares_per_grid, unit.buy_price)
                    self.sell(unit, "CLOSE", order_id)
                else:
                    price = unit.buy_price
                    self.account.sell_done(order.symbol, unit.shares_per_grid, unit.sell_price)
                    # 要先结算在挂单
                    self.buy(unit, "CLOSE", order_id)
                
                # print(f"  OPEN {action} filled for {self.strategy_id}: {self.contract.symbol} @{lmt_price:.2f} {self.shares_per_grid:.2f}. Placing CLOSE {new_action} @{price}")
            elif order_id in self.close_orders.keys():
                # 更新平仓成交时间，然后进行统计
                cycle = self.close_orders[order_id]
                cycle.done()
                
                del self.close_orders[order_id]
                self._log_completed_trade(cycle)

                # 成交的是平仓单, 提交一个对应的建仓单
                # await self.sell("OPEN", order_id) if action.upper() == "BUY" else await self.buy("OPEN", order_id)
                if action.upper() == "BUY":
                    self.account.buy_done(order.symbol, unit.shares_per_grid, unit.buy_price)
                    self.sell(unit, "OPEN", order_id)
                else:
                    self.account.sell_done(order.symbol, unit.shares_per_grid, unit.sell_price)
                    self.buy(unit, "OPEN", order_id)
                # print(f"  CLOSE {action} filled for {self.strategy_id}: {self.contract.symbol} @{lmt_price:.2f} {self.shares_per_grid:.2f}. Placing OPEN {new_action}")
            else:
                print(f"Order Status Update: unknown order id: {order_id}")
            self.account.calc(self.symbol, lmt_price)
        elif order.status in ["Cancelled", "ApiCancelled", "Inactive", "Rejected"]:
            print(f"OrderStatus: {order.orderId} {self.symbol} {order.totalQuantity} {order.status}")
            self.clean_when_cancel_order(order_id)

    def cancel_order(self, order: GridOrder) -> bool:
        return self.api.cancel_order()

    # 取消对应网格下所有处于建仓的订单
    def cancel_open_orders(self, unit: GridUnit):
        # print("GridStrategy: cancel open orders: ")
        cycles_to_remove: List[LiveGridCycle] = []
        data = self.open_orders.copy()
        for order_id, cycle in data.items() or {}:
            # 提交取消命令，只需要提交命令即可，订单状态更新事件中会清理相关数据
            # print(f"GridStrategy: cancel open orders: {order_id}")
            if cycle.open_action == "BUY" and cycle.open_price == unit.buy_price:
                cycles_to_remove.append(cycle)
            if cycle.open_action == "SELL" and cycle.open_price == unit.sell_price:
                cycles_to_remove.append(cycle)
            
        for cycle in cycles_to_remove:
            # print(f"GridStrategy: cancel open orders: {cycle.open_order_id}")
            del self.open_orders[cycle.open_order_id]
            self.cancel_order(cycle._open_order)
            
          
    def cancel_all_orders(self):
        # 取消订单操作之间增加一点时间，避免操作过快导致问题
        for order_id, cycle in self.open_orders.items() or {}:
            # 提交取消命令，只需要提交命令即可，订单状态更新事件中会清理相关数据
            if not self.cancel_order(cycle._open_order):
                print(f"cancel open order failed: orderId:{cycle.open_order_id} price:{cycle.open_price}")
        
        for order_id, cycle in self.close_orders.items() or {}:
            # 提交取消命令，只需要提交命令即可，订单状态更新事件中会清理相关数据
            if not self.cancel_order(cycle._close_order):
                print(f"cancel close order failed: {cycle.close_order_id} {cycle.close_price}")
            time.sleep(0.1)
        
    def build_grid_units(self, curr_price: float) -> List[GridUnit]:
        propur = 0.05 # 金字塔网格，每个网格价差增加5%
        cost_diff_propor = 0.05  # 相邻网格的成本差异
        base_buy_price, base_sell_price = round(curr_price * 0.998, 4), round(curr_price * 0.998 + self.price_spacing, 4)
        # base_buy_price, base_sell_price = 57.5, 58.0
        
        all_units = []
        buy_price, sell_price = base_buy_price, base_sell_price
        curr_unit = GridUnit(round(buy_price, 2), round(sell_price, 2), self.cost_per_grid)
        
        # 组织上网格
        cost_per_grid = self.cost_per_grid
        buy_price, sell_price, spacing = base_sell_price, base_sell_price + self.price_spacing, self.price_spacing
        while sell_price <= self.upper_bound:
            unit = GridUnit(round(buy_price, 2), round(sell_price, 2), cost_per_grid)
            all_units.append(unit)
            
            # 更新下一个网格的配置
            buy_price = sell_price
            # 通过控制每个网格的价差控制网格间距，默认使用classic固定价差.
            if self.grid_price_type == "tower":
                # 等比例增大价差，距离当前价格越远价差越大
                # spacing = round(spacing * (1 + propur), 4)
                cost_per_grid = round(cost_per_grid * (1 + cost_diff_propor), 4)
            sell_price = buy_price + spacing
        
        # 网格根据卖单价格从高到低
        all_units.reverse()
        all_units.append(curr_unit)

        # 组织下网格
        cost_per_grid = self.cost_per_grid
        buy_price, sell_price, spacing = base_buy_price - self.price_spacing, base_buy_price, self.price_spacing
        while buy_price > self.lower_bound:
            unit = GridUnit(round(buy_price, 2), round(sell_price, 2), cost_per_grid)
            all_units.append(unit)
            
            # 更新新网格配置
            sell_price = buy_price
            # 通过控制每个网格的价差控制网格间距，默认使用classic固定价差.
            if self.grid_price_type == "tower":
                # 等比例增大价差，距离当前价格越远价差越大
                # spacing = round(spacing * (1 + propur), 4)
                cost_per_grid = round(cost_per_grid * (1 - cost_diff_propor), 4)
            buy_price = round(sell_price - spacing, 4)

        return all_units
        
    def is_stock_in_ranging_phase(
        self,
        data_df: pd.DataFrame,
        bollinger_period: int = 20,
        bollinger_std_dev: int = 2,
        adx_period: int = 14,
        bollinger_bandwidth_threshold_pct: float = 0.10, # Example: Bandwidth < 10% of middle band
        adx_threshold_low: float = 25,
        min_data_points: Optional[int] = None # Minimum data points required for calculation
    ) -> bool:
        """
        初步判断股票是否处于震荡区间。

        Args:
            data_df (pd.DataFrame): 包含 'High', 'Low', 'Close' 列的 DataFrame，索引为 DatetimeIndex。
            bollinger_period (int): 布林带周期。
            bollinger_std_dev (int): 布林带标准差倍数。
            adx_period (int): ADX 指标周期。
            bollinger_bandwidth_threshold_pct (float): 布林带带宽与中轨比值的阈值。
                                                    带宽小于此百分比可能表示震荡。
            adx_threshold_low (float): ADX 低于此阈值可能表示无趋势/震荡。
            min_data_points (Optional[int]): 计算指标所需的最少数据点。如果为None, 会基于指标周期推断。


        Returns:
            bool: 如果股票当前可能处于震荡区间，则为 True，否则为 False。
        """
        if not isinstance(data_df, pd.DataFrame) or data_df.empty:
            print("is_stock_in_ranging_phase: Input DataFrame is empty or invalid.")
            return False
        required_cols = ['High', 'Low', 'Close']
        if not all(col in data_df.columns for col in required_cols):
            print(f"is_stock_in_ranging_phase: DataFrame must contain {required_cols} columns.")
            return False
        for col in required_cols:
            if not pd.api.types.is_numeric_dtype(data_df[col]):
                print(f"is_stock_in_ranging_phase: Column '{col}' is not numeric.")
                return False

        # 确定所需的最小数据量
        if min_data_points is None:
            min_data_points = max(bollinger_period, adx_period) + 5 # Add a small buffer

        if len(data_df) < min_data_points:
            print(f"is_stock_in_ranging_phase: Not enough data ({len(data_df)}) to calculate indicators "
                f"(need at least {min_data_points}). Assuming not ranging.")
            return False

        # 1. 计算布林带 (Bollinger Bands)
        # pandas_ta 会自动添加列到 DataFrame，或者可以获取 Series
        # BBANDS_
        # BBL_20_2.0  BBM_20_2.0  BBU_20_2.0  BBB_20_2.0  BBP_20_2.0
        # Lower       Middle      Upper       Bandwidth   PercentB
        try:
            bbands = data_df.ta.bbands(length=bollinger_period, std=bollinger_std_dev, append=False) # Don't append to original df
            if bbands is None or bbands.empty:
                print("is_stock_in_ranging_phase: Failed to calculate Bollinger Bands.")
                return False
                
            # 获取最新的布林带值
            # 列名通常是 BBL_<period>_<stddev>, BBM_<period>_<stddev>, BBU_<period>_<stddev>
            # BBB_<period>_<stddev> 是带宽 (BBU - BBL)
            # BBP_<period>_<stddev> 是价格在带宽中的位置百分比
            upper_band_col = f'BBU_{bollinger_period}_{float(bollinger_std_dev)}'
            lower_band_col = f'BBL_{bollinger_period}_{float(bollinger_std_dev)}'
            middle_band_col = f'BBM_{bollinger_period}_{float(bollinger_std_dev)}'
            
            if not all(col in bbands.columns for col in [upper_band_col, lower_band_col, middle_band_col]):
                print(f"is_stock_in_ranging_phase: Bollinger Bands calculation did not produce expected columns. Columns: {bbands.columns.tolist()}")
                return False

            last_upper_band = bbands[upper_band_col].iloc[-1]
            last_lower_band = bbands[lower_band_col].iloc[-1]
            last_middle_band = bbands[middle_band_col].iloc[-1]

            if pd.isna(last_upper_band) or pd.isna(last_lower_band) or pd.isna(last_middle_band) or last_middle_band == 0:
                print("is_stock_in_ranging_phase: Bollinger Bands calculation resulted in NaN values for the last period.")
                return False

            # 计算布林带带宽与中轨的比率
            bollinger_bandwidth = last_upper_band - last_lower_band
            bollinger_bandwidth_ratio = bollinger_bandwidth / last_middle_band
            is_bandwidth_narrow = bollinger_bandwidth_ratio < bollinger_bandwidth_threshold_pct
            # print(f"  Debug BB: Bandwidth={bollinger_bandwidth:.2f}, Middle={last_middle_band:.2f}, Ratio={bollinger_bandwidth_ratio:.4f}, Narrow? {is_bandwidth_narrow}")

        except Exception as e:
            print(f"is_stock_in_ranging_phase: Error calculating Bollinger Bands: {e}")
            return False

        # 2. 计算 ADX (Average Directional Index)
        try:
            adx_series = data_df.ta.adx(length=adx_period, append=False) # Returns a DataFrame with ADX, DMP, DMN
            if adx_series is None or adx_series.empty or f'ADX_{adx_period}' not in adx_series.columns:
                print("is_stock_in_ranging_phase: Failed to calculate ADX or ADX column missing.")
                return False
                
            last_adx = adx_series[f'ADX_{adx_period}'].iloc[-1]

            if pd.isna(last_adx):
                print("is_stock_in_ranging_phase: ADX calculation resulted in NaN for the last period.")
                return False
                
            is_adx_low = last_adx < adx_threshold_low
            # print(f"  Debug ADX: Value={last_adx:.2f}, Low? {is_adx_low}")

        except Exception as e:
            print(f"is_stock_in_ranging_phase: Error calculating ADX: {e}")
            return False

        # 3. 综合判断
        # 如果布林带带宽窄 AND ADX 低，则可能处于震荡
        is_ranging = is_bandwidth_narrow and is_adx_low
        
        if is_ranging:
            print(f"Stock {data_df.iloc[-1].name if hasattr(data_df.iloc[-1], 'name') else 'current'} " # Assuming index has name (like symbol) or get symbol differently
                f"MAY BE RANGING. BB_Ratio: {bollinger_bandwidth_ratio:.4f} (Thr: <{bollinger_bandwidth_threshold_pct}), "
                f"ADX: {last_adx:.2f} (Thr: <{adx_threshold_low})")
        else:
            print(f"Stock {data_df.iloc[-1].name if hasattr(data_df.iloc[-1], 'name') else 'current'} "
                  f"likely NOT RANGING. BB_Ratio: {bollinger_bandwidth_ratio:.4f}, ADX: {last_adx:.2f}")

        return is_ranging


    # 策略执行之前先做一些检查，不满足条件的策略当天不运行。
    async def check_before_running(self, atr: float) -> bool:
        # atrp = round(atr*100/self.start_price, 2)
        # # atr率在[1.5, 3]之间，避免波动过低，以及当天价格高开过高（很难触发这种情况）
        # if not 1.5 <= atrp <= 4:
        #     print(f" check before running: atrp({atrp}) is not suit for running.")
        #     return False
        
        # 当前标的是否处于震荡区间
        # df = await self.api.get_historical_data(self.contract)
        # return self.is_stock_in_ranging_phase(df)
        return True
        
    
    # 策略初始化
    async def InitStrategy(self):
        
        # self.contract = await self.api.get_contract_details(self.symbol, primary_exchange=self.primary_exchange)
        # 使用当前价格作为初始价格，网格价格都是固定的
        # 需要订阅才能用
        # curr_price = await self.api.get_current_price(self.contract)
        # if curr_price > 0:
        #     self.start_price = curr_price
        
        # 根据标的历史数据计算当前策略参数
        # atr = await self.api.get_atr(self.contract)
        self.lower_bound = round(self.start_price * 1.5, 2)
        self.upper_bound = round(self.start_price * 0.5, 2)
        self.price_spacing = round(self.proportion * self.start_price/100, 2)
        # self.price_spacing = 0.5
        print(f"Configured strategy: {self}")
        
        # 达不到运行条件的直接退出
        # if not await self.check_before_running(atr):
        #     return 

        # 根据策略配置生成网格列表
        self.units = self.build_grid_units(self.start_price)
        
        # 载入历史未完成订单，将这些提交的订单id收集起来
        # 载入未完成的历史订单，包括：已买入未卖出的（低买高未卖）、已卖出未买入的（高卖低未买）
        # await self._load_active_grid_cycles()
        
        # 根据配置参数开启网格
        await self.maintain_active_grids_status(self.start_price, True, True)
        # for unit in self.units:
        #     print(f"unit: {unit.status} {unit.buy_price} - {unit.sell_price} {unit.cost_per_grid}")
        
        return 


    def find_the_grid_unit(self, target_price: float, is_buy: bool) -> Optional[int]:
        if not self.lower_bound <= target_price <= self.upper_bound:
          return None
        
        for i in range(len(self.units)):
            if self.units[i].buy_price < target_price < self.units[i].sell_price:
                return i
            if is_buy and self.units[i].buy_price == target_price:
                return i
            if not is_buy and self.units[i].sell_price == target_price:
                return i
        return None

    # 策略下的订单状态更新
    def update_order_status(self, order: GridOrder):
        order_id = order.order_id
        # 历史未完成的订单成交后不做任何动作
        if order_id in self.pending_orders.keys() or []:
            if order.status == "Filled":
                # TODO：是否需要统计
                cycle = self.pending_orders[order_id]
                net_profit = abs(round((cycle.close_price - cycle.open_price)*cycle.shares, 2))
                print(f" QUICK_CLOSE filled, {cycle.symbol} {cycle.shares} {cycle.open_action} @{cycle.open_price} - {cycle.close_action} @{cycle.close_price} profit: {net_profit}")
                
                # 已经平仓的订单从dict中删除
                del self.pending_orders[order_id]
            return 
        
        action = order.action # BUY or SELL
        # order_id = trade.order.orderId
        # print(f"Grid: handle OrderUpdate for OrderID {order_id} ({action}), Status: {order_status.status}, Filled: {order_status.filled}@{order_status.lastFillPrice:.2f}")
        # 根据trade.order.limprice和trade.order.action获取对应的网格单元
        lmtprice, action = order.lmt_price, order.action
        # 只有成交事件才刷新当前市场价格
        if order.status == "Filled":
            # After a fill, re-evaluate active grids for new opening orders
            self.maintain_active_grids_status(lmtprice, action.upper() == "BUY")
        
        # 函数内部会检查订单状态
        offset = self.find_the_grid_unit(lmtprice, action.upper() == "BUY")
        if offset:
            # print(f"Grid: handle OrderUpdate for OrderID {order_id} ({action}), Status: {order_status.status}, Filled: {order_status.filled}@{order_status.lastFillPrice:.2f}")
            self.order_status_update(self.units[offset], order)
        
        return 
    
    
    # 维护网格状态：激活、失活。当某个网格从激活变为失活时对其下的建仓订单执行取消操作。
    def maintain_active_grids_status(self, current_market_price: float, is_buy: bool, is_init: bool = False):
        
        # 直接遍历网格，与网格的具体价格解耦，适应不同的网格策略。
        # 先找到当前价格处于哪个网格
        offset = self.find_the_grid_unit(current_market_price, is_buy)
        if not offset:
            return
        
        # 以当前网格之后的第2个网格的买入价格作为最低价格
        active_lower_price = self.lower_bound
        if offset + 2 < len(self.units):
            active_lower_price = self.units[offset+2].buy_price
        # 以当前网格之前的第2个网格的卖出价格作为最高价格
        active_upper_price = self.upper_bound
        if offset - 2 >= 0:
            active_upper_price = self.units[offset-2].sell_price
        
        buy_prices_low, buy_price_high = round(active_lower_price, 2), round(self.units[offset].buy_price, 2)
        sell_price_low, sell_price_high = round(self.units[offset].sell_price, 2), round(active_upper_price, 2)
        # 如果是策略初始化挂单，当前网格上同时挂卖单和买单；如果是运行时有订单成交触发的，当前网格上不挂单，由事件响应函数处理当前网格的挂单操作。
        # if not is_init:
        #     buy_price_high = round(buy_price_high - 0.01, 2)
        #     sell_price_low = round(sell_price_low + 0.01, 2)
            
        # print(f" maintain: {is_init} {buy_prices_low} - {buy_price_high}, {sell_price_low} - {sell_price_high}")
        for unit in self.units or []:
            # 建仓单：低于当前价格的买单、高于当前价格的卖单。任一时刻都有3个网格处于激活状态
            if buy_prices_low <= unit.buy_price <= buy_price_high or sell_price_low <= unit.sell_price <= sell_price_high:
                # 已经激活的不用处理
                if unit.is_active():
                    continue
                
                unit.status = "active"
                if buy_prices_low <= unit.buy_price < buy_price_high or (is_init and unit.buy_price == buy_price_high):
                    # 当前价格刚好是网格买价5时可以挂买单
                    self.buy(unit, "OPEN")
                if sell_price_low < unit.sell_price <= sell_price_high or (is_init and unit.sell_price == sell_price_low):
                    # 建仓卖单：高于当前价格的卖单。
                    self.sell(unit, "OPEN")
            else:
                # 对于不在激活范围内并且原状态是激活的，执行取消订单，并设置失活状态
                if unit.status == "active":
                    unit.status = "deactive"
                    self.cancel_open_orders(unit)
                    
        # for unit in self.units:
        #     if unit.is_active():
        #         print(f"unit: {unit.status} {unit.buy_price} - {unit.sell_price} {unit.cost_per_grid}")
        return 
    
    def _get_persistence_file_path(self) -> str:
        return os.path.join(self.data_dir, PENDING_ORDERS_FILE_TPL.format(strategy_id=self.strategy_id.replace("/", "_"))) # Sanitize ID for filename

    # 从文件中读取历史未完成的平仓单，直接挂单，不再参与网格策略
    async def _load_active_grid_cycles(self):
        file_path = self._get_persistence_file_path()
        try:
            with open(file_path, 'r') as f:
                cycles_data = json.load(f) # Should be a list of cycle dicts
                for data in cycles_data:
                    cycle = LiveGridCycle.from_dict(data)
                    # # 历史未完成订单，这部分只需要平仓即可
                    # 目标是尽快平仓，使用新的purpose：QUICK_CLOSE，与普通平仓区分开
                    order = await self.place_grid_leg_order(cycle.close_action, cycle.close_price, cycle.shares, "QUICK_CLOSE")
                    # 将提交的order收集起来
                    if order:
                        cycle._close_order = order
                        self.pending_orders[order.orderId] = cycle
                print(f"Loaded {len(self.pending_orders.keys())} active grid cycles for {self.strategy_id} from {file_path}")
        except FileNotFoundError:
            # print(f"No pending grid cycles file for {self.strategy_id} ('{file_path}'). Starting fresh for this strategy.")
            pass
        except json.JSONDecodeError:
            print(f"Error decoding JSON for {self.strategy_id} from '{file_path}'. Starting fresh.")


    def _save_active_grid_cycles(self):
        file_path = self._get_persistence_file_path()
        # 把pending orders中未完成的部分也写入到文件
        active_to_save = [cycle.to_dict() for cycle in self.pending_orders.values() or []]
        
        for cycle in self.close_orders.values() or []:
            active_to_save.append(cycle.to_dict())
        if active_to_save:
            try:
                with open(file_path, 'w') as f:
                    json.dump(active_to_save, f, indent=4)
            except Exception as e:
                print(f"Error saving active grid cycles for {self.strategy_id} to {file_path}: {e}")
        elif os.path.exists(file_path): # If no active cycles to save, remove old file
            try: os.remove(file_path)
            except Exception as e: print(f"Error removing old pending file {file_path}: {e}")

    def DoStop(self) -> Any:
        print(f"stop strategy: {self} ------")
        # self._save_active_grid_cycles()
        # pending list 也需要取消
        for cycle in self.pending_orders.values() or []:
            self.cancel_order(cycle._close_order)
        self.pending_orders = {}
        
        # 取消所有已提交的订单
        self.cancel_all_orders()
        # 等待几秒，等待取消订单操作处理完成。
        time.sleep(5)
        self.account.Summy(self.symbol, 611)

        if self.completed_count > 0:
            print(f" Net Profit: {round(self.net_profit, 2)},  Completed Count: {self.completed_count},  AVG Time Cost: open({round(self.total_open_cost_time/self.completed_count, 2)}) close({round(self.total_close_cost_time/self.completed_count, 2)})")

        return {"proportion": self.proportion, "grid_type": self.grid_price_type, "net_profit": self.net_profit}
