# logger_manager.py
import inspect
import os
import logging
import structlog
from logging.handlers import TimedRotatingFileHandler
import gzip
import shutil
import json
import datetime
import atexit
import threading
import queue
import time
from clickhouse_driver import Client
from zoneinfo import ZoneInfo

from data import config
from common.real_time_data_processer import RealTimeDataProcessor


class GZipTimedRotatingFileHandler(TimedRotatingFileHandler):
    """支持按天切割并压缩日志文件（.gz）"""
    def doRollover(self):
        super().doRollover()
        if self.backupCount > 0:
            log_dir, base_filename = os.path.split(self.baseFilename)
            for filename in os.listdir(log_dir):
                # 匹配切割产生的文件（不含已压缩的和原文件）
                if filename.startswith(base_filename) and not filename.endswith(".gz") and filename != base_filename:
                    filepath = os.path.join(log_dir, filename)
                    try:
                        with open(filepath, 'rb') as f_in, gzip.open(filepath + ".gz", 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)

                        # 删除原文件
                        os.remove(filename)
                    except Exception:
                        # 压缩失败时，不抛出以免影响 rollover
                        pass

# --- 【新增】自定义 Structlog 处理器 ---
def add_dynamic_timestamp(_, __, event_dict):
    """
    自定义时间戳处理器：
    优先从 LoggerManager.data_processor 获取回测时间。
    如果获取失败或未初始化，回退到系统当前时间。
    """
    # 默认使用系统时间 (带时区)
    current_dt = datetime.datetime.now(ZoneInfo(config.time_zone))

    if LoggerManager.data_processor:
        try:
            # 尝试调用 get_current_time
            # 注意：这里假设 get_current_time 是同步方法，或者返回的是 datetime 对象
            t = LoggerManager.data_processor.get_current_time()
            
            # 【关键检查】如果 data_processor 是异步接口，t 会是一个协程对象
            if inspect.iscoroutine(t):
                # 日志是同步的，无法 await。必须销毁协程防止报警，并回退到系统时间
                t.close() 
                # 警告：建议修改 DataProcessor 增加同步获取时间的方法
            elif isinstance(t, datetime.datetime):
                # 如果返回的是 naive time (无时区)，补全时区
                if t.tzinfo is None:
                    t = t.replace(tzinfo=ZoneInfo(config.time_zone))
                current_dt = t
        except Exception:
            # 获取时间出错时，忽略错误，使用系统时间，防止日志系统崩溃
            pass

    # structlog 标准格式：iso
    event_dict["timestamp"] = current_dt.isoformat()
    return event_dict

