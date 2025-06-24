# backtesting_system/order_model.py
import time
import uuid
from enum import Enum
from typing import Optional, List

class OrderStatus(Enum):
    PENDING_SUBMIT = "PendingSubmit"
    SUBMITTED = "Submitted" # Broker Acknowledged
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    PENDING_CANCEL = "PendingCancel"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    EXPIRED = "Expired"

class Order:
    def __init__(self, internal_id: str, strategy_id: str, symbol: str, side: str, 
                 order_type: str, quantity: float, limit_price: Optional[float] = None, 
                 stop_price: Optional[float] = None, tif: str = "GTC", 
                 creation_time: Optional[float] = None, order_ref: Optional[str] = None):
        self.internal_id: str = internal_id # OMS/Backtester unique ID
        self.broker_order_id: Optional[str] = f"sim_{internal_id[:8]}" # Simulated broker ID
        self.strategy_id: str = strategy_id
        self.symbol: str = symbol
        self.side: str = side.upper() # "BUY" or "SELL"
        self.order_type: str = order_type.upper() # "LMT", "MKT"
        self.quantity: float = quantity
        self.limit_price: Optional[float] = limit_price
        self.stop_price: Optional[float] = stop_price
        self.tif: str = tif
        self.order_ref: Optional[str] = order_ref

        self.status: OrderStatus = OrderStatus.PENDING_SUBMIT
        self.filled_quantity: float = 0.0
        self.avg_fill_price: Optional[float] = None
        self.commission: float = 0.0
        self.creation_time: float = creation_time if creation_time else time.time()
        self.last_update_time: float = self.creation_time

    def is_active(self) -> bool:
        return self.status not in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]

    def __repr__(self):
        return (f"Order(id={self.internal_id}, sym={self.symbol}, {self.side} {self.quantity}@{self.limit_price or 'MKT'}, "
                f"status={self.status.value}, filled={self.filled_quantity})")

class Fill:
    def __init__(self, internal_order_id: str, symbol: str, side: str, 
                 fill_qty: float, fill_price: float, commission: float, 
                 timestamp: float, execution_id: Optional[str] = None):
        self.internal_order_id = internal_order_id
        self.execution_id = execution_id if execution_id else f"sim_exec_{uuid.uuid4().hex[:8]}"
        self.symbol = symbol
        self.side = side
        self.fill_qty = fill_qty
        self.fill_price = fill_price
        self.commission = commission
        self.timestamp = timestamp
    
    def __repr__(self):
        return (f"Fill(order_id={self.internal_order_id}, sym={self.symbol}, {self.side} {self.fill_qty}@{self.fill_price}, "
                f"comm={self.commission})")