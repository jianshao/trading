#!/usr/bin/python3

import asyncio
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
import sys
import os

# 把项目根目录加入 sys.path, 假设 backtester_main.py 在项目的某个子目录中，
# 而 strategy 和 apis 文件夹在项目的根目录下
# 例如: project_root/backtester_main.py, project_root/strategy/, project_root/apis/
# 如果 backtester_main.py 就在项目根目录，则不需要下面的 sys.path.append
# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# 如果 backtester_main.py 在一个名为 "scripts"或 "main_scripts" 的文件夹中，则使用:
# project_root/scripts/backtester_main.py
if __name__ == '__main__': # Only adjust path if running as script directly
    # This assumes backtester_main.py is in a subdirectory (e.g., 'scripts')
    # and 'apis' and 'strategy' are in the parent directory of that subdirectory.
    # Adjust as per your actual project structure.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


from apis.ibkr import IBapi # 从 apis/api.py 导入
from strategy.grid import backtest_grid_strategy # 从 strategy/grid_strategy.py 导入
from strategy.grid_new import GridStrategy

# from config import IB_HOST, IB_PORT, CLIENT_ID # 从 config.py 导入 (如果需要)

async def run_parameterized_backtests_main(ib_api_instance: IBapi, stock_symbol: str,
                                           data_end_date_str: str, data_duration_str: str,
                                           param_combinations: list[GridStrategy], initial_capital: float,
                                           primary_exchange: str = "NASDAQ"):
    print(f"\n--- Starting Parameterized Backtests for {stock_symbol} ---")
    
    historical_data = await ib_api_instance.get_historical_data(
        stock_symbol, 
        end_date_time=data_end_date_str, 
        duration_str=data_duration_str, 
        bar_size_setting='10 min', # Consider making this a parameter too
    )
    
    if historical_data.empty:
        print(f"Failed to fetch historical data for {stock_symbol}. Aborting backtests for this symbol.")
        return []

    all_results = []
    for params in param_combinations:
        if params.symbol != stock_symbol:
            print(f"Skipping params for {params.symbol}, current backtest is for {stock_symbol}")
            continue

        # print(f"\nRunning backtest for: {params}") # This is printed inside backtest_grid_strategy
        
        # Ensure 'Open', 'High', 'Low', 'Close' columns exist
        required_cols = ['Open', 'High', 'Low', 'Close']
        if not all(col in historical_data.columns for col in required_cols):
            print(f"Error: Historical data for {stock_symbol} is missing one or more required columns: {required_cols}. Columns found: {historical_data.columns.tolist()}")
            continue

        realized_pnl, unrealized_pnl, buy_trades, sell_trades, log_df, used_params, final_cash = backtest_grid_strategy(
            historical_data.copy(), # Pass a copy to avoid modification issues if any
            params,
            initial_capital
        )
        
        all_results.append({
            'params': used_params,
            'realized_pnl': realized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'buy_trades': buy_trades,
            'sell_trades': sell_trades,
            'log_df': log_df,
            'final_cash': final_cash,
            'initial_capital': initial_capital # Store for reference
        })
        
        net_gain_loss = realized_pnl + unrealized_pnl
        print(f"Result for {used_params.strategy_id}: "
              f"Realized PNL=${realized_pnl:.2f}, "
              f"Unrealized PNL=${unrealized_pnl:.2f}, "
              f"Net Gain/Loss=${net_gain_loss:.2f}, "
              f"Buy Trades={buy_trades}, Sell Trades={sell_trades}, "
              f"Final Cash=${final_cash:.2f}")
        if sell_trades > 0 and realized_pnl != 0:
            print(f"Avg Realized PNL per Completed Grid (Sell Trade): ${realized_pnl/sell_trades:.2f}")
        print("-" * 30)

    print(f"\n--- All Parameterized Backtests for {stock_symbol} Completed ---")
    return all_results


