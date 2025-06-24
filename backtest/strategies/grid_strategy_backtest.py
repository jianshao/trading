import pandas as pd
import asyncio
from typing import Dict, Any, Optional
from ib_async import Order, Trade, OrderStatus

from backtest.strategy_interface import StrategyBase
from strategy.grid_new import GridStrategy

class GridStrategyBacktest(StrategyBase):
    def __init__(self, strategy_id: str, symbol: str, params: Dict[str, Any]):
        super().__init__(strategy_id, symbol, params)
        
        # Create GridStrategyParams from the generic params dict
        # This assumes params dict has keys matching GridStrategyParams constructor
        try:
            self.grid_params = GridStrategy(
                symbol=symbol, # Symbol comes from StrategyBase
                lower_bound=float(params["lower_bound"]),
                upper_bound=float(params["upper_bound"]),
                price_spacing=float(params["price_spacing"]),
                shares_per_grid=int(params["shares_per_grid"]),
                fee_per_trade=float(params.get("fee_per_trade", 0.0)), # Fee comes from portfolio now
                strategy_id=strategy_id, # strategy_id comes from StrategyBase
                primary_exchange=params.get("primary_exchange", "SMART"),
                # max_grids_up_config and max_grids_down_config for GridStrategy internal logic
                max_grids_up_config=int(params.get("max_grids_up", 2)), 
                max_grids_down_config=int(params.get("max_grids_down", 2))
            )
        except KeyError as e:
            raise ValueError(f"Missing parameter for GridStrategy: {e}")

        # The GridStrategy needs an "API-like" object to place orders and a way to get order IDs.
        # In backtesting, these will be routed to the BacktestEngine's Portfolio.
        # We will pass `self` (this GridStrategyBacktest instance) as the "api" to GridStrategy,
        # and GridStrategy will call self.place_limit_order, self.cancel_order, etc.
        # which are defined in StrategyBase and call the portfolio.
        
        # The get_order_id function for GridStrategy needs to come from the Portfolio/BacktestEngine
        # For now, placeholder. This will be set by BacktestEngine.
        self.grid_logic: Optional[GridStrategy] = None
        self.is_initialized = False


    def _get_sim_order_id_for_grid_logic(self, strategy_id_from_grid: str) -> int:
        # The backtester's portfolio will generate internal IDs which are strings.
        # GridStrategy's get_order_id expects an int.
        # This is a slight mismatch. For now, let's assume the portfolio's place_limit_order
        # will return a string internal_id, and GridStrategy's order maps need to handle this.
        # OR, we modify GridStrategy to work with string IDs, or OMS provides int IDs.
        # For simplicity in this adapter, let's assume the main backtest engine's portfolio provides
        # a way to get a *numeric* ID if GridStrategy absolutely needs it, or we adapt GridStrategy.
        # Here, we'll assume Portfolio's place_limit_order returns a string,
        # and we might not directly use this callback if GridStrategy calls self.place_limit_order.
        if self.portfolio:
            # This simulates getting a new *broker* order ID.
            # In backtesting, we might just use a counter from the portfolio for its internal string IDs.
            return self.portfolio._get_next_sim_broker_order_id() # Requires this method in Portfolio
        raise RuntimeError("Portfolio not available to get order ID.")


    def initialize(self):
        """Initialize the underlying GridStrategy logic."""
        print(f"Initializing GridStrategyBacktest for {self.strategy_id} ({self.symbol})")
        if not self.portfolio:
            raise RuntimeError("Portfolio access not set for strategy before initialization.")

        # Pass `self` as the `api` argument to GridStrategy, so it uses the helper methods.
        # GridStrategy needs `get_contract_details` and `place_limit_order` etc.
        # StrategyBase provides `place_limit_order`, `cancel_order`.
        # We need to ensure GridStrategy can get contract details.
        # For backtesting, contract details are often simplified or assumed.
        
        # Mocking parts of the API that GridStrategy might expect, routed to self or portfolio
        class MockApiForGridStrategy:
            def __init__(self, strategy_adapter: GridStrategyBacktest):
                self.adapter = strategy_adapter
                # Mock contract cache if GridStrategy uses it
                self.contracts_cache = {self.adapter.symbol: self.adapter.portfolio.get_contract_object_for_symbol(self.adapter.symbol)} # Portfolio needs to provide this

            async def get_contract_details(self, symbol, primary_exchange=None, exchange=None): # Async to match IBapi
                # In backtest, we might just return a simple contract object
                if symbol == self.adapter.symbol:
                    return self.adapter.portfolio.get_contract_object_for_symbol(symbol)
                return None

            async def place_limit_order(self, contract, action, quantity, price, order_ref, order_id_to_use): # Async
                # Route to the adapter's place_limit_order which goes to portfolio
                # The order_id_to_use is the one from get_order_id from GridStrategy
                # The adapter's place_limit_order will generate its own internal_id
                print(f"MockApi: GridStrategy trying to place order: {action} {quantity} {contract.symbol} @ {price} (GridOrderID: {order_id_to_use})")
                # The internal_id returned by portfolio is what matters for tracking.
                # GridStrategy will map its own order_id to this internal_id.
                internal_oms_id = self.adapter.place_limit_order(side=action, price=price, quantity=quantity, order_ref=order_ref)
                if internal_oms_id:
                    # GridStrategy needs to know its original order_id succeeded.
                    # It also needs to map its order_id to the OMS internal_id for cancellations.
                    # This is tricky. A better way is for GridStrategy to directly use
                    # the adapter's place_limit_order and get back the OMS internal_id.
                    # For now, let's assume GridStrategy's own order_id is just for its internal logic
                    # and the OMS internal_id is the real tracker.
                    
                    # We need to return a mock Trade object if GridStrategy expects it
                    mock_order = Order(internal_id=internal_oms_id, strategy_id=self.adapter.strategy_id, # Using OMS ID
                                       symbol=contract.symbol, side=action, order_type="LMT",
                                       quantity=quantity, limit_price=price, order_ref=order_ref)
                    mock_order.broker_order_id = str(order_id_to_use) # Store GridStrategy's ID here
                    mock_order.status = OrderStatus.SUBMITTED 
                    # This mock Trade object is very simplified
                    return {'order': mock_order, 'orderStatus': mock_order.status, 'contract': contract, 'isDone': lambda: False} # Mocking a Trade-like dict
                return None
            
            def get_next_order_id(self): # Sync, called by GridStrategy
                # GridStrategy calls this via its get_order_id callback.
                # The callback should be self.adapter._get_sim_order_id_for_grid_logic
                return self.adapter._get_sim_order_id_for_grid_logic(self.adapter.strategy_id)

            def isConnected(self): return True # Always connected in backtest

            def register_order_status_update_handler(self, handler): pass # Not used by GridStrategy directly

        mock_api = MockApiForGridStrategy(self)

        self.grid_logic = GridStrategy(
            api=mock_api, # Pass the mock API that routes calls to self.portfolio
            symbol=self.symbol,
            start_price=self.params["start_price"], # GridStrategy needs start_price
            lower_bound=self.grid_params.lower_bound,
            upper_bound=self.grid_params.upper_bound,
            price_spacing=self.grid_params.price_spacing,
            shares_per_grid=self.grid_params.shares_per_grid,
            # fee_per_trade is handled by Portfolio
            strategy_id=self.strategy_id,
            primary_exchange=self.grid_params.primary_exchange,
            get_order_id=mock_api.get_next_order_id # Route get_order_id through mock_api
        )
        
        # GridStrategy.InitStrategy is async, but StrategyBase.initialize is sync.
        # This requires running the async init in the event loop.
        # The BacktestEngine should ideally handle this if it's async-aware.
        # For a sync backtester, this is a problem.
        # Assuming a sync backtester for now, GridStrategy.InitStrategy MUST be made sync
        # or we need a way to run its async parts.
        
        # If GridStrategy.InitStrategy was made synchronous (difficult if it uses await):
        # self.grid_logic.InitStrategy()
        
        # If BacktestEngine is async, it can do: await self.grid_logic.InitStrategyAsync(...)
        print(f"GridStrategyBacktest for {self.strategy_id}: Underlying GridStrategy instance created.")
        self.is_initialized = True # Mark that our adapter is ready

    async def initialize_async(self, current_market_ref_price: float, existing_pos_shares: float = 0, existing_pos_avg_cost: float = 0.0):
        """Async initialization for strategies that need it (like our GridStrategy)."""
        if not self.is_initialized : self.initialize() # Call sync part first if any

        if self.grid_logic:
            # We need to ensure the contract is set on grid_logic before its InitStrategy
            if not self.grid_logic.contract and self.portfolio:
                self.grid_logic.contract = self.portfolio.get_contract_object_for_symbol(self.symbol)

            if hasattr(self.grid_logic, "InitStrategyAsync"):
                await self.grid_logic.InitStrategyAsync(current_market_ref_price, existing_pos_shares, existing_pos_avg_cost)
            elif hasattr(self.grid_logic, "InitStrategy"): # Fallback if it's sync (problematic)
                 print("Warning: Calling synchronous GridStrategy.InitStrategy from async context. This might block.")
                 self.grid_logic.InitStrategy() # This might need start_price passed differently
            print(f"GridStrategyBacktest for {self.strategy_id}: Underlying GridStrategy initialized.")


    def on_bar_data(self, bar_data: pd.Series, dt: pd.Timestamp):
        if not self.is_initialized or not self.grid_logic:
            return

        self._set_current_data_and_time(bar_data, dt) # Update current bar in StrategyBase

        # The GridStrategy's maintain_active_grids_status is async.
        # This is an issue if on_bar_data is called from a sync loop.
        # For now, assuming BacktestEngine handles this.
        # If GridStrategy.maintain_active_grids_status must be called,
        # it needs to be adapted or called from an async context.
        # Let's assume for now that the primary driver is on_order_event for grid.
        
        # A more typical backtest would pass the bar to a method in self.grid_logic
        # e.g., self.grid_logic.process_new_bar(bar_data, dt)
        # which might then trigger maintain_active_grids_status internally if needed.
        # For now, let's assume order events are the main driver after init.
        # However, maintain_active_grids_status IS crucial.
        
        # If BacktestEngine is async:
        # asyncio.create_task(self.grid_logic.maintain_active_grids_status(bar_data['Close'], is_buy=False)) # is_buy for find_the_grid not ideal here
        
        # Simplified: for backtesting, let's assume maintain_active_grids is called after fills.
        # Or, we need a reference price. Let's use the close of the current bar.
        if hasattr(self.grid_logic, "maintain_active_grids_status") and callable(self.grid_logic.maintain_active_grids_status):
            # This needs to be callable from a sync context or the engine needs to be async.
            # For now, we'll assume it can be called, but an async backtester is better.
            # print(f"Debug: Calling maintain_active_grids_status in on_bar_data for {self.strategy_id} with price {bar_data['Close']}")
            # await self.grid_logic.maintain_active_grids_status(bar_data['Close']) # Problematic if this method is sync

            # Hack for sync call to async (NOT RECOMMENDED FOR PRODUCTION/COMPLEX BACKTESTERS)
            # This is only if on_bar_data is truly synchronous and needs to call an async method.
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running(): # If an outer loop is running (e.g. from main_backtester)
                    # This is still not ideal as on_bar_data might be called rapidly.
                    # Better for BacktestEngine to manage an async task queue.
                    # For now, let's assume maintain_active_grids_status is the strategy's internal decision trigger
                    # and it gets called correctly from on_order_event.
                    pass # Avoid direct call for now, rely on on_order_event to trigger maintenance.
            except RuntimeError: # No running loop
                 pass # Cannot call async from here without a running loop

    def on_order_event(self, order_event: Dict[str, Any]):
        if not self.is_initialized or not self.grid_logic:
            return

        print(f"GridStrategyBacktest ({self.strategy_id}): Received order event: {order_event}")
        
        # The GridStrategy.update_order_status expects ib_insync.Trade and OrderStatus.
        # We need to adapt the backtester's order_event (which is a dict) to these types
        # or change GridStrategy to accept a more generic event dict.
        # This is a key point of mismatch between live API objects and backtest simulation.

        # For now, let's assume GridStrategy.update_order_status can be adapted or
        # we pass enough info for it to work.
        # This requires GridStrategy's update_order_status to be async.
        
        # Mocking the Trade and OrderStatus objects for GridStrategy's update_order_status
        # This is highly dependent on what GridStrategy.update_order_status actually uses from these.
        if 'broker_order_id' in order_event and 'status' in order_event:
            mock_order = Order(orderId=order_event.get('broker_order_id_int', 0), # GridStrategy uses int IDs from its get_order_id
                               action=order_event.get('side', 'BUY'), 
                               lmtPrice=order_event.get('limit_price', 0.0),
                               totalQuantity=order_event.get('quantity',0.0))
            mock_order_status = OrderStatus(status=order_event['status'], 
                                            filled=order_event.get('filled_qty', 0.0),
                                            remaining=order_event.get('quantity', 0.0) - order_event.get('filled_qty', 0.0),
                                            avgFillPrice=order_event.get('avg_fill_price', 0.0))
            
            # The contract object is also needed by GridStrategy's update_order_status via trade.contract
            mock_contract = self.portfolio.get_contract_object_for_symbol(self.symbol) if self.portfolio else None
            
            if mock_contract:
                mock_trade = Trade(contract=mock_contract, order=mock_order, orderStatus=mock_order_status, fills=[], log=[])

                if hasattr(self.grid_logic, "update_order_status") and callable(self.grid_logic.update_order_status):
                    # Again, problem if update_order_status is async and this is sync.
                    # Assuming BacktestEngine handles async execution of strategy methods.
                    # For now, we'll just note this needs to be handled by an async engine.
                    print(f"  Passing to GridStrategy.update_order_status for order ID {mock_order.orderId}")
                    # await self.grid_logic.update_order_status(mock_trade, mock_order_status) # If engine is async
            else:
                print(f"  Could not create mock contract for order event: {order_event}")
        else:
            print(f"  Order event missing broker_order_id or status: {order_event}")


    def on_stop(self):
        print(f"Stopping GridStrategyBacktest for {self.strategy_id} ({self.symbol})")
        if self.grid_logic:
            if hasattr(self.grid_logic, "DoStopAsync") and callable(self.grid_logic.DoStopAsync):
                # await self.grid_logic.DoStopAsync() # If engine is async
                pass
            elif hasattr(self.grid_logic, "DoStop") and callable(self.grid_logic.DoStop):
                 self.grid_logic.DoStop()
        print(f"GridStrategyBacktest for {self.strategy_id} stopped.")