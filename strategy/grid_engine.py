#!/usr/bin/python3
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Dict, List, Any, Optional
from ib_insync import *
import numpy as np
import pandas as pd

if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi
from strategy.engine import Engine

# --- Constants ---
PENDING_ORDERS_FILE = "pending_grid_orders.json" # For persisting active grids
WATCHLIST_FILE = "watchlist_grid_config.json"  # For symbols and their daily params
ORDER_REF_PREFIX = "livegrid_"

class GridStrategyParams: # Simplified version, real params might be richer
    def __init__(self, symbol: str, lower_bound: float, upper_bound: float,
                 price_spacing: float, shares_per_grid: int, fee_per_trade: float,
                 strategy_id: str = ""):
        self.strategy_id = strategy_id if strategy_id else f"{symbol}_grid"
        self.symbol = symbol
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.price_spacing = price_spacing # This might be dynamically calculated
        self.shares_per_grid = shares_per_grid
        self.fee_per_trade = fee_per_trade

    def __str__(self):
        return (f"GridParams(ID={self.strategy_id}, Symbol={self.symbol}, Bounds=[{self.lower_bound:.2f}-{self.upper_bound:.2f}], "
                f"Spacing={self.price_spacing:.4f}, Shares={self.shares_per_grid}, Fee={self.fee_per_trade:.2f})")


class LiveGridTrade:
    """Represents an active grid: a buy order has filled, waiting for a sell."""
    def __init__(self, symbol: str, buy_order_id: int, buy_price: float, shares: int,
                 sell_target_price: float, buy_timestamp: float, strategy_id: str):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.buy_order_id = buy_order_id
        self.buy_price = buy_price
        self.shares = shares
        self.sell_target_price = sell_target_price
        self.buy_timestamp = buy_timestamp
        self.sell_order_id: Optional[int] = None
        self.sell_price: Optional[float] = None
        self.sell_timestamp: Optional[float] = None
        self.status = "BOUGHT_PENDING_SELL" # BOUGHT_PENDING_SELL, SELL_ORDER_PLACED, SOLD

    def to_dict(self):
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict):
        trade = cls(
            data['symbol'], data['buy_order_id'], data['buy_price'], data['shares'],
            data['sell_target_price'], data['buy_timestamp'], data.get('strategy_id', data['symbol'] + "_grid")
        )
        trade.sell_order_id = data.get('sell_order_id')
        trade.sell_price = data.get('sell_price')
        trade.sell_timestamp = data.get('sell_timestamp')
        trade.status = data.get('status', "BOUGHT_PENDING_SELL")
        return trade

