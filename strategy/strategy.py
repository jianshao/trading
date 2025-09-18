from strategy.common import GridOrder


class Strategy:
    def InitStrategy(self, **kwargs):
        pass
      
    def DoStop(self, **kwargs):
        pass
      
    def Reconnect(self, **kwargs):
        pass
    
    async def update_order_status(self, order: GridOrder):
        pass