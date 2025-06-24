import datetime
import logging

logger = logging.getLogger("orders")
def WriteFile(logger: logging.Logger, level: int, content: str):
  if level == logging.DEBUG:
    logger.debug(content)
  elif level == logging.INFO:
    logger.info(content)
  elif level == logging.WARNING:
    logger.warning(content)
  elif level == logging.ERROR:
    logger.error(content)
  elif level == logging.FATAL:
    logger.fatal(content)
  else:
    pass

def InitLogger():
  filename = "../logs/orders.log" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%M%D")
  handle = logging.FileHandler(filename=filename)
  handle.setLevel(logging.INFO)
  formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
  handle.setFormatter(formatter)
  logger.addHandler(handle)
  