"""기술적 지표 라이브러리.

pandas Series 기반 계산. 외부 TA 라이브러리 의존 없이 numpy/pandas 만 사용.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    sig_line = ema(macd_line, signal)
    hist = macd_line - sig_line
    return macd_line, sig_line, hist


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns: (upper, middle, lower, %B)."""
    mid = sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    pctb = (close - lower) / (upper - lower).replace(0, np.nan)
    return upper, mid, lower, pctb.fillna(0.5)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-Weighted Average Price. df 는 open/high/low/close/volume 컬럼 필요."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    return pv.cumsum() / df["volume"].cumsum().replace(0, np.nan)


def noise_ratio(df: pd.DataFrame, period: int = 20) -> float:
    """Larry Williams noise ratio — 변동성 돌파 전략의 K값 힌트.

    noise = |close - open| / (high - low)
    낮을수록 추세, 높을수록 횡보.
    """
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    noise = 1 - (df["close"] - df["open"]).abs() / rng
    return float(noise.tail(period).mean()) if len(df) >= period else 0.5


def volume_spike(volume: pd.Series, lookback: int = 20) -> float:
    """현재 봉 거래량이 평균 대비 몇 배인지."""
    if len(volume) < lookback + 1:
        return 1.0
    avg = volume.iloc[-lookback - 1 : -1].mean()
    cur = volume.iloc[-1]
    if avg <= 0:
        return 1.0
    return float(cur / avg)


def price_high_n(high: pd.Series, n: int) -> float:
    """최근 n개 봉의 고가 중 최댓값 (현재 봉 제외)."""
    if len(high) <= n:
        return float(high.max())
    return float(high.iloc[-n - 1 : -1].max())


def price_low_n(low: pd.Series, n: int) -> float:
    if len(low) <= n:
        return float(low.min())
    return float(low.iloc[-n - 1 : -1].min())


def daily_range_pct(df: pd.DataFrame) -> float:
    """마지막 봉의 (high-low)/close 비율."""
    if df.empty:
        return 0.0
    last = df.iloc[-1]
    if last["close"] <= 0:
        return 0.0
    return float((last["high"] - last["low"]) / last["close"])


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ADX (Average Directional Index) — 추세의 강도 (방향성 무관).

    Wilder's smoothing 사용.
    - < 15: 추세 없음(횡보)
    - 15~25: 약한 추세
    - > 25: 강한 추세
    """
    up = high.diff()
    dn = -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def donchian(high: pd.Series, low: pd.Series, n: int = 20) -> tuple[pd.Series, pd.Series]:
    """Donchian Channel — (n일 고가, n일 저가) 중 현재봉 제외 shift(1).

    Turtle Trading 의 핵심 돌파 지표.
    """
    upper = high.rolling(n).max().shift(1)
    lower = low.rolling(n).min().shift(1)
    return upper, lower


def parkinson_volatility(high: pd.Series, low: pd.Series, period: int = 20) -> pd.Series:
    """Parkinson(1980) 고저 기반 변동성 추정치.

    close-to-close 표준편차 대비 약 5배 효율적.
    반환 단위는 봉 단위 변동성 (일봉이면 daily vol).
    """
    hl = np.log(high.replace(0, np.nan) / low.replace(0, np.nan))
    var = (hl ** 2) / (4 * np.log(2))
    return np.sqrt(var.rolling(period).mean())


def hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
    """Hurst 지수 간이 추정 (variance-of-lagged-diffs).

    - H > 0.55: 추세 지속성 강함
    - H ≈ 0.5: 랜덤워크
    - H < 0.45: 평균회귀 성향

    추세 전략(VB/MB)은 H > 0.5 일 때 우위.
    """
    s = series.dropna().values
    if len(s) < max_lag + 5:
        return 0.5
    lags = range(2, max_lag)
    tau = []
    valid_lags = []
    for lag in lags:
        diff = s[lag:] - s[:-lag]
        if len(diff) < 2:
            continue
        std = diff.std()
        if std <= 0 or np.isnan(std):
            continue
        tau.append(std)
        valid_lags.append(lag)
    if len(valid_lags) < 4:
        return 0.5
    try:
        poly = np.polyfit(np.log(valid_lags), np.log(tau), 1)
        h = float(poly[0])
    except Exception:
        return 0.5
    # clamp to plausible range
    return max(0.0, min(1.0, h))


def correlation(a: pd.Series, b: pd.Series, period: int = 30) -> float:
    """최근 period 봉 수익률 Pearson 상관계수."""
    ra = a.pct_change().dropna().tail(period)
    rb = b.pct_change().dropna().tail(period)
    common = ra.index.intersection(rb.index)
    if len(common) < 10:
        return 0.0
    try:
        return float(ra.loc[common].corr(rb.loc[common]))
    except Exception:
        return 0.0


def trend_regime(close: pd.Series, short: int = 9, long: int = 50) -> str:
    """bull / bear / range 간단 판정."""
    if len(close) < long:
        return "unknown"
    es = ema(close, short).iloc[-1]
    el = ema(close, long).iloc[-1]
    cur = close.iloc[-1]
    # 최근 20봉 변동폭으로 range 판정
    recent = close.tail(20)
    rng_pct = (recent.max() - recent.min()) / recent.mean() if recent.mean() > 0 else 0
    if rng_pct < 0.04:
        return "range"
    if es > el and cur > es:
        return "bull"
    if es < el and cur < es:
        return "bear"
    return "range"


# ─── CLI 테스트 ───
if __name__ == "__main__":
    import sys
    from upbit_client import UpbitClient
    from config import get_api_keys

    sym = sys.argv[1] if len(sys.argv) > 1 else "KRW-BTC"
    c = UpbitClient(*get_api_keys(), dry_run=True)
    df = c.get_ohlcv(sym, interval="day", count=60)
    if df is None or df.empty:
        print("데이터 없음")
        sys.exit(1)

    close = df["close"]
    print(f"=== {sym} (최근 60일) ===")
    print(f"현재가: {close.iloc[-1]:,.0f}")
    print(f"RSI(14): {rsi(close).iloc[-1]:.1f}")
    m, s, h = macd(close)
    print(f"MACD: {m.iloc[-1]:.2f} / Signal: {s.iloc[-1]:.2f} / Hist: {h.iloc[-1]:.2f}")
    u, mid, l, pb = bollinger_bands(close)
    print(f"BB: U={u.iloc[-1]:,.0f} M={mid.iloc[-1]:,.0f} L={l.iloc[-1]:,.0f} %B={pb.iloc[-1]:.2f}")
    print(f"ATR(14): {atr(df['high'], df['low'], close).iloc[-1]:,.0f}")
    print(f"노이즈비율: {noise_ratio(df):.3f}")
    print(f"거래량 spike: {volume_spike(df['volume']):.2f}x")
    print(f"레짐: {trend_regime(close)}")
