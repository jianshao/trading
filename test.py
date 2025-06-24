from ib_insync import IB, Stock
import asyncio
from apis.ibkr import IBapi

def main_loop():
  ib = IBapi()
  ib.connect()
  # await ib.connectAsync("127.0.0.1", 7497, 2)
  # contract = Stock('WBD', 'SMART', 'USD')
  # result = await ib.reqContractDetailsAsync(contract)
  result = asyncio.get_event_loop().run_until_complete(ib.get_current_positions())
  print(result)
  
  # count = 1
  # while True:
  #   ib.ib.sleep(5)
  #   print("count: ", count)
  #   count += 1
  #   break
  ib.disconnect()
  
if __name__ == "__main__":
  main_loop()