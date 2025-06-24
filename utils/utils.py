#!/usr/bin/python3
import datetime

def build_order_id(strategy: str, symbol: str, order_type: str, price: float, amount: float):
  # 订单id格式：策略ID+标的代码+订单类型+价格+数量+提交时间
  now = datetime.now()
  return f"${strategy}-${symbol}-${order_type}-${price}-${amount}-${now}"