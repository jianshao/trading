#!/usr/bin/python3
import asyncio
from dataclasses import dataclass, field
import datetime
import json
import os
import random
import sys
import time
from typing import Dict, List, Any, Optional, Tuple, Callable
import pandas as pd

from strategy.common import OrderStatus, GridOrder, LiveGridCycle
from strategy.strategy import Strategy

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.api import BaseAPI

# --- Constants ---
PENDING_ORDERS_FILE_TPL = "{strategy_id}_pending_cycles.json" # For persisting active grid cycles
# 是否使用优化选项
DO_OPTIMIZE = True


@dataclass
class GridUnit:
    price: float
    quantity: float
    open_order: Optional['GridOrder'] = None
    close_order: Optional['GridOrder'] = None
    completed_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """将 GridUnit 对象转换为字典"""
        return {
            'price': self.price,
            'quantity': self.quantity,
            'open_order': self.open_order.to_dict() if self.open_order else None,
            'close_order': self.close_order.to_dict() if self.close_order else None,
            'completed_count': self.completed_count
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GridUnit':
        """从字典创建 GridUnit 对象"""
        # 处理 GridOrder 对象
        open_order = None
        if data.get('open_order'):
            # 假设 GridOrder 也有 from_dict 方法
            open_order = GridOrder.from_dict(data['open_order'])
        
        close_order = None
        if data.get('close_order'):
            close_order = GridOrder.from_dict(data['close_order'])
        
        return cls(
            price=data['price'],
            quantity=data['quantity'],
            open_order=open_order,
            close_order=close_order,
            completed_count=data.get('completed_count', 0)
        )
        
    def __str__(self):
        return "{" + f" @{self.price}, quantity: {self.quantity}" + "}"
        
class GridStrategy(Strategy):
    def __init__(self, api: BaseAPI, strategy_id, symbol: str,
                 base_price: float, lowwer: float, upper: float, # 网格上下限
                 cost_per_grid: float, space_propor: float = 0.01,
                 max_orders: int = 0, # 最大单方向同时挂单数
                 spacing_ratio: float = 0, # 按比例增大网格价差，1.0为价差不变
                 position_sizing_ratio: float = 0, # 按比例增加每网格成本，1.0为成本不变
                 do_optimize: bool = False, num_when_optimize: int = 1,
                 get_order_id: Callable[[str], int] = None, data_file: str = "data/strategies/grid"): # For initial setup only
        self.data_file = data_file
        
        self.api = api
        self.strategy_id = strategy_id
        self.symbol = symbol
        
        # 策略参数
        self.base_price = base_price
        self.lower_bound = lowwer # 网格上下限默认使用过去一年的最低价和最高价
        self.upper_bound = upper
        self.space_propor = space_propor
        self.cost_per_grid = cost_per_grid  # 单网格成本
        self.max_orders = max_orders  # 最大单方向同时挂单数，0为不限制
        self.num_when_optimize = num_when_optimize  # 当开启优化选项时单次多买入或少卖出多少股
        self.do_optimize = do_optimize  # 是否开启优化，开启优化后可以逐步建仓。
        self.price_growth_ratio = spacing_ratio # 网格价差增长的比例，1.0为不增长
        self.cost_growth_ratio = position_sizing_ratio # 每个网格股数增长比例，1为不变化
        self.primary_exchange = "NASDAQ"
        # 上层传入的获取本地关联order_id，并且关联到对应策略的方法
        self.get_order_id: Callable[[str], int] = get_order_id
        
        self.init_cash = 0
        self.cash = 0
        self.position = 0
        self.init_position = 0
        
        # --- NEW: Dynamic Grid Generation ---
        # --- State Management (mostly unchanged, but now uses dynamic shares) ---
        # 运行时数据
        self.grid_definitions: Dict[Any, GridUnit] = {}
        self.open_orders: Dict[Any, LiveGridCycle] = {}
        self.close_orders: Dict[Any, LiveGridCycle] = {}
        self.pending_orders: Dict[Any, GridOrder] = {}
        self.order_id_2_unit: Dict[Any, GridUnit] = {}
        
        self.optimize_shares = 0
        
        # 统计数据
        self.trade_logs = []
        self.pending_sell_count = 0
        self.pending_sell_cost = 0
        self.pending_buy_count = 0
        self.pending_buy_cost = 0
        
        self.completed_count = 0
        self.net_profit = 0
        self.total_cost = 0
        
        self.profit_logs = []
        self.start_time = None
        

    def __str__(self):
        return (f"(ID={self.strategy_id}, Sym={self.symbol}, Space={self.space_propor*100:.1f}% Cost={self.cost_per_grid:.0f})")
    
    def generate_grid_upward(self, base_price, base_cost, grid_price_ratio, price_growth_ratio, cost_growth_ratio):
        """
        生成向上的网格列表
        
        参数:
        base_price: 基础价格（第一个网格的买入价格）
        base_cost: 基础成本（第一个网格的成本）
        grid_price_ratio: 网格价差比例（卖出价格相对买入价格的增长比例）
        price_growth_ratio: 网格价差增长比例（每个网格价差的增长比例）
        cost_growth_ratio: 单个网格成本增长比例（每个网格成本的增长比例）
        num_grids: 要生成的网格数量
        
        返回:
        网格列表，每个元素包含：buy_price, sell_price, quantity
        """
        
        grids = []
        current_buy_price = base_price
        current_cost = base_cost
        current_price_ratio = grid_price_ratio
        
        while True:
            if current_buy_price > self.upper_bound:
                break
            
            # 计算当前网格的股数
            quantity = current_cost / current_buy_price
            
            # 创建网格字典
            grids.append(GridUnit(round(current_buy_price, 2), int(quantity)))
            
            # 计算当前网格的卖出价格
            # 下一个网格的买入价格 = 当前网格的卖出价格
            current_buy_price = current_buy_price * (1 + current_price_ratio)
            
            # 下一个网格的成本增长
            current_cost = current_cost * (1 + cost_growth_ratio)
            
            # 下一个网格的价差比例增长
            current_price_ratio = current_price_ratio * (1 + price_growth_ratio)
        
        return grids


    def generate_grid_downward(self, base_price, base_cost, grid_price_ratio, price_growth_ratio, cost_growth_ratio):
        """
        生成向下的网格列表
        
        参数:
        base_price: 基础价格（最后一个网格的卖出价格）
        base_cost: 基础成本（最后一个网格的成本）
        grid_price_ratio: 网格价差比例
        price_growth_ratio: 网格价差增长比例
        cost_growth_ratio: 单个网格成本增长比例
        num_grids: 要生成的网格数量
        
        返回:
        网格列表，每个元素包含：buy_price, sell_price, quantity
        """
        
        grids = []
        # 从基础价格开始向下计算
        current_sell_price = base_price
        current_cost = base_cost
        current_price_ratio = grid_price_ratio
        
        while True:
            # 计算当前网格的买入价格
            buy_price = current_sell_price / (1 + current_price_ratio)
            
            if buy_price < self.lower_bound:
                break
            
            # 计算当前网格的股数
            quantity = current_cost / buy_price
            
            # 创建网格字典
            grids.append(GridUnit(round(buy_price, 2), int(quantity)))
            
            # 下一个网格的卖出价格 = 当前网格的买入价格
            current_sell_price = buy_price
            current_price_ratio = current_price_ratio * (1 + price_growth_ratio)
            current_cost = current_cost * (1 + cost_growth_ratio)
        
        # 反转列表，让价格从低到高排序
        grids.reverse()
        
        return grids


    def _generate_dynamic_grids(self, current_price, up_grids=60, down_grids=40):
        """
        生成双向网格：以当前价格为基础，生成向上和向下的网格
        
        参数:
        current_price: 当前价格（基础价格）
        base_cost: 基础成本
        grid_price_ratio: 网格价差比例
        price_growth_ratio: 网格价差增长比例
        cost_growth_ratio: 单个网格成本增长比例
        up_grids: 向上生成的网格数量
        down_grids: 向下生成的网格数量
        
        返回:
        dict: {'up_grids': [...], 'down_grids': [...], 'all_grids': [...]}
        """
        result = {}
        
        # 生成向上的网格
        if up_grids > 0:
            up_grid_list = self.generate_grid_upward(
                base_price=current_price,
                base_cost=self.cost_per_grid,
                grid_price_ratio=self.space_propor,
                price_growth_ratio=self.price_growth_ratio,
                cost_growth_ratio=self.cost_growth_ratio
            )
            result['up_grids'] = up_grid_list
        else:
            result['up_grids'] = []
        
        # 生成向下的网格
        if down_grids > 0:
            down_grid_list = self.generate_grid_downward(
                base_price=current_price,
                base_cost=self.cost_per_grid,
                grid_price_ratio=self.space_propor,
                price_growth_ratio=self.price_growth_ratio,
                cost_growth_ratio=self.cost_growth_ratio
            )
            result['down_grids'] = down_grid_list
        else:
            result['down_grids'] = []
        
        # 合并所有网格，按价格从低到高排序
        all_grids = result['down_grids'] + result['up_grids']
        all_grids.sort(key=lambda x: x.price)
        for grid in all_grids:
            self.grid_definitions[grid.price] = grid
        

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
                [price for price in self.grid_definitions.keys() if price <= current_price],
                reverse=True
            )

        self.log(f"Target Sell Levels: {target_buy_levels}", level=0)
        for price in target_buy_levels:
            unit = self.grid_definitions[price]
            # 当前网格不是空闲的
            if unit.open_order:
                continue
            # 现有资金仍然可以挂买单
            if round(self.pending_buy_cost + unit.quantity * unit.price, 2) > round(self.cash):
                continue
            order = asyncio.get_event_loop().run_until_complete(self.grid_buy(purpose="OPEN", price=unit.price, size=unit.quantity))
            if order:
                self.order_id_2_unit[order.order_id] = unit
                unit.open_order = order
            
        
        target_sell_levels = sorted(
                [price for price in self.grid_definitions.keys() if price > current_price]
            )
        
        self.log(f"Target Buy Levels: {target_sell_levels}", level=0)
        for price in target_sell_levels:
            unit = self.grid_definitions[price]
            if unit.open_order:
                continue
            if self.pending_sell_count + unit.quantity > self.position:
                continue
            
            order = asyncio.get_event_loop().run_until_complete(self.grid_sell(purpose="OPEN", price=unit.price, size=unit.quantity))
            if order:
                # self.log(f"Price {unit.sell_price:.2f} is in core sell zone and free. Placing SELL order for {unit} {order.order_id} shares.", level=1)
                self.order_id_2_unit[order.order_id] = unit
                unit.open_order = order
            
    # 根据订单情况决定优化的股数，针对价值属性高的标的可以逐步建仓。
    def optimize(self, price, action):
        # 没有打开优化选项
        if not self.do_optimize:
            return 0
        
        if action.upper() == "SELL":
            return 0
        
        # 低于基础价格的卖出和高于基础价格的买入不触发优化
        if (action == "SELL" and price < self.base_price) or (action == "BUY" and price > self.base_price):
            return 0
        
        self.optimize_shares += 1
        # 根据当前价格与基础价格的偏移比例使用随机控制，如果触发优化则多或少1股（目前是1股）。
        # 选择的标的波动本身都比较小，直接使用偏移比例触发优化概率很低，扩大4倍
        prop = abs((price-self.base_price)/self.base_price)*100*4
        if random.randint(1, 100) <= prop:
            self.optimize_shares += self.num_when_optimize*100
            return self.num_when_optimize
        return 0
    
    async def grid_buy(self, purpose: str, price: float, size: float) -> Optional[GridOrder]:
        size += self.optimize(price, "BUY")
        # 对应网格未激活时不能提交订单，只针对建仓订单
        
        order_id = None
        if self.get_order_id:
            order_id = self.get_order_id(self.strategy_id)
            
        order = await self.api.place_limit_order(self.symbol, "BUY", quantity=size, limit_price=price, order_id_to_use=order_id)
        if order:
            cost = round(price * size, 2)
            self.pending_buy_count += abs(size)
            self.pending_buy_cost = round(self.pending_buy_cost + abs(cost), 2)
            self.log(f"Place BUY order, Price: {price:.2f}, Qty: {size} Id: {order.order_id}", level=0)
        return order
      
    async def grid_sell(self, purpose: str, price: float, size: float) -> Optional[GridOrder]:
        size -= self.optimize(price, "SELL")
        order_id = None
        if self.get_order_id:
            order_id = self.get_order_id(self.strategy_id)
        self.pending_sell_count += abs(size)
        self.pending_sell_cost = round(self.pending_sell_cost + abs(price * size), 2)
        sell_order = await self.api.place_limit_order(self.symbol, "SELL", quantity=size, limit_price=price, order_id_to_use=order_id)
        if sell_order:
            self.log(f"Place SELL order, Price: {price:.2f}, Qty: {size} Id: {sell_order.order_id}", level=0)
        return sell_order

    
    def _log_completed_trade(self, open_order: GridOrder, close_order: GridOrder):
        """Logs the details of a completed grid cycle to self.trade_logs."""
        log_entry = {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "open_order_id": open_order.order_id,
            "open_shares": open_order.shares,
            "open_action": "SELL" if close_order.isbuy() else "BUY",
            "open_price": open_order.lmt_price,
            # "open_apply_time": open_order,
            # "open_done_time": open_order.open_done_time,
            "close_order_id": close_order.order_id,
            "close_action": close_order.action,
            "close_price": close_order.lmt_price,
            # "close_apply_time": close_order.close_apply_time,
            # "close_done_time": close_order.close_done_time,
            # "fee": cycle.open_fee + cycle.close_fee
        }
        
        # 计算净利润
        self.log(f"open_order: {open_order}, close_order: {close_order}", level=0)
        gross_profit = abs(round((close_order.done_price - open_order.done_price) * close_order.done_shares, 2))
        log_entry["gross_profit"] = gross_profit
        net_profit = round(gross_profit - 2*close_order.fee, 2)
        log_entry["net_profit"] = net_profit
        
        self.log(f"GRID CYCLE ({'BUY-SELL' if open_order.isbuy() else 'SELL-BUY'}) COMPLETED for grid @{open_order.lmt_price:.2f} - @{close_order.lmt_price:.2f} {net_profit}", level=0)
        
        self.net_profit += net_profit
        self.completed_count += 1
        # unit.completed_count += 1
        # self.total_open_cost_time += round((cycle.open_done_time - cycle.open_apply_time))
        # self.total_close_cost_time += round((cycle.close_done_time - cycle.close_apply_time))
        self.trade_logs.append(log_entry)
        return

        
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
        
    
    def Reconnect(self, **kwargs):
        # 刷新api
        self.api = kwargs.get("api", None)
    
    # 策略初始化
    def InitStrategy(self, current_market_price, position, cash):
        self.log(f"Strategy {self} Initialling...", level=1)
        # 根据标的历史数据计算当前策略参数
        # atr = await self.api.get_atr(self.contract)
        
        # 达不到运行条件的直接退出
        # if not await self.check_before_running(atr):
        #     return 

        self.start_time = datetime.datetime.now()
        # # 计算并生成网格列表
        self._generate_dynamic_grids(self.base_price) # New method to generate grids based on params
        self.log(f"Strategy Initialized with Dynamic Grids.", level=0)
        self.log(f"  Grids (price: shares): { {k:v.__str__() for k,v in self.grid_definitions.items()} }", level=0)
        
        # 从文件中载入未完成的历史平仓单
        self._load_active_grid_cycles()
        
        self.init_position = position if self.init_position == 0 else self.init_position
        self.position = self.init_position
        self.init_cash = cash if self.init_cash == 0 else self.init_cash
        self.cash = self.init_cash
        
        # 使用开盘价激活网格
        self.maintain_active_grid_orders(current_market_price)
        
        self.log(f" 价格基线：{self.base_price}, 价格范围：[{self.lower_bound}, {self.upper_bound}], 单格投入：{self.cost_per_grid} 单格价差：{self.space_propor*100:.1f}%", level=1)
        self.log(f" 当前持仓：{self.position:.0f} 可用资金：{self.cash} 是否开启优化：{self.do_optimize} 优化股数：{self.num_when_optimize}", level=1)
        self.log(f"Strategy {self} Running.", level=1)
        return 

    def log(self, txt, level=0):
        """ Logging function for this strategy"""
        if level > 0:
            print(f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {txt}')

    def _find_the_unit(self, price: float, isbuy: bool):
        price = round(price, 2)
        if isbuy:
            return self.grid_definitions.get(price, None)
        
        for unit in self.grid_definitions.values():
            if price == round(unit.sell_price, 2):
                return unit
        return None

    # 查找当前网格的下一个网格，如果当前网格是买入网格，则返回对应的卖出网格；如果当前网格是卖出网格，则返回对应的买入网格。
    def _find_next_unit(self, curr_price: float, isbuy: bool) -> Optional[GridUnit]:
        """
        Returns the GridUnit for the given price and action (buy/sell).
        If no unit found, returns None.
        """
        # 当前网格是买入网格，找到对应的卖出网格
        if isbuy:
            # 找到当前价格的下一个网格
            prices = sorted(
                [price for price in self.grid_definitions.keys() if price > curr_price]
            )
            if prices:
                return self.grid_definitions[prices[0]]
        else:
            # 找到当前价格的上一个网格
            # 只考虑当前价格以上的网格
            # 这里curr_price是当前价格，price是网格的买入价格
            prices = sorted(
                [price for price in self.grid_definitions.keys() if price < curr_price],
                reverse=True
            )
            if prices:
                return self.grid_definitions[prices[0]]
        return None
    
    # 策略下的订单状态更新
    async def update_order_status(self, order: GridOrder):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        if order.status in [OrderStatus.Submitted, OrderStatus.Accepted]:
            return
        
        self.log(f"Reciver order: {order.order_id} {order.action} {order.lmt_price} {order.shares} {order.status}", level=0)
        if order.order_id not in self.order_id_2_unit.keys():
            self.log(f"unknown order ref: {order.order_id}.")
            return 

        unit = self.order_id_2_unit[order.order_id]
        if order.status in [OrderStatus.Completed]:
            # 订单成交更新持仓和成本
            # 统计数据要使用成交价和成交数量
            if order.isbuy():
                self.position += abs(order.done_shares)
                self.cash -= abs(order.done_price*order.done_shares)
            
                self.pending_buy_count -= abs(order.done_shares)
                self.pending_buy_cost = round(self.pending_buy_cost - abs(order.done_shares * order.done_price), 2)
                self.log(f"BUY: Updated Position: {self.position}, Cash: {round(self.cash)}", level=0)
            else:
                self.position -= abs(order.done_shares)
                self.cash += abs(order.done_price*order.done_shares)
                
                self.pending_sell_count -= abs(order.done_shares)
                self.pending_sell_cost = round(self.pending_sell_cost - abs(order.done_shares * order.done_price), 2)
                self.log(f"SOLD: Updated Position: {self.position}, Cash: {round(self.cash)}", level=0)

                
            # 成交价可能与限价不一样，更新对应网格的状态要用限价
            next_unit = self._find_next_unit(order.lmt_price, order.isbuy())
            if order.isbuy():
                self.log(f'BUY EXECUTED, Lmt Price: {order.lmt_price:.2f}, Done Price: {order.done_price} Qty: {order.done_shares:.0f}', level=0)
            
                # 找到对应网格，提交使用的限价一定是网格的买入价格
                if unit.open_order and order.order_id == unit.open_order.order_id:
                    # 一定要记得刷新建仓单，否则统计时拿不到建仓单的成交价
                    order.apply_time = unit.open_order.apply_time
                    unit.open_order = order
                    # 使用对应的网格提交卖单
                    sell_order = await self.grid_sell(purpose="CLOSE", price=next_unit.price, size=next_unit.quantity)
                    if sell_order:
                        self.order_id_2_unit[sell_order.order_id] = next_unit
                        # 记录平仓单
                        if next_unit.close_order:
                            self.log(f"Canceling existing close order: {next_unit.close_order.order_id} @{next_unit.close_order.lmt_price} {next_unit.close_order.shares} shares", level=1)
                            self.api.cancel_order(next_unit.close_order)
                        next_unit.close_order = sell_order
                        # 更新当前网格状态
                        
                elif unit.close_order and order.order_id == unit.close_order.order_id:
                    # 盈利统计
                    self._log_completed_trade(next_unit.open_order, order)
                    
                    # 重新开仓
                    sell_order = await self.grid_sell(purpose="CLOSE", price=next_unit.price, size=next_unit.quantity)
                    if sell_order:
                        unit.close_order = None
                        next_unit.open_order = sell_order
                        self.order_id_2_unit[sell_order.order_id] = next_unit

            else:
                
                self.log(f'SELL EXECUTED, LmtPrice: {order.lmt_price} DonePrice: {order.done_price:.2f}, Qty: {order.done_shares:.0f} Id: {order.order_id}', level=0)
                if unit.open_order and  order.order_id == unit.open_order.order_id:
                    order.apply_time = unit.open_order.apply_time
                    unit.open_order = order
                    # 建仓单成交，提交平仓单
                    buy_order = await self.grid_buy(purpose="CLOSE", price=next_unit.price, size=next_unit.quantity)
                    if buy_order:
                        self.order_id_2_unit[buy_order.order_id] = next_unit
                        if next_unit.close_order:
                            self.log(f"Canceling existing close order: {next_unit.close_order.order_id} @{next_unit.close_order.lmt_price} {next_unit.close_order.shares} shares", level=1)
                            self.api.cancel_order(next_unit.close_order)
                        next_unit.close_order = buy_order
                    
                elif unit.close_order and order.order_id == unit.close_order.order_id:
                    self._log_completed_trade(next_unit.open_order, order)
                    
                    buy_order = await self.grid_buy(purpose="CLOSE", price=next_unit.price, size=next_unit.quantity)
                    if buy_order:
                        self.order_id_2_unit[buy_order.order_id] = next_unit
                        next_unit.open_order = buy_order
                        unit.close_order = None
                
            # 成交价可能与提交的限价不一样，此处要以成交价刷新网格
            # self.maintain_active_grid_orders(order.done_price)

        elif order.status in [OrderStatus.Canceled, OrderStatus.Margin, OrderStatus.Rejected, OrderStatus.Expired]:
            if not order.lmt_price:
                print(f"no lmt price: {order}")
            self.log(f'Order Canceled/Margin/Rejected/Expired: {order.status} Ref {order.order_id} {order.shares}')
            if not order.isbuy():
                self.pending_sell_count -= abs(order.shares)
                self.pending_sell_cost = round(self.pending_sell_cost - abs(order.lmt_price * order.shares), 2)
            else:
                self.pending_buy_count -= abs(order.shares)
                self.pending_buy_cost = round(self.pending_buy_cost - abs(order.lmt_price * order.shares), 2)
            
            if unit.open_order and unit.open_order.order_id == order.order_id:
                unit.open_order = None
            elif unit.close_order and unit.close_order.order_id == order.order_id:
                unit.close_order = None
        
        # 不管是成交还是取消，都需要将该订单删除
        del self.order_id_2_unit[order.order_id]
            
    def recover_curr_position(self):
        """从历史日志中恢复当前持仓"""
        if not self.profit_logs or len(self.profit_logs) == 0:
            return
        
        log = self.profit_logs[-1]
        self.init_position = log.get('position', [0, 0])[1]
        self.init_cash = log.get('cash', [0, 0])[1]
        self.log(f"Recovered position from logs. Position: {self.init_position}, Total Cost: {self.init_cash}", level=0)
        return

    # 从文件中读取历史未完成的平仓单，直接挂单，不再参与网格策略
    def _load_active_grid_cycles(self):
        file_path = self.data_file
        try:
            with open(file_path, 'r') as f:
                data = json.load(f) # Should be a list of cycle dicts
                self.profit_logs = data.get('profits', [])
                self.recover_curr_position()
                
                units = data.get('units', [])
                for unit in units:
                    # 将网格单元转换成结构体
                    old_unit = GridUnit.from_dict(unit)
                    # 新旧网格可能不一致，使用买入价去匹配
                    curr_unit = self._find_the_unit(old_unit.price, True)
                    if not curr_unit:
                        continue
                    # 如果存在建仓单，重新提交，并更新到现有网格中。网格内已有建仓单的不再提交挂单
                    if old_unit.open_order and not curr_unit.open_order:
                        old_open_order = old_unit.open_order
                        self.log(f" Order From File: {old_open_order}", level=0)
                        # 已经成交的订单处于统计时考虑也会放在文件中
                        if old_open_order.status != OrderStatus.Completed:
                            if old_open_order.isbuy():
                                new_order = asyncio.get_event_loop().run_until_complete(self.grid_buy("OPEN", old_open_order.lmt_price, old_open_order.shares))
                            else:
                                new_order = asyncio.get_event_loop().run_until_complete(self.grid_sell("OPEN", old_open_order.lmt_price, old_open_order.shares))
                            if new_order:
                                curr_unit.open_order = new_order
                                self.order_id_2_unit[new_order.order_id] = curr_unit
                        else:
                            # 已经成交的订单不需要重新提交，记录即可
                            curr_unit.open_order = old_open_order
                    
                    # 平仓单同理
                    if old_unit.close_order and not curr_unit.close_order:
                        old_close_order = old_unit.close_order
                        if old_close_order.status != OrderStatus.Completed:
                            if old_close_order.isbuy():
                                new_order = asyncio.get_event_loop().run_until_complete(self.grid_buy("CLOSE", old_close_order.lmt_price, old_close_order.shares))
                            else:
                                new_order = asyncio.get_event_loop().run_until_complete(self.grid_sell("CLOSE", old_close_order.lmt_price, old_close_order.shares))
                            if new_order:
                                curr_unit.close_order = new_order
                                self.order_id_2_unit[new_order.order_id] = curr_unit
                        else:
                            # 该分支正常不会被触发
                            curr_unit.close_order = old_close_order
                            
                self.log(f"Loaded {len(units)} active grid cycles for {self.strategy_id} from {file_path}")
        except FileNotFoundError:
            # print(f"No pending grid cycles file for {self.strategy_id} ('{file_path}'). Starting fresh for this strategy.")
            pass
        except json.JSONDecodeError:
            self.log(f"Error decoding JSON for {self.strategy_id} from '{file_path}'. Starting fresh.")


    def reorganize_profits(self):
        """ Reorganizes the profit logs to ensure they are sorted by start time. and one log per day.
        """
        if not self.profit_logs:
            return []
        
        # 将同一天的利润日志合并
        merged_logs = {}
        for log in self.profit_logs:
            date_str = log['start_time'][:10]  # 只取日期部分
            if date_str not in merged_logs:
                merged_logs[date_str] = log
            else:
                # 合并利润和计数
                merged_logs[date_str]['profit'] += log['profit']
                merged_logs[date_str]['completed_count'] += log['completed_count']
                merged_logs[date_str]['position'][1] = log['position'][1]
                merged_logs[date_str]['cash'][1] = log['cash'][1]
                
                if log['start_time'] < merged_logs[date_str]['start_time']:
                    merged_logs[date_str]['start_time'] = log['start_time']
                if log['end_time'] > merged_logs[date_str]['end_time']:
                    merged_logs[date_str]['end_time'] = log['end_time']
                    
        # 将dict转换成list
        merged_logs = list(merged_logs.values())
        # 按照开始时间排序
        merged_logs.sort(key=lambda x: datetime.datetime.strptime(x['start_time'], "%Y-%m-%d %H:%M:%S"))
        return merged_logs

    def _save_active_grid_cycles(self):
        file_path = self.data_file
        # 把pending orders中未完成的部分也写入到文件
        active_to_save = []
        for unit in self.grid_definitions.values():
            if unit.open_order or unit.close_order:
                active_to_save.append(unit.to_dict())
        
        # 如果没有未完成的网格单元，说明出错了，不需要保存
        if len(active_to_save) == 0:
            self.log(f"No active grid cycles to save for {self.strategy_id}.", level=0)
            return

        self.profit_logs.append({
            "start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "position": [round(self.init_position, 2), round(self.position, 2)],
            "cash": [round(self.init_cash, 2), round(self.cash, 2)],
            "completed_count": self.completed_count,
            "profit": round(self.net_profit, 2),
        })
        data = {
            "profits": self.reorganize_profits(),
            "units": active_to_save
        }
        try:
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving active grid cycles for {self.strategy_id} to {file_path}: {e}")

    def DoStop(self):
        self.log(f"Stop Strategy: {self}", level=1)
        self._save_active_grid_cycles()
        cancel_tasks = []
        for unit in self.grid_definitions.values():
            if unit.open_order:
                cancel_tasks.append(self.api.cancel_order(unit.open_order))
            if unit.close_order:
                cancel_tasks.append(self.api.cancel_order(unit.close_order))
        time.sleep(2)  # 等待订单取消完成

        if self.completed_count > 0:
            self.log(f"Pos: {self.position} Completed: {self.completed_count}, Profit: {round(self.net_profit, 2)}, Avg: {round(self.net_profit/self.completed_count, 2)} Pending: Buy({self.pending_buy_count}, {self.pending_buy_cost}) Sell({self.pending_sell_count}, {self.pending_sell_cost})", level=1)
        
        return {"spacing_ratio": self.price_growth_ratio, "position_sizing_ratio": self.cost_growth_ratio, "net_profit": round(self.net_profit, 2)}

    def daily_summy(self, date_str: str) -> str:
        pending_buy_order_count_map = {}
        pending_sell_order_count_map = {}
        for ref, cycle in self.close_orders.items():
            order = cycle.open_order
            price, shares = round(order.lmt_price), order.shares
            if not order.isbuy():
                if price not in pending_buy_order_count_map.keys():
                    pending_buy_order_count_map[price] = 0
                pending_buy_order_count_map[price] += abs(shares)
            else:
                if price not in pending_sell_order_count_map.keys():
                    pending_sell_order_count_map[price] = 0
                pending_sell_order_count_map[price] += abs(shares)
        
        avg = 0
        if self.completed_count:
            avg = round(self.net_profit/self.completed_count, 2)
        return f"Completed: {self.completed_count:>3}, Profit: {round(self.net_profit, 2):>7.2f}, Pending: Buy({self.pending_buy_count:>3}, {self.pending_buy_cost:>8.2f}) Sell({self.pending_sell_count:>3}, {self.pending_sell_cost:>8.2f})"
