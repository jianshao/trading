import backtrader as bt
import numpy as np


class MarketTrendScore(bt.Indicator):
    """
    0 = 强震荡
    100 = 强趋势
    """
    lines = ("score",)

    params = dict(
        # ---------- VXN ----------
        vxn_low=15.0,
        vxn_high=30.0,

        # ---------- ADX ----------
        adx_low=15.0,
        adx_high=40.0,

        # ---------- CHOP ----------
        chop_low=38.0,
        chop_high=62.0,

        # ---------- ATR% ----------
        atr_low=0.015,
        atr_high=0.05,

        # ---------- MACD ----------
        macd_min=0.001,
        macd_max=0.01,

        # ---------- SLOPE ----------
        slope_period=14,
        slope_low=0.0005,
        slope_high=0.005,

        # ---------- WEIGHTS ----------
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

    # ---------- score functions ----------
    @staticmethod
    def _linear_score(val, low, high, reverse=False):
        if val <= low:
            return 100.0 if reverse else 0.0
        if val >= high:
            return 0.0 if reverse else 100.0
        ratio = (val - low) / (high - low)
        return 100.0 * (1 - ratio if reverse else ratio)

    def next(self):
        close = self.data.close[0]

        if len(self.data) < self.p.slope_period:
            self.lines.score[0] = 50.0
            return

        # slope
        y = np.array(self.data.close.get(size=self.p.slope_period))
        x = np.arange(len(y))
        slope = np.polyfit(x, y, 1)[0]
        score_slope = abs(slope / close)

        score_adx = self._linear_score(
            self.adx[0], self.p.adx_low, self.p.adx_high
        )

        score_chop = self._linear_score(
            self.chop[0], self.p.chop_low, self.p.chop_high, reverse=True
        )

        score_vxn = self._linear_score(
            self.data.vxn[0], self.p.vxn_low, self.p.vxn_high, reverse=True
        )

        atr_pct = self.atr[0] / close
        score_atr = self._linear_score(
            atr_pct, self.p.atr_low, self.p.atr_high
        )

        macd_diff = abs(self.macd.macd[0] - self.macd.signal[0]) / close
        score_macd = self._linear_score(
            macd_diff, self.p.macd_min, self.p.macd_max
        )

        self.lines.score[0] = (
            score_adx * self.p.w_adx +
            score_chop * self.p.w_chop +
            score_vxn * self.p.w_vxn +
            score_atr * self.p.w_atr +
            score_slope * self.p.w_slope +
            score_macd * self.p.w_macd
        )
