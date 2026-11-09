"""Scores use only observations available by the signal date; higher = preferred."""
import pandas as pd


def roc20(close: pd.DataFrame) -> pd.DataFrame:
    return (close / close.shift(20) - 1).where(close.rolling(21).count() == 21)


def std20(close: pd.DataFrame) -> pd.DataFrame:
    # Negative score fixes the low-volatility hypothesis before looking at results.
    return -(close.pct_change(fill_method=None).rolling(20).std(ddof=1))


def rev3(close: pd.DataFrame) -> pd.DataFrame:
    return (1 - close / close.shift(3)).where(close.rolling(4).count() == 4)


FACTORS = {"ROC20": roc20, "STD20": std20, "REV3": rev3}
