
import asyncio
import datetime
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from apis.api import BaseAPI
from strategy import common
from strategy.strategy import Strategy
from strategy.grid import GridStrategy

WATCHLIST_FILE = "watchlist_grid_config.json"  # For symbols and their daily params

class GridStrategyEngine:
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

    def _load_grid_strategies(self, params: List[Dict[Any, Any]]) -> bool:
        for param in params:
            symbol = param.get("symbol", "")
            unique_tag = param.get("unique_tag", 1) 
            base_price = param.get("base_price") 
            lower = param.get("lower") 
            upper = param.get("upper") 
            proportion = param.get("proportion", 0.015)
            cost_per_grid = param.get("cost_per_grid", 500)
            data_file = param.get('data_file')
            do_optimize = param.get("do_optimize", False)
            num_when_optimize = param.get('num_when_optimize', 1)
            
            strategy_id = f"GRID_{unique_tag}_{symbol}"
            grid = GridStrategy(self.api, strategy_id, symbol, 
                                    base_price, lower, upper, 
                                    cost_per_grid, proportion, 
                                    get_order_id=self.get_register_order_id_strategy_id,
                                    do_optimize=do_optimize, num_when_optimize=num_when_optimize,
                                    data_file=self.data_dir + "grid/" + data_file)
            self.strategy_params[strategy_id] = grid
            
            start_price = param.get("start_price")
            pos = 0
            for position in self.positions:
                # 只处理股票
                if position.contract.secType == "STK" and position.contract.symbol == grid.symbol:
                    pos = position.position
            grid.InitStrategy(start_price, pos, 20000)
        
        return True
    
    # --- Config and State Loading/Saving ---
    def _load_watchlist_and_calculate_params(self, names):
        strategy_config_map = {
            "grid": {
                "func": self._load_grid_strategies,
                "filename": "/grid_config.json"
            },
        }
        for name in names:
            strategy_config = strategy_config_map.get(name, None)
            if strategy_config:
                filename = strategy_config.get("filename", "")
                if filename:
                    self.read_json_file(self.data_dir + name + filename, strategy_config.get("func"))
                else:
                    filepath = self.data_dir + name
                    self.read_json_files_from_directory(filepath, strategy_config.get("func"))
            else:
                self._log(f"Error: Invalid Function for {name} when loading configure file.", level=1)


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

    def _log(self, content, level: int = 0):
        if level in [0, 1]:
            print(f"{datetime.datetime.now()} {content}")
        
        
    def InitStrategy(self): # Renamed and made async
        self._log(f"Strategy Engine Initialize starting", level=1)
        if not self.api.isConnected():
            print("Error: IB API not connected. Abort init!")
            return False
        
        self.is_running = True
        self.positions = self.api.get_current_positions()
        
        # 注册订单状态更新事件
        self.api.register_order_status_update_handler(self.handle_order_update_async)
        self.api.register_execution_fill_handler(self.handle_fill_async)
        self.api.register_disconnected_handler(self.handle_disconnect_event)
        self._log(f"Event Register Done.", level=1)

        # 从文件中载入策略配置
        strategy_names = ["grid"]
        self._load_watchlist_and_calculate_params(strategy_names) # Sync

        self._log(f"Load Strategy Configure Files Done.", level=1)
        # Initialize order ID counter
        initial_ib_order_id = self.api.get_next_order_id() # Assuming this is a sync call in your IBapi
        if initial_ib_order_id is None:
            self._log(f"Error: Could not get initial next valid order ID from IB API. Halting strategy init.", level=1)
            return False
        self.next_order_id_counter = initial_ib_order_id
        self._log(f"Initialize Order Id Done: {self.next_order_id_counter}.", level=1)

        self._log(f"Strategy Engine Initialize Done.", level=1)
        return True

    async def handle_disconnect_event(self):
        self.is_running = False
        for i in range(5):
            try:
                if self.api.isConnected():
                    self.api.disconnect()
                    
                await asyncio.sleep(3)
                
                await self.api.connect()
                if self.api.isConnected():
                    self.is_running = True
                    return
                
            except Exception as e:
                self._log(f"重连第{i}次失败：{e}")

    async def handle_order_update_async(self, trade: Any, order_status: Any):
        if not self.is_running: return
        order = common.convert_trade_to_gridorder(trade)
        # 根据order_id_strategy获取对应的策略
        strategy_id = self.order_id_strategy_id.get(order.order_id, "")
        if not strategy_id: return
        params = self.strategy_params[strategy_id]
        if not params:
            return 
        
        await params.update_order_status(order)


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
        self._log(f"Strategy Engine Stop Running...", level=1)
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
                
        self._log(f"All Strategies Stopped.", level=1)
        result = []
        for sid, item in self.strategy_result.items():
            result.append({"grid_type": item.get("grid_type", "classic"),
                           "strategy_id": sid.split("_")[2], 
                           "proportion": item.get("proportion", 0),
                           "net_profit": item.get("net_profit", 0)})
        # utils.show_figure(result)
        
        self._log(f"Strategy Engine Stop Running Done.", level=1)
        return self.strategy_result
        
    def read_json_file(self, filename: str, proc: Callable[[Dict[Any, Any]], bool]):
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                data = json.load(file)
                if proc(data):
                    self._log(f"成功载入策略配置文件: {filename}", level=1)
                
        except json.JSONDecodeError as e:
            error_msg = f"JSON解析错误 - {filename}: {str(e)}"
            self._log(f"Error: {error_msg}", level=1)
            
        except Exception as e:
            error_msg = f"文件读取错误 - {filename}: {str(e)}"
            self._log(f"Error: {error_msg}", level=1)
        
    def read_json_files_from_directory(self, directory_path: str, proc: Callable[[Dict[Any, Any]], bool]):
        """
        遍历指定目录下的所有JSON文件并读取内容
        
        参数:
        directory_path: 要遍历的目录路径
        
        返回:
        字典，键为文件名（不含扩展名），值为JSON内容
        """
        if not os.path.exists(directory_path):
            raise FileNotFoundError(f"目录不存在: {directory_path}")
        
        if not os.path.isdir(directory_path):
            raise NotADirectoryError(f"路径不是目录: {directory_path}")
        
        json_data = {}
        errors = []
        
        # 遍历目录中的所有文件
        for filename in os.listdir(directory_path):
            if filename.lower().endswith('.json'):
                file_path = os.path.join(directory_path, filename)
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        data = json.load(file)
                        if proc(data):
                            print(f"成功读取: {filename}")
                        
                except json.JSONDecodeError as e:
                    error_msg = f"JSON解析错误 - {filename}: {str(e)}"
                    errors.append(error_msg)
                    print(f"错误: {error_msg}")
                    
                except Exception as e:
                    error_msg = f"文件读取错误 - {filename}: {str(e)}"
                    errors.append(error_msg)
                    print(f"错误: {error_msg}")
        
        # 如果有错误，打印汇总
        if errors:
            print(f"\n读取过程中发生了 {len(errors)} 个错误:")
            for error in errors:
                print(f"  - {error}")
                

    def validate_json_structure(self, data: Dict[str, Any], filename: str = "") -> bool:
        """
        验证JSON文件是否符合预期的结构
        
        参数:
        data: JSON数据
        filename: 文件名（用于错误提示）
        
        返回:
        bool: 是否符合预期结构
        """
        try:
            # 检查必需的顶级键
            required_keys = ['config', 'profits', 'pending_orders']
            for key in required_keys:
                if key not in data:
                    print(f"警告 - {filename}: 缺少必需的键 '{key}'")
                    return False
            
            # 检查config结构
            config = data['config']
            config_required_keys = ['symbol', 'unique_tag', 'base_price', 'lower', 'upper', 'cost_per_grid', 'proportion']
            for key in config_required_keys:
                if key not in config:
                    print(f"警告 - {filename}: config中缺少必需的键 '{key}'")
                    return False
            
            # 检查profits是否为列表
            if not isinstance(data['profits'], list):
                print(f"警告 - {filename}: profits应该是列表类型")
                return False
            
            # 检查pending_orders是否为列表
            if not isinstance(data['pending_orders'], list):
                print(f"警告 - {filename}: pending_orders应该是列表类型")
                return False
            
            # 检查pending_orders中的订单结构
            order_required_keys = ['symbol', 'action', 'lmt_price', 'shares', 'done_price', 'done_shares', 'status']
            for i, order in enumerate(data['pending_orders']):
                if not isinstance(order, dict):
                    print(f"警告 - {filename}: pending_orders[{i}]应该是字典类型")
                    return False
                
                for key in order_required_keys:
                    if key not in order:
                        print(f"警告 - {filename}: pending_orders[{i}]中缺少必需的键 '{key}'")
                        return False
            
            return True
            
        except Exception as e:
            print(f"验证错误 - {filename}: {str(e)}")
            return False

    def read_and_validate_json_files(self, directory_path: str, validate: bool = True) -> Dict[str, Any]:
        """
        读取并验证JSON文件
        
        参数:
        directory_path: 目录路径
        validate: 是否验证文件结构
        
        返回:
        字典，包含所有成功读取且验证通过的JSON数据
        """
        json_data = self.read_json_files_from_directory(directory_path)
        
        if validate:
            print("\n开始验证文件结构...")
            valid_data = {}
            
            for filename, data in json_data.items():
                if self.validate_json_structure(data, filename):
                    valid_data[filename] = data
                    print(f"✓ {filename}: 结构验证通过")
                else:
                    print(f"✗ {filename}: 结构验证失败")
            
            print(f"\n验证结果: {len(valid_data)}/{len(json_data)} 个文件通过验证")
            return valid_data
        
        return json_data