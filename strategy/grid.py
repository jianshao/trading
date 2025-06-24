#!/usr/bin/python3
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple # Ensure Tuple is imported
from apis import api
from grid_new import GridStrategy

class GridStrategyParams:
    def __init__(self, strategy_id: str, symbol: str, lower_bound: float, upper_bound: float,
                 price_spacing: float, shares_per_grid: int, fee_per_trade: float):
        self.strategy_id = strategy_id # Unique ID for this parameter set
        self.symbol = symbol
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.price_spacing = price_spacing
        self.shares_per_grid = shares_per_grid
        # Assuming fee_per_trade is the fee for a single trade action (one buy or one sell)
        self.fee_per_trade = fee_per_trade

    def __str__(self):
        return (f"Params(ID={self.strategy_id}, Symbol={self.symbol}, Bounds=[{self.lower_bound:.2f}-{self.upper_bound:.2f}], "
                f"Spacing={self.price_spacing:.3f}, Shares={self.shares_per_grid}, Fee={self.fee_per_trade:.2f})")

def backtest_grid_strategy(data: pd.DataFrame, params: GridStrategy, initial_capital: float
                          ) -> Tuple[float, float, int, int, pd.DataFrame, GridStrategy, float]:
    """
    网格策略回测逻辑，增加了现金余额检查。
    Returns:
        total_realized_pnl (float): 总已实现盈亏
        unrealized_pnl (float): 期末未实现盈亏
        total_buy_trades (int): 总买入交易次数
        total_sell_trades (int): 总卖出交易次数
        trade_log_df (pd.DataFrame): 交易日志
        params (GridStrategy): 使用的策略参数
        final_cash (float): 期末现金余额
    """
    if data.empty or not all(col in data.columns for col in ['High', 'Low', 'Close', 'Open']):
        print(f"Warning for {params.strategy_id} ({params.symbol}): Data is empty or missing required columns (Open, High, Low, Close). Skipping backtest.")
        return 0.0, 0.0, 0, 0, pd.DataFrame(), params, initial_capital

    if params.lower_bound >= params.upper_bound or params.price_spacing <= 0 or params.shares_per_grid <= 0:
        print(f"Warning for {params.strategy_id} ({params.symbol}): Invalid parameters (bounds, spacing, or shares). Skipping backtest.")
        return 0.0, 0.0, 0, 0, pd.DataFrame(), params, initial_capital
        
    grid_lines = np.arange(params.lower_bound, params.upper_bound + params.price_spacing, params.price_spacing)
    # Ensure grid lines are within bounds and have at least two lines for a grid
    grid_lines = grid_lines[(grid_lines >= params.lower_bound) & (grid_lines <= params.upper_bound)]

    if not grid_lines.size > 1: # Need at least two lines to form a grid cell
        print(f"Warning for {params.strategy_id} ({params.symbol}): Not enough grid lines after adjustment. "
              f"Bounds: [{params.lower_bound:.2f}-{params.upper_bound:.2f}], Spacing: {params.price_spacing:.3f}. Skipping.")
        return 0.0, 0.0, 0, 0, pd.DataFrame(), params, initial_capital

    print(f"Backtesting with: {params}, Initial Capital: ${initial_capital:.2f}")

    position = 0
    cash = initial_capital
    total_realized_pnl = 0.0
    total_buy_trades = 0
    total_sell_trades = 0
    # key: buy_price_key (rounded for stability), value: {'shares': X, 'sell_target': Y, 'original_buy_price': Z}
    pending_sells: Dict[float, Dict] = {}
    trade_log = []

    for i in range(len(data)):
        current_bar_time = data.index[i]
        bar_high = data['High'].iloc[i]
        bar_low = data['Low'].iloc[i]
        current_market_price_for_decision = data['Close'].iloc[i-1] if i > 0 else data['Open'].iloc[i]

        # --- Check Sells ---
        sell_targets_processed_this_bar = []
        # Sort pending_sells by sell_target to process lower sell targets first if multiple can trigger
        sorted_pending_sells = sorted(pending_sells.items(), key=lambda item: item[1]['sell_target'])

        for buy_price_key, sell_info in sorted_pending_sells:
            sell_target_price = sell_info['sell_target']
            shares_to_sell = sell_info['shares']
            original_buy_price = sell_info['original_buy_price']

            if bar_high >= sell_target_price:
                if position >= shares_to_sell:
                    sell_price_actual = sell_target_price # Assume sell at target

                    cash_from_sell = shares_to_sell * sell_price_actual
                    cash += (cash_from_sell - 1.05)
                    position -= shares_to_sell
                    
                    # PNL for this specific grid completion (buy and sell)
                    realized_pnl_for_grid_trade = (sell_price_actual * shares_to_sell) - \
                                                  (original_buy_price * shares_to_sell) - \
                                                  (2.1) # Fee for buy + Fee for sell

                    total_realized_pnl += realized_pnl_for_grid_trade
                    total_sell_trades += 1
                    
                    trade_log.append({
                        'Timestamp': current_bar_time, 'Action': 'SELL', 
                        'Price': sell_price_actual, 'Shares': shares_to_sell,
                        'Associated_Buy_Price': original_buy_price,
                        'Realized_PNL_This_Grid_Trade': realized_pnl_for_grid_trade,
                        'Position_After': position, 'Cash_After': round(cash, 2), 
                        'Total_Realized_PNL': round(total_realized_pnl, 2)
                    })
                    sell_targets_processed_this_bar.append(buy_price_key)
                # else:
                #     print(f"DEBUG: {current_bar_time} Attempted to sell {shares_to_sell} from buy_price {original_buy_price} but position is {position}")


        for bp_key in sell_targets_processed_this_bar:
            if bp_key in pending_sells:
                del pending_sells[bp_key]

        # --- Check Buys ---
        # Grid lines to consider for buying: must be below current decision price and at/above lower_bound
        potential_buy_grids = sorted(
            [gl for gl in grid_lines if gl < current_market_price_for_decision and gl >= params.lower_bound], 
            reverse=True
        )

        for buy_target_price in potential_buy_grids:
            # Use a sufficiently precise key for pending_sells to avoid float precision issues
            # The precision should ideally match or exceed the precision of your price_spacing
            buy_price_key = round(buy_target_price, 8) # Increased precision for key

            # Check if this grid level already has a pending sell order (i.e., we bought at this level and are waiting to sell)
            is_buy_level_occupied = any(
                abs(buy_target_price - ps_info['original_buy_price']) < (params.price_spacing * 0.01) # Small tolerance
                for ps_info in pending_sells.values()
            )

            if bar_low <= buy_target_price and not is_buy_level_occupied:
                cost_of_buy = params.shares_per_grid * buy_target_price
                total_cost_with_fee = cost_of_buy + 1.05

                if cash >= total_cost_with_fee:
                    buy_price_actual = buy_target_price # Assume buy at target
                    
                    cash -= total_cost_with_fee
                    position += params.shares_per_grid
                    total_buy_trades += 1

                    sell_target_for_this_buy = round(buy_price_actual + params.price_spacing, 2) # Output price often 2 decimal
                    # Ensure sell target is not beyond the upper bound (or too close to it to be meaningful)
                    if sell_target_for_this_buy > params.upper_bound:
                         # This might happen if last buy grid + spacing > upper_bound.
                         # Decide policy: either cap at upper_bound or don't buy if sell target is out.
                         # For now, let's cap it, but this means the last grid might have a smaller profit margin.
                        sell_target_for_this_buy = params.upper_bound
                    
                    # Only place buy if the sell target is strictly greater than buy price (after rounding)
                    if sell_target_for_this_buy <= buy_price_actual:
                        # This scenario should be rare if price_spacing is reasonable
                        # print(f"Warning: Sell target {sell_target_for_this_buy} not > buy price {buy_price_actual}. Skipping buy.")
                        cash += total_cost_with_fee # Refund cash
                        position -= params.shares_per_grid # Revert position
                        total_buy_trades -= 1 # Revert trade count
                        continue # Try next grid line


                    pending_sells[buy_price_key] = {
                        'shares': params.shares_per_grid, 
                        'sell_target': sell_target_for_this_buy,
                        'original_buy_price': buy_price_actual 
                    }
                    
                    trade_log.append({
                        'Timestamp': current_bar_time, 'Action': 'BUY', 
                        'Price': buy_price_actual, 'Shares': params.shares_per_grid,
                        'Cost_This_Trade': round(cost_of_buy,2), 'Fee_This_Trade': 1.05,
                        'Sell_Target': sell_target_for_this_buy,
                        'Position_After': position, 'Cash_After': round(cash,2), 
                        'Total_Realized_PNL': round(total_realized_pnl,2)
                    })
                    break # One buy action per bar
                # else:
                #     print(f"DEBUG: {current_bar_time} Skipped BUY at {buy_target_price:.2f} due to insufficient cash. Needed: {total_cost_with_fee:.2f}, Have: {cash:.2f}")


    trade_log_df = pd.DataFrame(trade_log)
    if not trade_log_df.empty:
        trade_log_df.set_index('Timestamp', inplace=True)
    
    final_cash = cash
    unrealized_pnl = 0.0
    if position > 0 and not data.empty:
        last_close_price = data['Close'].iloc[-1]
        cost_of_open_positions = 0
        current_market_value_of_open_positions = 0 # Initialize

        for sell_info in pending_sells.values():
            cost_of_open_positions += sell_info['original_buy_price'] * sell_info['shares']
            # Market value for this specific pending sell based on last_close_price
            current_market_value_of_open_positions += last_close_price * sell_info['shares']
        
        # More accurate market value if shares per grid are consistent:
        # market_value_of_open_positions = position * last_close_price
        # However, the loop above is more precise if shares_per_grid could vary per open position (not the case here)
        # Sticking to the loop sum for current_market_value_of_open_positions as it sums up value of shares *in pending_sells*

        unrealized_pnl = current_market_value_of_open_positions - cost_of_open_positions
        # Deduct potential selling fees for unrealized PNL calculation if desired (more conservative)
        # unrealized_pnl -= len(pending_sells) * params.fee_per_trade


    return total_realized_pnl, unrealized_pnl, total_buy_trades, total_sell_trades, trade_log_df, params, final_cash
