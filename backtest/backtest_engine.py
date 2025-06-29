# backtesting_system/backtest_engine.py
import asyncio
import pandas as pd
from typing import List, Dict, Any, Type, Optional
from .strategy_interface import StrategyBase
from .portfolio import Portfolio
from .order_model import OrderStatus # For event creation

class BacktestEngine:
    def __init__(self, data_feed: Dict[str, pd.DataFrame], 
                 strategy_configs: List[Dict[str, Any]], # List of {'module': 'strategies.grid_strategy_backtest', 'class': 'GridStrategyBacktest', 'id': 'GRID_WBD_1', 'symbol': 'WBD', 'params': {...}}
                 initial_capital: float, fee_per_trade: float = 0.0):
        self.data_feed = data_feed # symbol -> DataFrame
        self.strategy_configs = strategy_configs
        self.portfolio = Portfolio(initial_capital, fee_per_trade=fee_per_trade)
        self.strategies: Dict[str, StrategyBase] = {} # strategy_id -> Strategy instance
        self.current_datetime: Optional[pd.Timestamp] = None
        self.portfolio.order_event_callback = self._on_portfolio_order_event # Link portfolio events back to engine

    def _load_strategy(self, config: Dict[str, Any]) -> Optional[StrategyBase]:
        try:
            module_path = config['module']
            class_name = config['class']
            strategy_id = config['id']
            symbol = config['symbol']
            params = config['params']
            
            module = __import__(module_path, fromlist=[class_name])
            StrategyClass = getattr(module, class_name)
            
            strategy_instance = StrategyClass(strategy_id, symbol, params)
            strategy_instance._set_portfolio_access(self.portfolio) # Inject portfolio
            return strategy_instance
        except Exception as e:
            print(f"Error loading strategy {config.get('id', 'Unknown')}: {e}")
            return None

    def _on_portfolio_order_event(self, event_data: Dict[str, Any]):
        """Callback from Portfolio when an order it manages has an event."""
        strategy_id = event_data.get('strategy_id')
        if strategy_id and strategy_id in self.strategies:
            # This needs to be scheduled in the async loop if strategy's on_order_event is async
            # For a fully async engine, this callback itself would be async or schedule an async task.
            # Assuming strategy's on_order_event is now async
            asyncio.create_task(self.strategies[strategy_id].on_order_event(event_data))
        else:
            print(f"BacktestEngine: Received order event for unknown or inactive strategy_id: {strategy_id}")


    async def run_backtest(self, start_date: Optional[str] = None, end_date: Optional[str] = None):
        print("--- Starting Backtest ---")
        # 1. Load and initialize strategies
        for config in self.strategy_configs:
            strategy = self._load_strategy(config)
            if strategy:
                self.strategies[strategy.strategy_id] = strategy
                # If strategy has async initialize_async (like our GridStrategyBacktest)
                if hasattr(strategy, "initialize_async") and callable(strategy.initialize_async):
                    # We need a reference price for GridStrategy's init.
                    # This could be the first open price of the data or a param.
                    symbol_data = self.data_feed.get(strategy.symbol)
                    if symbol_data is not None and not symbol_data.empty:
                        initial_ref_price = symbol_data['Open'].iloc[0] # Example
                        await strategy.initialize_async(initial_ref_price) # Pass necessary init args
                    else:
                        print(f"Warning: No data for {strategy.symbol} to get initial ref price for {strategy.strategy_id}")
                elif hasattr(strategy, "initialize") and callable(strategy.initialize): # Sync fallback
                    strategy.initialize() 
        
        if not self.strategies:
            print("No strategies loaded. Exiting backtest.")
            return {}

        # 2. Determine common time index for all symbols (simplified: use first symbol's index)
        # A more robust engine would handle multiple data feeds and align them.
        first_symbol = next(iter(self.data_feed))
        if not first_symbol: print("No data in data_feed."); return {}
        
        event_schedule = self.data_feed[first_symbol].index
        if start_date: event_schedule = event_schedule[event_schedule >= pd.to_datetime(start_date)]
        if end_date: event_schedule = event_schedule[event_schedule <= pd.to_datetime(end_date)]

        if event_schedule.empty:
            print("No data for the specified date range.")
            return {}
            
        print(f"Backtesting from {event_schedule[0]} to {event_schedule[-1]}")

        # 3. Main event loop (iterating through time)
        for dt in event_schedule:
            self.current_datetime = dt
            current_prices_for_portfolio: Dict[str, float] = {}

            # A. Process fills for any pending orders based on current bar's H/L/O/C
            # This is the "broker simulation" part.
            orders_to_process = list(self.portfolio.pending_orders.values()) # Copy for safe iteration
            for order in orders_to_process:
                if order.symbol not in self.data_feed or self.data_feed[order.symbol].empty: continue
                
                bar_data_for_order_symbol = self.data_feed[order.symbol].loc[dt] if dt in self.data_feed[order.symbol].index else None
                if bar_data_for_order_symbol is None: continue # No data for this symbol at this timestamp

                # Check for cancellation first
                if order.status == OrderStatus.PENDING_CANCEL:
                    order.status = OrderStatus.CANCELLED
                    order.last_update_time = dt.timestamp()
                    print(f"  {dt} Order {order.internal_id} CANCELLED for {order.symbol}")
                    self.portfolio.pending_orders.pop(order.internal_id, None)
                    if self.portfolio.order_event_callback:
                         self.portfolio.order_event_callback({
                            'internal_id': order.internal_id, 'strategy_id': order.strategy_id, 
                            'symbol': order.symbol, 'status': order.status.value,
                            'timestamp': dt
                        })
                    continue # Move to next order

                if not order.is_active(): continue

                # Simplified fill logic: if limit price is touched by High/Low
                filled_this_bar = False
                fill_price = 0.0
                if order.order_type == "LMT":
                    if order.side == "BUY" and bar_data_for_order_symbol['Low'] <= order.limit_price:
                        fill_price = min(bar_data_for_order_symbol['Open'], order.limit_price) # Simulate slippage or open fill
                        filled_this_bar = True
                    elif order.side == "SELL" and bar_data_for_order_symbol['High'] >= order.limit_price:
                        fill_price = max(bar_data_for_order_symbol['Open'], order.limit_price)
                        filled_this_bar = True
                
                if filled_this_bar:
                    # For simplicity, assume full fill if condition met
                    self.portfolio.process_fill(order.internal_id, order.quantity - order.filled_quantity, fill_price, dt.timestamp())
                    # process_fill will update order status and call order_event_callback

            # B. Pass current bar data to all strategies
            for strategy_id, strategy in self.strategies.items():
                if strategy.symbol not in self.data_feed or self.data_feed[strategy.symbol].empty: continue
                
                bar_data_for_strat_symbol = self.data_feed[strategy.symbol].loc[dt] if dt in self.data_feed[strategy.symbol].index else None
                if bar_data_for_strat_symbol is not None:
                    current_prices_for_portfolio[strategy.symbol] = bar_data_for_strat_symbol['Close']
                    # Call strategy's on_bar_data (should be async if it does async work)
                    if hasattr(strategy, "on_bar_data_async") and callable(strategy.on_bar_data_async):
                        await strategy.on_bar_data_async(bar_data_for_strat_symbol, dt)
                    elif hasattr(strategy, "on_bar_data") and callable(strategy.on_bar_data):
                        strategy.on_bar_data(bar_data_for_strat_symbol, dt) # Sync call

            # C. Yield control to allow any async tasks (like strategy order processing) to run
            await asyncio.sleep(0) # Crucial for async strategies

        # 4. End of backtest: call on_stop for strategies
        for strategy in self.strategies.values():
            if hasattr(strategy, "on_stop_async") and callable(strategy.on_stop_async):
                await strategy.on_stop_async()
            elif hasattr(strategy, "DoStop") and callable(strategy.DoStop):
                strategy.on_stop()
        
        print("--- Backtest Completed ---")
        final_summary = self.portfolio.get_summary(current_prices_for_portfolio if current_prices_for_portfolio else None)
        print("Portfolio Summary:", final_summary)
        return final_summary, self.portfolio.trade_history