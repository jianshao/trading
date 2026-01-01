from datetime import datetime
import json
import os
import pickle
import time
import pandas as pd
import redis
from typing import Any, Callable, Coroutine, Dict, List, Optional
import backtrader as bt

from apis.ibkr import IBapi
from apis.api import BaseAPI
from common.utils import OrderStatus
from strategy.grid import GridOrder
from common.logger_manager import LoggerManager

class MockApi(BaseAPI):
    def __init__(self, bt_api: bt.Strategy = None, ib_api: IBapi = None, symbol: str = ""):
        self.symbol = symbol
        self.bt_api = bt_api
        self.ib_api = ib_api
        # order_id -> symbol
        self.order_id_symbol: Dict[int, str] = {}
        self.pending_orders = {}
        # symbol -> orders
        self.symbol_orders_list: Dict[str, List[Any]] = {}
        self.order_status_update_handler: Callable[[Any, Any], Coroutine[Any, Any, None]] = None
        self.redis = redis.Redis(host='localhost', port=6379, db=0)
        # self.account = Account(init_money, [Position(symbol="TQQQ", init_holding=100, init_cost=5780)])


    def connect(self) -> bool:
        return True
      
    def disconnect(self) -> bool:
        return True
    
    def isConnected(self) -> bool:
        return True

    # --- Order Management ---
    def get_next_order_id(self) -> int:
        """
        Gets the next available client-side order ID.
        This ID should be unique for orders placed by this API instance.
        """
        return 1

    async def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Optional[Any] = None):
        return
    
    async def _on_ib_order_status_event(self, trade: Any):
        return
    
    async def _on_ib_exec_details_event(self, trade: Any, fill: Any):
        return 
    
    async def _on_ib_open_order_snapshot_event(self, trade: Any):
        return

    def _on_ib_disconnected_event(self):
        return 
    
    def map_bt_status(self, bt_status):
        if bt_status == bt.Order.Submitted:
            return OrderStatus.Submitted
        elif bt_status == bt.Order.Accepted:
            return OrderStatus.Accepted
        elif bt_status == bt.Order.Completed:
            return OrderStatus.Completed
        elif bt_status == bt.Order.Canceled:
            return OrderStatus.Canceled
        elif bt_status == bt.Order.Expired:
            return OrderStatus.Expired
        elif bt_status == bt.Order.Rejected:
            return OrderStatus.Rejected
        elif bt_status == bt.Order.Margin:
            # Backtrader 的 Margin (保证金不足) 通常归类为 Rejected 或 Error
            return OrderStatus.Rejected 
        return OrderStatus.ERROR

    def bt_order_to_grid_order(self, order: bt.Order) -> GridOrder:
        """
        将 Backtrader 的 Order 对象转换为自定义的 GridOrder 对象
        """
        # 1. 获取原本的自定义 ID
        
        # 2. 确定买卖方向 (BT中 size>0 为买, size<0 为卖)
        action = "BUY" if order.isbuy() else "SELL"
        
        # 3. 获取价格和数量
        # created 是挂单时的请求，executed 是实际执行的结果
        lmt_price = order.created.price
        # 注意：BT的sell size是负数，我们需要转为绝对值
        shares = abs(order.created.size) 
        
        done_price = order.executed.price if order.executed.price else 0.0
        done_shares = abs(order.executed.size)
        
        # 4. 映射状态
        status = self.map_bt_status(order.status)

        # 5. 组装 GridOrder
        grid_order = GridOrder(
            order_id=order.ref,     # 使用我们追踪的字符串 ID
            symbol="TQQQ", # 获取合约名称
            action=action,
            price=lmt_price,
            shares=shares,
            status=status,
        )
        
        return grid_order

    async def place_limit_order(self, contract: Any, action: str, quantity: float, 
                                limit_price: float, order_ref: str = "", tif: str = "GTC", 
                                transmit: bool = True, outside_rth: bool = False,
                                order_id_to_use: Optional[int] = None) -> Optional[Any]:
        """Places a limit order and returns the ib_insync Trade object."""
        # print("place order....")
        if action.upper() == "BUY":
            order = self.bt_api.buy(price=limit_price, size=quantity, exectype=bt.Order.Limit)
        else:
            order = self.bt_api.sell(price=limit_price, size=quantity, exectype=bt.Order.Limit)
        # 将order转换成GridOrder
        new_order = None
        if order:
            LoggerManager.Debug("app", strategy="mockapi", event=f"place_order", content=f"place order {order.ref} {type(order.ref)}")
            self.pending_orders[order.ref] = order
            new_order = self.bt_order_to_grid_order(order)
        return new_order
        

    def cancel_order(
        self,
        order_to_cancel: GridOrder # Broker-specific Order object or GenericOrder or just orderId
    ) -> bool:
        """Cancels an existing order."""
        # print(f" MockApi: cancel limit order {order_to_cancel.orderId} {order_to_cancel.action} {order_to_cancel.totalQuantity} @{order_to_cancel.lmtPrice}.")
        LoggerManager.Debug("app", strategt="mockapi", event="cancel", content=f"cancel order: {order_to_cancel.order_id}")
        if not order_to_cancel:
            print(" Cancel Order Failed: invalid param.")
            LoggerManager.Error("app", strategt="mockapi", event="cancel_failed", content=f"cancel order: {order_to_cancel.order_id} invalid params")
            return False
        if order_to_cancel.order_id not in self.pending_orders:
            LoggerManager.Error("app", strategt="mockapi", event="cancel_failed", content=f"cancel order: {order_to_cancel.order_id} not exist")
            return True
        
        order = self.pending_orders[order_to_cancel.order_id]
        if order:
            LoggerManager.Debug("app", strategt="mockapi", event="cancel_ok", content=f"cancel order: {order_to_cancel.order_id}")
            self.bt_api.cancel(order)
            del self.pending_orders[order_to_cancel.order_id]
        
        return True
        
        
        
    async def fetch_all_open_orders(self) -> List[Any]: # List of broker-specific Trade/Order objects or GenericOrder
        """Requests all currently open orders for the account."""
        pass

    # --- Market & Account Data ---
    async def get_contract_details(self, symbol: str, exchange: str = "SMART", 
                             currency: str = "USD", primary_exchange: Optional[str] = None) -> Optional[Any]:
        return await self.ib_api.get_contract_details(symbol, exchange, currency, primary_exchange)

    async def get_current_price(
        self,
        contract: Any, # Expects a qualified ib_insync.Contract object
        timeout_seconds: int = 10
    ) -> Optional[float]: # Returns the last traded price or mid-price
        """Fetches the current market price for a contract."""
        return await self.ib_api.get_current_price(contract, timeout_seconds)
        
    async def get_account_summary(self, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """Fetches account summary information (e.g., NetLiq, CashBalance)."""
        return await self.ib_api.get_account_summary(tags)

    def get_current_positions(self, account: Optional[str] = None, timeout_seconds: int = 10) -> List[Any]:  # List of position details
        """Fetches current account positions."""
        # return self.ib_api.get_current_positions(account, timeout_seconds)
        return [
            {
                "contract": {
                    "symbol": self.symbol,
                },
                "position": self.position.size
            }
        ]

    async def get_historical_data(self, symbol: str, end_date_time: str = "", 
                                  duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                  what_to_show: str = 'TRADES', use_rth: bool = True, 
                                  format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:

        key = f"{symbol}_{end_date_time}_{duration_str}_{bar_size_setting}"
        value = self.redis.get(key)
        if value:
            return pickle.loads(value)
        data = await self.ib_api.get_historical_data(symbol, end_date_time, duration_str, bar_size_setting, what_to_show, use_rth, format_date)
        if len(data) > 0:
            # print("data set redis.")
            # print(f" {data[:4]}")
            self.redis.set(key, pickle.dumps(data))
        return data

    async def get_atr(self, symbol: str, # Can be an unqualified Contract object
                      atr_period: int = 14, 
                      hist_duration: int = 30, # Fetch enough data for ATR calc
                      hist_bar_size: str = "1 day") -> Optional[float]:
        return self.bt_api.atr[0]

    # --- Callback Registration Methods ---
    def register_order_status_update_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        self.order_status_update_handler = handler

    def register_execution_fill_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        pass

    def register_error_handler(self, handler: Callable[[int, int, str, Optional[Any]], Coroutine[Any, Any, None]]):
        pass
        
    def register_open_order_snapshot_handler(self, handler: Callable[[Any, Any, Any], Coroutine[Any, Any, None]]):
        pass

    def register_disconnected_handler(self, handler: Callable[[], Coroutine[Any, Any, None]]):
        pass
    
    def reqMktData(self, contract: Any, 
                   genericTickList: str = "", 
                   snapshot: bool = False, 
                   regulatorySnapshot: bool = False, 
                   mktDataOptions: Optional[List[Any]] = None) -> Any:
        """Requests market data for a given contract."""
        pass
    
    async def get_latest_price(self, symbol: str, exchange="SMART", currency="USD"):
        return self.bt_api.data.close[0]

    async def get_ma(self, symbol: str, ma_period: int, bar_size: str = "1 day", duration: str = "60 D") -> float:
        # 根据周期返回对应的 Backtrader 指标值
        if ma_period == 20:
            return self.bt_api.ema20[0]
        elif ma_period == 12:
            return self.bt_api.ema12[0]
        elif ma_period == 26:
            return self.bt_api.ema26[0]
        else:
            # 如果有其他周期，需要在这里扩展，或者用 dict 映射
            return 0
        
    async def get_ema(self, symbol: str, ema_period: int, bar_size: str = "1 day", duration: str = "60 D") -> float:
        # 根据周期返回对应的 Backtrader 指标值
        if ema_period == 20:
            return self.bt_api.ema20[0]
        elif ema_period == 12:
            return self.bt_api.ema12[0]
        elif ema_period == 26:
            return self.bt_api.ema26[0]
        elif ema_period == 5:
            return self.bt_api.ema5[0]
        elif ema_period == 7:
            return self.bt_api.ema7[0]
        else:
            # 如果有其他周期，需要在这里扩展，或者用 dict 映射
            return 0
        
    async def get_macd(self, symbol):
        # print(self.bt_api.macd.lines.getlinealiases())
        h0 = self.bt_api.macd.histo[0]
        h1 = self.bt_api.macd.histo[-1]
        dif = self.bt_api.macd.macd[0]
        return h0, h1, dif
    
    async def get_vxn(self, durationStr="5 D", barSizeSetting="1 day") -> float:
        # return 20
        return self.bt_api.vxn[0]
    
    def get_current_time(self) -> datetime:
        """实盘返回系统当前时间"""
        """回测返回当前 Bar 的时间"""
        # self.bt.datetime.datetime(0) 返回的是 naive time (不带时区)
        # 如果你的策略逻辑里用了带时区的计算，这里需要加上时区，否则会报错
        # 假设 config.time_zone 是字符串 'US/Eastern'
        
        bt_time = self.bt_api.datetime.datetime(0)
        
        # 简单的处理方式：如果实盘代码用了 ZoneInfo，这里最好加上
        # 如果实在麻烦，可以把实盘代码的时区去掉，保持 naive datetime
        from zoneinfo import ZoneInfo
        from data import config 
        
        if bt_time.tzinfo is None:
             return bt_time.replace(tzinfo=ZoneInfo(config.time_zone))
        return bt_time


    async def get_adx(self, durationStr="5 D", barSizeSetting="1 day") -> float:
        return self.bt_api.adx[0]