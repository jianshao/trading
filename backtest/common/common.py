import datetime
from typing import Any, Optional
from strategy.common import GridOrder, OrderStatus

def buildGridOrder(order: Any) -> GridOrder:
    grid_order = GridOrder("", order.ref, "BUY" if order.isbuy() else "SELL", order.price, order.size, order.status)
    grid_order.apply_time = datetime.datetime.now()
    if grid_order.status == OrderStatus.Completed:
        grid_order.done_price = order.executed.price
        grid_order.done_shares = order.executed.size
    return grid_order
    
def gridorder_2_order(grid_order: GridOrder) -> Any:
    return 
