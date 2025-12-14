#!/usr/bin/python3
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Callable, Any, List, Dict, Coroutine, Union # Added Union
import pandas as pd

from strategy.common import GridOrder

# --- Forward declarations for type hints if using concrete ib_insync types in ABC ---
# These are not strictly necessary for ABC if using Any, but good for clarity if you
# intend specific structures for callbacks even at the ABC level.
# However, it's often better to use more generic types in ABCs.
# from ib_insync import Contract as IBContract, Order as IBOrder, Trade as IBTrade
# from ib_insync import OrderStatus as IBOrderStatus, Fill as IBFill

# For ABC, let's try to use more generic type hints where possible,
# or define simple data structures if needed by the ABC interface.
OrderUpdateCallback = Callable[[GridOrder], Coroutine[Any, Any, None]]

class BaseAPI(ABC):
    """
    Abstract Base Class for a trading API.
    Defines the common interface that all specific API wrappers should implement.
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Connects to the trading API."""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnects from the trading API."""
        pass

    @abstractmethod
    def isConnected(self) -> bool:
        """Returns True if currently connected, False otherwise."""
        pass

    # --- Order Management ---
    @abstractmethod
    def get_next_order_id(self) -> int:
        """
        Gets the next available client-side order ID.
        This ID should be unique for orders placed by this API instance.
        """
        pass

    @abstractmethod
    async def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Optional[Any] = None):
        pass
    
    @abstractmethod
    async def _on_ib_order_status_event(self, trade: Any):
        pass
    
    @abstractmethod
    async def _on_ib_exec_details_event(self, trade: Any, fill: Any):
        pass
    
    @abstractmethod
    async def _on_ib_open_order_snapshot_event(self, trade: Any):
        pass

    @abstractmethod
    def _on_ib_disconnected_event(self):
        pass
    

    @abstractmethod
    async def place_limit_order(self, symbol: str, action: str, quantity: float, 
                                limit_price: float, order_ref: str = "", tif: str = "GTC", 
                                transmit: bool = True, outside_rth: bool = False,
                                order_id_to_use: Optional[int] = None) -> Optional[GridOrder]:
        """Places a limit order and returns the ib_insync Trade object."""
        pass

    @abstractmethod
    def cancel_order(
        self,
        order_to_cancel: Any # Broker-specific Order object or GenericOrder or just orderId
    ) -> bool:
        """Cancels an existing order."""
        pass
        
    @abstractmethod
    async def fetch_all_open_orders(self) -> List[Any]: # List of broker-specific Trade/Order objects or GenericOrder
        """Requests all currently open orders for the account."""
        pass

    # --- Market & Account Data ---
    @abstractmethod
    def get_contract_details(self, symbol: str, exchange: str = "SMART", 
                             currency: str = "USD", primary_exchange: Optional[str] = None) -> Optional[Any]:
        pass

    @abstractmethod
    async def get_current_price(
        self,
        contract: Any # Broker-specific Contract object or GenericContract
    ) -> Optional[float]: # Returns the last traded price or mid-price
        """Fetches the current market price for a contract."""
        pass
        
    @abstractmethod
    async def get_account_summary(self, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """Fetches account summary information (e.g., NetLiq, CashBalance)."""
        pass

    @abstractmethod
    async def get_current_positions(self, account: Optional[str] = None, timeout_seconds: int = 10) -> List[Any]:  # List of position details
        """Fetches current account positions."""
        pass

    @abstractmethod
    async def get_atr(self, symbol: str, # Can be an unqualified Contract object
                      atr_period: int = 14, 
                      hist_duration: int = 30, # Fetch enough data for ATR calc
                      hist_bar_size: str = "1 day") -> Optional[float]:
        pass

    # --- Callback Registration Methods ---
    @abstractmethod
    def register_order_status_update_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        pass

    @abstractmethod
    def register_execution_fill_handler(self, handler: Callable[[Any, Any], Coroutine[Any, Any, None]]):
        pass

    @abstractmethod
    def register_error_handler(self, handler: Callable[[int, int, str, Optional[Any]], Coroutine[Any, Any, None]]):
        pass
        
    @abstractmethod
    def register_open_order_snapshot_handler(self, handler: Callable[[Any, Any, Any], Coroutine[Any, Any, None]]):
        pass

    @abstractmethod
    def register_disconnected_handler(self, handler: Callable[[], Coroutine[Any, Any, None]]):
        pass

    @abstractmethod
    def reqMktData(self, contract: Any, 
                   genericTickList: str = "", 
                   snapshot: bool = False, 
                   regulatorySnapshot: bool = False, 
                   mktDataOptions: Optional[List[Any]] = None) -> Any:
        """Requests market data for a given contract."""
        pass
    
    @abstractmethod
    async def get_historical_data(self, symbol: str, end_date_time: str = "", 
                                  duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                  what_to_show: str = 'TRADES', use_rth: bool = True, 
                                  format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:
        pass
    
    @abstractmethod
    async def get_latest_price(self, symbol: str, exchange="SMART", currency="USD"):
        pass

    @abstractmethod
    async def get_ma(self, symbol: str, ema_period: int, bar_size: str = "1 day", duration: str = "60 D") -> float:
        pass

    @abstractmethod
    async def get_ema(self, symbol: str, ema_period: int, bar_size: str = "1 day", duration: str = "60 D") -> float:
        pass
    
    @abstractmethod
    def get_current_time(self) -> datetime:
        pass