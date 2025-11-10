import os
import subprocess
import sys
import time
import socket
import psutil
from ib_insync import IB


if __name__ == '__main__': # Only adjust path if running as script directly
    # This assumes backtester_main.py is in a subdirectory (e.g., 'scripts')
    # and 'apis' and 'strategy' are in the parent directory of that subdirectory.
    # Adjust as per your actual project structure.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from apis.ibkr import IBapi


class IBClientManager:
    def __init__(self, client, account, timeout=60):
        """
        管理 TWS 或 IB Gateway 客户端的启动、连接、关闭

        :param client: "tws" 或 "gateway"
        :param tws_path: TWS 启动路径
        :param gateway_path: Gateway 启动路径
        :param host: API 主机地址
        :param port: API 端口 (TWS 默认 7497, Gateway 默认 4001)
        :param client_id: IB API clientId
        :param timeout: 启动 + 登录 最大等待时间 (秒)
        """
        print(f"client: {client}")
        print(f"account: {account}")
        self.client = client
        if client == "tws":
            self.client_path = "/Users/eric/Applications/Trader Workstation 10.40/Trader Workstation 10.40.app/Contents/MacOS/JavaApplicationStub"
            self.port = 7496 if account == "real" else 7497
        else:
            self.client_path = "/Users/eric/Applications/IB Gateway 10.30/IB Gateway 10.30.app/Contents/MacOS/JavaApplicationStub"
            self.port = 4001 if account == "real" else 4002
        self.timeout = timeout
        self.ib = IBapi(port=self.port)
        self.proc = None
      
    # ----------------- 对外接口 -----------------
    def start_and_connect(self) -> IB:
        """启动客户端并建立连接"""
        # 启动IB客户端
        # 存在几种情况：1.客户端未启动，2.客户端已启动但未登录，3.客户端已启动且已登陆。
        # 首先尝试建立连接，如果连接成功直接返回实例
        if self.ib.connect():
            return self.ib
        
        # 如果不能建立连接，说明未登录，直接执行关闭客户端。
        time.sleep(2)
        self.disconnect_and_quit()
        
        # 重启客户端，触发自动登录
        time.sleep(10)
        env = os.environ.copy()
        self.proc = subprocess.Popen([self.client_path, "start"],
                                     env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        print(f"🚀 启动客户端 ...")
        
        # 等待10s，再次尝试连接，如果连接失败就退出；如果成功则返回实例
        time.sleep(30)
        if self.ib.connect():
            print(f"✅ 已建立连接。")
            return self.ib
        return None
        
    def disconnect(self):
        """断开 API 连接"""
        if self.ib.isConnected():
            self.ib.disconnect()
            print("🔌 已断开 API 连接")

    def stop_client(self):
        """关闭客户端进程"""
        if self.proc:
            print(f"🛑 关闭 {self.client.upper()} (pid={self.proc.pid})")
            self.proc.terminate()
        # print(f"🛑 新开启的{self.client}已退出.")

    def disconnect_and_quit(self):
        """断开 API 并关闭客户端"""
        self.disconnect()
        self.stop_client()


# ----------------- 示例 -----------------
if __name__ == "__main__":
    manager = IBClientManager(client="tws", client_id=123)

    ib = manager.start_and_connect()
    # 这里可以直接用 ib.reqMktData() 等方法

    # 断开 API 连接
    manager.disconnect()

    # 彻底关闭客户端
    manager.stop_client()
