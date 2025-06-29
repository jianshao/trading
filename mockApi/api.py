import json
import os
import time
from ib_insync import Order, OrderStatus, Trade
from matplotlib import pyplot as plt
import pandas as pd
from mockApi.account import Account
import redis
from typing import Any, Callable, Coroutine, Dict, List, Optional
from io import StringIO

from apis.ibkr import IBapi
from apis.api import BaseAPI

class MockApi(BaseAPI):
    def __init__(self, api: IBapi, init_money: float = 10000):
        self.api = api
        # order_id -> symbol
        self.order_id_symbol: Dict[int, str] = {}
        # symbol -> orders
        self.symbol_orders_list: Dict[str, List[Any]] = {}
        self.order_status_update_handler: Callable[[Any, Any], Coroutine[Any, Any, None]] = None
        self.redis = redis.Redis(host='localhost', port=6379, db=0)
        self.account = Account(init_money)
    
    # 刷新账户现金额
    def reflash_account(self, cash: float):
        self.account.reflash(cash)

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
        # print(f" MockApi: place limit order {order_id_to_use} {contract.symbol} {action} {quantity} @{limit_price}.")
        # 当前现金是否足够
        if action.upper() == "BUY":
            if not self.account.place_buy(contract.symbol, quantity, limit_price):
                # print("Placing Buy Order Failed: not enough money.")
                return None
        else:
            if not self.account.place_sell(contract.symbol, quantity, limit_price):
                print("Placing Sell Order Failed: not enough holding.")
                return None
          
        # 生成一个Order等待成交
        order = Order(orderId=order_id_to_use, action=action, totalQuantity=quantity, orderType="LMT", lmtPrice=limit_price)
        orderStatus = OrderStatus(orderId=order_id_to_use, status="Submitted")
        trade = Trade(contract=contract, order=order, orderStatus=orderStatus)
        
        if contract.symbol not in self.symbol_orders_list:
            self.symbol_orders_list[contract.symbol] = []
        
        self.symbol_orders_list[contract.symbol].append(trade)
        self.symbol_orders_list[contract.symbol].sort(key=lambda x: x.order.lmtPrice)
        self.order_id_symbol[order_id_to_use] = contract.symbol
        
        return trade
        

    def cancel_order(
        self,
        order_to_cancel: Any # Broker-specific Order object or GenericOrder or just orderId
    ) -> bool:
        """Cancels an existing order."""
        # print(f" MockApi: cancel limit order {order_to_cancel.orderId} {order_to_cancel.action} {order_to_cancel.totalQuantity} @{order_to_cancel.lmtPrice}.")
        if not order_to_cancel:
            print(" Cancel Order Failed: invalid param.")
            return False
        
        order_id = order_to_cancel.orderId
        if order_id not in self.order_id_symbol.keys():
            print(f" Cancel Order Failed: unknown order id: {order_id}.")
            return False
        
        symbol = self.order_id_symbol[order_id]
        if symbol not in self.symbol_orders_list.keys():
            return False
        
        offset = -1
        trades = self.symbol_orders_list[symbol]
        for i in range(len(trades)):
            if trades[i].order.orderId == order_id:
                trades[i].orderStatus.status = "Cancelled"
                offset = i
                # 取消买入订单要释放现金
                if trades[i].order.action.upper() == "BUY":
                    self.account.cancel_buy(symbol, trades[i].order.totalQuantity, trades[i].order.lmtPrice)
                else:
                    self.account.cancel_sell(symbol, trades[i].order.totalQuantity, trades[i].order.lmtPrice)
                break
        if offset != -1:
            self.symbol_orders_list[symbol] = trades[:offset] + trades[offset+1:]
        
        del self.order_id_symbol[order_id]
        return True
        
        
        
    async def fetch_all_open_orders(self) -> List[Any]: # List of broker-specific Trade/Order objects or GenericOrder
        """Requests all currently open orders for the account."""
        pass

    # --- Market & Account Data ---
    async def get_contract_details(self, symbol: str, exchange: str = "SMART", 
                             currency: str = "USD", primary_exchange: Optional[str] = None) -> Optional[Any]:
        return await self.api.get_contract_details(symbol, exchange, currency, primary_exchange)

    async def get_current_price(
        self,
        contract: Any, # Expects a qualified ib_insync.Contract object
        timeout_seconds: int = 10
    ) -> Optional[float]: # Returns the last traded price or mid-price
        """Fetches the current market price for a contract."""
        return await self.api.get_current_price(contract, timeout_seconds)
        
    async def get_account_summary(self, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """Fetches account summary information (e.g., NetLiq, CashBalance)."""
        return await self.api.get_account_summary(tags)

    async def get_current_positions(self, account: Optional[str] = None, timeout_seconds: int = 10) -> List[Any]:  # List of position details
        """Fetches current account positions."""
        return await self.api.get_current_positions(account, timeout_seconds)

    async def get_historical_data(self, contract: Any, end_date_time: str = "", 
                                  duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                  what_to_show: str = 'TRADES', use_rth: bool = True, 
                                  format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:
        key = f"{contract.symbol}_{end_date_time}_{duration_str}_{bar_size_setting}"
        value = self.redis.get(key)
        if value:
            print("data from redis.")
            return pd.read_json(StringIO(value.decode('utf-8')), orient="split")
        data = await self.api.get_historical_data(contract, end_date_time, duration_str, bar_size_setting, what_to_show, use_rth, format_date)
        if not data.empty:
            print("data set redis.")
            self.redis.set(key, data.to_json(orient="split"))
        return data

    async def get_atr(self, contract_spec: Any, # Can be an unqualified Contract object
                      atr_period: int = 14, 
                      hist_duration_str: str = "30 D", # Fetch enough data for ATR calc
                      hist_bar_size: str = "1 day") -> Optional[float]:
        return await self.api.get_atr(contract_spec, atr_period, hist_duration_str, hist_bar_size)

    # --- Callback Registration Methods ---
    def register_order_status_update_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        self.order_status_update_handler = handler

    def register_execution_fill_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        pass

    def register_error_handler(self, handler: Callable[[int, int, str, Optional[Any]], Coroutine[Any, Any, None]]):
        pass
        
    def register_open_order_snapshot_handler(self, handler: Callable[[Any, Any, Any], Coroutine[Any, Any, None]]):
        pass

    async def input(self, symbol:str, data: pd.DataFrame):
        if data.empty or not all(col in data.columns for col in ['High', 'Low', 'Close', 'Open']):
            return None

        profits = []
        length = len(data)
        step = 1
        for i in range(length):
            if i == round((length/10) * step):
                print(f"{step * 10}% done.")
                step += 1

            bar_high = data['High'].iloc[i]
            bar_low = data['Low'].iloc[i]

            # print(f"Recive Price: {bar_low} - {bar_high}:")
            list_to_remove: List[Any] = []
            trades = self.symbol_orders_list[symbol].copy()
            for trade in trades:
                action = trade.order.action.upper()
                if (action == "BUY" and trade.order.lmtPrice >= bar_low) or (action == "SELL" and trade.order.lmtPrice <= bar_high):
                    list_to_remove.append(trade)
            
            for trade in list_to_remove or []:
                # 成交
                # print(f" Order Done: {trade.order.orderId} {trade.order.action} @{trade.order.lmtPrice}")
                trade.orderStatus.status = "Filled"
                trade.orderStatus.filled = trade.order.totalQuantity
                trade.orderStatus.lastFillPrice = trade.order.lmtPrice
                # 卖单成交，收到现金
                if trade.order.action == "SELL":
                    self.account.sell_done(symbol, trade.order.totalQuantity, trade.order.lmtPrice)
                else:
                    self.account.buy_done(symbol, trade.order.totalQuantity, trade.order.lmtPrice)

                if self.order_status_update_handler:
                    await self.order_status_update_handler(trade, trade.orderStatus)
                    
                self.symbol_orders_list[symbol] = [t for t in self.symbol_orders_list[symbol] if t.order.orderId != trade.order.orderId]
                if trade.order.orderId in self.order_id_symbol.keys():
                    del self.order_id_symbol[trade.order.orderId]
            
            self.account.calc(symbol, data['Close'].iloc[i])
            
            if i % 780 == 0:
                profits.append({"i": i/780, "profit": self.account.cross_profit})

        self.account.Summy(symbol, data['Close'].iloc[i])
        
        df = pd.DataFrame(profits)
        
        # 绘制折线图 + 散点图
        plt.plot(df['i'], df['profit'], marker='o', label='Profit')
        plt.xlabel('i')
        plt.ylabel('profit')
        plt.title('Profit vs i')
        plt.grid(True)
        plt.legend()
        plt.show()
        return None
