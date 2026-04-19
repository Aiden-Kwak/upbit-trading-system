"""간단 백테스터 — VB 전략 중심.

한 코인에 대해 일봉 기준 VB 전략을 과거 N일 돌려보고 통계 반환.
현실적 제약:
- 슬리피지 0.1% 가정
- 수수료 0.05% × 2 (왕복 0.1%)
- 다음날 시가에 매수, 다음날 종가 or 손절가에 매도
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import indicators as ta
from config import get_api_keys, load_config
from upbit_client import UpbitClient

COMMISSION = 0.0005
SLIPPAGE = 0.001


def backtest_vb(df: pd.DataFrame, cfg: dict) -> dict:
    """VB 전략 백테스트.

    매일 시가에서 "매수가 = open + K × 전일Range" 설정 후 장중 돌파 시 매수,
    당일 종가에 청산 (or 손절가 도달 시 손절).
    """
    if len(df) < 30:
        return {"error": "데이터 부족"}

    trades = []
    for i in range(21, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]
        prev_range = prev["high"] - prev["low"]
        if prev_range <= 0:
            continue

        # K 동적
        window = df.iloc[i - cfg["vb_noise_lookback"]: i]
        noise = ta.noise_ratio(window, period=cfg["vb_noise_lookback"])
        k = max(cfg["vb_k_min"], min(cfg["vb_k_max"], 1.0 - noise))
        target = today["open"] + k * prev_range

        # 추세 필터
        ema9 = ta.ema(df["close"].iloc[:i], 9).iloc[-1]
        ema21 = ta.ema(df["close"].iloc[:i], 21).iloc[-1]
        if ema9 <= ema21:
            continue

        # 거래량 필터
        avg_vol = df["volume"].iloc[i - 21: i - 1].mean()
        if today["volume"] < avg_vol * cfg["vb_volume_confirm_ratio"]:
            continue

        # 장중에 target 돌파했는지
        if today["high"] < target:
            continue

        entry_price = target * (1 + SLIPPAGE)
        # 손절: -5% or 당일 저가 중 가까운 쪽
        stop = max(prev["low"], entry_price * (1 + cfg["stop_loss_pct"] / 100))
        # 손절 도달?
        if today["low"] <= stop:
            exit_price = stop * (1 - SLIPPAGE)
        else:
            exit_price = today["close"] * (1 - SLIPPAGE)

        ret = (exit_price - entry_price) / entry_price - 2 * COMMISSION
        trades.append({
            "date": df.index[i].strftime("%Y-%m-%d") if hasattr(df.index[i], "strftime") else str(df.index[i]),
            "entry": entry_price,
            "exit": exit_price,
            "ret": ret,
            "k": k,
        })

    if not trades:
        return {"n": 0, "ret": 0, "win_rate": 0, "avg_ret": 0}

    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    cum = 1.0
    for r in rets:
        cum *= (1 + r)
    peak = 1.0
    mdd = 0.0
    cur = 1.0
    for r in rets:
        cur *= (1 + r)
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak)

    return {
        "n": len(trades),
        "cum_return_pct": round((cum - 1) * 100, 2),
        "avg_ret_pct": round(sum(rets) / len(rets) * 100, 3),
        "win_rate": round(len(wins) / len(rets) * 100, 1),
        "avg_win_pct": round(sum(wins) / max(len(wins), 1) * 100, 2) if wins else 0,
        "avg_loss_pct": round(sum(losses) / max(len(losses), 1) * 100, 2) if losses else 0,
        "mdd_pct": round(mdd * 100, 2),
        "trades": trades[-10:],
    }


def backtest_mb(df: pd.DataFrame, cfg: dict) -> dict:
    """MB(Momentum Breakout) 백테스트 — 60분봉 기준.

    조건: EMA9>EMA21 + 20봉 돌파 + 거래량 1.5x + 50≤RSI≤75 + MACD 상승.
    진입: 돌파 확인 직후 다음 봉 시가.
    청산: max(-5%, entry - 2.5*ATR) 손절 or +12% 익절 or +8% 진입 후 고점 -3.5% 트레일링.
    보유 한도: 24시간 (24봉).
    """
    if len(df) < 50:
        return {"error": "데이터 부족"}

    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    vols = df["volume"].astype(float)

    ema9 = ta.ema(closes, cfg["mb_ema_short"])
    ema21 = ta.ema(closes, cfg["mb_ema_long"])
    rsi_s = ta.rsi(closes, 14)
    macd_line, sig_line, hist = ta.macd(closes, 12, 26, 9)
    atr_s = ta.atr(df["high"], df["low"], df["close"], 14)
    n = cfg["mb_breakout_lookback"]

    trades = []
    i = n + 30
    while i < len(df) - 1:
        # 기본 필터
        if not (ema9.iloc[i] > ema21.iloc[i]):
            i += 1; continue
        prev_high = highs.iloc[i - n: i].max()
        if closes.iloc[i] <= prev_high:
            i += 1; continue
        avg_vol = vols.iloc[i - 20: i].mean()
        if vols.iloc[i] < avg_vol * cfg["mb_volume_spike_ratio"]:
            i += 1; continue
        r = rsi_s.iloc[i]
        if not (cfg["mb_rsi_min"] <= r <= cfg["mb_rsi_max"]):
            i += 1; continue
        if hist.iloc[i] <= 0 or hist.iloc[i] <= hist.iloc[i - 1]:
            i += 1; continue

        # 진입: 다음 봉 시가
        entry_idx = i + 1
        if entry_idx >= len(df):
            break
        entry = float(df.iloc[entry_idx]["open"]) * (1 + SLIPPAGE)
        atr_val = float(atr_s.iloc[i]) if not pd.isna(atr_s.iloc[i]) else entry * 0.02
        stop = max(entry - cfg["stop_atr_multiple"] * atr_val,
                   entry * (1 + cfg["stop_loss_pct"] / 100))
        tp = entry * (1 + cfg["take_profit_pct"] / 100)
        trail_trigger = entry * (1 + cfg["trailing_trigger_pct"] / 100)
        trail_dist = cfg["trailing_distance_pct"] / 100

        exit_price = None
        max_hold = 24  # 24봉 = 24시간 (60분봉 기준)
        peak = entry
        trail_active = False
        exit_reason = "timeout"

        for j in range(entry_idx, min(entry_idx + max_hold, len(df))):
            bar = df.iloc[j]
            h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
            peak = max(peak, h)
            if peak >= trail_trigger:
                trail_active = True
            # 손절 체크
            if l <= stop:
                exit_price = stop * (1 - SLIPPAGE)
                exit_reason = "stop"
                break
            # 익절
            if h >= tp:
                exit_price = tp * (1 - SLIPPAGE)
                exit_reason = "tp"
                break
            # 트레일링
            if trail_active and l <= peak * (1 - trail_dist):
                exit_price = peak * (1 - trail_dist) * (1 - SLIPPAGE)
                exit_reason = "trail"
                break

        if exit_price is None:
            exit_price = float(df.iloc[min(entry_idx + max_hold - 1, len(df) - 1)]["close"]) * (1 - SLIPPAGE)

        ret = (exit_price - entry) / entry - 2 * COMMISSION
        trades.append({
            "date": str(df.index[entry_idx]),
            "entry": entry,
            "exit": exit_price,
            "ret": ret,
            "reason": exit_reason,
        })
        # 다음 진입은 현재 청산 시점 이후
        i = entry_idx + max_hold + 1

    return _summarize(trades)


def backtest_mr(df: pd.DataFrame, cfg: dict) -> dict:
    """MR(Mean Reversion) 백테스트 — 60분봉.

    활성 조건: 직전 20봉 변동폭 < 4% (range regime 대체).
    진입: BB %B ≤ 0.1, RSI ≤ 28, 가격 < VWAP, MACD hist 반등.
    청산: BB 중앙선 도달 or -5% 손절 or 24봉 timeout.
    """
    if len(df) < 50:
        return {"error": "데이터 부족"}

    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    vols = df["volume"].astype(float)

    period = cfg["mr_bb_period"]
    upper, mid, lower, _pctb = ta.bollinger_bands(closes, period=period, std_mult=cfg["mr_bb_std"])
    rsi_s = ta.rsi(closes, 14)
    _, _, hist = ta.macd(closes, 12, 26, 9)

    trades = []
    i = period + 25
    while i < len(df) - 1:
        # range regime: 최근 20봉 (high-low)/close < 4%
        recent = df.iloc[i - 20: i]
        range_pct = (recent["high"].max() - recent["low"].min()) / recent["close"].iloc[-1] * 100
        if range_pct >= 4.0:
            i += 1; continue

        c = closes.iloc[i]
        bb_lower = lower.iloc[i]
        bb_upper = upper.iloc[i]
        bb_mid = mid.iloc[i]
        if bb_upper == bb_lower:
            i += 1; continue
        pct_b = (c - bb_lower) / (bb_upper - bb_lower)
        if pct_b > 0.1:  # BB %B 하단 10% 이내
            i += 1; continue
        if rsi_s.iloc[i] > cfg["mr_rsi_oversold"]:
            i += 1; continue
        # VWAP 대용 — 최근 20봉 TP×Vol 평균
        tp = (highs.iloc[i - 20: i + 1] + lows.iloc[i - 20: i + 1] + closes.iloc[i - 20: i + 1]) / 3
        v = vols.iloc[i - 20: i + 1]
        vwap = (tp * v).sum() / v.sum() if v.sum() > 0 else c
        if c >= vwap:
            i += 1; continue
        # MACD 반등
        if hist.iloc[i] <= hist.iloc[i - 1]:
            i += 1; continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            break
        entry = float(df.iloc[entry_idx]["open"]) * (1 + SLIPPAGE)
        stop = entry * (1 + cfg["stop_loss_pct"] / 100)

        exit_price = None
        exit_reason = "timeout"
        for j in range(entry_idx, min(entry_idx + 24, len(df))):
            bar = df.iloc[j]
            h, l, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            if l <= stop:
                exit_price = stop * (1 - SLIPPAGE)
                exit_reason = "stop"
                break
            # BB 중앙선 터치 → 1차 익절
            if h >= float(mid.iloc[j]):
                exit_price = float(mid.iloc[j]) * (1 - SLIPPAGE)
                exit_reason = "bb_mid"
                break

        if exit_price is None:
            exit_price = float(df.iloc[min(entry_idx + 23, len(df) - 1)]["close"]) * (1 - SLIPPAGE)

        ret = (exit_price - entry) / entry - 2 * COMMISSION
        trades.append({
            "date": str(df.index[entry_idx]),
            "entry": entry,
            "exit": exit_price,
            "ret": ret,
            "reason": exit_reason,
        })
        i = entry_idx + 24 + 1

    return _summarize(trades)


def _summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "cum_return_pct": 0, "win_rate": 0, "avg_ret_pct": 0,
                "avg_win_pct": 0, "avg_loss_pct": 0, "mdd_pct": 0, "trades": []}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    cur = 1.0
    for r in rets:
        cum *= (1 + r)
        cur *= (1 + r)
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak)
    return {
        "n": len(trades),
        "cum_return_pct": round((cum - 1) * 100, 2),
        "avg_ret_pct": round(sum(rets) / len(rets) * 100, 3),
        "win_rate": round(len(wins) / len(rets) * 100, 1),
        "avg_win_pct": round(sum(wins) / max(len(wins), 1) * 100, 2) if wins else 0,
        "avg_loss_pct": round(sum(losses) / max(len(losses), 1) * 100, 2) if losses else 0,
        "mdd_pct": round(mdd * 100, 2),
        "trades": trades[-10:],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="예: KRW-BTC")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--strategy", choices=["vb", "mb", "mr"], default="vb")
    args = ap.parse_args()

    cfg = load_config()
    c = UpbitClient(*get_api_keys(), dry_run=True)

    if args.strategy == "vb":
        df = c.get_ohlcv(args.symbol, interval="day", count=args.days)
        if df is None or df.empty:
            print("데이터 없음"); sys.exit(1)
        res = backtest_vb(df, cfg)
    else:
        # MB/MR은 60분봉 필요. 200일 × 24 = 4800봉 (최대 200개씩 페이지 필요)
        bars = args.days * 24
        bars = min(bars, 2000)  # 대량 방지
        df = c.get_ohlcv(args.symbol, interval="minute60", count=bars)
        if df is None or df.empty:
            print("데이터 없음"); sys.exit(1)
        res = backtest_mb(df, cfg) if args.strategy == "mb" else backtest_mr(df, cfg)

    print(f"=== {args.symbol} {args.strategy.upper()} 백테스트 ({args.days}일) ===")
    for k, v in res.items():
        if k == "trades":
            continue
        print(f"  {k}: {v}")
    print("\n최근 10 trades:")
    for t in res.get("trades", []):
        extras = f"K={t.get('k', 0):.2f}" if "k" in t else f"[{t.get('reason', '')}]"
        print(f"  {t['date']}: ret={t['ret']*100:+.2f}% {extras}")


if __name__ == "__main__":
    main()
