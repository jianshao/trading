import argparse
import asyncio
from datetime import datetime, timedelta
import signal
from ib_insync import IB, Stock, Ticker
import logging

from apis.api import BaseAPI
from apis.ibkr import IBapi
from data import config
from strategy.strategy_engine import GridStrategyEngine
from common.kafka_producer import KafkaProducerService
from common.logger_manager import LoggerManager
from common import utils

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- 1. 业务逻辑 async 函数 (与之前相同，但增加了更精细的异常处理) ---

# 启动 Kafka 生产者的协程，需要单独的协程周期性发送消息保持kafka连接不中断
async def kafka_producer_run(producer: KafkaProducerService):
    LoggerManager.Info("app", "init", content=f"kafka producer start...")
    try:
        await producer.start()
    except Exception as e:
        LoggerManager.Error("app", "error", f"Kafka 生产者任务发生异常: {e}")
    except asyncio.CancelledError:
        LoggerManager.Info("app", event="stop", content=f"Kafka 生产者任务被取消，正在执行最后的清理工作...")
    finally:
        await producer.stop()
        LoggerManager.Info("app", "stop", content=f"Kafka 生产者任务已退出。")

# 策略管理任务
async def strategy_engine(ib: BaseAPI, producer: KafkaProducerService = None):
    """一个模拟的策略逻辑，它会定期运行直到被取消。"""
    LoggerManager.Info("app", "init", content=f"策略引擎任务已启动...")
    engine = GridStrategyEngine(ib, "data/real/strategies/", producer=producer)
    try:
        if not await engine.InitStrategy():
            LoggerManager.Error("app", "error", content=f"初始化策略引擎失败，退出策略任务。")
            return
        await engine.run()
        LoggerManager.Info("app", "stop", content=f"策略引擎任务已退出")
    except Exception as e:
        LoggerManager.Error("app", "error", content=f"策略任务发生异常: {e}")
    except asyncio.CancelledError:
        # 这是优雅退出的关键：捕获 CancelledError 并执行清理
        LoggerManager.Info("app", "stop", content=f"策略任务被取消，正在执行最后的清理工作...")
    finally:
        # disconnect触发的退出
        request_shutdown(signal.SIGTERM, None)
        await engine.DoStop()
        LoggerManager.Info("app", "stop", content=f"策略任务已退出。")


# 实时数据处理任务，包括ticker数据保存和技术指标计算
async def real_data_processing(ib: IB, producer: KafkaProducerService = None):
    """一个模拟的实时数据处理任务，同样支持取消。"""
    LoggerManager.Info("app", "init", content=f"实时数据处理任务已启动...")
    while True:
        try:
            # LoggerManager.Info("app", "init", content=f"实时数据处理任务正在运行...")
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            LoggerManager.Info("app", "stop", content=f"实时数据处理任务被取消，正在退出。")
            break
        except Exception as e:
            LoggerManager.Error("app", "error", content=f"实时数据处理任务发生错误: {e}")
            await asyncio.sleep(15)


# --- 3. 优雅退出处理机制 ---
async def shutdown(sig: signal.Signals, ib: IB, tasks: set):
    """
    一个更完整的关闭协程，现在它也负责断开 IB 连接。
    """
    LoggerManager.Info("app", "stop", content=f"接收到退出信号({sig.name}), 正在取消 {len(tasks)} 个后台任务...")
    
    # 1. 取消所有后台任务
    # 如果没有任务，就没必要取消
    if not tasks:
        return

    for task in tasks:
        task.cancel()
    
    # 2. 等待所有任务完成清理
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # 3. 在所有任务结束后，断开 IB 连接
    # 这是关键修改：将断开连接的操作移到这里，确保它在任务清理后、循环停止前执行
    if ib and ib.isConnected():
        LoggerManager.Info("app", "stop", content=f"所有任务已清理完毕，正在断开 IB 连接...")
        ib.disconnect()
        LoggerManager.Info("app", "stop", content=f"已断开 IB 连接。")


# --- 全局 ---
_SHUTDOWN_REQUESTED = False

