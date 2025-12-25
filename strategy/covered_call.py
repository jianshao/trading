import asyncio
from ib_insync import *
import pandas as pd
from datetime import datetime, timedelta
import logging
import time
from typing import List, Dict, Optional

from apis.api import BaseAPI

class CoveredCallStrategy:
    """
    基于盈利率要求的备兑看涨策略
    定时扫描期权机会，当满足盈利率要求时开仓
    """
    
    def __init__(self, API: BaseAPI, symbol: str):
        """
        初始化策略
        
        Args:
            API: ib_insync的IB连接对象
            symbol: 股票代码，如'AAPL'
        """
        self.ib = API
        self.symbol = symbol
        self.stock_contract = None
        
        # 策略参数（将在InitStrategy中设置）
        self.expected_profit_rate = 0.0    # 期望盈利率
        self.premium_weight = 0.0          # 权利金在盈利率中的占比
        
        # 固定参数
        self.scan_interval = 60            # 扫描间隔（秒）
        self.min_dte = 2                   # 最小到期天数
        self.max_dte = 30                  # 最大到期天数
        self.profit_close_threshold = 0.9  # 平仓盈利阈值（90%）
        self.time_remaining_threshold = 0.1 # 剩余时间阈值（10%）
        
        # 持仓状态
        self.stock_position = 0            # 股票持仓数量
        self.max_option_contracts = 0      # 最大可卖期权合约数
        self.current_option_positions = {} # 当前期权持仓 {contract: {'quantity': int, 'entry_price': float, 'entry_time': datetime}}
        
        # 运行状态
        self.is_running = False
        self.last_scan_time = None
        
        # 日志设置
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        
    def InitStrategy(self, expected_profit_rate: float = 0.05, premium_weight: float = 0.5):
        """
        初始化策略
        
        Args:
            expected_profit_rate: 期望盈利率（如0.05表示5%）
            premium_weight: 权利金在盈利率中的占比（如0.5表示权利金占期望盈利的50%）
        """
        try:
            self.logger.info(f"初始化备兑看涨策略 - 股票: {self.symbol}")
            self.logger.info(f"期望盈利率: {expected_profit_rate:.1%}, 权利金占比: {premium_weight:.1%}")
            
            # 设置策略参数
            self.expected_profit_rate = expected_profit_rate
            self.premium_weight = premium_weight
            
            # 创建股票合约
            self.stock_contract = Stock(self.symbol, 'SMART', 'USD')
            self.ib.qualifyContracts(self.stock_contract)
            
            # 订阅股票市场数据
            self.ib.reqMktData(self.stock_contract)
            
            # 设置事件监听
            self.ib.orderStatusEvent += self.on_order_status
            
            # 获取当前股票持仓
            self._update_stock_position()
            
            if self.stock_position <= 0:
                self.logger.error("没有发现股票持仓，策略无法启动")
                return
                
            # 计算可卖出期权合约数
            self.max_option_contracts = self.stock_position // 100
            self.logger.info(f"股票持仓: {self.stock_position}股, 最大可卖期权合约: {self.max_option_contracts}手")
            
            if self.max_option_contracts <= 0:
                self.logger.error("股票持仓不足100股，无法执行备兑看涨策略")
                return
            
            # 获取当前期权持仓
            self._update_option_positions()
            
            # 开始策略执行
            self.is_running = True
            self.logger.info("策略初始化完成，开始执行")
            
            # 启动定时扫描
            self._start_scanning()
            
        except Exception as e:
            self.logger.error(f"策略初始化失败: {e}")
            self.DoStop()
    
    def DoStop(self):
        """
        停止策略执行
        """
        try:
            self.logger.info("停止备兑看涨策略")
            self.is_running = False
            
            # 取消所有未成交订单
            open_orders = self.ib.openOrders()
            for order in open_orders:
                if hasattr(order.contract, 'symbol') and order.contract.symbol == self.symbol:
                    self.ib.cancelOrder(order)
                    self.logger.info(f"取消订单: {order.contract.localSymbol if hasattr(order.contract, 'localSymbol') else order.contract.symbol}")
            
            # 取消市场数据订阅
            if self.stock_contract:
                self.ib.cancelMktData(self.stock_contract)
            
            for contract in self.current_option_positions.keys():
                self.ib.cancelMktData(contract)
            
            self.logger.info("策略已停止")
            
        except Exception as e:
            self.logger.error(f"停止策略时出错: {e}")
    
    def _start_scanning(self):
        """
        开始定时扫描
        """
        async def scan_loop():
            while self.is_running:
                try:
                    # 管理现有持仓
                    self._manage_existing_positions()
                    
                    # 寻找新的期权机会
                    available_contracts = self.max_option_contracts - len(self.current_option_positions)
                    if available_contracts > 0:
                        self._scan_for_opportunities(available_contracts)
                    
                    self.last_scan_time = datetime.now()
                    
                    # 等待下一次扫描
                    await asyncio.sleep(self.scan_interval)
                    
                except Exception as e:
                    self.logger.error(f"扫描过程中出错: {e}")
                    await asyncio.sleep(self.scan_interval)
        
        # 启动异步扫描循环
        if self.ib.isConnected():
            asyncio.create_task(scan_loop())
    
    def _update_stock_position(self):
        """
        更新股票持仓
        """
        try:
            positions = self.ib.positions()
            self.stock_position = 0
            
            for position in positions:
                if (isinstance(position.contract, Stock) and 
                    position.contract.symbol == self.symbol):
                    self.stock_position = position.position
                    break
            
            self.logger.info(f"股票持仓更新: {self.stock_position}股")
            
        except Exception as e:
            self.logger.error(f"更新股票持仓失败: {e}")
    
    def _update_option_positions(self):
        """
        更新期权持仓
        """
        try:
            positions = self.ib.positions()
            current_positions = {}
            
            for position in positions:
                if (isinstance(position.contract, Option) and 
                    position.contract.symbol == self.symbol and
                    position.position != 0):
                    
                    # 订阅期权市场数据
                    self.ib.reqMktData(position.contract)
                    
                    # 记录持仓信息（简化版，实际入场价格和时间需要从交易记录获取）
                    current_positions[position.contract] = {
                        'quantity': position.position,
                        'entry_price': 0,  # 需要从历史数据获取
                        'entry_time': datetime.now()  # 需要从历史数据获取
                    }
            
            self.current_option_positions = current_positions
            self.logger.info(f"期权持仓更新: {len(self.current_option_positions)}手")
            
        except Exception as e:
            self.logger.error(f"更新期权持仓失败: {e}")
    
    def _scan_for_opportunities(self, available_contracts: int):
        """
        扫描期权机会
        
        Args:
            available_contracts: 可用的合约数量
        """
        try:
            # 获取当前股价
            stock_ticker = self.ib.ticker(self.stock_contract)
            current_price = stock_ticker.last or stock_ticker.close
            
            if not current_price or current_price <= 0:
                self.logger.warning("无法获取有效的股票价格")
                return
            
            self.logger.info(f"开始扫描期权机会，当前股价: ${current_price:.2f}")
            
            # 获取期权链
            option_opportunities = self._get_option_opportunities(current_price)
            
            if not option_opportunities:
                self.logger.info("未找到符合条件的期权机会")
                return
            
            # 按到期日排序（优先最近日期）
            option_opportunities.sort(key=lambda x: x['expiration'])
            
            # 选择符合盈利率要求的期权
            contracts_to_sell = 0
            for opportunity in option_opportunities:
                if contracts_to_sell >= available_contracts:
                    break
                
                if self._meets_profit_requirement(opportunity, current_price):
                    self._sell_option(opportunity)
                    contracts_to_sell += 1
            
        except Exception as e:
            self.logger.error(f"扫描期权机会失败: {e}")
    
    def _get_option_opportunities(self, current_price: float) -> List[Dict]:
        """
        获取可用的期权机会
        """
        opportunities = []
        
        try:
            # 获取期权参数
            chains = self.ib.reqSecDefOptParams(self.symbol, '', 'STK', self.stock_contract.conId)
            
            if not chains:
                return opportunities
            
            chain = chains[0]
            
            # 筛选到期日
            target_expirations = []
            now = datetime.now()
            
            for expiration in chain.expirations:
                exp_date = datetime.strptime(expiration, '%Y%m%d')
                days_to_expiry = (exp_date - now).days
                
                if self.min_dte <= days_to_expiry < self.max_dte:
                    target_expirations.append((expiration, days_to_expiry))
            
            # 对每个到期日和行权价组合获取报价
            for expiration, dte in target_expirations:
                for strike in chain.strikes:
                    # 只考虑价外期权（行权价 > 当前价格）
                    if strike > current_price:
                        try:
                            option_contract = Option(
                                symbol=self.symbol,
                                lastTradeDateOrContractMonth=expiration,
                                strike=strike,
                                right='C',
                                exchange='SMART'
                            )
                            
                            self.ib.qualifyContracts(option_contract)
                            
                            # 获取期权报价
                            ticker = self.ib.reqMktData(option_contract, '', False, False)
                            self.ib.sleep(0.1)  # 短暂等待数据
                            
                            if ticker.bid and ticker.bid > 0:
                                opportunities.append({
                                    'contract': option_contract,
                                    'expiration': expiration,
                                    'strike': strike,
                                    'dte': dte,
                                    'bid': ticker.bid,
                                    'ask': ticker.ask or 0
                                })
                            
                            # 取消数据订阅以节省资源
                            self.ib.cancelMktData(option_contract)
                            
                        except Exception as e:
                            self.logger.debug(f"获取期权 {strike} {expiration} 数据失败: {e}")
                            continue
            
        except Exception as e:
            self.logger.error(f"获取期权机会失败: {e}")
        
        return opportunities
    
    def _meets_profit_requirement(self, opportunity: Dict, current_price: float) -> bool:
        """
        检查是否满足盈利率要求
        
        Args:
            opportunity: 期权机会信息
            current_price: 当前股价
        """
        try:
            strike = opportunity['strike']
            premium = opportunity['bid']
            
            # 计算理论盈利
            # 价差盈利：行权价 - 当前价格
            price_diff_profit = strike - current_price
            
            # 总理论盈利 = 价差盈利 + 权利金
            total_profit = price_diff_profit + premium
            
            # 理论盈利率
            profit_rate = total_profit / current_price
            
            # 检查是否满足要求
            meets_requirement = profit_rate >= self.expected_profit_rate
            
            if meets_requirement:
                self.logger.info(f"找到符合条件的期权: {opportunity['contract'].localSymbol}, "
                               f"行权价: ${strike:.2f}, 权利金: ${premium:.2f}, "
                               f"理论盈利率: {profit_rate:.2%} (要求: {self.expected_profit_rate:.2%})")
            
            return meets_requirement
            
        except Exception as e:
            self.logger.error(f"检查盈利率要求失败: {e}")
            return False
    
    def _sell_option(self, opportunity: Dict):
        """
        卖出期权
        """
        try:
            contract = opportunity['contract']
            bid_price = opportunity['bid']
            
            # 以买价或稍低的价格卖出
            sell_price = bid_price * 0.99  # 稍微调低以提高成交概率
            
            order = LimitOrder('SELL', 1, round(sell_price, 2))
            trade = self.ib.placeOrder(contract, order)
            
            self.logger.info(f"提交卖出期权订单: {contract.localSymbol}, "
                           f"价格: ${sell_price:.2f}, DTE: {opportunity['dte']}天")
            
        except Exception as e:
            self.logger.error(f"卖出期权失败: {e}")
    
    def _manage_existing_positions(self):
        """
        管理现有期权持仓
        """
        positions_to_close = []
        
        for contract, position_info in self.current_option_positions.items():
            try:
                # 获取当前期权价格
                ticker = self.ib.ticker(contract)
                current_price = ticker.ask or ticker.last or ticker.close
                
                if not current_price:
                    continue
                
                # 计算到期时间比例
                exp_date = datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d')
                total_time = (exp_date - position_info['entry_time']).total_seconds()
                remaining_time = (exp_date - datetime.now()).total_seconds()
                time_ratio = remaining_time / total_time if total_time > 0 else 0
                
                # 如果入场价格为0（初始化时的持仓），尝试从市价估算
                entry_price = position_info['entry_price']
                if entry_price <= 0:
                    entry_price = current_price / 0.5  # 假设已盈利50%
                
                # 计算盈利比例
                if entry_price > 0:
                    profit_ratio = (entry_price - current_price) / entry_price
                else:
                    profit_ratio = 0
                
                self.logger.info(f"持仓监控: {contract.localSymbol}, "
                               f"当前价: ${current_price:.2f}, 入场价: ${entry_price:.2f}, "
                               f"盈利: {profit_ratio:.1%}, 剩余时间: {time_ratio:.1%}")
                
                # 检查平仓条件：盈利90%以上且剩余时间超过10%
                if (profit_ratio >= self.profit_close_threshold and 
                    time_ratio > self.time_remaining_threshold):
                    positions_to_close.append(contract)
                    self.logger.info(f"满足平仓条件: {contract.localSymbol}")
                
            except Exception as e:
                self.logger.error(f"管理持仓 {contract.localSymbol} 时出错: {e}")
        
        # 执行平仓
        for contract in positions_to_close:
            self._close_position(contract)
    
    def _close_position(self, contract: Option):
        """
        平仓期权持仓
        """
        try:
            # 市价买回期权
            order = MarketOrder('BUY', 1)
            trade = self.ib.placeOrder(contract, order)
            
            self.logger.info(f"提交平仓订单: {contract.localSymbol}")
            
        except Exception as e:
            self.logger.error(f"平仓失败 {contract.localSymbol}: {e}")
    
    def on_order_status(self, trade):
        """
        订单状态更新事件处理
        """
        try:
            if hasattr(trade.contract, 'symbol') and trade.contract.symbol == self.symbol:
                status = trade.orderStatus.status
                action = trade.order.action
                
                self.logger.info(f"订单状态: {action} {getattr(trade.contract, 'localSymbol', trade.contract.symbol)} - {status}")
                
                if status == 'Filled':
                    # 订单成交，更新持仓
                    if isinstance(trade.contract, Option):
                        if action == 'SELL':
                            # 新开空头持仓
                            self.current_option_positions[trade.contract] = {
                                'quantity': -trade.orderStatus.filled,
                                'entry_price': trade.orderStatus.avgFillPrice,
                                'entry_time': datetime.now()
                            }
                            self.logger.info(f"新增期权空头持仓: {trade.contract.localSymbol}, "
                                           f"价格: ${trade.orderStatus.avgFillPrice:.2f}")
                        
                        elif action == 'BUY':
                            # 平仓
                            if trade.contract in self.current_option_positions:
                                del self.current_option_positions[trade.contract]
                                self.ib.cancelMktData(trade.contract)  # 取消数据订阅
                                self.logger.info(f"平仓完成: {trade.contract.localSymbol}")
                
        except Exception as e:
            self.logger.error(f"处理订单状态更新失败: {e}")