async def main(api: IBapi):
    try:
        stock_to_test = "VZ"
        # Define data fetching period
        end_datetime = datetime.now() # For most recent data
        # end_datetime = datetime(2023, 11, 30, 23, 59, 59) # Example fixed end date for repeatable backtests
        ib_end_date_str = "" # IB format with timezone (e.g., EST/EDT or GMT)
        # Or leave ib_end_date_str = "" to fetch up to "now" (less repeatable for backtests)
        
        data_duration = '1 M' # Fetch 1 month of 1-min data leading up to end_datetime

        # Set fee to 0 for initial debugging of PNL logic
        # For realistic backtesting, use your actual fee per trade.
        # If fee_per_trade = 1.0 means $1 per buy and $1 per sell, then a round trip costs $2.
        # fee = 0 # DEBUG: Set to 0 to check core PNL logic
        fee = 1.05 # Example realistic fee

        # Define parameter combinations
        param_combos_wbd = [
            GridStrategy(strategy_id="VZ_Grid1", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.30, shares_per_grid=14, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid2", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=10, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid2", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=12, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid2", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=14, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid2", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=16, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid3", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=18, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid3", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.4, shares_per_grid=20, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid3", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.15, shares_per_grid=30, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid3", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.20, shares_per_grid=20, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid3", symbol="VZ", lower_bound=40.0, upper_bound=46.0, price_spacing=0.25, shares_per_grid=20, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid4", symbol="VZ", lower_bound=40.5, upper_bound=46.5, price_spacing=0.6, shares_per_grid=8, fee_per_trade=fee),
            GridStrategy(strategy_id="WBD_Grid4", symbol="VZ", lower_bound=40.5, upper_bound=46.5, price_spacing=0.8, shares_per_grid=5, fee_per_trade=fee),
        ]
        
        results_wbd = await run_parameterized_backtests_main(
            api, 
            stock_to_test, 
            ib_end_date_str, 
            data_duration, 
            param_combos_wbd, 
            initial_capital=5000.0, # Increased capital for more shares
            primary_exchange="NASDAQ" # WBD is on NASDAQ
        )

        # --- Process and Display Results ---
        if results_wbd:
            sorted_results = sorted(
                results_wbd,
                key=lambda x: x.get('realized_pnl', 0) + x.get('unrealized_pnl', 0), # Sort by (Realized + Unrealized PNL)
                reverse=True
            )
            print(f"\n--- Top Performing Parameters for {stock_to_test} (Sorted by Net Gain/Loss) ---")
            for i, res in enumerate(sorted_results[:3]): # Display top 3
                params_obj = res['params']
                realized_pnl = res['realized_pnl']
                unrealized_pnl_val = res['unrealized_pnl']
                buy_trades_count = res['buy_trades']
                sell_trades_count = res['sell_trades']
                final_cash_val = res['final_cash']
                initial_cap = res['initial_capital']
                log_df_res = res['log_df']

                net_gain_loss = realized_pnl + unrealized_pnl_val
                total_executed_trades = buy_trades_count + sell_trades_count

                print(f"\nRank {i+1}: Strategy ID: {params_obj.strategy_id}")
                print(f"  Params: {params_obj}")
                print(f"  Initial Capital: ${initial_cap:.2f}")
                print(f"  Final Cash: ${final_cash_val:.2f}")
                print(f"  Realized PNL: ${realized_pnl:.2f}")
                print(f"  Unrealized PNL: ${unrealized_pnl_val:.2f}")
                print(f"  Net Gain/Loss (Realized + Unrealized): ${net_gain_loss:.2f}")
                print(f"  Buy Trades: {buy_trades_count}, Sell Trades: {sell_trades_count}, Total Executed Trades: {total_executed_trades}")
                if sell_trades_count > 0 and realized_pnl != 0: # Avoid division by zero
                     print(f"  Avg Realized PNL per Completed Grid (Sell Trade): ${realized_pnl/sell_trades_count:.2f}")

                if not log_df_res.empty:
                    print("  --- Last 5 Trades for this param set ---")
                    cols_to_print_default = ['Action', 'Price', 'Shares', 'Total_Realized_PNL']
                    # Check if PNL-specific column exists from SELL log
                    if 'Realized_PNL_This_Grid_Trade' in log_df_res.columns:
                        cols_to_print_final = ['Action', 'Price', 'Shares', 'Realized_PNL_This_Grid_Trade', 'Total_Realized_PNL']
                    else: # Fallback for BUY logs or if PNL_This_Grid_Trade is not present
                        cols_to_print_final = [col for col in cols_to_print_default if col in log_df_res.columns]
                    
                    print(log_df_res[cols_to_print_final].tail(5))

                    # Plot PNL for the best performing strategy
                    if i == 0 and 'Total_Realized_PNL' in log_df_res.columns:
                        plt.figure(figsize=(12, 6))
                        # Plot PNL realized on each SELL trade
                        sell_trades_log = log_df_res[log_df_res['Action'] == 'SELL'].copy()
                        if not sell_trades_log.empty:
                            plt.plot(sell_trades_log.index, sell_trades_log['Total_Realized_PNL'], marker='.', linestyle='-', label=f'Cumulative Realized PNL ({params_obj.strategy_id})')
                            
                            # Optional: Plot equity curve (Cash + Market Value of Positions)
                            # This requires calculating market value at each step in trade_log_df
                            # For now, just plotting realized PNL.

                            plt.title(f'Grid Strategy Cumulative Realized PNL for {stock_to_test} - {params_obj.strategy_id}')
                            plt.xlabel('Timestamp')
                            plt.ylabel('Cumulative Realized PNL ($)')
                            plt.legend()
                            plt.grid(True)
                            plt.tight_layout() # Adjust layout to prevent labels from overlapping
                            plt.show()
                print("="*50)
        
    except ConnectionRefusedError:
        print("Connection to IB TWS/Gateway refused. Ensure it's running and API port is open.")
    except asyncio.TimeoutError:
        print("IB API request timed out. Check connection or reduce data request size.")
    except Exception as e:
        print(f"An error occurred in main execution: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'api' in locals() and api.isConnected(): # Check if api was initialized and connected
            print("Disconnecting from IB...")
            await api.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    # If on Windows and using ib_insync, you might need this for asyncio event loop issues
    # from ib_insync import util
    # util.patchAsyncio() 
    
    # It's good practice to ensure the event loop is handled correctly,
    # especially if other asyncio libraries are used or if running in certain environments.
    # asyncio.run(main()) is generally fine for straightforward scripts.
    if sys.platform == "win32" and sys.version_info >= (3, 8):
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        # Replace with your actual IB connection details if not using defaults
        api = IBapi(host="127.0.0.1", port=7497, client_id=10) # Changed client_id just in case
        if not api.connect():
            print("Could not connect to IB TWS/Gateway. Ensure it's running and API connections are enabled. Exiting.")
        else:
            asyncio.run(main(api))
    except KeyboardInterrupt:
        print("\nProgram interrupted by user.")
    except Exception as e: # Catch any other unhandled exceptions from asyncio.run itself
        print(f"Critical error during asyncio.run: {e}")