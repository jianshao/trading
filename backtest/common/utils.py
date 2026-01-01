#!/usr/bin/python3
import datetime
from typing import Any
import holidays
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import pandas_market_calendars as mcal
import seaborn as sns
from typing import Any, Optional
from common.utils import GridOrder, OrderStatus

from common.utils import GridOrder


def get_us_trading_days(start_date: str, end_date: str) -> list:
    """
    获取指定日期范围内的所有美股(NYSE)交易日。

    参数:
        start_date (str): 起始日期，格式为 'YYYY-MM-DD'
        end_date (str): 结束日期，格式为 'YYYY-MM-DD'

    返回:
        list[str]: 所有交易日的字符串列表，格式为 'YYYYMMDD'
    """
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=start_date, end_date=end_date)
    trading_days = schedule.index.strftime('%Y%m%d').tolist()
    return trading_days


def is_us_stock_open() -> bool:
    """
    判断给定日期是否是美股开盘日 (NYSE/NASDAQ)
    :param date: datetime.date, 默认为今天
    :return: bool
    """
    date = datetime.date.today()
    # 周六周日不开盘
    if date.weekday() >= 5:  # 5=周六, 6=周日
        return False

    # 美国节假日
    us_holidays = holidays.US(years=date.year)
    if date in us_holidays:
        return False

    return True

def to_datetime(s) -> datetime:
    if isinstance(s, str) and s != "":
        return datetime.datetime.fromisoformat(s)
    if isinstance(s, datetime):
        return s
    return None

def buildGridOrder(order: Any) -> GridOrder:
    grid_order = GridOrder("", order.ref, "BUY" if order.isbuy() else "SELL", order.price, order.size, order.status)
    grid_order.apply_time = datetime.datetime.now()
    if grid_order.status == OrderStatus.Completed:
        grid_order.done_price = order.executed.price
        grid_order.done_shares = order.executed.size
    return grid_order
    
def gridorder_2_order(grid_order: GridOrder) -> Any:
    return 
