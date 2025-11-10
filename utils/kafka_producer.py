import asyncio
import json
import time
from datetime import datetime
from confluent_kafka import Producer


class KafkaProducerService:
    def __init__(self,
                 bootstrap_servers: str = "kafka:9093",
                 heartbeat_topic: str = "system_heartbeat",
                 heartbeat_interval: int = 300):
        """
        :param bootstrap_servers: Kafka服务器地址
        :param heartbeat_topic: 心跳消息Topic
        :param heartbeat_interval: 心跳间隔（秒）
        """
        self.heartbeat_topic = heartbeat_topic
        self.heartbeat_interval = heartbeat_interval
        self._last_send_time = time.time()

        self._producer = Producer({
            'bootstrap.servers': bootstrap_servers,
            'socket.keepalive.enable': True,
            'connections.max.idle.ms': 1800000,
            'metadata.max.age.ms': 300000,
            'reconnect.backoff.max.ms': 10000,
            'message.timeout.ms': 30000,
        })

        self._queue = asyncio.Queue()
        self._running = False

    async def start(self):
        """启动后台协程任务"""
        self._running = True
        # asyncio.create_task(self._send_loop())
        await self._send_loop()

    async def _send_loop(self):
        """持续检查队列并发送消息；若空闲则发送心跳"""
        while self._running:
            try:
                # 如果在 heartbeat_interval 内有消息，就发消息；否则发心跳
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=self.heartbeat_interval)
                    await self._send(msg.topic, msg.data)
                except asyncio.TimeoutError:
                    await self._send_heartbeat()

                # 触发消息投递报告
                self._producer.poll(0)
            except Exception as e:
                print(f"[KafkaProducer] Error in send loop: {e}")
                await asyncio.sleep(5)

    async def _send(self, topic, data):
        """底层发送方法"""
        try:
            self._producer.produce(
                topic=topic,
                value=json.dumps(data).encode("utf-8"),
            )
            self._producer.flush(0.1)
            self._last_send_time = time.time()
        except BufferError:
            # 缓冲区满时等待重试
            self._producer.poll(1)
            await self._send(topic, data)
        except Exception as e:
            print(f"[KafkaProducer] Send failed: {e}")

    async def _send_heartbeat(self):
        """发送心跳消息"""
        heartbeat_msg = {
            "type": "heartbeat",
            "timestamp": datetime.utcnow().isoformat()
        }
        print(f"[KafkaProducer] Sending heartbeat at {heartbeat_msg['timestamp']}")
        await self._send(self.heartbeat_topic, heartbeat_msg)

    async def send_message(self, topic: str, data: dict):
        """供业务调用的消息发送接口"""
        await self._queue.put({"topic": topic, "data": data})

    async def stop(self):
        """优雅关闭"""
        self._running = False
        self._producer.flush()


# ===================== 示例用法 =====================
async def main():
    kafka_service = KafkaProducerService(
        bootstrap_servers="kafka:9093",
        heartbeat_topic="system_heartbeat",
        heartbeat_interval=120  # 每2分钟心跳
    )
    await kafka_service.start()

    # 模拟业务不断提交消息
    for i in range(3):
        msg = {
            "event": "trade_completed",
            "order_id": f"T{i+1}",
            "profit": round(10 + i * 0.5, 2),
            "timestamp": datetime.utcnow().isoformat()
        }
        await kafka_service.send_message(msg)
        await asyncio.sleep(10)

    # 等待一段时间看看心跳机制是否正常
    await asyncio.sleep(300)
    await kafka_service.stop()


if __name__ == "__main__":
    asyncio.run(main())
