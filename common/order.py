#!/usr/bin/python3
import asyncio
import time
from typing import Dict, Optional, Any, Callable, Coroutine, List

from ib_insync import Contract, Order, Trade, OrderStatus, Fill # Core ib_insync types

# Assuming IBapi is in apis.api
from apis.api import BaseApi

# --- Order State Constants (Optional but good for clarity) ---
ORDER_STATUS_ACTIVE = ["PendingSubmit", "ApiPending", "PendingCancel", "PreSubmitted", "Submitted", "ApiCancelled"] # ApiCancelled is tricky, might be terminal
ORDER_STATUS_TERMINAL = ["Filled", "Cancelled", "Inactive", "Expired", "Unknown"] # Add more as needed

class ActiveOrder:
    """Represents an order managed by the OrderManager."""
    def __init__(self, ib_trade_obj: Trade, strategy_ref: str = "default_strategy", user_ref: Optional[str] = None):
        self.trade: Trade = ib_trade_obj # The ib_insync Trade object
        self.strategy_ref: str = strategy_ref # Which strategy placed this order
        self.user_ref: Optional[str] = user_ref # Any custom user reference
        self.last_status_update_time: float = time.time()
        self.is_being_cancelled: bool = False

    @property
    def order_id(self) -> int:
        return self.trade.order.orderId

    @property
    def perm_id(self) -> Optional[int]:
        return self.trade.order.permId
    
    @property
    def status(self) -> Optional[str]:
        return self.trade.orderStatus.status if self.trade.orderStatus else None

    def is_done(self) -> bool:
        return self.trade.isDone()

