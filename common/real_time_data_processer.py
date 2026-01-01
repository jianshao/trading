from datetime import datetime
from typing import Optional
import pandas as pd
from apis.api import BaseAPI


class RealTimeDataProcessor:
    def __init__(self, api: BaseAPI):
        self.api = api
        
        self.stocks_data = {}
            
    def on_market_data(self, ticker):
        contract = ticker.contract
        if contract.symbol not in self.stocks_data:
            return
        
        price = ticker.last if ticker.last else ticker.close  
        for indicator in self.stocks_data[contract.symbol]:
            if indicator == "SMA":
                self.update_sma(contract.symbol, price)
            elif indicator == "EMA":
                self.update_ema(contract.symbol, price)
            # Add more indicators as needed
            
    def update_sma(self, symbol: str, price: float):
        # Implement SMA update logic here
        pass
      
    def update_ema(self, symbol: str, price: float):
        # Implement EMA update logic here
        pass
    
    async def get_atr(self, symbol: str, atr_period: int = 14, duration: int = 30, bar_size: str = "1 day") -> float:
        return await self.api.get_atr(symbol=symbol, atr_period=atr_period, hist_bar_size=bar_size, hist_duration=duration)
    
    async def get_ema(self, symbol: str, ema_period: int, bar_size: str = "1 day", duration: str = "60 D") -> float:
        return await self.api.get_ema(symbol, ema_period, bar_size, duration)
    
    async def get_latest_price(self, symbol: str, exchange="SMART", currency="USD"):
        return await self.api.get_latest_price(symbol, exchange, currency)
    
    def get_current_time(self) -> datetime:
        """实盘返回系统当前时间"""
        return self.api.get_current_time()
    
    async def get_historical_data(self, symbol: str, end_date_time: str = "", 
                                duration_str: str = "1 M", bar_size_setting: str = "1 min", 
                                what_to_show: str = 'TRADES', use_rth: bool = True, 
                                format_date: int = 1, timeout_seconds: int = 60) -> Optional[pd.DataFrame]:
        return await self.api.get_historical_data(symbol, end_date_time, duration_str, bar_size_setting, what_to_show, use_rth, format_date, timeout_seconds)


    async def get_macd(self, symbol):
        return await self.api.get_macd(symbol=symbol)
    
    async def get_vxn(self, durationStr="5 D", barSizeSetting="1 day") -> float:
        return await self.api.get_vxn(durationStr, barSizeSetting)

    async def get_adx(self, symbol, durationStr="1 M", barSizeSetting="1 day") -> float:
        return await self.api.get_adx(symbol, durationStr=durationStr, barSizeSetting=barSizeSetting)
