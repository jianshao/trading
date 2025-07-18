#!/usr/bin/python3#!/usr/bin/python3
import datetime
import sys
import os
import signal
from typing import Dict
from datetime import timedelta

if __name__ == '__main__': # Only adjust path if running as script directly
    # This assumes backtester_main.py is in a subdirectory (e.g., 'scripts')
    # and 'apis' and 'strategy' are in the parent directory of that subdirectory.
    # Adjust as per your actual project structure.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi
from strategy.engine import Engine
from strategy.strategy_engine import GridStrategyEngine

STRATEGY_GRID = "grid"

is_running: bool = False

# connect只能用同步的，使用异步的会出问题
def GetApi() -> IBapi:
    api = IBapi()
    if api.connect():
        return api
    return None

# 初始所有化策略环境，必须等待初始化完成才能继续
# 初始化时只能使用同步接口，否则会出现报错：This event loop is already running
def InitStrategies(api: IBapi, strategies: Dict[str, Engine]) -> bool:
    # grid 策略
    grid = GridStrategyEngine(api, "data/strategies/")
    grid.InitStrategy()
    strategies[STRATEGY_GRID] = grid
    
    return True


def StopStrategies(strategies: Dict[str, any]):
    print("Stop all Strategies: ")
    if STRATEGY_GRID in strategies:
        grid = strategies[STRATEGY_GRID]
        grid.DoStop()
    
    print("All Strategies Exited!")

def HandleExit(signum, frame):
    print("收到退出信号，准备退出")
    global is_running
    is_running = False


if __name__ == "__main__":
    # 注册信号退出事件
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, HandleExit)
        
    strategiesMap: Dict[str, any] = {}

    print(f"******************************************** Auto Trading System Starting ********************************************")
    try:
        # 组装、连接api，使api与策略解耦
        api = GetApi()
        if not api:
            print("System Error: Connect to IBKR FAIL, Aborting.....")
        else:
            if not InitStrategies(api, strategiesMap):
                print("Initializing Strategies Failed, Aborting...")
            else:
                # 使用纯同步方式，否则会有以下问题
                # 1.使用asyncio.run(xxx)：IB内部已经使用asyncio管理异步事件了，如果入口再使用会导致2个管理混乱出错
                # 2.直接使用ib.run()：ib.run()会阻塞线程成为常驻主线程，但是ib内部可能在执行某些事件，导致监听IBKR通知事件得不到处理
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

    except Exception as e:
        print(f"Trading System Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 先执行清理动作，然后关闭连接
        StopStrategies(strategiesMap)
        api.disconnect()
        print(f"******************************************** Auto Trading System Exited! ********************************************")

    

