import datetime
from enum import Enum
from zoneinfo import ZoneInfo
import ib_insync
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from data.config import *
# from common.logger_manager import LoggerManager
from data import config
    
class OrderStatus(Enum):
    Created = 0,
    Submitted = 1
    Accepted = 2
    Partial = 3
    Completed = 4
    Canceled = 5
    Expired = 6
    Margin = 7
    Rejected = 8
    Cancelled = 9
    ERROR = 10

class GridOrder:
    def __init__(self, symbol, order_id, action, price, shares, status: int, perm_id: int = 0, order_ref: str = "", order_type: any = ""):
        self.symbol = symbol
        self.action = action
        self.order_id = order_id
        self.order_type = "LIMIT"
        self.lmt_price = price
        self.shares = shares
        self.done_price = 0
        self.done_shares = 0
        self.fee = 0.35
        self.status: OrderStatus = OrderStatus(status)
        self.apply_time = datetime.datetime.now(ZoneInfo(config.time_zone))
        self.done_time = None
        self.perm_id = perm_id  # IB PermId, default to 0
        self.order_ref = order_ref  # 可选的订单引用字段
        self.order_type = order_type
    
    def __str__(self):
        return f"symbol:{self.symbol} action:{self.action} price:({round(self.lmt_price, 2)}, {round(self.done_price, 2)}) shares:({self.shares}, {self.done_shares}) status: {self.status}"
    
    def isbuy(self):
        return True if self.action == "BUY" else False
    
    def to_dict(self):
        return {
            "symbol": self.symbol,
            "action": self.action,
            "order_id": self.order_id,
            "lmt_price": self.lmt_price,
            "shares": self.shares,
            "done_price": round(self.done_price, 2),
            "done_shares": self.done_shares,
            "fee": self.fee,
            "status": self.status.name,  # Store as string for readability
            "apply_time": self.apply_time.isoformat(),
            "done_time": self.done_time.isoformat() if self.done_time else "",
            "perm_id": self.perm_id,
            "order_ref": self.order_ref,
            "order_type": self.order_type
        }

    @classmethod
    def from_dict(cls, d):
        obj = cls(
            symbol=d["symbol"],
            order_id=d["order_id"],
            action=d["action"],
            price=d["lmt_price"],
            shares=d["shares"],
            status=OrderStatus[d["status"]].value,  # Convert back from name to int
            perm_id=d.get("perm_id", 0),
            order_ref=d.get("order_ref", ""),
            order_type=d.get("order_type", "LIMIT")
        )
        obj.done_price = d["done_price"]
        obj.done_shares = d["done_shares"]
        obj.fee = d["fee"]
        obj.apply_time = datetime.datetime.fromisoformat(d["apply_time"])
        obj.done_time = datetime.datetime.fromisoformat(d["done_time"]) if d["done_time"] else None
        return obj


def convert_gridorder_to_ib_order(grid_order: GridOrder) -> ib_insync.Order:
    """
    将 GridOrder 转换为 ib_insync.Order
    """
    ib_order = ib_insync.Order()
    ib_order.orderId = grid_order.order_id
    ib_order.action = grid_order.action.upper()  # 'BUY' / 'SELL'
    ib_order.orderType = grid_order.order_type  # 'LIMIT'
    ib_order.totalQuantity = abs(grid_order.shares)
    ib_order.lmtPrice = grid_order.lmt_price
    ib_order.tif = 'GTC'  # 默认使用 GTC（Good Till Canceled）
    ib_order.permId = grid_order.perm_id  # permId 默认为 0，IB 会自动分配
    ib_order.orderRef = grid_order.order_ref  # 可选的订单引用字段

    return ib_order

