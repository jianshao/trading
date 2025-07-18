import json
import os
import pickle
import time
from ib_insync import Order, OrderStatus, Trade
from matplotlib import pyplot as plt
import pandas as pd
from backtest.common import common
from common.account import Account, Position
import redis
from typing import Any, Callable, Coroutine, Dict, List, Optional
from io import StringIO
import backtrader as bt

from apis.ibkr import IBapi
from apis.api import BaseAPI
from strategy.grid import GridOrder

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
    

    async def place_limit_order(self, contract: Any, action: str, quantity: float, 
                                limit_price: float, order_ref: str = "", tif: str = "GTC", 
                                transmit: bool = True, outside_rth: bool = False,
                                order_id_to_use: Optional[int] = None) -> Optional[Any]:
        """Places a limit order and returns the ib_insync Trade object."""
        if action.upper() == "BUY":
            order = self.bt_api.buy(price=limit_price, size=quantity, exectype=bt.Order.Limit)
        else:
            order = self.bt_api.sell(price=limit_price, size=quantity, exectype=bt.Order.Limit)
        # 将order转换成GridOrder
        self.pending_orders[order.ref] = order
        return common.buildGridOrder(order)
        

    def cancel_order(
        self,
        order_to_cancel: GridOrder # Broker-specific Order object or GenericOrder or just orderId
    ) -> bool:
        """Cancels an existing order."""
        # print(f" MockApi: cancel limit order {order_to_cancel.orderId} {order_to_cancel.action} {order_to_cancel.totalQuantity} @{order_to_cancel.lmtPrice}.")
        if not order_to_cancel:
            print(" Cancel Order Failed: invalid param.")
            return False
        if order_to_cancel.order_id not in self.pending_orders:
            return True
        
        order = self.pending_orders[order_to_cancel.order_id]
        if order:
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

    async def get_historical_data(self, contract: Any, end_date_time: str = "", 
                                  duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                  what_to_show: str = 'TRADES', use_rth: bool = True, 
                                  format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:
        if isinstance(contract, str):
            contract = await self.ib_api.get_contract_details(contract)
        key = f"{contract.symbol}_{end_date_time}_{duration_str}_{bar_size_setting}"
        value = self.redis.get(key)
        if value:
            return pickle.loads(value)
        data = await self.ib_api.get_historical_data(contract, end_date_time, duration_str, bar_size_setting, what_to_show, use_rth, format_date)
        if not data.empty:
            # print("data set redis.")
            # print(f" {data[:4]}")
            self.redis.set(key, pickle.dumps(data))
        return data

    async def get_atr(self, contract_spec: Any, # Can be an unqualified Contract object
                      atr_period: int = 14, 
                      hist_duration_str: str = "30 D", # Fetch enough data for ATR calc
                      hist_bar_size: str = "1 day") -> Optional[float]:
        return await self.ib_api.get_atr(contract_spec, atr_period, hist_duration_str, hist_bar_size)

    # --- Callback Registration Methods ---
    def register_order_status_update_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        self.order_status_update_handler = handler

    def register_execution_fill_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        pass

    def register_error_handler(self, handler: Callable[[int, int, str, Optional[Any]], Coroutine[Any, Any, None]]):
        pass
        
    def register_open_order_snapshot_handler(self, handler: Callable[[Any, Any, Any], Coroutine[Any, Any, None]]):
        pass
