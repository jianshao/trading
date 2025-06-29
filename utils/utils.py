#!/usr/bin/python3
import datetime
import matplotlib.pyplot as plt
import pandas as pd
import pandas_market_calendars as mcal

def build_order_id(strategy: str, symbol: str, order_type: str, price: float, amount: float):
    # 订单id格式：策略ID+标的代码+订单类型+价格+数量+提交时间
    now = datetime.now()
    return f"${strategy}-${symbol}-${order_type}-${price}-${amount}-${now}"

def show_figure(data):
    df = pd.DataFrame(data)

    # 开始绘图
    plt.figure(figsize=(8, 6))

    # 按 type 分组，每个 group 一条线
    for t, group in df.groupby(["grid_type", "strategy_id"]):
        plt.plot(group["proportion"], group["net_profit"], marker='o', label=f"grid_type {t}")

    # 添加图例和标签
    plt.title("Profit vs Proportion for Different Types")
    plt.xlabel("Proportion")
    plt.ylabel("Profit")
    plt.legend()
    plt.grid(True)
    plt.show()

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