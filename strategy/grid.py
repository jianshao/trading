#!/usr/bin/python3
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import sys
import time
from typing import Dict, List, Any, Optional, Tuple, Callable
# from zoneinfo import ZoneInfo
import aiofiles
import pandas as pd

from data import config
from strategy.common import DailyProfitSummary, OrderStatus, GridOrder
from strategy.order_manager import OrderManager
from strategy.strategy import Strategy
from utils.kafka_producer import KafkaProducerService
from utils.logger_manager import LoggerManager

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.api import BaseAPI

# --- Constants ---
PENDING_ORDERS_FILE_TPL = "{strategy_id}_pending_cycles.json" # For persisting active grid cycles


@dataclass
class GridUnit:
    open_order: Optional['GridOrder'] = None
    close_order: Optional['GridOrder'] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """将 GridUnit 对象转换为字典"""
        return {
            'open_order': self.open_order.to_dict() if self.open_order else None,
            'close_order': self.close_order.to_dict() if self.close_order else None,
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
            open_order=open_order,
            close_order=close_order,
        )
        
    def __str__(self):
        return "{" + f" @{self.open_order.lmt_price} - {self.close_order.lmt_price}, quantity: {self.open_order.shares}" + "}"
        
class GridStrategy(Strategy):
    def __init__(self, 
        om: OrderManager,
        strategy_id: str,
        get_order_id: Callable[[str], int],
        **kwargs
    ): # For initial setup only
        # self.data_file = kwargs.get("data_file", "data/strategies/grid")
        LoggerManager.Info("app", strategy="GridStrategy", event="init", content=f"Initializing GridStrategy with params: {kwargs}")
        # ✅ 基础属性
        # self.api = api
        self.om = om
        self.strategy_id = strategy_id
        self.primary_exchange = "NASDAQ"

        # ✅ 策略参数
        # ✅ 统一配置默认参数并支持扩展
        defaults = {
            "symbol": "TQQQ",
            "limit_price_range": [80, 130],
            "max_orders_in_single_direction": 8,
            "cost_per_grid": 800,
            "ratio_per_grid": 0.008,
            "init_position": 0,
            "init_cash": 10000,
            "data_file": "data/strategies/grid",
        }
        defaults.update(kwargs)

        # ✅ 一次性展开成实例属性
        for key, value in defaults.items():
            setattr(self, key, value)

        # ✅ 注入回调方法
        self.get_order_id: Callable[[str], int] = get_order_id
        
        self.init_cash = 0
        self.init_position = 0
        self.cash = 0
        self.position = 0
        
        # --- NEW: Dynamic Grid Generation ---
        # --- State Management (mostly unchanged, but now uses dynamic shares) ---
        # 运行时数据
        self.close_units: Dict[Any, GridUnit] = {}
        self.open_orders: Dict[Any, GridOrder] = {}
        
        # 统计数据
        self.trade_logs = []
        self.pending_sell_count = 0
        self.pending_sell_cost = 0
        self.pending_buy_count = 0
        self.pending_buy_cost = 0
        
        self.completed_count = 0
        self.net_profit = 0
        self.extra_profit = 0
        self.total_cost = 0
        
        self.profit_logs = []
        
        self.start_time = datetime.now(config.time_zone)
        self.end_time = datetime.now(config.time_zone)
        self.is_running = False
        self.lock = asyncio.Lock()
        
        self._all_orders_done_event = asyncio.Event()
        self.producer: KafkaProducerService = kwargs.get("producer", None)  # To be set by StrategyEngine
        

    def __str__(self):
        return (f"(ID={self.strategy_id}, Sym={self.symbol}, Space={self.ratio_per_grid*100:.1f}% Cost={self.cost_per_grid:.0f})")
    
    
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
        gross_profit = abs(round((close_order.done_price - open_order.done_price) * close_order.done_shares, 2))
        base_profit = abs(round((close_order.lmt_price - open_order.lmt_price) * close_order.done_shares, 2))
        log_entry["gross_profit"] = gross_profit
        net_profit = round(gross_profit - 2*close_order.fee, 2)
        log_entry["net_profit"] = net_profit
        
        content = f"GRID CYCLE ({'BUY-SELL' if open_order.isbuy() else 'SELL-BUY'}) COMPLETED for grid @{open_order.lmt_price:.2f} - @{close_order.lmt_price:.2f} {net_profit}"
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="grid_cycle_completed", content=content)
        
        self.net_profit += net_profit
        self.extra_profit += (gross_profit - base_profit)
        self.completed_count += 1
        # unit.completed_count += 1
        # self.total_open_cost_time += round((cycle.open_done_time - cycle.open_apply_time))
        # self.total_close_cost_time += round((cycle.close_done_time - cycle.close_apply_time))
        self.trade_logs.append(log_entry)
        return
    
    def Reconnect(self, **kwargs):
        # 刷新api
        self.api = kwargs.get("api", None)

    # 策略初始化
    async def InitStrategy(self, current_market_price, position, cash):
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", params=f"{self}", event="init")

        self.start_time = datetime.now()
        # # 计算并生成网格列表
        # self._generate_dynamic_grids(self.base_price) # New method to generate grids based on params
        # LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"units", content=f"Grids (price: shares): { {k:v.__str__() for k,v in self.grid_definitions.items()} }")
        
        self.position = position
        self.cash = cash
        # 从文件中载入未完成的历史平仓单
        await self._load_active_grid_cycles(current_market_price)
        
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"价格范围：[{self.limit_price_range[0]}, {self.limit_price_range[1]}], 单格投入：{self.cost_per_grid} 单格价差：{self.ratio_per_grid*100:.1f}%")
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"当前持仓：{self.position:.0f} 可用资金：{self.cash}")
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"Running.")
        
        self.is_running = True
        return 

    def run(self):
        self.is_running = True

    
    def handle_order_cancelled(self, order: GridOrder):
        LoggerManager.Debug("order", strategy=f"{self.strategy_id}", event="order_cancel", content=f'Order Canceled/Margin/Rejected/Expired: {order.status} Ref {order.order_id} {order.shares}')
        if not order.isbuy():
            self.pending_sell_count -= abs(order.shares)
            self.pending_sell_cost = round(self.pending_sell_cost - abs(order.lmt_price * order.shares), 2)
        else:
            self.pending_buy_count -= abs(order.shares)
            self.pending_buy_cost = round(self.pending_buy_cost - abs(order.lmt_price * order.shares), 2)

        # 取消的是建仓单，直接从建仓单列表中移除
        if order.order_id in self.open_orders:
            del self.open_orders[order.order_id]
            
        # 如果是平仓单，只更新平仓单状态，不删除，要保留写文件
        if order.order_id in self.close_units:
            self.close_units[order.order_id].close_order = order
        
        # 最后策略停止运行的检查
        if self.is_running:
            return
        if len(self.open_orders) != 0:
            return
        for unit in self.close_units.values():
            if not self.is_final(unit.close_order.status):
                return

        # 所有订单都处理完毕，策略停止运行
        self._all_orders_done_event.set()
        

    async def grid_buy(self, price: float, size: float, order_ref: str = "") -> Optional[GridOrder]:
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="grid_buy", content=f"Place BUY order, Price: {price:.2f}, Qty: {size}")
        # 对应网格未激活时不能提交订单，只针对建仓订单
        # 现有资金仍然可以挂买单
        cost = abs(round(price * size, 2))
        if cost > self.cash - self.pending_buy_cost:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="grid_buy_failed", content=f"Not enough cash to place BUY order @{price:.2f} for {size} shares. Pending Buy Cost: {self.pending_buy_cost}, Cash: {self.cash}")
            return None
        
        order_id = None
        if self.get_order_id:
            order_id = self.get_order_id(self.strategy_id)
        
        order = await self.om.place_order(self.symbol, "BUY", size, price, callback=self.update_order_status, tif="DAY", order_id_to_use=order_id, order_ref=order_ref)
        if order:
            self.pending_buy_count += abs(size)
            self.pending_buy_cost = round(self.pending_buy_cost + cost, 2)
            LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="grid_buy", content=f"Place BUY order, Price: {price:.2f}, Qty: {size} Id: {order.order_id}")
        return order
      
    async def grid_sell(self, price: float, size: float, order_ref: str = "") -> Optional[GridOrder]:
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="grid_sell", content=f"Place SELL order, Price: {price:.2f}, Qty: {size}")
        if self.position - self.pending_sell_count < size:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="grid_sell_failed", content=f"Not enough position to place SELL order @{price:.2f} for {size} shares. Pending Sell Count: {self.pending_sell_count}, Position: {self.position}")
            return None
        
        order_id = None
        if self.get_order_id:
            order_id = self.get_order_id(self.strategy_id)

        sell_order = await self.om.place_order(self.symbol, "SELL", size, price, callback=self.update_order_status, tif="DAY", order_id_to_use=order_id, order_ref=order_ref)
        if sell_order:
            self.pending_sell_count += abs(size)
            self.pending_sell_cost = round(self.pending_sell_cost + abs(price * size), 2)
            # LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="grid_sell", content=f"Place SELL order, Price: {price:.2f}, Qty: {size} Id: {sell_order.order_id}")
        return sell_order

    
    async def reflesh_open_orders(self, dealed_order: GridOrder):
        """刷新建仓单"""
        # 如果成交的是买单，就挂一个更低位置的买单；如果成交的是卖单，就挂一个更高位置的卖单。
        # 取消当前的所有建仓单，这里只做提交，状态更新事件中会清理
        for order_id, order in self.open_orders.items():
            if order_id != dealed_order.order_id:
                await self.om.cancel_order(order_id)
        self.open_orders.pop(dealed_order.order_id, None)
        
        order_ref = {"strategy_id": self.strategy_id, "purpose": "OPEN"}
        # 提交新的建仓单
        buy_price = round(dealed_order.lmt_price * (1 - self.ratio_per_grid), 2)
        buy_order = await self.place_order("BUY", buy_price, order_ref)
        if buy_order:
            self.open_orders[buy_order.order_id] = buy_order
            
        sell_price = round(dealed_order.lmt_price * (1 + self.ratio_per_grid), 2)
        sell_order = await self.place_order("SELL", sell_price, order_ref)
        if sell_order:
            self.open_orders[sell_order.order_id] = sell_order


    async def place_order(self, action, price, order_ref) -> Optional[GridOrder]:
        """提交订单"""
        if action.upper() == "SELL":
            quantity = round(self.cost_per_grid / price)
            return await self.grid_sell(price, quantity, order_ref)
        else:
            quantity = round(self.cost_per_grid / price)
            return await self.grid_buy(price, quantity, order_ref)
    
    async def handle_order_dealed(self, order: GridOrder):
            
        # 订单成交更新持仓和成本
        # 统计数据要使用成交价和成交数量
        if order.isbuy():
            self.position += abs(order.done_shares)
            self.cash -= abs(order.done_price*order.done_shares)
        
            self.pending_buy_count -= abs(order.done_shares)
            self.pending_buy_cost = round(self.pending_buy_cost - abs(order.done_shares * order.done_price), 2)
            LoggerManager.Debug("order", strategy=f"{self.strategy_id}", event="order_deal", content=f'BUY Order EXECUTED, Lmt Price: {order.lmt_price:.2f}, Done Price: {order.done_price} Qty: {order.done_shares:.0f} Id: {order.order_id}')
        else:
            self.position -= abs(order.done_shares)
            self.cash += abs(order.done_price*order.done_shares)
            
            self.pending_sell_count -= abs(order.done_shares)
            self.pending_sell_cost = round(self.pending_sell_cost - abs(order.done_shares * order.done_price), 2)
            LoggerManager.Debug("order", strategy=f"{self.strategy_id}", event="order_deal", content=f'SELL Order EXECUTED, LmtPrice: {order.lmt_price} DonePrice: {order.done_price:.2f}, Qty: {order.done_shares:.0f} Id: {order.order_id}')

        # 如果是平仓单，记录完成的交易
        if order.order_id in self.close_units:
            unit = self.close_units[order.order_id]
            self._log_completed_trade(unit.open_order, order)
            self.close_units.pop(order.order_id, None)
            
        if order.order_id in self.open_orders:
            # 建仓单成交，移除建仓单，提交平仓单，刷新建仓单
            await self.reflesh_open_orders(order)
            order_ref = {"strategy_id": self.strategy_id, "purpose": "CLOSE", "open_order_id": f"{order.order_id}"}
            if order.isbuy():
                action = "SELL"
                price = round(order.lmt_price * (1+self.ratio_per_grid), 2)
            else:
                price = round(order.lmt_price * (1-self.ratio_per_grid), 2)
                action = "BUY"
            close_order = await self.place_order(action, price, order_ref)
            if close_order:
                self.close_units[close_order.order_id] = GridUnit(order, close_order)
        
        # 成交价可能与提交的限价不一样，使用限价网格移动更平滑

    
    # 策略下的订单状态更新
    async def update_order_status(self, order: GridOrder):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        if order.status in [OrderStatus.Submitted, OrderStatus.Accepted]:
            return
        
        LoggerManager.Info("order", strategy=f"{self.strategy_id}", event=f"order_update", content=f"Order Update: {order}")
        async with self.lock:
            if order.order_id not in self.open_orders and order.order_id not in self.close_units:
                LoggerManager.Error("order", strategy=f"{self.strategy_id}", event="order_update", content=f"Unknow order: {order}")
                return
            
            # 判断订单状态
            # 如果是取消订单，针对原订单是建仓单还是平仓单做不同处理
            if order.status in [OrderStatus.Canceled, OrderStatus.Cancelled, OrderStatus.Rejected, OrderStatus.Expired]:
                self.handle_order_cancelled(order)
            
            # 如果是订单成交，更新持仓和资金，执行对应网格的挂单
            # 使用成交订单的限制价格刷新网格，主要是更新网格生效范围
            if order.status in [OrderStatus.Completed]:
                await self.handle_order_dealed(order)
        
            
    def recover_curr_position(self):
        """从历史日志中恢复当前持仓"""
        if not self.profit_logs or len(self.profit_logs) == 0:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"init", content=f"No profit logs found to recover position.")
            return
        
        log = self.profit_logs[-1]
        self.init_position = log.get('position', [0, 0])[1]
        self.init_cash = log.get('cash', [0, 0])[1]
        
        self.init_position = self.position if self.init_position == 0 else self.init_position
        self.position = self.init_position
        self.init_cash = self.cash if self.init_cash == 0 else self.init_cash
        self.cash = self.init_cash
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"Recovered position from logs. Position: {self.init_position}, Total Cost: {self.init_cash}")

    async def build_init_open_orders(self, last_price: float):
        # last_price = await self.om.ib.get_latest_price(self.symbol)
        buy_price = round(last_price * (1-self.ratio_per_grid), 2)
        order_ref = {"strategy_id": self.strategy_id, "purpose": "OPEN"}
        new_order = await self.place_order("buy", buy_price, order_ref)
        if new_order:
            self.open_orders[new_order.order_id] = new_order
            
        sell_price = round(last_price * (1 + self.ratio_per_grid), 2)
        sell_order = await self.place_order("sell", sell_price, order_ref)
        if sell_order:
            self.open_orders[sell_order.order_id] = sell_order
        

    # 从文件中读取历史未完成的平仓单，直接挂单，不再参与网格策略
    async def _load_active_grid_cycles(self, current_market_price: float):
        file_path = self.data_file
        try:
            async with aiofiles.open(file_path, 'r') as f:
                content = await f.read()
                data = json.loads(content) # Should be a list of cycle dicts
                    
                # 从盈利历史中恢复可用持仓和资金
                self.profit_logs = data.get('profits', [])
                self.recover_curr_position()
                
                open_orders = data.get('open_orders', [])
                order_ref = {"strategy_id": self.strategy_id, "purpose": "OPEN"}
                if len(open_orders) != 0:
                    for order_data in open_orders:
                        order = GridOrder.from_dict(order_data)
                        # 恢复开仓单
                        new_order = await self.place_order(order.action, order.lmt_price, order_ref)
                        if new_order:
                            self.open_orders[new_order.order_id] = new_order
                else:
                    await self.build_init_open_orders(current_market_price)
                    
                    
                close_units_data = data.get('close_units', [])
                for unit_data in close_units_data:
                    unit = GridUnit.from_dict(unit_data)
                    if not self.is_final(unit.close_order.status):
                        # 恢复平仓单,只恢复未取消的平仓单，要刷新订单信息
                        order = unit.close_order
                        order_ref = {"strategy_id": self.strategy_id, "purpose": "CLOSE", "open_order_id": f"{unit.open_order.order_id}"}
                        new_order = await self.place_order(order.action, order.lmt_price, order_ref)
                        if new_order:
                            unit.close_order = new_order
                    self.close_units[unit.close_order.order_id] = unit

        except FileNotFoundError:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"init", content=f"No pending grid cycles file ('{file_path}'). Starting fresh for this strategy.")
        except json.JSONDecodeError:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"init", content=f"Error decoding JSON for {self.strategy_id} from '{file_path}'. Starting fresh for this strategy.")


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
                merged_logs[date_str]['profit'] += round(log['profit'], 2)
                merged_logs[date_str]['completed_count'] += log['completed_count']
                if 'position' not in merged_logs[date_str]:
                    merged_logs[date_str]['position'] = log['position'] if 'position' in log else [0, 0]
                if 'cash' not in merged_logs[date_str]:
                    merged_logs[date_str]['cash'] = log['cash'] if 'cash' in log else [0, 0]
                merged_logs[date_str]['position'][1] = log['position'][1] if 'position' in log else log['init_position']
                merged_logs[date_str]['cash'][1] = log['cash'][1] if 'cash' in log else 0
                
                if log['start_time'] < merged_logs[date_str]['start_time']:
                    merged_logs[date_str]['start_time'] = log['start_time']
                if log['end_time'] > merged_logs[date_str]['end_time']:
                    merged_logs[date_str]['end_time'] = log['end_time']
                    
        # 将dict转换成list
        merged_logs = list(merged_logs.values())
        # 按照开始时间排序
        merged_logs.sort(key=lambda x: datetime.strptime(x['start_time'], "%Y-%m-%d %H:%M:%S"))
        return merged_logs

    async def _save_active_grid_cycles(self):
        file_path = self.data_file
        # 把pending orders中未完成的部分也写入到文件

        self.profit_logs.append({
            "start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": datetime.now(config.time_zone).strftime("%Y-%m-%d %H:%M:%S"),
            "position": [round(self.init_position, 2), round(self.position, 2)],
            "cash": [round(self.init_cash, 2), round(self.cash + self.net_profit, 2)],
            "completed_count": self.completed_count,
            "profit": round(self.net_profit, 2),
        })
        self.profit_logs = self.reorganize_profits()
        data = {
            "profits": self.profit_logs,
            "close_units": [unit.to_dict() for unit in self.close_units.values()],
            "open_orders": [order.to_dict() for order in self.open_orders.values()],
        }
        try:
            async with aiofiles.open(file_path, 'w') as f:
                await f.write(json.dumps(data, indent=4))
                
        except Exception as e:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"Error saving active grid cycles for {self.strategy_id} to {file_path}: {e}")
    
    def is_final(self, status: OrderStatus) -> bool:
        return status in (OrderStatus.Canceled, OrderStatus.Cancelled, OrderStatus.Completed, OrderStatus.Rejected, OrderStatus.Expired)

    async def _cancel_active_orders(self):
        # LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"Cancelling all active orders for strategy {self.strategy_id}...")
        # 
        orders_to_cancel: List[GridOrder] = []
        for order in list(self.open_orders.values()):
            orders_to_cancel.append(order)

        for unit in self.close_units.values():
            orders_to_cancel.append(unit.close_order)

        # 发起取消（全部异步执行）
        for order in orders_to_cancel:
            try:
                await self.om.cancel_order(order.order_id)
            except Exception as e:
                LoggerManager.Error("app", content=f"Cancel failed {order.order_id}: {e}")
        return 
    
    async def DoStop(self):
        self.is_running = False
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"Stopping strategy {self.strategy_id}...")
        # 保存当前未完成的网格单元
        await self._save_active_grid_cycles()
        # 收集要取消的订单
        await self._cancel_active_orders()
        
        # 等待订单取消完成
        try:
            await asyncio.wait_for(self._all_orders_done_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            LoggerManager.Error("app", content="Timeout waiting for order cancellation")
        
        self.end_time = datetime.now(config.time_zone)
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"profit_summy", content=f"Pos: {self.position} Completed: {self.completed_count}, Profit: {round(self.net_profit, 2)}, Pending: Buy({self.pending_buy_count}, {self.pending_buy_cost}) Sell({self.pending_sell_count}, {self.pending_sell_cost})")
        return

    # 每日总结
    def DailySummary(self, date_str: str) -> DailyProfitSummary:
        """返回每日盈利总结字符串"""
        self.end_time = datetime.now(config.time_zone)
        params = {
            "total_profit": round(self.net_profit, 2),
            "extra_price": round(self.extra_profit, 2),
            "completed_count": self.completed_count,
            "before_position": round(self.init_position, 2),
            "before_cash": round(self.init_cash, 2),
            "after_position": round(self.position, 2),
            "after_cash": round(self.cash + self.net_profit, 2),
            "pending_buy_count": self.pending_buy_count,
            "pending_buy_cost": round(self.pending_buy_cost, 2),
            "pending_sell_count": self.pending_sell_count,
            "pending_sell_cost": round(self.pending_sell_cost, 2)
        }
        return DailyProfitSummary("grid", self.strategy_id, self.symbol, self.net_profit, position=self.position, cash=self.cash, date=date_str, params=params, start_time=self.start_time, end_time=self.end_time)
