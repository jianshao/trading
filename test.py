from ib_insync import IB, Stock
import asyncio
from apis.ibkr import IBapi

async def main_loop(api: IBapi):
  # await ib.connectAsync("127.0.0.1", 7497, 2)
  # contract = Stock('WBD', 'SMART', 'USD')
  # result = await ib.reqContractDetailsAsync(contract)
  contract = await api.get_contract_details("WBD")
  result = await api.get_historical_data(contract, end_date_time="20250605 17:00:00 us/eastern", duration_str="1 D", bar_size_setting="5 secs")
  print(len(result))
  
  # count = 1
  # while True:
  #   ib.ib.sleep(5)
  #   print("count: ", count)
  #   count += 1
  #   break
  
if __name__ == "__main__":
  ib = IBapi(client_id=12)
  ib.connect()
  asyncio.get_event_loop().run_until_complete(main_loop(ib))
  ib.disconnect()