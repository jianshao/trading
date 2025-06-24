# backtesting_system/portfolio.py
import time
import uuid
from typing import Dict, List, Optional, Tuple, Callable, Any
from .order_model import Order, Fill, OrderStatus # Use . for relative import
import pandas as pd

class Portfolio:
    def __init__(self, initial_capital: float, fee_per_trade: float = 0.0, fee_per_share: float = 0.0):
        self.initial_capital: float = initial_capital
        self.cash: float = initial_capital
        self.positions: Dict[str, float] = {}  # symbol -> quantity
        self.avg_costs: Dict[str, float] = {} # symbol -> average cost of current position
        self.realized_pnl: float = 0.0
        self.trade_history: List[Fill] = []
        
        self.pending_orders: Dict[str, Order] = {} # internal_order_id -> Order object
        self._next_internal_order_id_counter: int = 1

        # Fee structure (example)
        self.fee_per_trade: float = fee_per_trade # Fixed fee per trade action
        self.fee_per_share: float = fee_per_share # Variable fee based on shares

        # Callback to notify BacktestEngine of order events
        self.order_event_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def _get_next_internal_order_id(self) -> str:
        internal_id = f"oms_{self._next_internal_order_id_counter:06d}"
        self._next_internal_order_id_counter += 1
        return internal_id

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def get_cash(self) -> float:
        return self.cash

    def _calculate_commission(self, quantity: float, price: float) -> float:
        commission = self.fee_per_trade
        commission += self.fee_per_share * abs(quantity)
        # Add more complex fee logic if needed (e.g., percentage, min/max)
        return commission

    def place_limit_order(self, strategy_id: str, symbol: str, side: str, price: float, 
                          quantity: float, tif: str = "GTC", order_ref: Optional[str] = None) -> Optional[str]:
        internal_id = self._get_next_internal_order_id()
        order = Order(
            internal_id=internal_id, strategy_id=strategy_id, symbol=symbol,
            side=side, order_type="LMT", quantity=abs(quantity), limit_price=price, 
            tif=tif, order_ref=order_ref
        )
        
        # Basic cash check for buys (simplified, no margin considered)
        if side.upper() == "BUY":
            required_cash = quantity * price + self._calculate_commission(quantity, price) # Estimated cost
            if self.cash < required_cash:
                print(f"Portfolio: Insufficient cash for BUY {quantity} {symbol} @ {price}. Need {required_cash:.2f}, Have {self.cash:.2f}")
                if self.order_event_callback:
                    self.order_event_callback({
                        'internal_id': internal_id, 'strategy_id': strategy_id, 'symbol': symbol,
                        'status': OrderStatus.REJECTED.value, 'reason': 'Insufficient funds'
                    })
                return None
        
        order.status = OrderStatus.SUBMITTED # Assume broker accepted it
        self.pending_orders[internal_id] = order
        print(f"Portfolio: Order placed - {order}")
        if self.order_event_callback:
            self.order_event_callback({
                'internal_id': internal_id, 'strategy_id': strategy_id, 'symbol': symbol,
                'status': order.status.value, 'order_obj': order # Pass full order for strategy to track
            })
        return internal_id

    def cancel_order(self, internal_order_id: str) -> bool:
        order = self.pending_orders.get(internal_order_id)
        if order and order.is_active():
            order.status = OrderStatus.PENDING_CANCEL # Mark for cancellation
            # In a real backtester, the engine would decide if it "gets cancelled" in the next bar
            # For simplicity, let's assume it gets cancelled if not filled in the same bar it was marked.
            print(f"Portfolio: Order {internal_order_id} marked PENDING_CANCEL.")
            # Actual cancellation processing (and moving to CANCELLED) happens in BacktestEngine's fill logic
            return True
        print(f"Portfolio: Order {internal_order_id} not found or not active for cancellation.")
        return False

    def process_fill(self, internal_order_id: str, fill_qty: float, fill_price: float, timestamp: float):
        """Called by BacktestEngine when an order is considered filled."""
        order = self.pending_orders.get(internal_order_id)
        if not order or not order.is_active():
            print(f"Portfolio Error: Fill received for unknown or inactive order {internal_order_id}")
            return

        if fill_qty > order.quantity - order.filled_quantity:
            fill_qty = order.quantity - order.filled_quantity # Cap fill at remaining

        commission = self._calculate_commission(fill_qty, fill_price)
        
        fill_event_data = {
            'internal_id': order.internal_id, 'strategy_id': order.strategy_id, 'symbol': order.symbol,
            'filled_qty': fill_qty, 'avg_fill_price': fill_price, 'commission': commission
        }

        if order.side == "BUY":
            cost = fill_qty * fill_price
            self.cash -= (cost + commission)
            
            current_qty = self.positions.get(order.symbol, 0.0)
            current_avg_cost = self.avg_costs.get(order.symbol, 0.0)
            
            new_total_cost = (current_avg_cost * current_qty) + cost
            self.positions[order.symbol] = current_qty + fill_qty
            self.avg_costs[order.symbol] = new_total_cost / self.positions[order.symbol] if self.positions[order.symbol] != 0 else 0.0
            
        elif order.side == "SELL":
            proceeds = fill_qty * fill_price
            self.cash += (proceeds - commission)
            
            # Calculate realized PnL for this sell
            if order.symbol in self.avg_costs and self.avg_costs[order.symbol] > 0: # Ensure we had a cost basis
                cost_of_sold_shares = self.avg_costs[order.symbol] * fill_qty
                pnl_this_fill = proceeds - cost_of_sold_shares - commission # PNL for this fill
                self.realized_pnl += pnl_this_fill
                fill_event_data['realized_pnl_this_fill'] = pnl_this_fill

            self.positions[order.symbol] = self.positions.get(order.symbol, 0.0) - fill_qty
            if abs(self.positions[order.symbol]) < 1e-9 : # Float comparison for zero
                self.positions.pop(order.symbol, None)
                self.avg_costs.pop(order.symbol, None)
        
        order.filled_quantity += fill_qty
        # Simple avg_fill_price update, more complex for partial fills over time
        if order.avg_fill_price is None:
            order.avg_fill_price = fill_price
        else: # Weighted average
            order.avg_fill_price = ((order.avg_fill_price * (order.filled_quantity - fill_qty)) + (fill_price * fill_qty)) / order.filled_quantity

        order.commission += commission
        order.last_update_time = timestamp

        fill_record = Fill(order.internal_id, order.symbol, order.side, fill_qty, fill_price, commission, timestamp)
        self.trade_history.append(fill_record)

        if order.filled_quantity >= order.quantity:
            order.status = OrderStatus.FILLED
            self.pending_orders.pop(order.internal_id, None) # Remove from pending
            print(f"Portfolio: Order {order.internal_id} FILLED - {order.side} {fill_qty} {order.symbol} @ {fill_price:.2f}")
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            print(f"Portfolio: Order {order.internal_id} PARTIALLY FILLED - {order.side} {fill_qty}/{order.quantity} {order.symbol} @ {fill_price:.2f}")

        if self.order_event_callback:
            fill_event_data['status'] = order.status.value
            fill_event_data['remaining_qty'] = order.quantity - order.filled_quantity
            self.order_event_callback(fill_event_data)


    def get_market_value(self, current_prices: Dict[str, float]) -> float:
        """Calculates current market value of all holdings."""
        market_value = 0.0
        for symbol, quantity in self.positions.items():
            price = current_prices.get(symbol, self.avg_costs.get(symbol, 0.0)) # Fallback to avg_cost if no current price
            market_value += quantity * price
        return market_value

    def get_unrealized_pnl(self, current_prices: Dict[str, float]) -> float:
        unrealized = 0.0
        for symbol, quantity in self.positions.items():
            if quantity == 0: continue
            current_price = current_prices.get(symbol)
            avg_cost = self.avg_costs.get(symbol)
            if current_price is not None and avg_cost is not None:
                unrealized += (current_price - avg_cost) * quantity
        return unrealized

    def get_total_equity(self, current_prices: Dict[str, float]) -> float:
        return self.cash + self.get_market_value(current_prices)

    def get_summary(self, current_prices: Optional[Dict[str, float]] = None) -> Dict:
        summary = {
            "initial_capital": self.initial_capital,
            "final_cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "open_positions_count": len(self.positions),
        }
        if current_prices:
            summary["market_value_positions"] = self.get_market_value(current_prices)
            summary["unrealized_pnl"] = self.get_unrealized_pnl(current_prices)
            summary["total_equity"] = self.get_total_equity(current_prices)
        return summary