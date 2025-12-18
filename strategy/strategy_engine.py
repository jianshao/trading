
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import traceback
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
# from zoneinfo import ZoneInfo

import aiofiles
import clickhouse_connect
import pandas as pd
from apis.api import BaseAPI
from strategy import common
from strategy.order_manager import OrderManager
from strategy.real_time_data_processer import RealTimeDataProcessor
from strategy.strategy import Strategy
from strategy.grid import GridStrategy
from utils import mail
from utils.kafka_producer import KafkaProducerService
from utils.logger_manager import LoggerManager
import data.config as config


class GridStrategyEngine:
    def __init__(self, ib_api: BaseAPI, data_dir: str = "data", producer: Optional[KafkaProducerService] = None):
        self.strategy_name = "Grid Strategy"
        self.api = ib_api
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.strategy_params: Dict[str, Strategy] = {}
        
        # order_id -> strategy_id
        self.order_id_strategy_id: Dict[int, str] = {}
        
        # strategy profits
        self.strategy_result: Dict[str, Any] = {}
        
        self.is_running = False
        self.next_order_id_counter: Optional[int] = None

        self.start_time = datetime.now(ZoneInfo(config.time_zone))
        
        self.producer: Optional[KafkaProducerService] = producer
        self.om = OrderManager(self.api, producer)
        self.real_data_processor = RealTimeDataProcessor(self.api)
        
        log_configs = {
            "order": "logs/order.log",
            "app": "logs/app.log"
        }
        LoggerManager.init(log_configs, level=logging.ERROR, realtime_data_processor=self.real_data_processor)


    async def _load_grid_strategies(self, params: List[Dict[Any, Any]]) -> bool:
        for param in params:
            defaults = {
                "symbol": "QQQ",
                "unique_tag": 1,
                "cycle_days": 14,
                "count_of_atr_for_price_range": 5,
                "atr_coefficient_of_grid_spread": 0.5,
                "retention_fund_ratio": 0.2,
                "skip_bear": True,
                "total_cost": 13000,
                "max_position_pct": 0.8
            }

            defaults.update(param)
            defaults["data_file"] = f"{self.data_dir}grid/{defaults['symbol']}_{defaults['unique_tag']}.json"
            strategy_id = f"GRID_{defaults['unique_tag']}_{defaults['symbol']}"

            grid = GridStrategy(
                self.om,
                strategy_id,
                producer=self.producer,
                real_data_processor=self.real_data_processor,
                **defaults,  # ✅ 把所有参数直接透传
            )
            self.strategy_params[strategy_id] = grid

            await grid.InitStrategy()
        
        return True
    
    # --- Config and State Loading/Saving ---
    async def _load_watchlist_and_calculate_params(self, names):
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
                    await self.read_json_file(self.data_dir + name + filename, strategy_config.get("func"))
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
        if level in [1]:
            print(f'{datetime.now(ZoneInfo(config.time_zone)).strftime("%Y-%m-%d %H:%M:%S")} {content}')
        
        
    async def InitStrategy(self): # Renamed and made async
        self._log(f"Strategy Engine Initialize starting", level=1)
        print(f"Strategy Engine Initialize starting")
        if not self.api.isConnected():
            print("Error: IB API not connected. Abort init!")
            return False
        
        # self.positions = await self.api.get_current_positions()
        
        # 注册订单状态更新事件
        # self.api.register_order_status_update_handler(self.handle_order_update_async)
        # self.api.register_execution_fill_handler(self.handle_fill_async)
        # self.api.register_disconnected_handler(self.handle_disconnect_event)
        self._log(f"Event Register Done.", level=1)
        
        # 从文件中载入策略配置
        strategy_names = ["grid"]
        await self._load_watchlist_and_calculate_params(strategy_names) # Sync

        self._log(f"Load Strategy Configure Files Done.", level=1)
        # Initialize order ID counter
        initial_ib_order_id = self.api.get_next_order_id() # Assuming this is a sync call in your IBapi
        if initial_ib_order_id is None:
            self._log(f"Error: Could not get initial next valid order ID from IB API. Halting strategy init.", level=1)
            return False
        self.next_order_id_counter = initial_ib_order_id
        self._log(f"Strategy Engine Initialize Completed. Initialize Order Id: {self.next_order_id_counter}.", level=0)

        self.start_time = datetime.now(ZoneInfo(config.time_zone))
        return True

    async def handle_disconnect_event(self):
        self._log(f"IB Disconnected Event Triggered. Attempting Reconnect...", level=1)
        # 清理状态
        if self.api.isConnected():
            self.api.disconnect()

        for i in range(5):
            try:
                if self.api.isConnected():
                    return
                await asyncio.sleep(3)
                await self.api.connect()
                
            except Exception as e:
                self._log(f"重连第{i}次失败：{e}")
        self.is_running = False
            
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

    async def run(self):
        self.is_running = True
        self._log(f"Strategy Engine Run Loop Started.", level=1)
        while self.api.isConnected() and self.is_running:
            # 增加策略管理动作，如启动、停止策略等
            await asyncio.sleep(1)
        self._log(f"Strategy Engine Run Loop Exited.", level=1)
        
    async def DoStop(self):
        """
        停止策略执行:
        1. 将未完成的订单 (active_grid_trades) 记录到文件中。
        2. (可选) 尝试取消所有在经纪商处的活动挂单 (这需要 self.api.cancel_order_by_id)。
        3. 统计今日所有订单情况，包括收益、手续费等等。
        """
        self._log(f"Strategy Engine Stop Running...", level=1)
        self.is_running = False
        
        try:
            today = datetime.now(ZoneInfo(config.time_zone))
            if today > self.start_time + timedelta(minutes=10):
                profits_summary = []
                for strategy_id, params in self.strategy_params.items() or {}:
                    summary = params.DailySummary(today.strftime("%Y%m%d"))
                    if summary:
                        profits_summary.append(summary)
                        if self.producer:
                            data = {
                                "strategy_id": strategy_id,
                                "symbol": summary.symbol,
                                "date": today.strftime("%Y-%m-%d"),
                                "start_time": summary.start_time,
                                "end_time": summary.end_time,
                                "profits": summary.profits,
                                "details": json.dumps(summary.params)
                            }
                            await self.producer.send_message("daily_profit_logs", data)
                # 发送日报邮件
                mail.send_email(f"[每日收益报告] {today.strftime('%Y%m%d')}", common.generate_html(profits_summary))
            else:
                mail.send_email(f"[出错了] {today.strftime('%Y%m%d')}", "策略启动失败，需要人工处理。")
            
            for strategy_id, params in self.strategy_params.items() or {}:
                self.strategy_result[strategy_id] = await params.DoStop()
        except Exception as e:
            LoggerManager.Error("app", event="stop", content=f"stop strategy engine error: {e}")
            
        self._log(f"All Strategies Stopped.", level=1)
        self._log(f"Strategy Engine Stop Running Done.", level=1)
        return 
        
    async def read_json_file(self, filename: str, proc: Callable[[Dict[Any, Any]], bool]):
        try:
            async with aiofiles.open(filename, 'r', encoding='utf-8') as file:
                content = await file.read()
                data = json.loads(content)
                if await proc(data):
                    self._log(f"成功载入策略配置文件: {filename}", level=1)
                
        except json.JSONDecodeError as e:
            error_msg = f"JSON解析错误 - {filename}: {str(e)}"
            self._log(f"Error: {error_msg}", level=1)
            
        except Exception as e:
            error_msg = f"文件读取错误 - {filename}: {str(e)}"
            self._log(f"Error: {error_msg}", level=1)
            traceback.print_exc()
        
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
