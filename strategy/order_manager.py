
from typing import Dict
from venv import logger
from ib_insync import Trade, OrderState, Execution


from apis.api import BaseAPI, OrderUpdateCallback
from strategy import common
from strategy.common import GridOrder, OrderStatus
from utils.kafka_producer import KafkaProducerService

# ... (假设 logger 已经配置好) ...

ORDER_TOPIC = "order_logs"

class ActiveOrderRecord:
    def __init__(self, grid_order: GridOrder, callback: OrderUpdateCallback):
        self.grid_order = grid_order
        self.callback = callback

class OrderManager:
    """
    内部订单管理器，现在是 IBWrapper 的一部分。
    它维护 ib_insync.Trade, GridOrder, 和 callback 之间的映射。
    """
    def __init__(self, ib: BaseAPI, producer:KafkaProducerService=None, send_kafka: bool=True):
        self.ib = ib
        self.producer = producer
        self.send_kafka = send_kafka
        # Key: ib_insync 的本地 orderId
        # Value: {'trade': Trade, 'grid_order': GridOrder, 'callback': Callable}
        self.active_orders: Dict[int, ActiveOrderRecord] = {}
        
        self.ib.register_order_status_update_handler(self._on_order_status)
        # self.ib.register_execution_update_handler(self._on_execution)
        self.ib.register_open_order_snapshot_handler(self._on_open_order)
        self.ib.register_error_handler(self._on_error)
        

    # --- 翻译函数 ---
    @staticmethod
    def _translate_ib_status_to_grid_status(ib_status: str) -> OrderStatus:
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
        return status_map.get(ib_status, OrderStatus.ERROR)

    # --- 事件处理器 (现在是私有方法) ---
    async def _dispatch_update(self, ib_trade: Trade):
        record = self.active_orders.get(ib_trade.order.orderId)
        if not record:
            logger.error(f"unknow IB订单 {ib_trade.order.orderId} 。")
            return

        # --- 核心翻译逻辑 ---
        record.grid_order = common.convert_trade_to_gridorder(ib_trade)

        callback = record.callback
        grid_order: GridOrder = record.grid_order
        if callback:
            await callback(grid_order)

        if grid_order.status in {OrderStatus.Completed, OrderStatus.Cancelled, OrderStatus.Rejected}:
            logger.info(f"IB订单 {grid_order.order_id} 已完成，从活动列表中移除。")
            self.active_orders.pop(ib_trade.order.orderId, None)
            if self.send_kafka and self.producer:
                # 发送订单信息到 Kafka
                order_info = {
                    "event": "deal" if grid_order.status == OrderStatus.Completed else "cancel",
                    "order_id": grid_order.order_id,
                    "symbol": grid_order.symbol,
                    "action": grid_order.action,
                    "order_type": grid_order.order_type,
                    "quantity": grid_order.shares,
                    "price": grid_order.lmt_price,
                    "apply_time": grid_order.apply_time.isoformat(),
                    "order_ref": grid_order.order_ref
                }
                await self.producer.send_message(ORDER_TOPIC, order_info)

    async def _on_open_order(self, contract, order, orderStatus):
        record = self.active_orders.get(order.orderId)
        if record:
            record.grid_order.perm_id = order.permId
            logger.info(f"OpenOrder: 业务订单 {record.grid_order.order_id} 获得 permId: {order.permId}")

    async def _on_order_status(self, trade: Trade):
        await self._dispatch_update(trade)
    
    async def _on_execution(self, trade: Trade, fill: Execution):
        await self._dispatch_update(trade)

    async def _on_error(self, reqId, errorCode, errorString, contract):
        logger.error(f"IB Error: ReqId={reqId}, Code={errorCode}, Msg={errorString}")
        # 如果错误与某个活动订单相关
        if reqId in self.active_orders:
            # 这是一个简化的处理，理想情况下应该更精细
            record = self.active_orders[reqId]
            grid_order = record.grid_order
            grid_order.status = OrderStatus.Rejected
            logger.error(f"业务订单 {grid_order.order_id} 发生错误: Code={errorCode}, Msg={errorString}")
            # 立即分发更新
            # await self._dispatch_update(record['trade'])
    
    async def place_order(self, symbol, action, quantity, price, callback: OrderUpdateCallback, **kwargs) -> GridOrder:
        try:
            # --- 翻译：从 GridOrder 到 ib_insync.Contract 和 ib_insync.Order ---

            order = await self.ib.place_limit_order(symbol, action, quantity, price, **kwargs)
            if order is None:
                raise Exception("IB 返回了 None 订单。")
            
            if self.send_kafka and self.producer:
                # 发送订单信息到 Kafka
                order_info = {
                    "event": "order_placed",
                    "order_id": order.order_id,
                    "perm_id": order.perm_id,
                    "symbol": order.symbol,
                    "action": order.action,
                    "order_type": order.order_type,
                    "price": order.lmt_price,
                    "size": order.shares,
                    "apply_time": order.apply_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "order_ref": order.order_ref
                }
                await self.producer.send_message(ORDER_TOPIC, order_info)
            # --- 存储映射关系 ---
            self.active_orders[order.order_id] = ActiveOrderRecord(order, callback)
            logger.info(f"业务订单 {order.order_id} 已提交到 IB, 分配的 api_order_id 为 {order.order_id}")
            return order
        except Exception as e:
            logger.error(f"IBWrapper: 提交订单 {order.order_id} 失败 - {e}")
            order.status = OrderStatus.Rejected
            # await callback(order) # 通知策略提交失败
            return None
      
    async def cancel_order(self, order_id: int) -> bool:
        # 实现取消逻辑...
        if order_id not in self.active_orders:
            # logger.warning(f"无法取消业务订单 {order_id}，因为没有有效的 api_order_id。")
            return False

        order = self.active_orders[order_id].grid_order
        ret = self.ib.cancel_order(order)
        if ret:
            # self.active_orders.pop(order_id, None)
            logger.info(f"业务订单 {order_id} 取消请求已发送。")
            
            if self.send_kafka and self.producer:
                # 发送订单信息到 Kafka
                order_info = {
                    "event": "cancel_sumbit",
                    "order_id": order.order_id,
                    "perm_id": order.perm_id,
                    "symbol": order.symbol,
                    "action": order.action,
                    "order_type": order.order_type,
                    "price": order.lmt_price,
                    "size": order.shares,
                    "apply_time": order.apply_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "order_ref": order.order_ref
                }
                await self.producer.send_message(ORDER_TOPIC, order_info)
            return True
        logger.info(f"业务订单 {order_id} 取消请求失败。")
        return ret
