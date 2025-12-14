from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from ib_insync import Stock, Ticker
import pandas as pd
from apis.api import BaseAPI
from data import config


class RealTimeDataProcessor:
    def __init__(self, api: BaseAPI):
        self.api = api
        
        self.stocks_data = {}
        
    def register_stock(self, symbol: str, exchange: str, sector: str):
        if symbol not in self.stocks_data:
            self.stocks_data[symbol] = []
            contact = Stock(symbol, exchange)
            ticker: Ticker = self.api.reqMktData(contact)
            ticker.updateEvent += self.on_market_data
            
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