class LoggerManager:
    """
    多文件 JSON 日志管理器 + 异步批量写 ClickHouse
    使用：
      LoggerManager.init(log_configs, clickhouse_config)
      LoggerManager.log("order", level="INFO", event="order_created", symbol="AAPL", price=123.4)
      # 退出时会自动 flush/close
    """
    _loggers = {}
    _ch_client = None
    _initialized = False
    _queue = queue.Queue(maxsize=10000)  # 可配置
    _worker_thread = None
    _stop_event = threading.Event()
    _batch_size = 50
    _flush_interval = 3.0  # seconds

    @classmethod
    def init(cls, log_configs, level: int=logging.INFO, clickhouse_config=None,
             batch_size: int = 50, flush_interval: float = 3.0, queue_maxsize: int = 10000, realtime_data_processor: RealTimeDataProcessor = None):
        """
        初始化
        :param log_configs: dict, e.g. {"order": "logs/order.log", "error": "logs/error.log"}
        :param clickhouse_config: dict for clickhouse_driver.Client(...) or None
        :param batch_size: CH 批量写入大小
        :param flush_interval: 后台线程最长刷新间隔（秒）
        :param queue_maxsize: 日志队列上限
        """
        if cls._initialized:
            return

        cls._batch_size = batch_size
        cls._flush_interval = flush_interval
        cls._queue = queue.Queue(maxsize=queue_maxsize)
        cls._stop_event.clear()
        cls.data_processor = realtime_data_processor

        # if clickhouse_config:
        #     cls._ch_client = Client(**clickhouse_config)

        # structlog 基本配置：TimeStamper + JSONRenderer（文件端）
        structlog.configure(
            processors=[
                # structlog.processors.TimeStamper(fmt="iso", utc=True),
                add_dynamic_timestamp,
                structlog.processors.JSONRenderer(sort_keys=False, ensure_ascii=False),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # 创建多个 logger，每个 logger 写到不同文件
        for name, filepath in log_configs.items():
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            handler = GZipTimedRotatingFileHandler(
                filepath, when="midnight", interval=1, backupCount=7, encoding="utf-8", atTime=datetime.time(5, 0, 0)
            )
            handler.setFormatter(logging.Formatter('%(message)s'))
            std_logger = logging.getLogger(name)
            std_logger.setLevel(level)
            # 避免重复添加 handler（多次 init 的情况）
            if not any(getattr(h, "baseFilename", None) == getattr(handler, "baseFilename", None)
                       for h in std_logger.handlers):
                std_logger.addHandler(handler)

            # wrap_logger 返回 BoundLogger（调用时需要 event positional）
            cls._loggers[name] = structlog.wrap_logger(std_logger)

        # 启动后台线程（如果配置了 clickhouse）
        # if cls._ch_client:
        #     cls._worker_thread = threading.Thread(target=cls._worker, daemon=True)
        #     cls._worker_thread.start()

        # atexit.register(cls.close)
        cls._initialized = True

    @classmethod
    def log(cls, logger_name, level="INFO", event: str = None, **kwargs):
        """
        写日志（主线程）
        :param logger_name: 注册时的名字，如 "order"
        :param level: DEBUG/INFO/WARNING/ERROR/CRITICAL
        :param event: 可选，日志事件的短文本（structlog 的 event 参数）；若为 None，会尝试从 kwargs 中取 'event' 或 'msg'，否则用 'log'
        :param kwargs: 结构化字段，会写到 JSON 中
        """
        if not cls._initialized:
            raise RuntimeError("LoggerManager not initialized. Call LoggerManager.init(...) first.")
        if logger_name not in cls._loggers:
            raise ValueError(f"Logger '{logger_name}' not initialized")

        # 先准备 event
        event_val = event or kwargs.pop("event", None) or kwargs.pop("msg", None) or "log"

        # structlog 的 BoundLogger 要求第一个位置参数是 event
        logger = cls._loggers[logger_name]
        level = (level or "INFO").upper()

        # call the logger with event as first arg, rest as kw
        log_method = getattr(logger, level.lower(), logger.info)
        try:
            # include timestamp only for CH; structlog will add its own TimeStamper for file
            data_copy = dict(kwargs)  # shallow copy
            log_method(event_val, **data_copy)
        except Exception:
            # 为了不影响主流程，捕获异常并输出到 stderr
            logging.getLogger().exception("Failed to write log to local logger")

        # enqueue for ClickHouse (异步批量写)
        if cls._ch_client:
            timestamp = cls.data_processor.get_current_time().strftime("%Y-%m-%d %H:%M:%S")
            item = (
                timestamp,
                logger_name,
                level,
                json.dumps(data_copy, ensure_ascii=False)
            )
            try:
                cls._queue.put_nowait(item)
            except queue.Full:
                # 丢弃或可扩展为统计丢弃计数
                logging.getLogger().warning("Log queue full, dropping a log entry")

    @classmethod
    def Debug(cls, logger_name, event: str = None, **kwargs):
        cls.log(logger_name, level="DEBUG", event=event, **kwargs)
        
    @classmethod
    def Info(cls, logger_name, event: str = None, **kwargs):
        cls.log(logger_name, level="INFO", event=event, **kwargs)
        
    @classmethod
    def Warning(cls, logger_name, event: str = None, **kwargs):
        cls.log(logger_name, level="WARNING", event=event, **kwargs)
    
    @classmethod
    def Error(cls, logger_name, event: str = None, **kwargs):
        cls.log(logger_name, level="ERROR", event=event, **kwargs)
        
    @classmethod
    def _worker(cls):
        """后台线程，从队列批量写 ClickHouse"""
        buffer = []
        last_flush = time.time()
        while not cls._stop_event.is_set() or not cls._queue.empty():
            try:
                item = cls._queue.get(timeout=0.5)
                buffer.append(item)
            except queue.Empty:
                pass

            now = time.time()
            if buffer and (len(buffer) >= cls._batch_size or now - last_flush >= cls._flush_interval):
                try:
                    cls._ch_client.execute(
                        "INSERT INTO logs (timestamp, logger, level, data) VALUES",
                        buffer
                    )
                    buffer.clear()
                    last_flush = now
                except Exception as e:
                    # 写失败：休眠并重试；不要丢掉 buffer（以免数据丢失），但要避免无限快重试
                    logging.getLogger().exception("ClickHouse insert failed: %s", e)
                    time.sleep(1)

        # 退出前 flush 剩余
        if buffer:
            try:
                cls._ch_client.execute(
                    "INSERT INTO logs (timestamp, logger, level, data) VALUES",
                    buffer
                )
            except Exception:
                logging.getLogger().exception("ClickHouse final flush failed")

    @classmethod
    def close(cls, timeout=5):
        """优雅关闭：停止后台线程并关闭 handler / CH"""
        if not cls._initialized:
            return

        # 停止 worker
        if cls._worker_thread and cls._worker_thread.is_alive():
            cls._stop_event.set()
            cls._worker_thread.join(timeout=timeout)

        # 关闭 ClickHouse 客户端
        if cls._ch_client:
            try:
                cls._ch_client.disconnect()
            except Exception:
                pass
            cls._ch_client = None

        # 关闭所有 handlers
        for bound_logger in cls._loggers.values():
            # bound_logger 是 structlog 的 BoundLogger，底层 stdlib logger 在 bound_logger._logger
            std_logger = getattr(bound_logger, "_logger", None)
            if std_logger:
                for h in list(std_logger.handlers):
                    try:
                        h.flush()
                        h.close()
                        std_logger.removeHandler(h)
                    except Exception:
                        pass

        cls._loggers.clear()
        cls._initialized = False


# 简单使用示例（放在 main）
if __name__ == "__main__":
    configs = {
        "order": "logs/order.log",
        "error": "logs/error.log",
    }
    ch_conf = {"host": "127.0.0.1", "database": "default"}  # adjust as needed

    LoggerManager.init(configs, clickhouse_config=ch_conf)

    # 推荐带 event 字段
    LoggerManager.log("order", level="INFO", event="order_created", symbol="AAPL", price=150.5, qty=10)
    LoggerManager.log("error", level="ERROR", event="trade_error", reason="timeout", code=500)

    # 程序退出时 atexit 会自动调用 close()