def convert_trade_to_gridorder(trade: ib_insync.Trade) -> GridOrder:
    """
    将 ib_insync.Trade 转换为 GridOrder
    """

    # 1. 提取基本信息
    ib_order = trade.order
    contract = trade.contract
    status = trade.orderStatus
    fills = trade.fills or []

    symbol = contract.symbol
    order_id = ib_order.orderId
    action = ib_order.action
    price = ib_order.lmtPrice or 0
    shares = ib_order.totalQuantity

    # 2. 状态映射
    status_map = {
        "PendingSubmit": OrderStatus.Created,
        "PreSubmitted": OrderStatus.Submitted,
        "Submitted": OrderStatus.Submitted,
        "Cancelled": OrderStatus.Cancelled,
        "ApiCancelled": OrderStatus.Cancelled,
        "Filled": OrderStatus.Completed,
        "Inactive": OrderStatus.Rejected,
        "Rejected": OrderStatus.Rejected,
        "PartiallyFilled": OrderStatus.Partial,
    }
    grid_status = status_map.get(status.status, OrderStatus.ERROR)

    # 3. 成交价格和数量
    total_fill_size = sum(fill.execution.shares for fill in fills)
    total_fill_cost = sum(fill.execution.shares * fill.execution.price for fill in fills)
    done_price = (total_fill_cost / total_fill_size) if total_fill_size > 0 else 0

    # 4. 创建 GridOrder 实例
    grid_order = GridOrder(
        symbol=symbol,
        order_id=order_id,
        action=action,
        price=price,
        shares=shares,
        status=grid_status.value,
        perm_id=ib_order.permId,
        order_ref=ib_order.orderRef,
        order_type=ib_order.orderType
    )
    grid_order.done_price = done_price
    grid_order.done_shares = total_fill_size

    # 5. 时间处理
    try:
        grid_order.apply_time = datetime.datetime.strptime(ib_order.manualOrderTime, "%Y%m%d  %H:%M:%S")
    except Exception:
        grid_order.apply_time = datetime.datetime.now(ZoneInfo(config.time_zone))

    if grid_status == OrderStatus.Completed:
        grid_order.done_time = datetime.datetime.now(ZoneInfo(config.time_zone))

    return grid_order

class DailyProfitSummary:
    """
    每日盈利总结
    """
    def __init__(self, strategy: str, strategy_name: str, symbol: str, profits: float, position: float, cash: float, date: str, params: dict = None, start_time: datetime.datetime = None, end_time: datetime.datetime = None):
        self.date = datetime.datetime.now().strftime("%Y%m%d") if not date else date
        self.strategy = strategy
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.profits = profits
        self.position = position
        self.cash = cash
        self.params = params
        self.start_time = start_time
        self.end_time = end_time


def generate_html(summaries: list[DailyProfitSummary]) -> str:
    """生成HTML格式日报"""
    total_profit = sum(s.profits for s in summaries)
    total_color = "green" if total_profit >= 0 else "red"
    total_str = f"{total_profit:+.2f}"

    html = """
    <html><body>
    <h2>📊 策略每日收益汇总报告</h2>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-family:Arial;font-size:14px;">
    <tr style="background-color:#f2f2f2;">
        <th>日期</th><th>策略名</th><th>当日收益</th><th>仓位</th><th>现金余额</th>
    </tr>
    """

    for s in summaries:
        profit_color = "green" if s.profits >= 0 else "red"
        profit_str = f"{s.profits:+.2f}"
        html += f"""
        <tr>
            <td>{s.date}</td>
            <td>{s.strategy_name}</td>
            <td style="color:{profit_color};font-weight:bold;">{profit_str}</td>
            <td>{s.position:.0f}</td>
            <td>${s.cash:,.2f}</td>
        </tr>
        """

    html += f"""
    </table>
    <h3>💰 总收益：<span style="color:{total_color};">{total_str}</span></h3>
    """

    # 若策略附加参数存在，则追加展示
    html += "<h4>📋 策略参数详情：</h4>"
    for s in summaries:
        if not s.params:
            continue
        if s.strategy in ["grid"]:
            html += f"<b>{s.strategy_name}</b><ul>"
            html += f"<li><b>跳开利润</b>：{s.params.get('extra_price', 0)}</li>"
            html += f"<li><b>网格完成量</b>：{s.params.get('completed_count', 0)}</li>"
            html += f"<li><b>买单挂单</b>：{s.params.get('pending_buy_count', 0)}</li>"
            html += f"<li><b>卖单挂单</b>：{s.params.get('pending_sell_count', 0)}</li>"
            html += "</ul>"

    html += "</body></html>"
    return html

def send_email(subject: str, body: str) -> any:
    """
    发送邮件通知
    :param subject: 邮件主题
    :param body: 邮件内容
    :return: None
    """
    # 构造邮件
    msg = MIMEText(body, "html", "utf-8")
    msg["From"] = Header(f"profits <{sender_email}>", "ascii")
    msg["To"] = Header(f"owner <{receiver_email}>", "ascii")
    msg["Subject"] = Header(subject, "utf-8")

    # 发送邮件
    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
            server.login(sender_email, auth_code)
            server.sendmail(sender_email, [receiver_email], msg.as_string())
            return None

    except Exception as e:
        return e
        # LoggerManager.Error("app", strategy="mail", event="send_email", content=f"Email sent to {receiver_email} with subject: {subject} failed. Error: {e}")
