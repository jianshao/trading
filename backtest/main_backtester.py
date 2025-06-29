
import os
import sys
import time
from typing import List
import asyncio
import pandas as pd

if __name__ == '__main__': # Allow running/importing from different locations
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        
from apis.ibkr import IBapi
from mockApi.api import MockApi
from strategy.strategy_engine import GridStrategyEngine
from utils import utils

Init_Cash = 5000

async def main_loop(mockApi: MockApi, api: IBapi, symbols: List[str]):
    # 获取历史数据，将历史数据导入mock服务。导入数据过程中会模拟券商的成交通知逻辑。
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
    
    