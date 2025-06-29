
import asyncio
import datetime
import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd
from apis.api import BaseAPI
from strategy.engine import Engine
from strategy.grid import GridStrategy

WATCHLIST_FILE = "watchlist_grid_config.json"  # For symbols and their daily params

class GridStrategyEngine(Engine):
    def __init__(self, ib_api: BaseAPI, data_dir: str = "data"):
        self.strategy_name = "Grid Strategy"
        self.api = ib_api
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.watchlist_file = os.path.join(self.data_dir, WATCHLIST_FILE) # Centralized path

        self.strategy_params: Dict[str, GridStrategy] = {}
        
        # order_id -> strategy_id
        self.order_id_strategy_id: Dict[int, str] = {}
        
        # strategy profits
        self.strategy_result: Dict[str, Any] = {}
        
        self.is_running = False
        self.next_order_id_counter: Optional[int] = None
        self.trade_log_df = pd.DataFrame(columns=[
            "Timestamp", "StrategyID", "Symbol", "ActionType", 
            "Price1", "Price2", "Shares", "GrossProfit", "Fees", "NetProfit", 
            "OrderRef1", "OrderRef2"
        ])

    # --- Config and State Loading/Saving ---
    def _load_watchlist_and_calculate_params(self):
        # ... (Your existing _load_watchlist_and_calculate_params method)
        # Make sure it populates self.strategy_params with GridStrategyParams instances
        # And ensure GridStrategyParams includes primary_exchange, max_grids_up_config, max_grids_down_config
        try:
            with open(self.watchlist_file, 'r') as f: watchlist_data = json.load(f)
        except Exception as e: print(f"Error loading watchlist: {e}"); watchlist_data = []

        for item in watchlist_data:
            symbol = item.get("symbol")
            start_price = item.get("start_price") 
            unique_tag = item.get("unique_tag", symbol) 
            grid_price_types = item.get("grid_price_types", ["classic"])
            proportions = item.get("proportions", [1.0])
            cost_per_grids = item.get("cost_per_grids", [500])

            if not all([symbol, start_price is not None]): # adr_abs can be 0
                print(f"Skipping watchlist item {item.get('symbol', 'Unknown')} due to missing symbol or start_price.")
                continue
            unique_tag = 1
            for grid_price_type in grid_price_types:
                for proportion in proportions:
                    for cost_per_grid in cost_per_grids: # cost不影响成交频率
                        strategy_id = f"GRID_{unique_tag}_{symbol}"
                        params = GridStrategy(self.api, symbol, start_price, cost_per_grid, proportion, grid_price_type, strategy_id, get_order_id=self.get_register_order_id_strategy_id, data_dir=self.data_dir)
                        self.strategy_params[strategy_id] = params
                        unique_tag += 1


    def get_register_order_id_strategy_id(self, strategy_id: str) -> int:
      order_id = self._get_next_order_id_local()
      self.order_id_strategy_id[order_id] = strategy_id
      return order_id
      
    def _get_next_order_id_local(self) -> int:
        if self.next_order_id_counter is None:
            fetched_id = self.api.get_next_order_id() # This should be a sync call to IBapi
            if fetched_id is None: raise Exception("Failed to get initial order ID from IBapi.")
            self.next_order_id_counter = fetched_id
        
        current_id = self.next_order_id_counter
        self.next_order_id_counter += 1
        return current_id

    def InitStrategy(self): # Renamed and made async
        print(f"--- {self.strategy_name} Async Initialize starting ---")
        print(f"Engine Start At {datetime.datetime.now()}")
        if not self.api.isConnected():
            print("Error: IB API not connected. Abort init!")
            return False
        
        # 注册订单状态更新事件
        self.api.register_order_status_update_handler(self.handle_order_update_async)
        self.api.register_execution_fill_handler(self.handle_fill_async)

        # 从文件中载入策略配置
        self._load_watchlist_and_calculate_params() # Sync
        # self._load_active_grid_cycles() # Sync, loads LGC objects

        # Initialize order ID counter
        initial_ib_order_id = self.api.get_next_order_id() # Assuming this is a sync call in your IBapi
        if initial_ib_order_id is None:
            print("Error: Could not get initial next valid order ID from IB API. Halting strategy init.")
            return False
        self.next_order_id_counter = initial_ib_order_id
        print(f"Base OrderID for this session will start from: {self.next_order_id_counter}")

        self.is_running = True

        for strategy_id, params in self.strategy_params.items():
            # print(f"Initializing orders for strategy: {strategy_id} ({params.symbol})")
            asyncio.get_event_loop().run_until_complete(params.InitStrategy())

        # self._save_active_grid_cycles()
        print(f"--- {self.strategy_name} Async Initialize complete ---")
        return True


    async def handle_order_update_async(self, trade: Any, order_status: Any):
        if not self.is_running: return
        # 根据order_id_strategy获取对应的策略
        order_id = trade.order.orderId
        strategy_id = self.order_id_strategy_id.get(order_id, "")
        if not strategy_id: return
        params = self.strategy_params[strategy_id]
        if not params:
            return 
        await params.update_order_status(trade, order_status)


    async def handle_fill_async(self, trade: Any, fill: Any):
        # ... (Your existing handle_fill_async logic for detailed logging) ...
        pass

    def DoStop(self) -> Dict[str, Any]:
        """
        停止策略执行:
        1. 将未完成的订单 (active_grid_trades) 记录到文件中。
        2. (可选) 尝试取消所有在经纪商处的活动挂单 (这需要 self.api.cancel_order_by_id)。
        3. 统计今日所有订单情况，包括收益、手续费等等。
        """
        print("Stopping Grid Strategy Engine")
        print(f"Engine Stop At {datetime.datetime.now()}")
        self.is_running = False
        
        # --- 统计今日所有订单情况 ---
        # This requires the trade_log to be comprehensive or to query IB for today's trades.
        # The self.trade_log currently only has completed grid sell PNL.
        # if self.trade_log:
        #     log_df = pd.DataFrame(self.trade_log)
        #     total_net_profit = log_df['net_profit'].sum()
        #     total_gross_profit = log_df['gross_profit'].sum()
        #     total_fees_paid = log_df['fees'].sum()
        #     num_closed_grids = len(log_df)

        #     print("\n--- Strategy Run Summary ---")
        #     print(f"Total Closed Grid Trades: {num_closed_grids}")
        #     print(f"Total Gross Profit (from closed grids): ${total_gross_profit:.2f}")
        #     print(f"Total Fees Paid (from closed grids): ${total_fees_paid:.2f}")
        #     print(f"Total Net Profit (from closed grids): ${total_net_profit:.2f}")
        #     if num_closed_grids > 0:
        #         print(f"Average Net Profit per Closed Grid: ${total_net_profit/num_closed_grids:.2f}")
        # else:
        #     print("No grid trades were fully closed and logged during this session.")
        for strategy_id, params in self.strategy_params.items() or {}:
            self.strategy_result[strategy_id] = params.DoStop()
                
        result = []
        for sid, item in self.strategy_result.items():
            result.append({"grid_type": item.get("grid_type", "classic"),
                           "strategy_id": sid.split("_")[2], 
                           "proportion": item.get("proportion", 0),
                           "net_profit": item.get("net_profit", 0)})
        # utils.show_figure(result)
        
        return self.strategy_result

    # _read_watchlist_raw can remain sync
    def _read_watchlist_raw(self) -> List[Dict]:
        try:
            with open(self.watchlist_file, 'r') as f: return json.load(f)
        except Exception: return []