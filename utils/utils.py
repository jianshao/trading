#!/usr/bin/python3
import datetime
import holidays
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import pandas_market_calendars as mcal
import seaborn as sns

def build_order_id(strategy: str, symbol: str, order_type: str, price: float, amount: float):
    # 订单id格式：策略ID+标的代码+订单类型+价格+数量+提交时间
    now = datetime.now()
    return f"${strategy}-${symbol}-${order_type}-${price}-${amount}-${now}"

def daily_profit(dates, profits, title="每日盈利"):
    # 解决中文问题
    matplotlib.rcParams['font.sans-serif'] = ['Arial Unicode MS']
    matplotlib.rcParams['axes.unicode_minus'] = False

    df = pd.DataFrame({"日期": dates, "盈利": profits})
    
    colors = ["green" if x > 0 else "red" for x in df["盈利"]]

    plt.figure(figsize=(8,5))
    sns.lineplot(x="日期", y="盈利", data=df, marker="o")  # palette=None
    # plt.bar(df["日期"], df["盈利"], color=colors)  # 用 matplotlib 直接设置颜色
    plt.title(title)
    plt.xlabel("日期")
    plt.ylabel("盈利($)")
    plt.show()
    plt.pause(0.1)

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
