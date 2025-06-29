
import os
import sys
import time
from typing import List
import asyncio
import matplotlib.pyplot as plt
import pandas as pd

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        
from apis.ibkr import IBapi
from mockApi.api import MockApi
from strategy.grid_new import GridStrategyEngine
# from utils.utils import *
from utils import utils

Init_Cash = 5000

async def main_loop(mockApi: MockApi, api: IBapi, symbols: List[str]):
    # 获取历史数据，将历史数据导入mock服务。导入数据过程中会模拟券商的成交通知逻辑。
    # print("Mock starting.......")
    # end_dates = ["20250505 17:00:00 us/eastern", "20250506 17:00:00 us/eastern", "20250507 17:00:00 us/eastern", "20250508 17:00:00 us/eastern", "20250509 17:00:00 us/eastern",
    #              "20250512 17:00:00 us/eastern", "20250513 17:00:00 us/eastern", "20250514 17:00:00 us/eastern", "20250515 17:00:00 us/eastern", "20250516 17:00:00 us/eastern",
    #              "20250519 17:00:00 us/eastern", "20250520 17:00:00 us/eastern", "20250521 17:00:00 us/eastern", "20250522 17:00:00 us/eastern", "20250523 17:00:00 us/eastern",
    #              "20250526 17:00:00 us/eastern", "20250527 17:00:00 us/eastern", "20250528 17:00:00 us/eastern", "20250529 17:00:00 us/eastern", "20250530 17:00:00 us/eastern",
    #              "20250602 17:00:00 us/eastern", "20250603 17:00:00 us/eastern", "20250604 17:00:00 us/eastern", "20250605 17:00:00 us/eastern", "20250606 17:00:00 us/eastern",
    #              "20250609 17:00:00 us/eastern", "20250610 17:00:00 us/eastern", "20250611 17:00:00 us/eastern", "20250612 17:00:00 us/eastern", "20250613 17:00:00 us/eastern",
    #              "20250616 17:00:00 us/eastern", "20250617 17:00:00 us/eastern", "20250618 17:00:00 us/eastern", "20250619 17:00:00 us/eastern", "20250620 17:00:00 us/eastern",
    #              "20250623 17:00:00 us/eastern", "20250624 17:00:00 us/eastern", "20250625 17:00:00 us/eastern", "20250626 17:00:00 us/eastern", "20250627 17:00:00 us/eastern"]
    # end_dates = ["20250602 17:00:00 us/eastern"]
    end_dates = utils.get_us_trading_days("2025-05-05", "2025-06-28")
    for symbol in symbols:
        print(f"Mock for {symbol}......")
        contract = await api.get_contract_details(symbol)
        all_data = pd.DataFrame([])
        for end_date in end_dates:
            valid_end_date = end_date+" 17:00:00 us/eastern"
            # 1 secs, 5 secs, 10 secs, 15 secs, 30 secs, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins, 20 mins, 30 mins, 1 hour, 2 hours, 3 hours, 4 hours, 8 hours, 1 day, 1W, 1M
            data = await mockApi.get_historical_data(contract, end_date_time=valid_end_date, duration_str="1 D", bar_size_setting="5 secs")
            if not data.empty:
                # if not all_data.empty:
                #     print(f"{all_data['Close'].iloc[len(all_data)-1]}")
                # print(f"{data['Open'].iloc[0]} len({len(data)})")
                all_data = pd.concat([all_data, data])
            else:
                print(f"{symbol} {end_date} failed.")
            # time.sleep(2)
        print(f"Recive Data: len({len(all_data)}) Open Price: {all_data['Open'].iloc[0]}")
        await mockApi.input(symbol, all_data)
        # print(f" {symbol} done.")
        mockApi.reflash_account(Init_Cash)

if __name__ == "__main__":
    print("------------------BackTest Starting-------------------")
    engine = None
    try :
        # 先启动mock服务
        api = IBapi(client_id=11)
        if api.connect():
            mockapi = MockApi(api, Init_Cash)
            
            # 运行策略，并初始化。此时会提交初始订单
            engine = GridStrategyEngine(mockapi, "data/mock")
            engine.InitStrategy()
            
            symbols = [ "TQQQ"]
            # symbols = ["WU"]
            # asyncio.run() 会与 in_asyncnei 内部使用的asyncio冲突，导致异步事件异常。
            asyncio.get_event_loop().run_until_complete(main_loop(mockapi, api, symbols))
        
    except Exception as e:
        print(f"Trading System Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 先执行清理动作，然后关闭连接
        # 结束策略执行。
        if engine:
            data = engine.DoStop()

        api.disconnect()
        print(f"******************************************** Auto Trading System Exited! ********************************************")
    
    