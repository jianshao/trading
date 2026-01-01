#!/usr/bin/python3
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
import sys
from typing import Dict, List, Any, Optional
import aiofiles

from common import utils

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


from common.utils import DailyProfitSummary, OrderStatus, GridOrder
from base_models.price_range_predict import ATRRangeEstimator
from common.order_manager import OrderManager
from common.real_time_data_processer import RealTimeDataProcessor
from common.strategy import Strategy
from common.logger_manager import LoggerManager


CYCLE_DAYS_DEFAULT = 21

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
        **kwargs
    ): # For initial setup only
        LoggerManager.Info("app", strategy="GridStrategy", event="init", content=f"Initializing GridStrategy with params: {kwargs}")
        # ✅ 基础属性
        self.om = om
        self.strategy_id = strategy_id
        self.primary_exchange = "NASDAQ"

        # ✅ 策略参数
        # ✅ 统一配置默认参数并支持扩展
        defaults = {
            "symbol": "QQQ",
            "unique_tag": 1,
            "retention_fund_ratio": 0.2,
            "total_cost": 13000,
            "skip": False,
            "data_file": "data/strategies/grid",
            "ema_short_period": 10,
            "ema_middle_period": 20,
            "ema_long_period": 50,
        }
        defaults.update(kwargs)

        # ✅ 一次性展开成实例属性
        for key, value in defaults.items():
            setattr(self, key, value)
        
        self.init_cash = 0
        self.init_position = 0
        self.cash = self.total_cost
        self.position = 0
        
        # --- NEW: Dynamic Grid Generation ---
        # --- State Management (mostly unchanged, but now uses dynamic shares) ---
        # 运行时数据
        self.close_units: Dict[Any, GridUnit] = {}
        self.open_orders: Dict[Any, GridOrder] = {}
        self.clear_order = None
        self.all_clear_order = None
        self.base_position_order: Optional[GridOrder] = None
        
        # 统计数据
        self.trade_logs = []
        self.pending_sell_count = 0
        self.pending_sell_cost = 0
        self.pending_buy_count = 0
        self.pending_buy_cost = 0
        
        self.completed_count = 0
        self.net_profit = 0
        self.extra_profit = 0
        
        self.profit_logs = []
        
        self.not_today = False
        self.start_time = None
        self.end_time = None
        self.is_running = False
        self.stop_buy = False
        self.lock = asyncio.Lock()
        self.price_range = []
        self.grid_spread = 1
        self.cost_per_grid = 1000
        
        self._all_orders_done_event = asyncio.Event()
        self.realtime_data_processor: RealTimeDataProcessor = kwargs.get("real_data_processor", None)
        

    def __str__(self):
        return (f"(ID={self.strategy_id}, Sym={self.symbol}, Space={self.grid_spread:.1f} Cost={self.cost_per_grid:.0f})")
    
    
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
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="grid_cycle_completed", content=content)
        
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
    
    
    async def reflesh_grid_params(self, last_price: float):
        coifcients = {
            "TQQQ": 1.2,
            "NLY": 1.1,
            "QQQ": 1.15
        }
        atr = await self.realtime_data_processor.get_atr(self.symbol)
        re = ATRRangeEstimator(k=coifcients.get(self.symbol, 1.1))
        range = re.predict(last_price, atr, 14)
        upper = range.get("upper", 0)
        lower = range.get("lower", 0)

        grid_count = 20
        return [lower, upper], grid_count
        
    async def reflesh_config(self, data: Dict[str, Any], last_price: float) -> Dict[str, Any]:
        # 每双周做一次配置更新，包括网格价格上下限、单个成本
        runtime = data.get("runtimes", {})
        current_time = self.realtime_data_processor.get_current_time()
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="reflesh_config", content=f"Curr_time: {current_time}, runtime: {runtime}")
        if runtime:
            last_rebalance = utils.to_datetime(runtime.get('last_rebalance', ""))
            if last_rebalance and current_time - last_rebalance < timedelta(days=CYCLE_DAYS_DEFAULT):
                LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="rebalance_skip", content=f"No rebalance needed. Last rebalance at {last_rebalance}, current time {current_time}.")
                return data
        
        # --- 开始执行新周期重置 ---
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="rebalance", content=f"Rebalance at current time {current_time}.")
        price_range, grid_count = await self.reflesh_grid_params(last_price)
        
        # 新周期开启时先建50%底仓
        await self._cancel_active_orders()
        await self.build_base_position(last_price)
        
        return {
            "runtimes": {
                "total_cost": round(self.cash + self.position * last_price),
                "grid_spread": round((price_range[1]-price_range[0]) / grid_count, 2),
                "price_range": price_range,
                "cost_per_grid": round(self.total_cost / grid_count, 2),
                "last_rebalance": current_time,
            }
        }
    
    async def build_base_position(self, last_price: float):
        order = None
        # 开启新周期建立50%底仓
        target_value = self.total_cost * 0.5
        # 计算当前持仓市值 (包含之前周期被套住的持仓)
        diff_value = max(target_value - self.position * last_price, 0)
        quantity_to_buy = max(round(diff_value / last_price), 1)
        buy_price = round(last_price * 1.02, 2)
        content=f"Building base position. Target Value: {target_value}, Diff Value: {diff_value}, Buying Qty: {quantity_to_buy} at Price: {last_price}"
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="build_base_position", content=content)
        order = await self.grid_buy(buy_price, quantity_to_buy, order_ref="BASE_POSITION_BUILD", force=True)
        if order:
            self.base_position_order = order

        return order

    
    # 每双周重新均衡，生成新的价格上下限和中线
    async def rebalance(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """每双周重新均衡，生成新的价格上下限和中线"""
        last_price = await self.realtime_data_processor.get_latest_price(self.symbol)
        if not await self.is_grid_friendly_day():
            self.not_today = True
            # 当日行情不适合运行
            await self.clear_all_position(last_price)
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"skip", content=f"not good for running today.")
            return {}
        
        self.not_today = False
        # 周期性刷新配置
        data = await self.reflesh_config(data, last_price)
        
        runtimes = data.get("runtimes", {})
        self.last_rebalance = utils.to_datetime(runtimes.get("last_rebalance", ""))
        self.price_range = runtimes.get("price_range")
        self.grid_spread = runtimes.get("grid_spread")
        self.cost_per_grid = runtimes.get("cost_per_grid")
        self.total_cost = runtimes.get("total_cost")
        
        # 更新单格投入资金 (确保资金被均匀分配到新的较窄区间内)
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="rebalance_params", content=f"Params: Range: {self.price_range}, Spread: {self.grid_spread}, Cost/Grid: {self.cost_per_grid}")
        
        # 恢复未完成的订单
        await self._recover_orders_from_file(data)
        
        return data
        
    async def is_grid_friendly_day(
        self,
        hist_limit: float = 0.8,
        hist_expand_ratio: float = 1.4,
        dif_limit: float = 2.0,
    ) -> bool:
        """
        判断当日是否适合运行网格策略：
        1. VXN ∈ [15, 28]
        2. MACD 无明显趋势（适合震荡）
        """

        if not self.skip:
            return True
        
        ema_short = await self.realtime_data_processor.get_ema(self.symbol, ema_period=self.ema_short_period)
        ema_middle = await self.realtime_data_processor.get_ema(self.symbol, ema_period=self.ema_middle_period)
        ema_long = await self.realtime_data_processor.get_ema(self.symbol, ema_period=self.ema_long_period)
        adx = await self.realtime_data_processor.get_adx()
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"EMA: {ema_short}, {ema_middle}, {ema_long}, ADX:{adx}")
    
        # if self.adx[0] > self.p.adx_threshold and self.ma5[0] < self.ma7[0] and self.macd.macd < self.macd.signal:
        #     is_downtrend = True
        # # if self.macd.macd < self.macd.signal:
        # #     is_downtrend = True
        # if self.ma5[0] < self.ma20[0]:
        #     is_downtrend = True
            
        if adx > 30 and ema_short < ema_middle:
            return False
        if ema_short < ema_long:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"skip because of EMA: {ema_short} < {ema_long} ADX({adx})")
            return False
        return True
        # ---------- 1. 获取标的日线 ----------
        h0, h1, dif = await self.realtime_data_processor.get_macd(self.symbol)

        # 1. 柱子不过度放大
        if abs(h0) > hist_limit:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"skip because of 柱子不过度放大")
            return False

        # 2. 防止趋势突然加速
        if abs(h1) > 0 and abs(h0) / abs(h1) > hist_expand_ratio:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"skip because of 趋势突然加速")
            return False

        # 3. 防止远离零轴的强趋势
        if abs(dif) > dif_limit:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"skip because of 远离零轴的强趋势")
            return False

        # ---------- 3. 获取 VXN ----------
        vxn_value = await self.realtime_data_processor.get_vxn()
        if vxn_value > 29:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"daily_check", content=f"skip because of VXN({vxn_value})")
            return False

        return True
        
    async def _load_runtime_data(self):
        try:
            data = {}
            async with aiofiles.open(self.data_file, 'r') as f:
                content = await f.read()
                if content:
                    data = json.loads(content) # Should be a list of cycle dicts
        except FileNotFoundError:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"init", content=f"No pending grid cycles file ('{self.data_file}'). Starting fresh for this strategy.")
        except json.JSONDecodeError:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"init", content=f"Error decoding JSON for {self.strategy_id} from '{self.data_file}'. Starting fresh for this strategy.")
            
        return data
    
    # 策略初始化
    async def InitStrategy(self):
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"Init", content=f"Initing Strategy: {self.strategy_id}")

        self.start_time = self.realtime_data_processor.get_current_time()
        # 从文件中读出数据
        data = await self._load_runtime_data()
        
        # 恢复已有持仓
        # 从盈利历史中恢复可用持仓和资金
        self.profit_logs = data.get('profits', [])
        if self.profit_logs:
            self.recover_curr_position()

        # 恢复运行时参数并执行重平衡
        await self.rebalance(data)
        
        if not self.not_today:
            self.is_running = True
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"价格范围：{self.price_range}, 单格投入：{self.cost_per_grid} 单格价差：{self.grid_spread:.2f}")
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"当前持仓：{self.position:.0f} 可用资金：{self.cash}")
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"init", content=f"Running.")
        

    def run(self):
        positions = self.om.ib.get_current_positions()
        target_position = None
        for position in positions:
            if self.symbol == position.contract.symbol:
                target_position = position
                break
        if target_position is None:
            return
        
        self.is_running = True

    
    def handle_order_cancelled(self, order: GridOrder):
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="order_cancel", content=f'Order Canceled/Margin/Rejected/Expired: {order.status} Ref {order.order_id} {order.shares}')
        if not order.isbuy():
            self.pending_sell_count -= abs(order.shares)
            self.pending_sell_cost = round(self.pending_sell_cost - abs(order.lmt_price * order.shares), 2)
        else:
            LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="DECREASE", content=f'Order Canceled/Margin/Rejected/Expired: {order.order_id} {order.shares}')
            self.pending_buy_count -= abs(order.shares)
            self.pending_buy_cost = round(self.pending_buy_cost - abs(order.lmt_price * order.shares), 2)

        # 如果是熔断清仓单，直接全部清仓
        if self.clear_order and order.order_id == self.clear_order.order_id:
            self.clear_order = None
            return

        # 取消的是建仓单，直接从建仓单列表中移除
        if order.order_id in self.open_orders:
            self.open_orders.pop(order.order_id, None)
            LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event=f"handle_cancel", content=f"pop order: {order.order_id}, left: {list(self.open_orders.keys())}")
            
        # 如果是平仓单，只更新平仓单状态，不删除，要保留写文件
        # 是否清除对实盘没影响，对于回测不清理会导致重复cancel
        if order.order_id in self.close_units:
            self.close_units[order.order_id].close_order = order
            self.close_units.pop(order.order_id, None)
        
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
        

    async def grid_buy(self, price: float, size: float, order_ref: str = "", force: bool = False) -> Optional[GridOrder]:
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="grid_buy", content=f"Place BUY order, Price: {price:.2f}, Qty: {size}")
        cost = abs(round(price * size, 2))
        if force is False:
            if not self.is_running:
                return None
            
            if self.stop_buy:
                LoggerManager.Error("app", event="grid_buy_failed", strategy=f"{self.strategy_id}", content=f"Buying stopped due to circuit breaker.")
                return None
            
            # 对应网格未激活时不能提交订单，只针对建仓订单
            if price < self.price_range[0]:
                LoggerManager.Error("app", event="grid_buy_failed", strategy=f"{self.strategy_id}", content=f"out of price range, expect {price} but {self.price_range[0]}")
                return None
            
            if self.cash < cost + self.pending_buy_cost:
                LoggerManager.Error("app", event="grid_buy_failed", strategy=f"{self.strategy_id}", content=f"No enough money, available: {self.cash - self.pending_buy_cost}, want: {cost}")
                return None
        
        order = await self.om.place_order(self.symbol, "BUY", size, price, callback=self.update_order_status, tif="GTC", order_ref=order_ref)
        if order:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="INCREASE", content=f"Place BUY order, OrderId: {order.order_id} Price: {price:.2f}, Qty: {size}")
            self.pending_buy_count += abs(size)
            self.pending_buy_cost = round(self.pending_buy_cost + cost, 2)
        else:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="grid_buy_failed", content=f"Place BUY order failed, Price: {price:.2f}, Qty: {size}")
        return order
      
    async def grid_sell(self, price: float, size: float, order_ref: str = "", force: bool = False) -> Optional[GridOrder]:
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="grid_sell", content=f"Place SELL order, Price: {price:.2f}, Qty: {size}")
        if force is False:
            if not self.is_running:
                return None
            
            if self.position - self.pending_sell_count < size:
                LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="grid_sell_failed", content=f"Not enough position to place SELL order @{price:.2f} for {size} shares. Pending Sell Count: {self.pending_sell_count}, Position: {self.position}")
                return None
            
            if price > self.price_range[1]:
                LoggerManager.Error("app", event="grid_sell_failed", strategy=f"{self.strategy_id}", content=f"out of price range, expect {price} but {self.price_range[1]}")
                return None

        sell_order = await self.om.place_order(self.symbol, "SELL", size, price, callback=self.update_order_status, tif="GTC", order_ref=order_ref)
        if sell_order:
            self.pending_sell_count += abs(size)
            self.pending_sell_cost = round(self.pending_sell_cost + abs(price * size), 2)
        else:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="grid_sell_failed", content=f"Place SELL order failed, Price: {price:.2f}, Qty: {size}")
        return sell_order

    
    async def reflesh_open_orders(self, dealed_order: GridOrder):
        """刷新建仓单"""
        # 如果成交的是买单，就挂一个更低位置的买单；如果成交的是卖单，就挂一个更高位置的卖单。
        # 取消当前的所有建仓单，这里只做提交，状态更新事件中会清理
        # 使用 list() 创建副本，避免迭代时修改字典报错
        self.open_orders.pop(dealed_order.order_id, None)
        for order in list(self.open_orders.values()):
            ret = await self._cancel_order(order)
            if not ret:
                LoggerManager.Error("app", event="cancel", strategy=f"{self.strategy_id}", content=f"Cancel failed {order.order_id} when reflesh_open_orders")
            else:
                LoggerManager.Debug("app", event="open_cancel", strategy=f"{self.strategy_id}", content=f"Cancel {order.order_id} when reflesh_open_orders")
        
        await self.place_open_orders(dealed_order.lmt_price)


    async def place_order(self, action, price, quantity, order_ref, force: bool = False) -> Optional[GridOrder]:
        """提交订单"""
        
        if action.upper() == "SELL":
            return await self.grid_sell(price, quantity, order_ref, force)
        else:
            return await self.grid_buy(price, quantity, order_ref, force)
    
    # 进入阶段二，停止新买入，并提交一个下限买单以触发清仓。只有出现资金低于预留资金比例时才会触发。
    # 注意：如果已经有熔断清仓单在执行，则不重复提交。
    # 当有卖单成交时，退出阶段二，恢复正常买入。
    async def set_stop_buy(self, current_price: float = 0.0):
        if self.stop_buy:
            return
        
        LoggerManager.Warning("app", strategy=f"{self.strategy_id}", event="circuit_breaker_warning", 
                              content=f"WARNING: Cash {self.cash:.2f} below retention fund threshold {self.total_cost * self.retention_fund_ratio:.2f}. Stopping new buys.")
        self.stop_buy = True
        if self.clear_order:
            return
        
        # 提交一个买单以触发清仓操作
        buy_price = max(round(current_price - self.grid_spread, 2), self.price_range[0])
        order = await self.grid_buy(buy_price, 1, order_ref="CIRCUIT_BREAKER_CLEAR", force=True)
        if order:
            self.clear_order = order

    async def clear_stop_buy(self):
        if self.stop_buy is False:
            return
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="circuit_breaker_cleared", content=f"Circuit breaker cleared. Resuming normal buy operations.")
        self.stop_buy = False
        if self.clear_order:
            await self._cancel_order(self.clear_order)
    
    # 阶段三：全部清仓并取消所有挂单.
    async def clear_all_position(self, current_price: float):
        """清仓所有持仓和挂单"""
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="do_clear", content=f"Clearing all positions and orders.")
        await self._cancel_active_orders()
        
        if self.position > 0:
            # 挂一个低价卖单确保成交，能容忍0.5%的滑点
            sell_price = round(current_price * 0.995, 2)
            order = await self.grid_sell(sell_price, self.position, order_ref="FORCED_CLEAR", force=True) # 挂低价确保成交
            if order:
                self.all_clear_order = order
    
    async def check_circuit_breaker(self, last_price: float):
        """检查是否触发硬止损熔断"""
        # 计算当前总权益 (Net Asset Value)
        # 注意：这里计算的是粗略值，严谨计算需要包含 pending 成本等
        
        # 计算资金剩余量，低于配置值时停止买入
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="circuit_breaker_check", content=f"Checking circuit breaker: Cash {self.cash:.2f}, Threshold {self.total_cost * self.retention_fund_ratio:.2f}")
        if self.cash < self.total_cost * self.retention_fund_ratio:
            await self.set_stop_buy(last_price)

            
    async def handle_order_dealed(self, order: GridOrder):
        # 订单成交更新持仓和成本
        # 统计数据要使用成交价和成交数量
        if order.isbuy():
            self.position += abs(order.done_shares)
            self.cash -= abs(order.done_price*order.done_shares)
        
            self.pending_buy_count -= abs(order.done_shares)
            self.pending_buy_cost = round(self.pending_buy_cost - abs(order.done_shares * order.done_price), 2)
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="order_deal", content=f'BUY Order EXECUTED, Lmt Price: {order.lmt_price:.2f}, Done Price: {order.done_price} Qty: {order.done_shares:.0f} Id: {order.order_id}')
        else:
            await self.clear_stop_buy()
            self.position -= abs(order.done_shares)
            self.cash += abs(order.done_price*order.done_shares)
            
            self.pending_sell_count -= abs(order.done_shares)
            self.pending_sell_cost = round(self.pending_sell_cost - abs(order.done_shares * order.done_price), 2)
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="order_deal", content=f'SELL Order EXECUTED, LmtPrice: {order.lmt_price} DonePrice: {order.done_price:.2f}, Qty: {order.done_shares:.0f} Id: {order.order_id}')


        # 如果是初始建仓单成交，触发初始订单提交
        if self.base_position_order and order.order_id == self.base_position_order.order_id:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="base_position_built", content=f"Base position build order executed.")
            self.base_position_order = None
            if len(self.open_orders) == 0:
                await self.place_open_orders(order.done_price)
            return
        
        # 如果是熔断清仓单，直接全部清仓
        if self.clear_order and order.order_id == self.clear_order.order_id:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="circuit_breaker_triggered", content=f"Circuit breaker triggered clear order executed.")
            await self.clear_all_position(order.done_price)
            self.clear_order = None
            return
        
        # 如果是平仓单，记录完成的交易
        if order.order_id in self.close_units:
            LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="close_over", content=f"Close order executed. {order.order_id} @{order.lmt_price} {order.shares}")
            unit = self.close_units[order.order_id]
            self._log_completed_trade(unit.open_order, order)
            self.close_units.pop(order.order_id, None)
            
        if order.order_id in self.open_orders:
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="open_over", content=f"Open order executed. {order.order_id} @{order.lmt_price} {order.shares}")
            # 建仓单成交，移除建仓单，提交平仓单，刷新建仓单
            await self.reflesh_open_orders(order)
            order_ref = {"strategy_id": self.strategy_id, "purpose": "CLOSE", "open_order_id": f"{order.order_id}"}
            if order.isbuy():
                action = "SELL"
                price = round(order.lmt_price + self.grid_spread, 2)
            else:
                action = "BUY"
                price = round(order.lmt_price - self.grid_spread, 2)
            
            size = max(round(self.cost_per_grid / price), 1)
            close_order = await self.place_order(action, price, size, order_ref)
            if close_order:
                self.close_units[close_order.order_id] = GridUnit(order, close_order)
        
        # 成交价可能与提交的限价不一样，使用限价网格移动更平滑
        # await self.check_circuit_breaker(order.done_price)

    
    # 策略下的订单状态更新
    async def update_order_status(self, order: GridOrder):
        """
        Handles order status notifications.
        MODIFIED: It now correctly calculates the closing target for dynamic spacing.
        """
        if order.status in [OrderStatus.Submitted, OrderStatus.Accepted]:
            return
        
        skip = True
        if order.order_id in self.open_orders:
            skip = False
        if order.order_id in self.close_units:
            skip = False
        if self.base_position_order and order.order_id == self.base_position_order.order_id:
            skip = False
        if self.all_clear_order and self.all_clear_order.order_id == order.order_id:
            skip = False
        if self.clear_order and order.order_id == self.clear_order.order_id:
            skip = False
            
        if skip:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="order_update", content=f"Unknow order: {order}")
            return
        
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event=f"order_update", content=f"Orders: {list(self.open_orders.keys())}")
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event=f"order_update", content=f"Orders: {list(self.close_units.keys())}")
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event=f"order_update", content=f"Order Update: {order.order_id} {order.status} {order.lmt_price}")
        async with self.lock:
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
            LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"position_recover", content=f"No profit logs found to recover position.")
            return
        
        log = self.profit_logs[-1]
        self.init_position = log.get('position', [0, 0])[1]
        self.init_cash = log.get('cash', [0, 0])[1]
        
        self.init_position = self.position if self.init_position == 0 else self.init_position
        self.position = self.init_position
        self.init_cash = self.cash if self.init_cash == 0 else self.init_cash
        self.cash = self.init_cash
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"position_recover", content=f"Recovered position from logs. Position: {self.init_position}, Total Cost: {self.init_cash}")

    # 每日提交初始开仓单
    async def place_open_orders(self, last_price: float):
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="place_open_orders", content=f"place open order: {last_price}")
        buy_price = round(last_price - self.grid_spread, 2)
        order_ref = {"strategy_id": self.strategy_id, "purpose": "OPEN"}
        size = max(round(self.cost_per_grid / buy_price), 1)
        new_order = await self.place_order("buy", buy_price, size, order_ref)
        if new_order:
            self.open_orders[new_order.order_id] = new_order
        else:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="place_open_orders", content=f"Failed to place initial BUY order at {buy_price:.2f}")
            
        sell_price = round(last_price + self.grid_spread, 2)
        size = max(round(self.cost_per_grid / sell_price), 1)
        sell_order = await self.place_order("sell", sell_price, size, order_ref)
        if sell_order:
            self.open_orders[sell_order.order_id] = sell_order
        else:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event="place_open_orders", content=f"Failed to place initial SELL order at {sell_price:.2f}")

    # 从文件中恢复未完成的订单
    async def _recover_orders_from_file(self, data: Dict[str, Any]):
        # 恢复建仓单
        open_orders = data.get('open_orders', [])
        for order in open_orders:
            open_order = GridOrder.from_dict(order)
            new_order = await self.place_order(open_order.action, open_order.lmt_price, open_order.shares, open_order.order_ref, force=True)
            if new_order:
                self.open_orders[new_order.order_id] = new_order
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event="recover_orders", content=f"Open Orders: {len(open_orders)}.")

        # 恢复平仓单
        close_units_data = data.get('close_units', [])
        for unit_data in close_units_data:
            unit = GridUnit.from_dict(unit_data)
            # 对已完成的一场订单做兼容
            if unit.close_order.status == OrderStatus.Completed:
                continue
            
            # 恢复平仓单,只恢复未取消的平仓单，要刷新订单信息
            order = unit.close_order
            new_order = await self.place_order(order.action, order.lmt_price, order.shares, order.order_ref, force=True)
            if new_order:
                unit.close_order = new_order
                self.close_units[unit.close_order.order_id] = unit
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event="recover_orders", content=f"Close Units: {len(close_units_data)}.")


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
            "end_time": self.realtime_data_processor.get_current_time().strftime("%Y-%m-%d %H:%M:%S"),
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
            "runtimes": {
                "total_cost": self.total_cost,
                "price_range": self.price_range,
                "grid_spread": self.grid_spread,
                "cost_per_grid": self.cost_per_grid,
                "last_rebalance": self.last_rebalance.isoformat()
            }
        }
        try:
            async with aiofiles.open(file_path, 'w') as f:
                await f.write(json.dumps(data, indent=4))
                
        except Exception as e:
            LoggerManager.Error("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"Error saving active grid cycles for {self.strategy_id} to {file_path}: {e}")
    
    def is_final(self, status: OrderStatus) -> bool:
        return status in (OrderStatus.Canceled, OrderStatus.Cancelled, OrderStatus.Completed, OrderStatus.Rejected, OrderStatus.Expired)

    async def _cancel_order(self, order: GridOrder) -> bool:
        ret = False
        if order:
            # 先触发底层订单取消
            ret = await self.om.cancel_order(order.order_id)
            
            # 清理维护的计数器和状态数据
            LoggerManager.Debug("app", strategy=f"_cancel_order", event=f"_cancel_order", content=f"_cancel_order {order.order_id}")
            self.handle_order_cancelled(order)
        return ret
        
    async def _cancel_active_orders(self):
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"_cancel_active_orders", content=f"Cancelling all active orders for strategy {self.strategy_id}...")
        orders_to_cancel: List[GridOrder] = []
        for order in list(self.open_orders.values()):
            LoggerManager.Debug("app", event=f"waiting_for_cancel", strategy=f"{self.strategy_id}", content=f"Preparing to cancel open order {order.order_id}")
            orders_to_cancel.append(order)

        for unit in list(self.close_units.values()):
            LoggerManager.Debug("app", event=f"waiting_for_cancel", strategy=f"{self.strategy_id}", content=f"Preparing to cancel close order {unit.close_order.order_id}")
            orders_to_cancel.append(unit.close_order)

        # 发起取消（全部异步执行）
        for order in orders_to_cancel:
            try:
                ret = await self._cancel_order(order)
                if not ret:
                    LoggerManager.Error("app", event="cancel_failed", strategy=f"{self.strategy_id}", content=f"Cancel failed {order.order_id}")
            except Exception as e:
                LoggerManager.Error("app", event=f"cancel_error", strategy=f"{self.strategy_id}", content=f"Cancel failed {order.order_id}: {e}")
        LoggerManager.Debug("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"All cancel requests sent for strategy {self.strategy_id}.")
        return 
    
    async def DoStop(self):
        self.is_running = False
        LoggerManager.Info("app", strategy=f"{self.strategy_id}", event=f"stop", content=f"Stopping strategy {self.strategy_id}...")
        if self.not_today:
            return
        
        # 保存当前未完成的网格单元，然后取消所有订单
        await self._save_active_grid_cycles()
        await self._cancel_active_orders()
        
        # 等待订单取消完成
        try:
            await asyncio.wait_for(self._all_orders_done_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            LoggerManager.Error("app", content="Timeout waiting for order cancellation")
        
        self.end_time = self.realtime_data_processor.get_current_time()
        return

    # 每日总结
    def DailySummary(self, date_str: str) -> DailyProfitSummary:
        """返回每日盈利总结字符串"""
        self.end_time = self.realtime_data_processor.get_current_time()
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
