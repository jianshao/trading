#!/usr/bin/python3
import asyncio
import sys
import os
import numpy as np
from typing import Optional, Callable, Any, List, Dict, Coroutine

# Make sure ib_insync is installed: pip install ib_insync
from ib_insync import *
import pandas as pd

if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        
from apis.api import BaseAPI

# Default connection parameters (can be overridden in constructor or connect method)
DEFAULT_IB_HOST = "127.0.0.1"
DEFAULT_IB_PORT = 7497  # 7497是模拟账户
# DEFAULT_IB_PORT = 7496  # 7496 for TWS, 4001/4002 for IB Gateway (paper/live)

# DEFAULT_IB_PORT = 4001  # 4001是ib gateway的真实账户
# DEFAULT_IB_PORT = 4002  # 4002是ib gateway的模拟账户
DEFAULT_CLIENT_ID = 2 # Ensure this is unique per connection

class IBapi(BaseAPI):
    def __init__(self, host: str = DEFAULT_IB_HOST, port: int = DEFAULT_IB_PORT, client_id: int = DEFAULT_CLIENT_ID):
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self._is_connected = False
        self._connection_lock = asyncio.Lock() # To prevent concurrent connection attempts
        self._current_req_id_counter = 0 # For requests that need a reqId not managed by ib_insync Trade objects

        # --- Callback Handlers (to be registered by the strategy engine or other consumers) ---
        # Signature: async def handler(trade: Trade, order_status: OrderStatus)
        self.order_status_update_handler: Optional[Callable[[Trade, OrderStatus], Coroutine[Any, Any, None]]] = None
        
        # Signature: async def handler(trade: Trade, fill: Fill)
        # Fill object in ib_insync contains both Execution and CommissionReport
        self.execution_fill_handler: Optional[Callable[[Trade, Fill], Coroutine[Any, Any, None]]] = None
        
        # Signature: async def handler(reqId: int, errorCode: int, errorString: str, contract: Optional[Contract])
        self.error_handler: Optional[Callable[[int, int, str, Optional[Contract]], Coroutine[Any, Any, None]]] = None
        
        # Signature: async def handler(contract: Contract, order: Order, orderState: OrderState)
        # This is for the initial dump of open orders on connection or manual request
        self.open_order_snapshot_handler: Optional[Callable[[Contract, Order, OrderState], Coroutine[Any, Any, None]]] = None

        # Register ib_insync's own event handlers to our internal methods
        self.ib.errorEvent += self._on_ib_error
        self.ib.orderStatusEvent += self._on_ib_order_status_event # For specific Trade objects
        self.ib.execDetailsEvent += self._on_ib_exec_details_event # For specific Trade objects
        # commissionReportEvent is often redundant as Fill object in execDetailsEvent contains it
        # self.ib.commissionReportEvent += self._on_ib_commission_report_event
        self.ib.openOrderEvent += self._on_ib_open_order_snapshot_event # For snapshot of all open orders
        self.ib.disconnectedEvent += self._on_ib_disconnected_event # Handle disconnections

    def connect(self) -> bool:
        # async with self._connection_lock:
        if self._is_connected and self.ib.isConnected():
            print("IBapi: Already connected to IB.")
            return True
        try:
            print(f"IBapi: Connecting to IB TWS/Gateway at {self.host}:{self.port} with ClientID {self.client_id}...")
            # Ensure previous connection is fully closed if any existed
            if self.ib.isConnected():
                self.ib.disconnect()
            
            self.ib.RequestTimeout = 10 # Set a general request timeout for ib_insync requests
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15) # Connection timeout
            
            if self.ib.isConnected():
                self._is_connected = True
                server_time = self.ib.reqCurrentTime() # Sync call, but good after connectAsync
                print(f"IBapi: Successfully connected. Server Time: {server_time}")
                # Request managed accounts to confirm (optional)
                # managed_accounts = self.ib.managedAccounts()
                # print(f"IBapi: Managed Accounts: {managed_accounts}")
                return True
            else:
                print("IBapi: Connection failed (ib.isConnected() is False after connectAsync).")
                self._is_connected = False
                return False
        except ConnectionRefusedError:
            print(f"IBapi: Connection refused by IB TWS/Gateway at {self.host}:{self.port}.")
        except asyncio.TimeoutError:
            print("IBapi: Connection attempt to IB TWS/Gateway timed out.")
        except Exception as e:
            print(f"IBapi: An unexpected error occurred during connection: {e}")
        
        self._is_connected = False
        return False

    def disconnect(self):
        if self.ib.isConnected():
            # print("IBapi: Disconnecting from IB...")
            self.ib.disconnect()
        self._is_connected = False
        # ib_insync handles its own loop cleanup mostly
        print("IBapi: Disconnected.")

    def isConnected(self) -> bool:
        return self._is_connected and self.ib.isConnected()

    # --- Public API Methods for Strategy Engine ---
    def get_next_order_id(self) -> int:
        """
        Gets the next available client-side order ID.
        ib_insync's placeOrder can auto-assign if order.orderId is 0.
        This method provides an ID if manual assignment is preferred or needed for other reasons.
        """
        if not self.isConnected():
            raise ConnectionError("Not connected to IB.")
        return self.ib.client.getReqId() # This gets an ID from TWS internal counter
      
    def _get_next_internal_req_id(self) -> int:
        """For requests not directly tied to Trade objects, like historical data or contract details."""
        self._current_req_id_counter += 1
        return self._current_req_id_counter

    # --- Internal IB Event Handlers (Delegating to registered handlers) ---
    async def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Optional[Contract] = None):
        # Filter out common informational messages or handle them differently
        info_codes = [2104, 2106, 2108, 2158, 2103, 2105, 1100, 1101, 1102, 2100, 2107, 2157, 2168, 2169, 2170]
        # 200: No security definition found (can be an error for reqContractDetails)
        # 321: Error validating request (e.g. bad generic tick list)
        if errorCode in info_codes:
            # print(f"IBapi Info (ReqId: {reqId}, Code: {errorCode}): {errorString}")
            return
        
        print(f"IBapi Error (ReqId: {reqId}, Code: {errorCode}): {errorString}" + (f" for Contract: {contract.localSymbol if contract else 'N/A'}" ))
        if self.error_handler:
            await self.error_handler(reqId, errorCode, errorString, contract)

    async def _on_ib_order_status_event(self, trade: Trade):
        # print(f"IBapi _on_ib_order_status_event: OrderID={trade.order.orderId}, Status={trade.orderStatus.status}")
        if self.order_status_update_handler:
            await self.order_status_update_handler(trade, trade.orderStatus)

    async def _on_ib_exec_details_event(self, trade: Trade, fill: Fill):
        # print(f"IBapi _on_ib_exec_details_event: OrderID={fill.execution.orderId}, ExecID={fill.execution.execId}, Shares={fill.execution.shares}")
        if self.execution_fill_handler:
            await self.execution_fill_handler(trade, fill)
    
    async def _on_ib_open_order_snapshot_event(self, trade: Trade):
        # print(f"IBapi _on_ib_open_order_snapshot_event: OrderID={order.orderId}, Symbol={contract.symbol}, Status={orderState.status}")
        if self.open_order_snapshot_handler:
            await self.open_order_snapshot_handler(trade.contract, trade.order, trade.orderStatus)

    def _on_ib_disconnected_event(self):
        print("IBapi: Received disconnected event from ib_insync.")
        self.disconnect()
        self._is_connected = False
        # Application should handle reconnection if desired.

    def register_disconnected_handler(
        self,
        handler: Callable[[], Coroutine[Any, Any, None]] # No arguments
    ):
        print("Registers a handler for when the API disconnects.")
        return 

    async def get_contract_details(self, symbol: str, exchange: str = "SMART", 
                                 currency: str = "USD", primary_exchange: Optional[str] = None) -> Optional[Contract]:
        """Creates and qualifies an IB Contract object for a stock."""
        if not self.isConnected(): return None
        
        contract = Stock(symbol, exchange, currency)
        if primary_exchange:
            contract.primaryExchange = primary_exchange
        
        try:
            qualified_contracts = await self.ib.reqContractDetailsAsync(contract)
            if qualified_contracts:
                return qualified_contracts[0].contract
            else:
                print(f"IBapi: Could not qualify contract for {symbol} on {exchange}.")
                return None
        except asyncio.TimeoutError:
            print(f"IBapi: Timeout qualifying contract for {symbol}.")
            return None
        except Exception as e:
            print(f"IBapi: Error qualifying contract for {symbol}: {e}")
            return None

    async def place_limit_order(self, contract: Contract, action: str, quantity: float, 
                                limit_price: float, order_ref: str = "", tif: str = "GTC", 
                                transmit: bool = True, outside_rth: bool = False,
                                order_id_to_use: Optional[int] = None) -> Optional[Trade]:
        """Places a limit order and returns the ib_insync Trade object."""
        if not self.isConnected():
            print("IBapi: Cannot place order, not connected.")
            return None

        order = Order()
        order.action = action.upper()  # "BUY" or "SELL"
        order.orderType = "LMT"
        order.totalQuantity = float(quantity) # Ensure float for ib_insync
        order.lmtPrice = float(limit_price)
        order.tif = tif
        order.transmit = transmit
        order.outsideRth = outside_rth # Allow trading outside regular trading hours
        if order_ref:
            order.orderRef = order_ref
        
        if order_id_to_use is not None:
            order.orderId = order_id_to_use
        else:
            order.orderId = self.get_next_order_id() # Get a fresh ID from TWS

        print(f"IBapi: Placing Order - ID:{order.orderId}, {order.action} {contract.localSymbol} @{order.lmtPrice} {order.totalQuantity}, "
              f"Type:{order.orderType}, Ref:'{order.orderRef}'")
        
        try:
            return self.ib.placeOrder(contract, order)
            # Wait for the order to be submitted to TWS/Gateway (optional, but good for confirmation)
            # For example, wait for the first status update or a short period.
            # await asyncio.wait_for(trade.statusEvent, timeout=5) # Example: wait for first status event
            # print(f"IBapi: Order submitted. OrderID: {trade.order.orderId}, Status: {trade.orderStatus.status if trade.orderStatus else 'Unknown'}")
        except asyncio.TimeoutError:
            print(f"IBapi: Timeout waiting for initial status of order {order.orderId} for {contract.localSymbol}.")
            return None # Or re-raise, or return a trade object that might update later
        except Exception as e:
            print(f"IBapi: Error placing order for {contract.localSymbol} @ {limit_price}: {e}")
            return None

    def cancel_order_by_id(self, order_id_to_cancel: int) -> bool:
        """
        Attempts to cancel an order given its client-side integer orderId.
        Returns True if the cancellation request was successfully sent, False otherwise.
        Actual cancellation confirmation comes via orderStatusEvent.
        """
        if not self.isConnected():
            print(f"IBapi: Cannot cancel order ID {order_id_to_cancel}, not connected.")
            return False

        if not isinstance(order_id_to_cancel, int) or order_id_to_cancel <= 0:
            print(f"IBapi: Invalid order_id ({order_id_to_cancel}) provided for cancellation.")
            return False
        
        order_obj_for_cancel = Order()
        order_obj_for_cancel.orderId = order_id_to_cancel
        
        try:
            cancellation_trade: Trade = self.ib.cancelOrder(order_obj_for_cancel)
            if cancellation_trade:
                print(f"IBapi: Cancellation request for order ID {order_id_to_cancel} sent. Cancel Order's own ID: {cancellation_trade.order.orderId}, Status: {cancellation_trade.orderStatus.status if cancellation_trade.orderStatus else 'Unknown'}")
                return True
            else:
                # This case is less likely if cancelOrder doesn't throw but returns None (ib_insync usually raises on API error)
                print(f"IBapi: ib.cancelOrder returned None for order ID {order_id_to_cancel}.")
                return False
        except Exception as e:
            print(f"IBapi: Error sending cancellation request for order ID {order_id_to_cancel}: {e}")
            return False
        
    def cancel_order(self, order_to_cancel: Order):
        """Cancels an existing order using the ib_insync Order object."""
        if not self.isConnected():
            print("IBapi: Cannot cancel order, not connected.")
            return False
        if order_to_cancel and (order_to_cancel.permId or order_to_cancel.orderId != 0) : # Check if it's a valid order reference
            try:
                # print(f"IBapi: Requesting cancellation for OrderID: {order_to_cancel.orderId} (PermID: {order_to_cancel.permId or 'N/A'})")
                trade = self.ib.cancelOrder(order_to_cancel) # cancelOrder returns a Trade object for the cancellation
                # await asyncio.wait_for(trade.statusEvent, timeout=5) # Wait for cancel status
                # print(f"IBapi: Cancellation request for order {order_to_cancel.orderId} status: {trade.orderStatus.status if trade.orderStatus else 'Unknown'}")
                return True
            except asyncio.TimeoutError:
                print(f"IBapi: Timeout waiting for cancellation status of order {order_to_cancel.orderId}.")
                return False
            except Exception as e:
                print(f"IBapi: Error cancelling order {order_to_cancel.orderId}: {e}")
                return False
        else:
            print(f"IBapi: Invalid order object provided for cancellation (OrderID: {order_to_cancel.orderId if order_to_cancel else 'N/A'}).")
            return False

    async def get_historical_data(self, contract: Contract, end_date_time: str = "", 
                                  duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                  what_to_show: str = 'TRADES', use_rth: bool = True, 
                                  format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:
        """Fetches historical bar data and returns a Pandas DataFrame."""
        if not self.isConnected(): return None
        try:
            # print(f"IBapi: Requesting historical data for {contract.symbol}: End={end_date_time or 'Now'}, Dur={duration_str}, Bar={bar_size_setting}")
            
            # Ensure contract is fully qualified if not already
            # if symbol:
            qualified_contract = await self.get_contract_details(contract.symbol)
            if not qualified_contract:
                print(f"IBapi: Could not qualify contract {contract.symbol} for historical data.")
                return None
            contract_to_use = qualified_contract

            bars: BarDataList = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract_to_use,
                    endDateTime=end_date_time,
                    durationStr=duration_str,
                    barSizeSetting=bar_size_setting,
                    whatToShow=what_to_show,
                    useRTH=use_rth,
                    formatDate=format_date,
                ),
                timeout=timeout_seconds
            )
            if bars:
                # print(f"IBapi: Fetched {len(bars)} bars for {contract_to_use.localSymbol}")
                df = util.df(bars) # Convert BarDataList to DataFrame
                if df is not None and not df.empty:
                    # Standardize column names if needed (ib_insync usually gives 'date', 'open', 'high', 'low', 'close', 'volume')
                    df.rename(columns={'date': 'Timestamp', 'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume':'Volume'}, inplace=True, errors='ignore')
                    if 'Timestamp' in df.columns:
                         df['Timestamp'] = pd.to_datetime(df['Timestamp'])
                         df.set_index('Timestamp', inplace=True)
                    return df
                return pd.DataFrame() # Return empty if conversion fails
            else:
                print(f"IBapi: No historical data returned for {contract_to_use.localSymbol}")
                return pd.DataFrame()
        except asyncio.TimeoutError:
            print(f"IBapi: Timeout fetching historical data for {contract.symbol if contract.symbol else 'N/A'}")
            return pd.DataFrame()
        except Exception as e:
            print(f"IBapi: Error fetching historical data for {contract.symbol if contract.symbol else 'N/A'}: {e}")
            import traceback
            traceback.print_exc() # Print full traceback for debugging
            return pd.DataFrame()

    # --- Callback Registration Methods ---
    def register_order_status_update_handler(self, handler: Callable[[Trade, OrderStatus], Coroutine[Any, Any, None]]):
        # print("IBapi: Order status update handler registered.")
        self.order_status_update_handler = handler

    def register_execution_fill_handler(self, handler: Callable[[Trade, Fill], Coroutine[Any, Any, None]]):
        # print("IBapi: Execution fill handler registered.")
        self.execution_fill_handler = handler

    def register_error_handler(self, handler: Callable[[int, int, str, Optional[Contract]], Coroutine[Any, Any, None]]):
        print("IBapi: Error handler registered.")
        self.error_handler = handler
        
    def register_open_order_snapshot_handler(self, handler: Callable[[Contract, Order, OrderState], Coroutine[Any, Any, None]]):
        print("IBapi: Open order snapshot handler registered.")
        self.open_order_snapshot_handler = handler

    async def fetch_all_open_orders(self):
        """Requests all open orders. Results come via openOrderEvent and openOrderEndEvent."""
        if not self.isConnected():
            print("IBapi: Not connected. Cannot fetch open orders.")
            return
        print("IBapi: Requesting all open orders...")
        self.ib.reqAllOpenOrders() # Listen to openOrderEvent and openOrderEndEvent
        # Or:
        # open_orders = await self.ib.reqOpenOrdersAsync() # This waits for openOrderEnd
        # for order_trade in open_orders: # This would be List[Trade]
        #     if self.open_order_snapshot_handler: # If you want to process them immediately
        #         await self.open_order_snapshot_handler(order_trade.contract, order_trade.order, order_trade.orderStatus)

    # 获取日均振幅，默认按照1min、1个月
    async def get_ada(self, data: pd.DataFrame) -> float:
        total = 0
        for i in range(len(data)):
            bar_low = data['Low'].iloc[i]
            bar_high = data['High'].iloc[i]
            total += bar_high - bar_low

        return round(total/len(data), 2)
    
    def run(self):
        self.ib.run()
    
    # 需要订阅才能使用
    async def get_current_price(
        self,
        contract: Contract, # Expects a qualified ib_insync.Contract object
        timeout_seconds: int = 10
    ) -> Optional[float]:
        """
        Fetches the current market price for a given contract.
        Tries to return Last Price, then Close Price, then Mid Price.
        Returns None if no price information is available or an error occurs.
        """
        if not self.isConnected():
            print("IBapi: Not connected. Cannot fetch current price.")
            return None
        if not contract or not contract.conId: # Contract must be qualified
            print(f"IBapi: Contract for '{contract.symbol if contract else 'Unknown'}' is not qualified (missing conId). Cannot fetch current price.")
            return None

        # print(f"IBapi: Requesting current price (ticker snapshot) for {contract.localSymbol}...")
        try:
            tickers: List[Ticker] = await asyncio.wait_for(
                self.ib.reqTickersAsync(contract),
                timeout=timeout_seconds
            )

            if tickers and tickers[0]:
                ticker = tickers[0]

                # Prioritize available prices: Last -> Close -> Mid
                if pd.notna(ticker.last) and ticker.last > 0: # Check for valid price
                    return float(ticker.last)
                elif pd.notna(ticker.close) and ticker.close > 0:
                    return float(ticker.close)
                elif pd.notna(ticker.bid) and ticker.bid > 0 and \
                     pd.notna(ticker.ask) and ticker.ask > 0:
                    return round((float(ticker.bid) + float(ticker.ask)) / 2, 2) # Round mid-price
                else:
                    print(f"IBapi: No valid last, close, or bid/ask price found in ticker for {contract.localSymbol}.")
                    print(f"  Full ticker received: {ticker}") # Log the ticker for debugging
                    return None
            else:
                print(f"IBapi: No ticker data returned for {contract.localSymbol}.")
                return None
        except asyncio.TimeoutError:
            print(f"IBapi: Timeout fetching current price for {contract.localSymbol}.")
            return None
        except ConnectionError as ce:
            print(f"IBapi: Connection error while fetching current price for {contract.localSymbol}: {ce}")
            self._is_connected_flag = False # Mark as disconnected
            self._is_connected = False
            return None
        except Exception as e:
            print(f"IBapi: Error fetching current price for {contract.localSymbol}: {e}")
            return None
  
        
    async def get_account_summary(self, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        print("""Fetches account summary information (e.g., NetLiq, CashBalance).""")

        
    async def get_current_positions(self, account: Optional[str] = None, timeout_seconds: int = 10) -> List[any]: # 修改 BaseAPI 时也应返回 List[Position] 或 List[GenericPosition]
        """
        Fetches all current positions for the default account or a specified account.
        Returns a list of ib_insync.Position objects.
        """
        if not self.isConnected():
            print("IBapi: Not connected. Cannot fetch positions.")
            return []
        
        try:
            print(f"IBapi: Requesting current positions{f' for account {account}' if account else ''}...")
            # ib.positions() is often preferred as it's kept up-to-date by positionEvent
            # If you need a fresh request:
            positions: List[Position] = await asyncio.wait_for(
                self.ib.reqPositionsAsync(), # This requests a fresh batch of positions
                timeout=timeout_seconds
            )
            
            if account: # Filter by account if specified
                positions = [p for p in positions if p.account == account]
            
            return positions
        except asyncio.TimeoutError:
            print(f"IBapi: Timeout fetching positions.")
            return []
        except Exception as e:
            print(f"IBapi: Error fetching positions: {e}")
            return []

    def calculate_atr_from_df(self, data_df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """
        Calculates the ATR for the last available period from a DataFrame.
        Returns the latest ATR value as a float, or None if calculation fails.
        """
        if not isinstance(data_df, pd.DataFrame) or data_df.empty:
            # print("IBapi ATR Calc: Input DataFrame is empty or invalid.")
            return None
        required_cols = ['High', 'Low', 'Close']
        if not all(col in data_df.columns for col in required_cols):
            print(f"IBapi ATR Calc: DataFrame must contain {required_cols} columns. Found: {data_df.columns.tolist()}")
            return None
        # Ensure data is numeric
        for col in required_cols:
            if not pd.api.types.is_numeric_dtype(data_df[col]):
                print(f"IBapi ATR Calc: Column '{col}' is not numeric.")
                return None

        if len(data_df) < period + 1 : 
            # print(f"IBapi ATR Calc: Not enough data ({len(data_df)}) for ATR period {period}.")
            return None

        high_low = data_df['High'] - data_df['Low']
        prev_close = data_df['Close'].shift(1)
        high_prev_close = abs(data_df['High'] - prev_close)
        low_prev_close = abs(data_df['Low'] - prev_close)

        tr_components = pd.DataFrame({'hl': high_low, 'h_pc': high_prev_close, 'l_pc': low_prev_close})
        true_range = tr_components.max(axis=1, skipna=True) # skipna is important
        
        if pd.isna(true_range.iloc[0]) and not pd.isna(high_low.iloc[0]): # First TR might rely on H-L only
            true_range.iloc[0] = high_low.iloc[0]
        
        true_range.dropna(inplace=True) # Remove any remaining NaNs before ATR calculation
        if len(true_range) < period:
            # print(f"IBapi ATR Calc: Not enough valid True Range values ({len(true_range)}) for period {period}.")
            return None

        # Wilder's Smoothing for ATR
        atr_series = pd.Series(np.nan, index=true_range.index, name=f'ATR_{period}')
        
        if not true_range.empty:
            atr_series.iloc[period - 1] = true_range.iloc[:period].mean()
            for i in range(period, len(true_range)):
                atr_series.iloc[i] = (atr_series.iloc[i-1] * (period - 1) + true_range.iloc[i]) / period
            
            latest_atr = atr_series.iloc[-1]
            if not pd.isna(latest_atr):
                # Round to a sensible number of decimal places, e.g., 4 or based on minTick
                # For simplicity, let's use 4 decimal places for ATR value.
                return round(float(latest_atr), 4) 
            # else:
                # print("IBapi ATR Calc: Last ATR value is NaN despite calculations.")
        # else:
            # print("IBapi ATR Calc: True Range series is empty after dropna.")
        return None

    async def get_atr(self, contract_spec: Contract, # Can be an unqualified Contract object
                      atr_period: int = 14, 
                      hist_duration_str: str = "30 D", # Fetch enough data for ATR calc
                      hist_bar_size: str = "1 day") -> Optional[float]:
        """
        Fetches historical daily data for the given contract and calculates its ATR.
        """
        if not self.isConnected(): return None

        days_to_fetch = max(atr_period * 2, 30) # Ensure enough data
        adjusted_duration_str = f"{days_to_fetch} D"

        historical_df = await self.get_historical_data(
            contract=contract_spec,
            duration_str=adjusted_duration_str,
            bar_size_setting=hist_bar_size, # Daily bars for daily ATR
            what_to_show='TRADES',
            use_rth=True,
            format_date=1 # YYYYMMDD HH:MM:SS
        )

        if historical_df.empty or len(historical_df) < atr_period +1 : # Check if enough data returned
            print(f"IBapi get_atr: Not enough historical data for {contract_spec.symbol} to calculate ATR({atr_period}). Got {len(historical_df)} bars.")
            return None

        # 3. Calculate ATR from the fetched DataFrame
        return self.calculate_atr_from_df(historical_df, period=atr_period)