class OrderManager:
    def __init__(self, ib_api_instance: BaseApi): # Should be type hinted with your IBapi class
        self.api = ib_api_instance
        self.active_orders: Dict[int, ActiveOrder] = {} # orderId -> ActiveOrder instance
        self.order_id_to_user_ref: Dict[int, str] = {} # Optional: map orderId to a user_ref if needed quickly

        # Callbacks that the OrderManager itself can expose to higher-level logic (e.g., a strategy engine)
        # Signature: async def handler(order_id: int, active_order_obj: ActiveOrder, status: str, fill_info: Optional[Fill])
        self.on_order_final_status_callback: Optional[Callable] = None # For Filled/Cancelled
        self.on_order_intermediate_status_callback: Optional[Callable] = None # For Submitted, etc.
        self.on_order_fill_callback: Optional[Callable] = None # Specifically for fills

        self._register_api_callbacks()

    def _register_api_callbacks(self):
        """Registers this manager's methods as handlers with the IBapi instance."""
        if self.api:
            self.api.register_order_status_update_handler(self._handle_api_order_status_update)
            self.api.register_execution_fill_handler(self._handle_api_execution_fill)
            # self.api.register_open_order_snapshot_handler(self._handle_api_open_order_snapshot) # If needed
            # self.api.register_error_handler(self._handle_api_error) # If OrderManager needs to react to specific errors
        else:
            print("OrderManager: IBapi instance not provided, cannot register callbacks.")

    async def _handle_api_order_status_update(self, trade: Trade, order_status: OrderStatus):
        order_id = trade.order.orderId
        # print(f"OrderManager: Received status update for OrderID {order_id}: {order_status.status}")

        active_order = self.active_orders.get(order_id)
        if not active_order:
            # This could be an order not managed by this instance, or from a previous session if not synced.
            # Or an order just placed for which we haven't stored ActiveOrder yet (race condition potential)
            # For orders placed THROUGH this manager, active_order should exist.
            # print(f"OrderManager: Status update for untracked OrderID {order_id}. Status: {order_status.status}")
            # If it's a new order that just got its first "Submitted" status, we might add it here
            # if order_status.status in ["Submitted", "PreSubmitted"] and order_id not in self.active_orders:
            #    self.active_orders[order_id] = ActiveOrder(trade) # Basic tracking
            return 

        active_order.last_status_update_time = time.time()
        # The trade object's orderStatus is updated by ib_insync directly, so active_order.trade.orderStatus is current.

        if self.on_order_intermediate_status_callback and order_status.status not in ORDER_STATUS_TERMINAL:
            await self.on_order_intermediate_status_callback(order_id, active_order, order_status.status)

        if trade.isDone(): # or order_status.status in ORDER_STATUS_TERMINAL:
            print(f"OrderManager: OrderID {order_id} has reached terminal state: {order_status.status}")
            if self.on_order_final_status_callback:
                # Fill info might not be on this event, but on the Fill event. Pass None for now.
                await self.on_order_final_status_callback(order_id, active_order, order_status.status, None)
            
            # Clean up if truly done (Filled, Cancelled)
            if order_status.status in ["Filled", "Cancelled", "Inactive"]: # Inactive means IB cancelled it
                self.active_orders.pop(order_id, None)
                self.order_id_to_user_ref.pop(order_id, None)
        # else:
            # print(f"OrderManager: OrderID {order_id} status: {order_status.status}, Filled: {order_status.filled}")
            pass


    async def _handle_api_execution_fill(self, trade: Trade, fill: Fill):
        order_id = fill.execution.orderId # or trade.order.orderId
        # print(f"OrderManager: Received fill for OrderID {order_id}: {fill.execution.shares} @ {fill.execution.price}")

        active_order = self.active_orders.get(order_id)
        if not active_order:
            # print(f"OrderManager: Fill for untracked or already processed OrderID {order_id}.")
            return
        
        # ib_insync updates trade.fills list automatically
        active_order.last_status_update_time = time.time()

        if self.on_order_fill_callback:
            await self.on_order_fill_callback(order_id, active_order, fill)
        
        # If the order is now fully filled due to this fill, the orderStatusEvent should reflect that.
        # We can also check here:
        # if active_order.trade.orderStatus and active_order.trade.orderStatus.status == "Filled":
        #     if self.on_order_final_status_callback:
        #         await self.on_order_final_status_callback(order_id, active_order, "Filled", fill)
        #     self.active_orders.pop(order_id, None)
        #     self.order_id_to_user_ref.pop(order_id, None)


    async def place_limit_order(self, contract: Contract, action: str, quantity: float,
                                limit_price: float, order_ref: str = "", 
                                strategy_ref: str = "default_strategy", user_ref: Optional[str] = None,
                                tif: str = "GTC", outside_rth: bool = False) -> Optional[int]:
        """
        Places a new limit order.
        Returns the client-side orderId if submission attempt was made, else None.
        """
        if not self.api.isConnected():
            print("OrderManager: Cannot place order, IB API not connected.")
            return None

        # Get next order ID from IBapi (which gets it from TWS)
        order_id = self.api.get_next_order_id() # This should be a sync call in IBapi as per previous design
        if order_id is None:
            print("OrderManager: Failed to get next valid order ID.")
            return None

        ib_trade_obj: Optional[Trade] = await self.api.place_limit_order(
            contract, action, quantity, limit_price, 
            order_ref=order_ref, tif=tif, outside_rth=outside_rth,
            order_id_to_use=order_id # Pass the fetched order_id
        )

        if ib_trade_obj and ib_trade_obj.order.orderId == order_id:
            active_order = ActiveOrder(ib_trade_obj, strategy_ref, user_ref)
            self.active_orders[order_id] = active_order
            if user_ref:
                self.order_id_to_user_ref[order_id] = user_ref
            print(f"OrderManager: Order {order_id} ({action} {quantity} {contract.localSymbol} @ {limit_price}) submitted successfully.")
            return order_id
        else:
            print(f"OrderManager: Failed to submit order ({action} {quantity} {contract.localSymbol} @ {limit_price}). IBapi.place_limit_order returned: {ib_trade_obj}")
            return None

    async def cancel_order(self, order_id: int) -> bool:
        """Attempts to cancel an active order by its client-side orderId."""
        if not self.api.isConnected():
            print(f"OrderManager: Cannot cancel order {order_id}, IB API not connected.")
            return False

        active_order = self.active_orders.get(order_id)
        if not active_order:
            print(f"OrderManager: OrderID {order_id} not found in active orders for cancellation.")
            # It might be an order managed elsewhere or already completed/cancelled.
            # You could try to cancel it anyway if you have its ib_insync.Order object.
            # For now, we only cancel orders tracked by this manager.
            return False

        if active_order.is_done():
            print(f"OrderManager: OrderID {order_id} is already in a terminal state ({active_order.status}). Cannot cancel.")
            return False
        
        if active_order.is_being_cancelled:
            print(f"OrderManager: OrderID {order_id} cancellation already in progress.")
            return True # Or False, depending on desired behavior

        active_order.is_being_cancelled = True # Mark to avoid duplicate cancel requests
        print(f"OrderManager: Requesting cancellation for OrderID {order_id}...")
        
        # Pass the ib_insync.Order object from the stored Trade object
        cancelled_by_api = await self.api.cancel_ib_order(active_order.trade.order)
        
        if cancelled_by_api:
            print(f"OrderManager: Cancellation request for OrderID {order_id} sent to IB.")
            # Actual confirmation of cancellation will come via _handle_api_order_status_update
        else:
            print(f"OrderManager: Failed to send cancellation request for OrderID {order_id} via API.")
            active_order.is_being_cancelled = False # Reset flag on failure
        
        return cancelled_by_api # Indicates if the cancel request was accepted by IBapi for sending

    def get_active_order(self, order_id: int) -> Optional[ActiveOrder]:
        return self.active_orders.get(order_id)

    def get_all_active_order_ids(self) -> List[int]:
        return list(self.active_orders.keys())

    # --- Methods to register callbacks from strategy ---
    def set_on_order_final_status_callback(self, callback: Callable):
        self.on_order_final_status_callback = callback

    def set_on_order_intermediate_status_callback(self, callback: Callable):
        self.on_order_intermediate_status_callback = callback
        
    def set_on_order_fill_callback(self, callback: Callable):
        self.on_order_fill_callback = callback