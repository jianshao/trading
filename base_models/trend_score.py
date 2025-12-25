import backtrader as bt
import numpy as np

class MarketTrendScore(bt.Indicator):
    lines = ("score",)
    params = dict(
        # VXN
        vxn_low=15, vxn_high=30,
        # ADX
        adx_low=15, adx_high=40,
        # CHOP
        chop_low=38, chop_high=62,
        # ATR%
        atr_low=0.015, atr_high=0.05,
        # MACD
        macd_min=0.001, macd_max=0.01,
        # Slope
        slope_low=0.0005, slope_high=0.005,

        # Weights
        w_adx=0.25,
        w_chop=0.20,
        w_vxn=0.15,
        w_atr=0.15,
        w_slope=0.15,
        w_macd=0.10,
    )

    def __init__(self):
        self.adx = bt.ind.ADX(self.data)
        self.atr = bt.ind.ATR(self.data)
        self.macd = bt.ind.MACD(self.data)
        self.chop = bt.ind.Choppiness(self.data)

        self.slope_period = 14

    def next(self):
        close = self.data.close[0]

        # slope
        if len(self.data) < self.slope_period:
            self.lines.score[0] = 50
            return

        y = np.array(self.data.close.get(size=self.slope_period))
        x = np.arange(len(y))
        slope = np.polyfit(x, y, 1)[0]

        scores = {
            "adx": score_adx(self.adx[0], self.p.adx_low, self.p.adx_high),
            "chop": score_chop(self.chop[0], self.p.chop_low, self.p.chop_high),
            "vxn": score_vxn(self.data.vxn[0], self.p.vxn_low, self.p.vxn_high),
            "atr": score_atr_pct(self.atr[0], close, self.p.atr_low, self.p.atr_high),
            "slope": score_slope(slope, close, self.p.slope_low, self.p.slope_high),
            "macd": score_macd(
                self.macd.macd[0],
                self.macd.signal[0],
                close,
                self.p.macd_min,
                self.p.macd_max,
            ),
        }

        self.lines.score[0] = (
            scores["adx"] * self.p.w_adx +
            scores["chop"] * self.p.w_chop +
            scores["vxn"] * self.p.w_vxn +
            scores["atr"] * self.p.w_atr +
            scores["slope"] * self.p.w_slope +
            scores["macd"] * self.p.w_macd
        )
