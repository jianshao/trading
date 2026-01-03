
import backtrader as bt

# 下跌行情识别并标记
class DowntrendBand(bt.Indicator):
    params = dict(
        downtrend_color='black',
        default_barup_color='blue', # 改为灰色
        default_bardown_color='blue',
        ma_period=50,
        adx_period=14,
        adx_threshold=30,
    )
    lines = ('band',)
    plotinfo = dict(subplot=False)

    plotlines = dict(
        band=dict(
            linewidth=10,
            alpha=0.4,
            drawstyle='steps-pre',
        )
    )

    def __init__(self):
        self.ma5 = bt.indicators.SMA(self.data.close, period=5)
        self.ma7 = bt.indicators.SMA(self.data.close, period=6)
        self.ma10 = bt.indicators.SMA(self.data.close, period=10)
        self.ma20 = bt.indicators.SMA(self.data.close, period=14)
        self.macd = bt.indicators.MACD(self.data)
        self.adx = bt.indicators.ADX(self.data, period=self.p.adx_period)

    def next(self):
        is_downtrend = False
        if self.adx[0] > self.p.adx_threshold and self.ma5[0] < self.ma7[0]:
            is_downtrend = True
        # if self.macd.macd < self.macd.signal:
        #     is_downtrend = True
        if self.ma5[0] < self.ma20[0]:
            is_downtrend = True
            
        if is_downtrend:
            self.plotlines.band.color = 'lightcoral'
            self.lines.band[0] = self.data.low[0] * 0.97
        else:
            self.lines.band[0] = float('nan')

class GateVisualStrategy(bt.Strategy):
    def __init__(self):
        # --- FIX: Instantiate the ACTUAL colorizer indicator directly here ---
        self.colorizer = DowntrendBand(self.data)
        
        self.ma5 = bt.indicators.SMA(self.data.close, period=5)
        self.ma7 = bt.indicators.SMA(self.data.close, period=7)
        self.ma10 = bt.indicators.SMA(self.data.close, period=10)
        self.ma20 = bt.indicators.SMA(self.data.close, period=30)
        self.macd = bt.indicators.MACD(self.data)
        self.adx = bt.indicators.ADX(self.data, period=14)
    
    def next(self):
        # next() 方法现在可以保持为空，因为 colorizer 会自动工作
        pass