from typing import List
import backtrader as bt
import pandas as pd
import matplotlib.pyplot as plt
import datetime
import numpy as np

# --- 辅助分析器：用于记录每日净值 ---
class DailyStatsAnalyzer(bt.Analyzer):
    def __init__(self):
        self.dates = []
        self.values = []
        
    def next(self):
        self.dates.append(self.strategy.data.datetime.date(0))
        self.values.append(self.strategy.broker.get_value())

    def get_analysis(self):
        return {
          'dates': self.dates, 
          'values': self.values,
          'final_value': self.values[-1]
        }

# --- 核心引擎封装 ---
class OptimizationEngine:
    def __init__(self, data_feed: List[pd.DataFrame], cash=100000.0, commission=0.0005):
        """
        :param data_feed: Backtrader的数据源对象
        :param cash: 初始资金
        :param commission: 佣金费率
        """
        self.data_feed = data_feed
        self.cash = cash
        self.commission = commission
        self.results = None
        self.df_results = None

    def run(self, strategy_cls, **kwargs):
        """
        执行参数优化
        :param strategy_cls: 策略类
        :param kwargs: 优化参数，例如 grid_gap=range(1, 10)
        """
        cerebro = bt.Cerebro(
            stdstats=True,      # 禁用observer，速度提升3-5倍
            optdatas=True,       # 数据预加载优化
            optreturn=True,      # 减少返回数据量
            maxcpus=4,           # 4核并行（根据CPU调整）
        )
        data = bt.feeds.PandasData(dataname=self.data_feed[0], name="data")
        cerebro.adddata(data)
        data = bt.feeds.PandasData(dataname=self.data_feed[1], name="data_DAILY")
        cerebro.adddata(data)
        data = bt.feeds.PandasData(dataname=self.data_feed[2], name="VXN")
        cerebro.adddata(data)
        cerebro.broker.setcash(self.cash)
        cerebro.broker.setcommission(commission=self.commission)

        # 1. 设置优化参数
        cerebro.optstrategy(strategy_cls, **kwargs)

        # 2. 添加通用分析器
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, compression=1, riskfreerate=0.0)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
        cerebro.addanalyzer(DailyStatsAnalyzer, _name='daily_stats')

        print(f"正在执行回测优化，参数空间: {kwargs} ...")
        self.results = cerebro.run(maxcpus=1, cheat_on_open=True) # maxcpus=1 方便调试，生产环境可去掉
        print("回测完成，正在解析数据...")
        
        self.df_results = self._parse_results()
        return self.df_results

    def _parse_results(self):
        """内部方法：将Backtrader复杂的嵌套结果解析为扁平的DataFrame"""
        final_data = []

        for run in self.results:
            strat = run[0]
            
            # --- 修复点 1: 兼容参数读取 ---
            # 如果是 OptReturn，params 是字典；如果是策略实例，params 是对象
            if hasattr(strat.params, '_getkeys'): 
                # 标准策略对象
                keys_map = {
                  'symbol': "Symbol",
                  'retention_fund_ratio': "FundRatio",
                }
                params = {keys_map[key]: getattr(strat.params, key) for key in list(keys_map.keys())}
            else:
                # OptReturn 对象 (params 已经是字典)
                params = strat.params
            
            # 提取分析器数据
            dd_stats = strat.analyzers.drawdown.get_analysis()
            sharpe_stats = strat.analyzers.sharpe.get_analysis()
            trade_stats = strat.analyzers.trades.get_analysis()
            daily_stats = strat.analyzers.daily_stats.get_analysis()
            
            # --- 修复点 2: 从 Analyzer 获取资金，而不是 strat.broker ---
            # 即使 Analyzer 没改，也可以用 values[-1] 取最后一天资金
            # 但最好是用我们在 Analyzer 里新加的 'final_value'
            final_value = daily_stats.get('final_value', daily_stats['values'][-1])
            
            # 计算利润
            profit = final_value - self.cash
            return_pct = profit / self.cash * 100
            
            # 处理夏普比率 (可能为 None)
            sharpe = sharpe_stats.get('sharperatio', 0.0)
            if sharpe is None: sharpe = 0.0

            # 处理交易统计
            total_trades = trade_stats.get('total', {}).get('closed', 0)
            won_trades = trade_stats.get('won', {}).get('total', 0)
            win_rate = (won_trades / total_trades * 100) if total_trades > 0 else 0.0
            
            # 盈亏比计算 (避免除零错误)
            gross_won = trade_stats.get('won', {}).get('pnl', {}).get('total', 0.0)
            gross_lost = abs(trade_stats.get('lost', {}).get('pnl', {}).get('total', 0.0))
            profit_factor = (gross_won / gross_lost) if gross_lost > 0 else 0.0

            maxdown = dd_stats.get('max', {}).get('drawdown', 0.0)
            # 汇总一行数据
            record = {
                **params, # 展开参数
                'FinalValue': final_value,
                'Profit': profit,
                'Return(%)': return_pct,
                'CalmarRatio': round(profit/maxdown, 2),
                'MaxDrawdown(%)': maxdown,
                'SharpeRatio': round(sharpe, 3),
                # 'WinRate(%)': win_rate,
                # 'ProfitFactor': profit_factor,
                # 'TotalTrades': total_trades,
                '_daily_dates': daily_stats['dates'],  # 隐藏字段，用于绘图
                '_daily_values': daily_stats['values'] # 隐藏字段，用于绘图
            }
            final_data.append(record)

        df = pd.DataFrame(final_data)
        # 默认按利润倒序排列
        return df.sort_values(by='CalmarRatio', ascending=False).reset_index(drop=True)

    def plot_top_results(self, sort_by='Profit', top_n=3):
        """
        可视化方法：绘制Top N的资金曲线和指标对比
        """
        if self.df_results is None or self.df_results.empty:
            print("没有结果数据，请先运行 run()")
            return

        # 重新排序
        df = self.df_results.sort_values(by=sort_by, ascending=False).head(top_n)
        
        # 创建画布
        fig, axes = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [2, 1]})
        
        # 图1: 资金曲线
        ax_curve = axes[0]
        ax_curve.set_title(f"Equity Curves (Top {top_n} by {sort_by})")
        ax_curve.set_ylabel("Account Value")
        
        for idx, row in df.iterrows():
            # 构建标签，显示关键参数和指标
            # 假设第一个key是优化参数
            param_keys = [k for k in row.keys() if k not in ['FinalValue', 'Profit', 'Return(%)', 'MaxDrawdown(%)', 'SharpeRatio', 'WinRate(%)', 'ProfitFactor', 'TotalTrades', '_daily_dates', '_daily_values']]
            param_str = ", ".join([f"{k}={row[k]}" for k in param_keys])
            
            label = f"[{param_str}] PnL:{row['Profit']:.0f} | DD:{row['MaxDrawdown(%)']:.2f}%"
            ax_curve.plot(row['_daily_dates'], row['_daily_values'], label=label, linewidth=2)
            
        ax_curve.legend()
        ax_curve.grid(True, linestyle='--', alpha=0.5)

        # 图2: 核心指标对比表格
        ax_table = axes[1]
        ax_table.axis('off') # 不显示坐标轴
        ax_table.set_title("Performance Metrics Detail")
        
        # 准备表格数据 (排除绘图用的隐藏数据)
        display_cols = [c for c in df.columns if not c.startswith('_')]
        table_data = df[display_cols].round(2).values
        col_labels = display_cols
        
        # 绘制表格
        table = ax_table.table(cellText=table_data, colLabels=col_labels, 
                               cellLoc='center', loc='center', bbox=[0, 0, 1, 1])
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        
        plt.tight_layout()
        plt.show()
        
    def run_is_oos_validation(self, strategy_cls, data_feed, 
                              split_date, opt_params, target_metric='Sharpe Ratio'):
      """
      执行样本内(IS)优化，并在样本外(OOS)进行验证
      
      :param engine: OptimizationEngine 实例
      :param strategy_cls: 策略类
      :param data_feed: 完整的数据源
      :param split_date: 切分日 (字符串 '2022-01-01')
      :param opt_params: 优化参数字典
      :param target_metric: 选优指标
      """
      
      # 将切分日期转换为 datetime
      split_dt = datetime.datetime.strptime(split_date, "%Y-%m-%d")
      start_dt = data_feed.p.fromdate
      end_dt = data_feed.p.todate

      print(f"\n=== 开始 IS/OOS 验证 ===")
      print(f"样本内 (IS) : {start_dt.date()} -> {split_dt.date()}")
      print(f"样本外 (OOS): {split_dt.date()} -> {end_dt.date()}")

      # ---------------------------
      # 第一步：样本内 (IS) 优化
      # ---------------------------
      # 创建 IS 数据源
      data_is = bt.feeds.YahooFinanceData(
          dataname=data_feed.p.dataname, 
          fromdate=start_dt, 
          todate=split_dt, 
          buffered=True
      )
      
      self.data_feed = data_is # 切换引擎数据源
      df_is = self.run(strategy_cls, **opt_params)
      
      # 选取最佳参数
      best_row = df_is.sort_values(by=target_metric, ascending=False).iloc[0]
      
      # 提取最佳参数 (排除非参数的列)
      ignore_cols = ['Final Value', 'Profit', 'Return %', 'Max Drawdown %', 
                    'Sharpe Ratio', 'Win Rate %', 'Profit Factor', 'Total Trades', 
                    '_daily_dates', '_daily_values']
      best_params = {k: v for k, v in best_row.items() if k not in ignore_cols}
      
      print(f"\nIS 最佳参数组合: {best_params}")
      print(f"IS {target_metric}: {best_row[target_metric]:.4f}")
      print(f"IS 净利润: {best_row['Profit']:.2f}")

      # ---------------------------
      # 第二步：样本外 (OOS) 回测
      # ---------------------------
      # 创建 OOS 数据源
      data_oos = bt.feeds.YahooFinanceData(
          dataname=data_feed.p.dataname, 
          fromdate=split_dt, 
          todate=end_dt, 
          buffered=True
      )
      
      # 这里我们只跑一组参数 (不需要 optstrategy，只需要 run)
      # 为了复用 engine 的逻辑，我们也可以传入列表长度为1的参数
      single_param_dict = {k: [v] for k, v in best_params.items()}
      
      self.data_feed = data_oos # 切换数据源
      df_oos = self.run(strategy_cls, **single_param_dict)
      
      oos_result = df_oos.iloc[0]
      print(f"\nOOS 验证结果:")
      print(f"OOS {target_metric}: {oos_result[target_metric]:.4f}")
      print(f"OOS 净利润: {oos_result['Profit']:.2f}")

      # ---------------------------
      # 第三步：可视化对比
      # ---------------------------
      self.plot_is_oos_comparison(best_row, oos_result, split_date)


    def plot_is_oos_comparison(self, is_row, oos_row, split_date):
        """画图对比 IS 和 OOS 的资金曲线"""
        plt.figure(figsize=(12, 6))
        
        # 绘制 IS 曲线
        plt.plot(is_row['_daily_dates'], is_row['_daily_values'], 
                label='In-Sample (Training)', color='blue')
        
        # 绘制 OOS 曲线
        plt.plot(oos_row['_daily_dates'], oos_row['_daily_values'], 
                label='Out-of-Sample (Testing)', color='orange')
        
        # 画一条分割线
        plt.axvline(x=datetime.datetime.strptime(split_date, "%Y-%m-%d").date(), 
                    color='red', linestyle='--', label='Split Date')
        
        plt.title(f"IS vs OOS Performance Check (Params: {is_row.name if isinstance(is_row.name, str) else ''})")
        plt.ylabel("Account Value")
        plt.legend()
        plt.grid(True)
        plt.show()