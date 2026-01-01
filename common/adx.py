import pandas as pd
import numpy as np

def calc_adx(
    df: pd.DataFrame,
    period: int = 14,
    high_col: str = "High",
    low_col: str = "Low",
    close_col: str = "Close"
) -> pd.DataFrame:
    """
    纯 pandas 实现 ADX（Welles Wilder 原始算法）
    返回 df 副本，包含：
      +DI, -DI, DX, ADX
    """

    df = df.copy()

    high = df[high_col]
    low = df[low_col]
    close = df[close_col]

    # ---------- 1. 计算 True Range ----------
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    # ---------- 2. 计算 Directional Movement ----------
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    # ---------- 3. Wilder 平滑 ----------
    def wilder_smooth(series, period):
        """
        Wilder smoothing:
        S_t = S_{t-1} - (S_{t-1} / period) + value_t
        """
        result = series.copy()
        result.iloc[:period] = np.nan

        initial = series.iloc[1:period+1].sum()
        result.iloc[period] = initial

        for i in range(period + 1, len(series)):
            result.iloc[i] = (
                result.iloc[i - 1]
                - (result.iloc[i - 1] / period)
                + series.iloc[i]
            )
        return result

    tr_smooth = wilder_smooth(tr, period)
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)

    # ---------- 4. 计算 DI ----------
    plus_di = 100 * (plus_dm_smooth / tr_smooth)
    minus_di = 100 * (minus_dm_smooth / tr_smooth)

    # ---------- 5. DX ----------
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    # ---------- 6. ADX ----------
    adx = wilder_smooth(dx, period)

    # ---------- 7. 写回 ----------
    df["+DI"] = plus_di
    df["-DI"] = minus_di
    df["DX"] = dx
    df["ADX"] = adx

    return df
