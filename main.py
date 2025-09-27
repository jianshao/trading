#!/usr/bin/python3#!/usr/bin/python3
import socket
import subprocess
import datetime
import sys
import os
import signal
import time
from typing import Dict
from datetime import timedelta
import argparse
import holidays

from ib_client_manager import IBClientManager
from utils import utils

if __name__ == '__main__': # Only adjust path if running as script directly
    # This assumes backtester_main.py is in a subdirectory (e.g., 'scripts')
    # and 'apis' and 'strategy' are in the parent directory of that subdirectory.
    # Adjust as per your actual project structure.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi
from strategy.strategy import Strategy
from strategy.strategy_engine import GridStrategyEngine

STRATEGY_GRID = "grid"

is_running: bool = False

# 初始所有化策略环境，必须等待初始化完成才能继续
# 初始化时只能使用同步接口，否则会出现报错：This event loop is already running
def InitStrategies(api: IBapi, strategies: Dict[str, Strategy], real: bool) -> bool:
    # grid 策略
    if not real:
        filename = "data/paper/strategies/"
    else:
        filename = "data/real/strategies/"
    grid = GridStrategyEngine(api, filename)
    grid.InitStrategy()
    strategies[STRATEGY_GRID] = grid
    
    return True


def StopStrategies(strategies: Dict[str, Strategy]):
    print("Stop all Strategies: ")
    if STRATEGY_GRID in strategies:
        grid = strategies[STRATEGY_GRID]
        grid.DoStop()
    
    print("All Strategies Exited!")

def handle_strategies(api: IBapi, args):
    if not InitStrategies(api, strategiesMap, args.account == 'real'):
        print("Initializing Strategies Failed, Aborting...")
    else:
        # 使用纯同步方式，否则会有以下问题
        # 1.使用asyncio.run(xxx)：IB内部已经使用asyncio管理异步事件了，如果入口再使用会导致2个管理混乱出错
        # 2.直接使用ib.run()：ib.run()会阻塞线程成为常驻主线程，但是ib内部可能在执行某些事件，导致监听IBKR通知事件得不到处理
        global is_running
        is_running = True
        
        now = datetime.datetime.now()
        # 构造"明天早上 5 点"的 datetime 对象
        next_day_5am = datetime.datetime.combine(now.date() + timedelta(days=1), datetime.datetime.min.time()) + timedelta(hours=5)

        # 计算时间差
        delta = (next_day_5am - now).seconds
        # print(f"remaining seconds: {delta}")
        while api.isConnected() and is_running and delta:
            api.ib.sleep(1)
            delta -= 1
            # 发送心跳，通知其他部分
        
        # 退出所有策略
        StopStrategies(strategiesMap)


def HandleExit(signum, frame):
    print("收到退出信号，准备退出")
    global is_running
    is_running = False

def cancel_all_orders(api: IBapi):
    print("Cancel All Orders Now...")
    for o in api.ib.reqAllOpenOrders():
        print(f"Cancelling Order: {o.order.orderId}, {o.contract.symbol}, {getattr(o.order, 'lmtPrice', 'MKT')}, {o.orderStatus.status}")
        api.ib.cancelOrder(o.order)
    print("All Orders Canceled.")

def cancel_order_by_symbol_price(api: IBapi, symbol: str, price: float):
    matched = 0
    orders = api.ib.reqAllOpenOrders()
    for o in orders:
        # 匹配标的和价格
        if o.contract.symbol.upper() == symbol and getattr(o.order, 'lmtPrice', None) == price:
            api.ib.cancelOrder(o.order)
            matched += 1
    print(f"已发送取消请求，匹配订单 {matched} 个")

def handle_cancel_orders(api: IBapi, args):
    if args.cancel[0].lower() == "all":
        cancel_all_orders(api)
    elif len(args.cancel) == 2:
        symbol = args.cancel[0].upper()
        price = float(args.cancel[1])
        cancel_order_by_symbol_price(api, symbol, price)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='命令行参数')
    parser.add_argument('account', type=str, choices=["real", "paper"], help='启动账户类型, real-真实账户、 paper-模拟账户')
    parser.add_argument('--cancel', nargs='+', help='取消订单，all-取消所有订单')
    parser.add_argument('client', type=str, help='指定启动的客户端, tws-TWS, ib-IB GATEWAY')
    parser.add_argument('--check', action='store_true', help='检查今天是不是交易日')
    args = parser.parse_args()
    # 注册信号退出事件
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, HandleExit)
        
    strategiesMap: Dict[str, any] = {}
    
    # 如果不是交易日直接退出
    if args.check and not utils.is_us_stock_open():
        print("today is not trading day. exiting...")
        exit(0)

    # print(args.client)
    # print(args.account)
    manager = IBClientManager(args.client, args.account)
    print(f"************************** Auto Trading System Starting **************************")
    try:
        # 组装、连接api，使api与策略解耦
        api = manager.start_and_connect()
        if not api:
            print("System Error: Connect to IBKR FAIL, Aborting.....")
        else:
            if args.cancel:
                handle_cancel_orders(api, args)
            elif args.account:
                handle_strategies(api, args)

    except Exception as e:
        print(f"Trading System Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 关闭连接
        manager.disconnect_and_quit()
        time.sleep(5)
        print(f"************************** Auto Trading System Exited! **************************")

    

