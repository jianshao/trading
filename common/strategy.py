from common.utils import DailyProfitSummary, GridOrder


class Strategy:
    def InitStrategy(self, **kwargs):
        pass
      
    def DoStop(self, **kwargs):
        pass
      
    def Reconnect(self, **kwargs):
        pass
    
    def DailySummary(self, date_str: str) -> DailyProfitSummary:
        pass
    
    async def update_order_status(self, order: GridOrder):
        pass
