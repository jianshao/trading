#!/usr/bin/python3
from abc import ABC, abstractmethod
from typing import Optional, Callable, Any, List, Dict, Coroutine, Union # Added Union
import pandas as pd

# --- Forward declarations for type hints if using concrete ib_insync types in ABC ---
# These are not strictly necessary for ABC if using Any, but good for clarity if you
# intend specific structures for callbacks even at the ABC level.
# However, it's often better to use more generic types in ABCs.
# from ib_insync import Contract as IBContract, Order as IBOrder, Trade as IBTrade
# from ib_insync import OrderStatus as IBOrderStatus, Fill as IBFill

# For ABC, let's try to use more generic type hints where possible,
# or define simple data structures if needed by the ABC interface.

# Simple data structures that could be used by the ABC if not relying on specific library types
class GenericContract:
    symbol: str
    secType: str # e.g., STK, OPT, FUT, CASH
    exchange: str
    currency: str
    primaryExchange: Optional[str] = None
    conId: Optional[int] = None # Broker-specific ID
    localSymbol: Optional[str] = None # Broker-specific symbol

class GenericOrder:
    orderId: int # Client-side order ID
    permId: Optional[int] # Broker-assigned permanent ID
    action: str # BUY, SELL
    orderType: str # LMT, MKT, STP, etc.
    totalQuantity: float
    lmtPrice: Optional[float] = None
    auxPrice: Optional[float] = None # For STP, TRAIL, etc.
    tif: str # GTC, DAY, IOC, etc.
    orderRef: Optional[str] = None # Client-side reference
    status: Optional[str] = None # Filled, Submitted, Cancelled, etc.

class GenericFill:
    orderId: int
    permId: Optional[int]
    execId: str # Broker's execution ID
    time: Any # Execution time (datetime or timestamp string)
    shares: float
    price: float
    avgPrice: Optional[float] # Often same as price for a single fill
    cumQty: Optional[float]
    commission: float
    commissionCurrency: str
    realizedPNL: Optional[float] = None


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
    async def place_limit_order(
        self,
        contract: Any, # Broker-specific Contract object or GenericContract
        action: str,    # "BUY" or "SELL"
        quantity: float,
        limit_price: float,
        order_ref: str = "",
        tif: str = "GTC",
        transmit: bool = True,
        outside_rth: bool = False,
        order_id_to_use: Optional[int] = None
    ) -> Optional[Any]: # Returns broker-specific Trade/Order object or a generic identifier
        """Places a limit order."""
        pass

    @abstractmethod
    async def cancel_order(
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
    async def get_contract_details(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        primary_exchange: Optional[str] = None
    ) -> Optional[Any]: # Returns broker-specific qualified Contract object or GenericContract
        """Fetches and qualifies contract details."""
        pass

    @abstractmethod
    async def get_historical_data(
        self,
        contract: Any, # Broker-specific Contract object or GenericContract
        end_date_time: str = "",
        duration_str: str = "1 M",
        bar_size_setting: str = "1 min",
        what_to_show: str = 'TRADES',
        use_rth: bool = True,
        format_date: int = 1, # Or a more generic way to specify date format
        timeout_seconds: int = 60
    ) -> pd.DataFrame: # Standardize to return Pandas DataFrame
        """Fetches historical bar data."""
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
    async def get_current_positions(self, account: Optional[str] = None, timeout_seconds: int = 10) -> List[any]:  # List of position details
        """Fetches current account positions."""
        pass


    # --- Callback Registration ---
    # Callbacks should ideally pass standardized data structures if possible,
    # or the consuming engine needs to know the broker-specific types.
    # For now, using 'Any' but could define GenericOrderStatus, GenericFill.

    @abstractmethod
    def register_order_status_update_handler(
        self,
        handler: Callable[[Any, Any], Coroutine[Any, Any, None]] # e.g., (broker_trade_obj, broker_order_status_obj)
    ):
        """Registers a handler for order status updates."""
        pass

    @abstractmethod
    def register_execution_fill_handler(
        self,
        handler: Callable[[Any, Any], Coroutine[Any, Any, None]] # e.g., (broker_trade_obj, broker_fill_obj)
    ):
        """Registers a handler for order execution (fill) details."""
        pass

    @abstractmethod
    def register_error_handler(
        self,
        handler: Callable[[int, int, str, Optional[Any]], Coroutine[Any, Any, None]] # (reqId, errCode, errMsg, contract_obj_if_any)
    ):
        """Registers a handler for API errors."""
        pass
        
    @abstractmethod
    def register_open_order_snapshot_handler(
        self,
        handler: Callable[[Any], Coroutine[Any, Any, None]] # e.g., (broker_trade_obj)
    ):
        """Registers a handler for open order snapshots received on connect or request."""
        pass
        
    @abstractmethod
    def register_disconnected_handler(
        self,
        handler: Callable[[], Coroutine[Any, Any, None]] # No arguments
    ):
        """Registers a handler for when the API disconnects."""
        pass