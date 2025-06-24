# backtesting_system/strategy_interface.py
from abc import ABC, abstractmethod
import pandas as pd
from typing import Any, Dict, Optional

# Forward declaration for Order and Portfolio if they are complex classes
# class Order: pass
# class Portfolio: pass

class StrategyBase(ABC):
    def __init__(self, strategy_id: str, symbol: str, params: Dict[str, Any]):
        self.strategy_id = strategy_id
        self.symbol = symbol # Assuming one symbol per strategy instance for simplicity
        self.params = params
        self.is_active = False
        self.portfolio: Optional[Any] = None # Will be injected by BacktestEngine
        self.current_data: Optional[pd.Series] = None # Current bar data
        self.current_datetime: Optional[pd.Timestamp] = None

    def _set_portfolio_access(self, portfolio_accessor: Any):
        """Called by the BacktestEngine to provide portfolio access."""
        self.portfolio = portfolio_accessor

    def _set_current_data_and_time(self, bar_data: pd.Series, dt: pd.Timestamp):
        self.current_data = bar_data
        self.current_datetime = dt

    @abstractmethod
    def initialize(self):
        """
        Called once at the start of the backtest for this strategy.
        Use this to set up initial state, calculate indicators, etc.
        """
        pass

    @abstractmethod
    def on_bar_data(self, bar_data: pd.Series, dt: pd.Timestamp):
        """
        Called for each new bar of data.
        bar_data: A Pandas Series representing the current bar (Open, High, Low, Close, Volume, etc.)
        dt: Timestamp of the current bar.
        """
        pass

    @abstractmethod
    def on_order_event(self, order_event: Dict[str, Any]):
        """
        Called when an order related to this strategy has an event (e.g., filled, cancelled).
        order_event: A dictionary containing order event details.
                     Example: {'order_id': int, 'internal_id': str, 'status': str, 
                               'filled_qty': float, 'avg_fill_price': float, 'commission': float, ...}
        """
        pass
    
    @abstractmethod
    def on_stop(self):
        """
        Called at the end of the backtest for any cleanup.
        """
        pass

    # --- Helper methods for placing orders via portfolio ---
    # These are convenience methods that call the portfolio's order functions.
    def place_limit_order(self, side: str, price: float, quantity: float, tif: str = "GTC", order_ref: Optional[str] = None) -> Optional[str]:
        """Helper to place a limit order."""
        if self.portfolio:
            return self.portfolio.place_limit_order(
                strategy_id=self.strategy_id,
                symbol=self.symbol,
                side=side.upper(),
                price=price,
                quantity=quantity,
                tif=tif,
                order_ref=order_ref
            )
        print(f"Warning ({self.strategy_id}): Portfolio not set, cannot place order.")
        return None

    def cancel_order(self, internal_order_id: str) -> bool:
        """Helper to cancel an order."""
        if self.portfolio:
            return self.portfolio.cancel_order(internal_order_id)
        print(f"Warning ({self.strategy_id}): Portfolio not set, cannot cancel order.")
        return False

    def get_position(self) -> float:
        """Helper to get current position for this strategy's symbol."""
        if self.portfolio:
            return self.portfolio.get_position(self.symbol)
        return 0.0
    
    def get_cash(self) -> float:
        """Helper to get current cash balance."""
        if self.portfolio:
            return self.portfolio.get_cash()
        return 0.0