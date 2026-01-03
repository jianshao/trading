import pandas as pd
import numpy as np

def calc_adx(
    data_df: pd.DataFrame,
    period: int = 14
) -> pd.DataFrame:
    """
    Manually calculates the Average Directional Index (ADX), +DI, and -DI
    for a given DataFrame, without using pandas_ta.

    Args:
        data_df (pd.DataFrame): DataFrame with 'High', 'Low', 'Close' columns.
                                Index should be a DatetimeIndex.
        period (int): The period over which to calculate the ADX. Default is 14.

    Returns:
        pd.DataFrame: A DataFrame containing 'ADX_p', 'DMP_p', 'DMN_p' columns
                      (where p is the period), or None if calculation fails.
    """
    if not isinstance(data_df, pd.DataFrame) or data_df.empty:
        print("calculate_adx_manual: Input DataFrame is empty or invalid.")
        return None
        
    required_cols = ['High', 'Low', 'Close']
    if not all(col in data_df.columns for col in required_cols):
        print(f"calculate_adx_manual: DataFrame must contain {required_cols} columns.")
        return None
    
    df = data_df.copy() # Work on a copy to avoid modifying the original DataFrame

    # 1. Calculate Directional Movement (+DM, -DM) and True Range (TR)
    df['High-Low'] = df['High'] - df['Low']
    df['High-PrevClose'] = abs(df['High'] - df['Close'].shift(1))
    df['Low-PrevClose'] = abs(df['Low'] - df['Close'].shift(1))

    df['TR'] = df[['High-Low', 'High-PrevClose', 'Low-PrevClose']].max(axis=1, skipna=True)

    df['UpMove'] = df['High'] - df['High'].shift(1)
    df['DownMove'] = df['Low'].shift(1) - df['Low']

    df['+DM'] = np.where((df['UpMove'] > df['DownMove']) & (df['UpMove'] > 0), df['UpMove'], 0)
    df['-DM'] = np.where((df['DownMove'] > df['UpMove']) & (df['DownMove'] > 0), df['DownMove'], 0)

    # 2. Calculate Smoothed +DM, -DM, and TR using Wilder's Smoothing
    # Wilder's Smoothing is equivalent to an EMA with alpha = 1 / period.
    # We can use pandas' ewm method with alpha=1/period and adjust=False.
    alpha = 1 / period
    
    # Calculate initial SMA for the first 'period' values
    df['+DI_smooth'] = df['+DM'].rolling(window=period).sum()
    df['-DI_smooth'] = df['-DM'].rolling(window=period).sum()
    df['TR_smooth'] = df['TR'].rolling(window=period).sum()

    # Apply Wilder's smoothing for subsequent values
    # For a more direct approach without complex slicing, we can use a loop,
    # or use ewm with some adjustments. Let's use the direct ewm approach.
    
    # Note: ewm(alpha=1/N, adjust=False) is Wilder's RMA (Relative Moving Average)
    df['+DM_smooth'] = df['+DM'].ewm(alpha=alpha, adjust=False).mean()
    df['-DM_smooth'] = df['-DM'].ewm(alpha=alpha, adjust=False).mean()
    df['TR_smooth'] = df['TR'].ewm(alpha=alpha, adjust=False).mean()

    # 3. Calculate +DI and -DI
    df[f'DMP_{period}'] = (df['+DM_smooth'] / df['TR_smooth']) * 100
    df[f'DMN_{period}'] = (df['-DM_smooth'] / df['TR_smooth']) * 100

    # 4. Calculate the Directional Index (DX)
    df['DX'] = (abs(df[f'DMP_{period}'] - df[f'DMN_{period}']) / abs(df[f'DMP_{period}'] + df[f'DMN_{period}'])) * 100

    # 5. Calculate the Average Directional Index (ADX) by smoothing DX
    df[f'ADX_{period}'] = df['DX'].ewm(alpha=alpha, adjust=False).mean()

    # Prepare final result DataFrame
    result_df = df[[f'ADX_{period}', f'DMP_{period}', f'DMN_{period}']].copy()
    
    # The first (period * 2 - 1) or so values will be NaN due to the multiple layers of smoothing and shifting.
    # This is normal.
    
    return result_df