class GridStrategyEngine(Engine):
    def __init__(self, ib_api: IBapi, data_dir: str = "data"): # Type hint IBapi once defined
        self.strategy_name = "Grid Strategy"
        self.api = ib_api
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True) # Ensure data directory exists

        self.pending_orders_file = os.path.join(self.data_dir, PENDING_ORDERS_FILE)
        self.watchlist_file = os.path.join(self.data_dir, WATCHLIST_FILE)

        # strategy_id -> GridStrategyParams
        self.strategy_params: Dict[str, GridStrategyParams] = {}
        # strategy_id -> list of LiveGridTrade (active buy filled, waiting for sell)
        self.active_grid_trades: Dict[str, List[LiveGridTrade]] = {}
        
        # order_id -> strategy_id (to map incoming order status to a strategy)
        self.order_id_to_strategy_map: Dict[int, str] = {}
        # order_id -> 'buy' or 'sell' (to know the type of order)
        self.order_id_to_type_map: Dict[int, str] = {}
        # order_id -> LiveGridTrade (for sell orders, to link back to the buy)
        self.sell_order_id_to_grid_trade_map: Dict[int, LiveGridTrade] = {}


        self.is_running = False
        self.next_order_id: Optional[int] = None
        self.contracts_cache: Dict[str, Any] = {} # Cache for IB Contract objects

        self.trade_log = [] # For logging PNL, etc.

    # 从文件中读取出配置参数，并计算出最新参数
    def _load_watchlist_and_calculate_params(self):
        """
        从 watchlist_file 读取关注的标的信息，并计算初始网格参数。
        预期 watchlist_file JSON 格式示例:
        [
            {
                "symbol": "WBD",
                "start_price": 9.50,
                "avg_daily_range_abs": 0.30, // 绝对值 ADR
                "shares_per_grid": 100,
                "fee_per_trade": 1.00,
                "max_grids_down": 5, // 从起始价向下挂几个买单网格
                "max_grids_up": 3    // 如果有持仓，从持仓成本向上挂几个卖单网格 (或用于设定上限)
            }
        ]
        """
        try:
            with open(self.watchlist_file, 'r') as f:
                watchlist_data = json.load(f)
        except FileNotFoundError:
            print(f"Warning: Watchlist file '{self.watchlist_file}' not found. No strategies will be configured.")
            watchlist_data = []
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON from watchlist file '{self.watchlist_file}'.")
            watchlist_data = []

        for item in watchlist_data:
            symbol = item.get("symbol")
            start_price = item.get("start_price")
            adr_abs = item.get("avg_daily_range_abs") # 日均绝对振幅
            # adr_abs = asyncio.get_event_loop().run_until_complete(self.api.get_ada(Contract(symbol=symbol)))
            shares = item.get("shares_per_grid", 10) # Default shares
            price_spacing = item.get("price_per_grid", 0.05)
            fee = item.get("fee_per_trade", 1.0)    # Default fee
            max_grids_down = item.get("max_grids_down", 5)
            unique_tag = item.get("unique_tag", "")

            if not all([symbol, start_price, adr_abs]):
                print(f"Skipping watchlist item due to missing data: {item}")
                continue

            # 根据起始价格、日均振幅计算出今日价格上下限以及网格价差（单格盈利1%）
            # 这是一个示例计算逻辑，你可以根据需求调整
            # 假设上下限基于 ADR 的一定倍数
            lower_bound = round(start_price - adr_abs, 2) # Example: ADR * 1.5 for total range
            upper_bound = round(start_price + adr_abs, 2)
            
            # 网格价差：使得单格盈利（不含手续费）为买入价的 TARGET_PROFIT_PER_GRID_PERCENT
            # price_spacing = round(start_price * TARGET_PROFIT_PER_GRID_PERCENT, 2)
            # 更稳健的方式是 price_spacing 基于一个固定的值或 ADR 的一部分，
            # 然后确保这个 spacing 对应的盈利能覆盖费用。
            # 假设 price_spacing 是 ADR 的 1/N，例如 1/5th of ADR
            # price_spacing = round(max(0.01, adr_abs / 5), 3) # Min spacing 0.01, or 1/5th of ADR
            # if price_spacing == 0: price_spacing = 0.01 # Ensure not zero

            # 使用指定唯一标识符，避免因时间变化导致历史未完成订单无法匹配到策略
            strategy_id = f"GRID_{unique_tag}_{symbol}" # Unique enough for a session
            params = GridStrategyParams(symbol, lower_bound, upper_bound, price_spacing, shares, fee, strategy_id)
            self.strategy_params[strategy_id] = params
            self.active_grid_trades[strategy_id] = [] # Initialize list for active trades
            print(f"Configured strategy: {params}")


    # 从文件中读取之前未成交的卖单
    def _load_pending_orders(self):
        """从 PENDING_ORDERS_FILE 加载未完成的历史挂单 (即已买入等待卖出的网格)"""
        try:
            with open(self.pending_orders_file, 'r') as f:
                pending_data = json.load(f)
            # 历史未成交的卖单直接挂卖单，这些历史订单等待卖出即可，不需要进入正常策略
            # TODO: 当策略删除后还残留有未完成的卖单时，第二天就会忽略这些订单，需要手动处理。
            for strategy_id, trades_data in pending_data.items():
                if strategy_id in self.strategy_params:
                    self.active_grid_trades[strategy_id] = [LiveGridTrade.from_dict(td) for td in trades_data]
                    print(f"Loaded {len(self.active_grid_trades[strategy_id])} pending grid trades for {strategy_id}")
        except FileNotFoundError:
            print(f"No pending orders file ('{self.pending_orders_file}') found. Starting fresh.")
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON from pending orders file '{self.pending_orders_file}'. Starting fresh.")


    def _save_pending_orders(self):
        """将当前所有 active_grid_trades 保存到文件"""
        data_to_save = {}
        # 只保存未完成的卖单
        for strategy_id, trades_list in self.active_grid_trades.items():
            data_to_save[strategy_id] = [ trade.to_dict() for trade in trades_list if trade.status=="SELL_ORDER_PLACED"]
        try:
            with open(self.pending_orders_file, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            # print(f"Saved pending grid trades to '{self.pending_orders_file}'")
        except Exception as e:
            print(f"Error saving pending orders: {e}")

    async def _get_contract(self, symbol: str, exchange: str = "SMART", primary_exchange: str = "NASDAQ") -> Optional[Any]:
        """获取并缓存 IB Contract 对象"""
        if symbol not in self.contracts_cache:
            # 你需要确保你的 IBapi 有 get_contract_details_obj 或类似的函数
            # primary_exchange 应该从 watchlist 或 params 中获取
            contract_obj = await self.api.get_contract_details(symbol, exchange=exchange, primary_exchange=primary_exchange)
            if not contract_obj:
                print(f"Error: Could not get contract details for {symbol} on {primary_exchange}")
                return None
            self.contracts_cache[symbol] = contract_obj
        return self.contracts_cache[symbol]
    
    # 1. 为已加载的 active_grid_trades (已买入，等待卖出) 挂卖单
    def _replace_unsold_orders(self, strategy_id: str, contract: Contract, params: GridStrategyParams):
        curr_price = next((item['start_price'] for item in self._read_watchlist_raw() if item['symbol'] == params.symbol), None) # Helper needed
        for trade in self.active_grid_trades.get(strategy_id, []):
            # 已经提交卖单但没成交的重新提交
            if trade.status == "BOUGHT_PENDING_SELL":
                # TODO: 历史卖单需要根据当前价格进行调整.
                if curr_price > trade.sell_target_price:
                    # 标的高开时以当前价为准
                    trade.sell_target_price = curr_price
                elif curr_price < trade.buy_price:
                    # 标的低开时目标是尽快平仓，卖价降低一些（要保证覆盖手续费）
                    trade.sell_target_price = (trade.buy_price + trade.sell_target_price) / 2.0

                print(f"Re-placing SELL for loaded active trade: {params.symbol} bought at {trade.buy_price}, target {trade.sell_target_price}")
                # TODO: Add logic to check if sell_target_price is still valid against current market
                sell_oid = asyncio.get_event_loop().run_until_complete(self._place_limit_order_internal(
                    contract, "PENDING_SELL", trade.sell_target_price, trade.shares, params,
                    linked_buy_trade=trade, # Pass the trade object
                    ref_suffix="PENDING_SELL"
                ))
                if sell_oid:
                    trade.sell_order_id = sell_oid
                    trade.status = "SELL_ORDER_PLACED"
                    # No need to add to sell_order_id_to_grid_trade_map here if done in _place_limit_order_internal

        
    def _get_next_order_id_local(self) -> int:
        if self.next_order_id is None:
            raise Exception("Next order ID not initialized from IB.")
        current_id = self.next_order_id
        self.next_order_id += 1
        return current_id

    # 根据策略参数提交对应订单，由action区分买单和卖单
    def place_orders_from_params(self, strategy_id: str, contract: Contract, action: str):
        params = self.strategy_params[strategy_id]
    
        # 2. 根据当前 watchlist 配置的起始价格和参数，挂初始的买单
        #  start_price 是开盘价，作为当前的参考
        # 初始买单和初始卖单都只挂一个，如果挂单成交再开启相邻的买卖单。这样在任一时刻只有少数挂单，减少资金占用。
        start_price_from_watchlist = next((item['start_price'] for item in self._read_watchlist_raw() if item['symbol'] == params.symbol), None) # Helper needed
        if start_price_from_watchlist is None: 
            start_price_from_watchlist = (params.lower_bound + params.upper_bound)/2.0 # Fallback

        max_grids_key = "max_grids_down" if action.upper() == "BUY" else "max_grids_up"
        max_grids = next((item.get(max_grids_key, 5) for item in self._read_watchlist_raw() if item['symbol'] == params.symbol), 5)
        # 只保留max_grids个网格，划分完网格后自然就是max_grids个。
        lower, higher = max(params.lower_bound, start_price_from_watchlist - max_grids * params.price_spacing), start_price_from_watchlist
        if action == "SELL":
            lower, higher = start_price_from_watchlist + params.price_spacing, min(params.upper_bound, start_price_from_watchlist + max_grids * params.price_spacing)
            
        grid_prices = np.arange(lower, higher, params.price_spacing)
        for buy_price in grid_prices:
            # 检查此价位是否已经有因为 active_grid_trades 而产生的卖单 (意味着已持有)
            # 或者已经有挂出的买单
            buy_price_rounded = round(buy_price, 4)
            is_level_occupied = any(
                round(agt.buy_price, 4) == buy_price_rounded or \
                (agt.sell_order_id and round(self.api.get_order_details(agt.sell_order_id).get('price', -1), 4) == round(buy_price_rounded + params.price_spacing, 4))
                for agt in self.active_grid_trades.get(strategy_id, [])
            )
            # Also check active buy orders
            is_buy_order_pending_at_level = any(
                round(buy_order_info['price'], 4) == buy_price_rounded
                for s_id, buy_order_info_list in self._get_active_buy_orders_by_strat().items()
                if s_id == strategy_id for buy_order_info in buy_order_info_list # Hypothetical getter
            )

            if not is_level_occupied and not is_buy_order_pending_at_level:
                try:
                    asyncio.get_event_loop().run_until_complete(
                        self._place_limit_order_internal(contract, action.upper(), buy_price, params.shares_per_grid, params, ref_suffix="INIT_" + action)
                    )
                except RuntimeError as e_runtime:
                    print(f"RuntimeError placing initial {action} for {params.symbol} (likely loop issue): {e_runtime}")
                    break 
                except Exception as e_place:
                    print(f"Exception placing initial {action} for {params.symbol}: {e_place}")
        return

    # 策略初始化需要是同步的，只有初始化完成后才能继续
    def InitStrategy(self):
        print(f"-------------------------- {self.strategy_name} Initialize starting --------------------------")
        if not self.api.isConnected():
            print("Error: IB API not connected. Abort!")
            return
        
        # 1.首先注册事件，订单状态变化之类的
        self.api.register_order_status_update_handler(self.handle_order_completed)
        
        # 2.根据最新历史数据计算出各个参数
        # 3.根据参数组装挂单列表，都是买单
        self._load_watchlist_and_calculate_params()
        
        # 4.从持久化文件中读取出未完成双边交易的订单，通常是未成交的卖单
        self._load_pending_orders()
        
        # 5.将新生成的买单和从文件中读出的卖单执行挂单操作
        # 先拉取最新的order_id，保存到本地
        self.next_order_id = self.api.get_next_order_id()
        if self.next_order_id is None:
            print("Error: Could not get next valid order ID. Halting strategy.")
            return

        self.is_running = True
        # print(f"Placing initial orders from: {self.next_order_id}")

        # 针对每个网格子策略单独处理，每个标的的不同参数就是一个子策略
        for strategy_id, params in self.strategy_params.items():
            # 异步函数的同步调用方法
            contract = asyncio.get_event_loop().run_until_complete(self._get_contract(params.symbol))
            if not contract:
                continue

            # 1.之前已买入但未卖出的订单重新提交，要先于正常订单提交
            self._replace_unsold_orders(strategy_id, contract, params)
            
            # --- 挂初始买单和卖单 (双向网格部分) ---
            self.place_orders_from_params(strategy_id, contract, "BUY")
            self.place_orders_from_params(strategy_id, contract, "SELL")
            
        # 6.完成退出
        # self._save_pending_orders() # Save state after initial placements
        print(f"-------------------------- {self.strategy_name} Initialize over --------------------------")

    # 这里只做订单提交，不执行业务逻辑
    async def _place_limit_order_internal(self, contract: Any, action: str, price: float,
                                          shares: int, params: GridStrategyParams,
                                          linked_buy_trade: Optional[LiveGridTrade] = None,
                                          ref_suffix: str = "") -> Optional[int]:
        # ... (获取 order_id 和 order_ref 的逻辑) ...
        # Modify order_ref based on flags for clarity in TWS logs
        order_id = self._get_next_order_id_local()
        # ref_suffix选项: PENDING_SELL、INIT_SELL、INIT_BUY、SELL、BUY
        ref_suffix = action.upper() if ref_suffix == "" else ref_suffix
        order_ref = f"{ORDER_REF_PREFIX}{params.strategy_id}_{ref_suffix}_{order_id}"
        price = round(price, 4)
        
        # 从文件载入的订单不再新提交
        # if action.upper() == "PENDING_SELL":
        #     return order_id

        # Get the ib_insync.Trade object returned by IBapi
        api_placed_trade_obj = await self.api.place_limit_order(
            contract, action, float(shares), price, 
            order_ref=order_ref, order_id_to_use=order_id # Pass order_id here
        )

        if api_placed_trade_obj and api_placed_trade_obj.order.orderId == order_id: # Check if orderId matches
            self.order_id_to_strategy_map[order_id] = params.strategy_id
            self.order_id_to_type_map[order_id] = action.lower()
            
            # 之前未完成的卖单，来源是文件
            if action.upper() == "PENDING_SELL":
                if linked_buy_trade: # This is a sell closing a prior buy
                    self.sell_order_id_to_grid_trade_map[order_id] = linked_buy_trade
                    linked_buy_trade.sell_order_id = order_id
                    linked_buy_trade.status = "SELL_ORDER_PLACED"
            
            # For BUYs, LiveGridTrade is created upon fill.
            # If is_initial_sell_closing_buy, this BUY is special.
            # We might need to link it back to the original initial SELL's orderId
            # if we want to calculate PNL for that sell-first grid.
            # This requires more advanced state tracking. For now, it's just a BUY.
            # if is_initial_sell_closing_buy:
            #     print(f"  BUY order {order_id} (to close initial sell) placed for {params.symbol} @ {price}")

            return order_id
        else:
            print(f"Failed to place {action} order (intended ID: {order_id}) for {params.symbol} via API. API returned: {api_placed_trade_obj}")
            return None


    async def handle_order_completed(self, trade: Trade, order_status: OrderStatus):
        """
        处理来自 IBapi 的订单完成通知 (Filled, Cancelled, etc.)
        order_obj: IB's Order object
        order_state_obj: IB's OrderState object (status, filled, remaining, etc.)
        """
        if not self.is_running: return

        order_id = trade.order.orderId
        strategy_id = self.order_id_to_strategy_map.get(trade.order.orderId)
        if not strategy_id:
            # print(f"Order ID {order_id} not managed by this engine instance or already processed.")
            return

        params = self.strategy_params.get(strategy_id)
        if not params:
            print(f"Error: No params found for strategy_id {strategy_id} from order {trade.order.orderId}")
            return

        order_type = self.order_id_to_type_map.get(trade.order.orderId)
        status = order_status.status
        filled_shares = float(order_status.filled) # Should be int, but API might give float
        avg_fill_price = float(order_status.avgFillPrice)
        # commission = float(order_state_obj.commission) # Might be on commissionReport

        # print(f"Order Update for {strategy_id} (Order ID: {trade.order.orderId}, Type: {order_type}): Status={status}, Filled={filled_shares}, AvgPrice={avg_fill_price}")

        if status == "Filled":
            if order_type == "buy":
                # --- BUY Order Filled ---
                if filled_shares > 0:
                    # Create a new active grid trade
                    sell_target = round(avg_fill_price + params.price_spacing, 4)
                    # 卖出价超过上限也要用卖出价
                    # if sell_target > params.upper_bound: sell_target = params.upper_bound

                    if sell_target <= avg_fill_price:
                        print(f"Error for {strategy_id} buy order {trade.order.orderId}: Sell target {sell_target} is not > buy price {avg_fill_price}. Something is wrong.")
                        # This buy should not have happened or params are bad. How to handle?
                        # Maybe try to sell immediately at a small loss or same price if possible? For now, log and investigate.
                    else:
                        # 生成新卖单对应的买单
                        active_trade = LiveGridTrade(
                            params.symbol, trade.order.orderId, avg_fill_price, int(filled_shares),
                            sell_target, time.time(), strategy_id
                        )
                        # Ensure the list for this strategy_id exists
                        if strategy_id not in self.active_grid_trades:
                            self.active_grid_trades[strategy_id] = []
                        self.active_grid_trades[strategy_id].append(active_trade)
                        print(f"BUY filled for {strategy_id}: {filled_shares} @{avg_fill_price}. Pending sell at {sell_target}")

                        # Place corresponding sell order
                        await self._place_limit_order_internal(
                            self.contracts_cache[params.symbol], "SELL", sell_target, int(filled_shares), params,
                            linked_buy_trade=active_trade
                        )
                # Clean up maps for this buy order
                self._cleanup_order_maps(trade.order.orderId)


            elif order_type == "sell":
                # --- SELL Order Filled ---
                grid_trade_obj = self.sell_order_id_to_grid_trade_map.get(order_id)
                if grid_trade_obj: 
                    # Case 1: This SELL was linked to a previous BUY (closing a buy-first grid)
                    if grid_trade_obj.status == "SELL_ORDER_PLACED" and filled_shares > 0:
                        grid_trade_obj.sell_price = avg_fill_price
                        grid_trade_obj.sell_timestamp = time.time()
                        grid_trade_obj.status = "SOLD"
                        
                        profit = (grid_trade_obj.sell_price * grid_trade_obj.shares) - \
                                 (grid_trade_obj.buy_price * grid_trade_obj.shares)
                        net_profit = profit - (params.fee_per_trade * 2) 

                        self.trade_log.append({
                            "timestamp": grid_trade_obj.sell_timestamp, "strategy_id": strategy_id, 
                            "symbol": params.symbol, "action": "SELL_GRID_CLOSE",
                            "buy_price": grid_trade_obj.buy_price, "sell_price": grid_trade_obj.sell_price,
                            "shares": grid_trade_obj.shares, "gross_profit": profit,
                            "fees": params.fee_per_trade * 2, "net_profit": net_profit,
                            "order_ref_buy": self.api.ib.tradeLog(grid_trade_obj.buy_order_id)[0].order.orderRef if self.api.ib.tradeLog(grid_trade_obj.buy_order_id) else "N/A", # Example, might need better way to get ref
                            "order_ref_sell": trade.order.orderRef
                        })
                        print(f"SELL (closing buy) filled for {strategy_id}: {grid_trade_obj.shares} @{avg_fill_price:.4f}. "
                              f"Original buy @ {grid_trade_obj.buy_price:.4f}. Net PNL: {net_profit:.2f}. OrderRef: {trade.order.orderRef}")

                        if strategy_id in self.active_grid_trades and grid_trade_obj in self.active_grid_trades[strategy_id]:
                            self.active_grid_trades[strategy_id].remove(grid_trade_obj)
                        
                        # Place new buy at the original buy level of the completed grid
                        print(f"  Re-placing BUY grid at {grid_trade_obj.buy_price:.4f} for {strategy_id}")
                        await self._place_limit_order_internal(
                             self.contracts_cache[params.symbol], "BUY", grid_trade_obj.buy_price, 
                             params.shares_per_grid, params # Use original shares_per_grid for new buy
                        )

                else: 
                    # Case 2: This SELL was an initial sell (starting a sell-first grid)
                    # It won't be in sell_order_id_to_grid_trade_map because it had no linked_buy_trade
                    if filled_shares > 0:
                        print(f"INITIAL SELL filled for {strategy_id}: {filled_shares} @{avg_fill_price:.4f}. OrderRef: {trade.order.orderRef}")
                        # Now, place a corresponding buy order one grid below to complete this "sell-first" grid
                        buy_target_for_this_sell = round(avg_fill_price - params.price_spacing, 4)
                        
                        # Ensure buy target is within bounds
                        if buy_target_for_this_sell < params.lower_bound:
                            # print(f"  Adjusting buy target from {buy_target_for_this_sell} to lower_bound {params.lower_bound}")
                            buy_target_for_this_sell = params.lower_bound 
                        
                        if buy_target_for_this_sell >= avg_fill_price:
                            print(f"Error for {strategy_id} initial sell order {order_id}: Buy target {buy_target_for_this_sell:.4f} is not < sell price {avg_fill_price:.4f}. Check price_spacing.")
                        else:
                            print(f"  Placing corresponding BUY for initial sell: target {buy_target_for_this_sell:.4f} for {strategy_id}")
                            # This buy order doesn't have a `linked_buy_trade` in the sense of closing a `LiveGridTrade`
                            # It's an opening buy for a "sell-first" grid.
                            # The PNL for this sell-first grid will be realized when *this new buy* fills.
                            # How to track this?
                            # Option A: Create a new type of object or flag in LiveGridTrade for "sell-first" grids.
                            # Option B (Simpler for now): Just place the buy. PNL tracking for sell-first needs more thought.
                            await self._place_limit_order_internal(
                                self.contracts_cache[params.symbol], "BUY", 
                                buy_target_for_this_sell, 
                                int(filled_shares), # Buy back the same amount that was initially sold
                                params
                            )
                
                # Cleanup for the sell order that just filled
                self._cleanup_order_maps(order_id)
                if order_id in self.sell_order_id_to_grid_trade_map: # Only if it was a linked sell
                    del self.sell_order_id_to_grid_trade_map[order_id]

            # After any fill, save pending orders state
            self._save_pending_orders()

        elif status in ["Cancelled", "ApiCancelled", "Inactive", "PendingCancel", "ApiPending", "Submitted"]: # Submitted might still change
            if status in ["Cancelled", "ApiCancelled", "Inactive"]: # Terminal states for non-filled
                print(f"Order {order_id} (Type: {order_type}) for {strategy_id} ended with status: {status}")
                if order_type == "sell":
                    grid_trade_obj = self.sell_order_id_to_grid_trade_map.get(order_id)
                    if grid_trade_obj:
                        grid_trade_obj.sell_order_id = None # Clear sell order ID
                        grid_trade_obj.status = "BOUGHT_PENDING_SELL" # Revert status
                        # Optionally, try to re-place the sell order, maybe after a delay or price check
                        print(f"Sell order {order_id} for {strategy_id} was {status}. Reverted grid trade status. Consider re-placing sell.")
                
                self._cleanup_order_maps(order_id)
                if order_id in self.sell_order_id_to_grid_trade_map: # Clean up specific map for sells
                    del self.sell_order_id_to_grid_trade_map[order_id]
                self._save_pending_orders() # Save state as an active grid might be open again
        else:
            # print(f"Unhandled order status for {order_id}: {status}")
            pass


    def _cleanup_order_maps(self, order_id: int):
        if order_id in self.order_id_to_strategy_map:
            del self.order_id_to_strategy_map[order_id]
        if order_id in self.order_id_to_type_map:
            del self.order_id_to_type_map[order_id]

    def DoStop(self):
        """
        停止策略执行:
        1. 将未完成的订单 (active_grid_trades) 记录到文件中。
        2. (可选) 尝试取消所有在经纪商处的活动挂单 (这需要 self.api.cancel_order_by_id)。
        3. 统计今日所有订单情况，包括收益、手续费等等。
        """
        print("Stopping Grid Strategy Engine")
        self.is_running = False
        self._save_pending_orders() # Save current state of active grids
        
        # --- 统计今日所有订单情况 ---
        # This requires the trade_log to be comprehensive or to query IB for today's trades.
        # The self.trade_log currently only has completed grid sell PNL.
        if self.trade_log:
            log_df = pd.DataFrame(self.trade_log)
            total_net_profit = log_df['net_profit'].sum()
            total_gross_profit = log_df['gross_profit'].sum()
            total_fees_paid = log_df['fees'].sum()
            num_closed_grids = len(log_df)

            print("\n--- Strategy Run Summary ---")
            print(f"Total Closed Grid Trades: {num_closed_grids}")
            print(f"Total Gross Profit (from closed grids): ${total_gross_profit:.2f}")
            print(f"Total Fees Paid (from closed grids): ${total_fees_paid:.2f}")
            print(f"Total Net Profit (from closed grids): ${total_net_profit:.2f}")
            if num_closed_grids > 0:
                print(f"Average Net Profit per Closed Grid: ${total_net_profit/num_closed_grids:.2f}")
        else:
            print("No grid trades were fully closed and logged during this session.")

        # For unrealized PNL, you would iterate through self.active_grid_trades
        # and calculate based on current market prices (if available) vs buy_price.
        print(f"Grid Strategy stopped. {len(self.active_grid_trades)} grid(s) might still be active (bought, pending sell).")

    # --- Helper methods for watchlist and active buy orders (used in start) ---
    def _read_watchlist_raw(self) -> List[Dict]:
        """Helper to read raw watchlist data, for use within other methods if needed."""
        try:
            with open(self.watchlist_file, 'r') as f:
                return json.load(f)
        except: return []

    def _get_active_buy_orders_by_strat(self) -> Dict[str, List[Dict]]:
        """Helper to quickly get active buy orders grouped by strategy_id (simplified)."""
        # This is a placeholder. A real implementation would query self.api or maintain a more detailed
        # internal state of buy orders that are *actually* live on the exchange.
        # The current self.order_id_to_type_map and self.order_id_to_strategy_map
        # track orders *sent* by the engine. Their actual status (live, filled, cancelled)
        # depends on IB callbacks.
        
        # For initial placement, we are more interested in *which grid levels are intended for buying*.
        # The `is_buy_order_pending_at_level` check should ideally look at `self.order_id_to_type_map`
        # for orders that are 'buy' and not yet filled/cancelled for that price level.
        # This is complex because order details (like price) are not directly in these maps.
        # A better approach is to use self.grid_level_status from the LiveGridEngine example.
        # Let's assume for now that we are checking against what we *intend* to have as buy orders.
        
        # This method as a direct query is hard without storing more buy order details.
        # The logic in `start()` iterates and checks `active_buys_for_strat` which is a counter.
        # A more robust `is_buy_order_pending_at_level` would iterate through `self.order_id_to_strategy_map`,
        # get the order_id, check `self.order_id_to_type_map`, and if it's a buy,
        # then fetch the actual order details (e.g., from a dict `order_id_to_details_map`
        # populated when order is placed) to get its price.
        # print("Warning: _get_active_buy_orders_by_strat is a simplified placeholder.")
        return {}