def request_shutdown(sig, frame):
    """同步信号处理器，只设置一个标志。"""
    global _SHUTDOWN_REQUESTED
    if not _SHUTDOWN_REQUESTED:
        print(f"\n收到退出信号 {sig}，将在当前循环结束后关闭...")
        _SHUTDOWN_REQUESTED = True


async def main():

    log_configs = {
        "order": "logs/order.log",
        "app": "logs/app.log"
    }
    LoggerManager.init(log_configs)
    
    api = IBapi('127.0.0.1', 7496, 12)
    loop = asyncio.get_running_loop()
    background_tasks = set()

    # ---------------- 关键：修改信号处理器 ----------------
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    # ----------------------------------------------------

    parser = argparse.ArgumentParser(description='命令行参数')
    parser.add_argument('account', type=str, choices=["real", "paper"], help='启动账户类型, real-真实账户、 paper-模拟账户')
    parser.add_argument('--cancel', nargs='+', help='取消订单，all-取消所有订单')
    parser.add_argument('client', type=str, help='指定启动的客户端, tws-TWS, ib-IB GATEWAY')
    parser.add_argument('--check', action='store_true', help='检查今天是不是交易日')
    args = parser.parse_args()
    
    # 如果不是交易日直接退出
    if args.check and not utils.is_us_stock_open():
        LoggerManager.Info("app", "init", content=f"today is not trading day. exiting...")
        exit(0)
    
    try:
        LoggerManager.Info("app", "init", content=f"正在连接到 IB Gateway/TWS...")
        
        if not await api.connectAsync():
            LoggerManager.Error("app", "error", content=f"系统错误: 连接到 IBKR 失败，正在中止.....")
            return
        LoggerManager.Info("app", "init", content=f"连接成功！")

        # contract = Stock('NVDA', 'SMART', 'USD')
        # await ib.qualifyContractsAsync(contract)
        # ib.reqMktData(contract, '', False, False)

        LoggerManager.Info("app", "init", content=f"启动后台业务任务...")
        producer = KafkaProducerService(
            bootstrap_servers=config.kafka_brokers,
            heartbeat_topic="system_heartbeat",
            heartbeat_interval=120  # 每2分钟心跳
        )
        producer_task = loop.create_task(kafka_producer_run(producer))
        background_tasks.add(producer_task)
        
        strategy_task = loop.create_task(strategy_engine(api, producer=producer))
        background_tasks.add(strategy_task)
        
        data_task = loop.create_task(real_data_processing(api))
        background_tasks.add(data_task)

        LoggerManager.Info("app", "init", content=f"系统已启动并运行中。按 Ctrl+C 退出。")
        
        now = datetime.now()
        # 构造"明天早上7点30分"的 datetime 对象
        next_day_5am = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) + timedelta(hours=7, minutes=20)
        # 计算时间差
        delta = (next_day_5am - now).seconds
        # delta = 120
        while True: # 主循环现在只负责检查退出条件
            if _SHUTDOWN_REQUESTED:
                print("检测到关闭请求 (来自信号)...")
                break
            if delta <= 0:
                print("到达预设关闭时间...")
                break
            if not api.isConnected():
                print("IB 连接已断开...")
                break
            
            delta -= 1
            await asyncio.sleep(1) # 主循环的心跳


    except Exception as e:
        LoggerManager.Error("app", "error", content=f"主程序发生严重错误: {e}")
    finally:
        # 循环退出后，执行关闭逻辑
        await shutdown(signal.SIGTERM, api, background_tasks)
        
        # 最终的保障性断开连接（如果 shutdown 失败）
        if api and api.isConnected():
            api.disconnect()
        LoggerManager.Info("app", "stop", content=f"主协程 main() 已退出。")


# --- 5. 程序的唯一入口 ---
if __name__ == "__main__":
    # 现在我们可以安全地使用 asyncio.run()，因为它会等待 main() 协程完成后再关闭循环。
    # 而 main() 协程会等待我们手动触发的 shutdown_signal。
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        LoggerManager.Info("app", "stop", content=f"程序被强制退出。")
    finally:
        LoggerManager.Info("app", "stop", content=f"程序已关闭。")
