"""시그널 엔진 — 3종 전략 앙상블.

전략:
1. VB (Volatility Breakout) — Larry Williams, 일봉 기준
2. MB (Momentum Breakout) — EMA cross + 거래량 확인 + N-bar 돌파
3. MR (Mean Reversion) — BB 하단 + RSI 과매도 (횡보 레짐 시만)

각 전략은 독립적으로 Grade(A/B/C/D) + score 를 산출합니다.
최종 매수 대상은 Grade A/B 중 전략 우선순위로 선택됩니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

import indicators as ta
from upbit_client import UpbitClient


@dataclass
class Signal:
    symbol: str
    strategy: str                      # VB / MB / MR
    action: str                        # BUY / HOLD / SKIP
    grade: str                         # A / B / C / D
    score: int
    score_max: int = 100
    reason: str = ""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def score_pct(self) -> int:
        return int(self.score / self.score_max * 100) if self.score_max else 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "action": self.action,
            "grade": self.grade,
            "score": self.score,
            "score_max": self.score_max,
            "score_pct": self.score_pct,
            "reason": self.reason,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "details": self.details,
        }


def _grade_from_score(pct: int, strategy: str = "VB") -> str:
    """전략별 임계값 분리 (MR은 조건 타이트 → 기준 완화)."""
    thresholds = {
        "VB": (80, 65, 50),
        "MB": (75, 60, 45),
        "MR": (70, 55, 40),
        "VS": (75, 60, 45),  # spike 40 + 일중 30 + rsi 20 = 기본 90 가능
    }.get(strategy, (75, 60, 45))
    if pct >= thresholds[0]:
        return "A"
    if pct >= thresholds[1]:
        return "B"
    if pct >= thresholds[2]:
        return "C"
    return "D"


def _effective_cfg(cfg: dict, strategy: str) -> dict:
    """전략별 오버라이드 적용된 유효 설정 반환."""
    merged = dict(cfg)
    ov = (cfg.get("strategy_overrides") or {}).get(strategy) or {}
    merged.update(ov)
    return merged


# ─── 전략 1: Volatility Breakout (Larry Williams) ───

def _downgrade(grade: str) -> str:
    return {"A": "B", "B": "C", "C": "D"}.get(grade, grade)


def evaluate_vb(df_day: pd.DataFrame, cfg: dict, current_price: float) -> Optional[Signal]:
    """일봉 변동성 돌파 + 추세/레짐/RSI 게이트.

    매수 조건:
      - 당일 현재가 >= 당일시가 + K × 전일Range (주 신호)
      - RSI < vb_rsi_buy_max (과열 추격 차단)
      - 레짐 != "bear" (하락장 돌파 페이크아웃 회피)

    학술 보강:
      - Donchian 20일 고가 동시 돌파 = 다중 타임프레임 confirmation
      - ADX < adx_min_for_trend → 약추세 강등 (Wilder)
      - Hurst < 0.5 → 평균회귀성 강등 (Mandelbrot/Peters)
    """
    if df_day is None or len(df_day) < 21:
        return None

    prev = df_day.iloc[-2]
    today = df_day.iloc[-1]
    prev_range = prev["high"] - prev["low"]
    if prev_range <= 0:
        return None

    regime = ta.trend_regime(df_day["close"])
    rsi = float(ta.rsi(df_day["close"]).iloc[-1])

    # ── 게이트 1: bear 레짐이면 VB 자체를 스킵 ──
    if cfg.get("vb_bear_skip", True) and regime == "bear":
        return Signal(
            symbol="", strategy="VB", action="HOLD", grade="D", score=0,
            reason="bear 레짐 — VB 돌파매수 차단",
            details={"regime": regime, "rsi": rsi},
        )

    # ── 게이트 2: RSI 과열 (>= vb_rsi_buy_max) 이면 BUY 차단 ──
    rsi_buy_max = cfg.get("vb_rsi_buy_max", 70)
    if rsi >= rsi_buy_max:
        return Signal(
            symbol="", strategy="VB", action="HOLD", grade="D", score=0,
            reason=f"RSI {rsi:.0f} 과열(≥{rsi_buy_max}) — 추격매수 차단",
            details={"regime": regime, "rsi": rsi},
        )

    # K 동적 조정: 노이즈비율 높을수록 K 낮춤 (진입 쉽게)
    noise = ta.noise_ratio(df_day.iloc[:-1], period=cfg["vb_noise_lookback"])
    k = max(cfg["vb_k_min"], min(cfg["vb_k_max"], 1.0 - noise))

    target = today["open"] + k * prev_range
    breakout = current_price >= target

    avg_vol = df_day["volume"].iloc[-21:-1].mean()
    vol_ratio = today["volume"] / avg_vol if avg_vol > 0 else 0
    vol_min = float(cfg.get("vb_volume_confirm_ratio", 3.0))

    ema9 = ta.ema(df_day["close"], 9).iloc[-1]
    ema21 = ta.ema(df_day["close"], 21).iloc[-1]
    trend_ok = ema9 > ema21

    # 돌파 미달 → 조기 반환
    if not breakout:
        gap_pct = (target - current_price) / current_price * 100
        return Signal(
            symbol="", strategy="VB", action="HOLD", grade="D", score=0,
            reason=f"돌파 미달 ({gap_pct:+.2f}%p 남음)",
            details={"target": target, "k": k, "noise": noise, "regime": regime},
        )

    # ── 게이트 3: 거래량 하한 (가짜 돌파 차단) ──
    # 백테스트: vol<3x 돌파는 승률 51%/음의 엣지. 3x 이상이어야 의미 있음.
    if vol_ratio < vol_min:
        return Signal(
            symbol="", strategy="VB", action="HOLD", grade="D", score=0,
            reason=f"거래량 {vol_ratio:.1f}x < 최소 {vol_min:.1f}x — 가짜 돌파 차단",
            details={"vol_ratio": vol_ratio, "regime": regime},
        )

    # ── 게이트 4: 당일 모멘텀 확인 (시가 대비 현재가) ──
    # 백테스트: vol≥3x + chg≥+2% 조합에서 승률 78% / 샤프 +0.44 (현재 51%/-0.05 대비)
    min_chg = float(cfg.get("vb_min_intraday_change_pct", 0) or 0)
    intraday_chg = (current_price - today["open"]) / today["open"] * 100 if today["open"] else 0
    if min_chg > 0 and intraday_chg < min_chg:
        return Signal(
            symbol="", strategy="VB", action="HOLD", grade="D", score=0,
            reason=f"당일 변동 {intraday_chg:+.2f}% < 최소 +{min_chg:.1f}% — 약세 돌파 차단",
            details={"intraday_change": intraday_chg, "regime": regime},
        )

    score = 40  # 돌파 기본점수 (통과 확정)
    reasons = [f"돌파 (target={target:,.0f}, K={k:.2f})"]
    reasons.append(f"거래량 {vol_ratio:.1f}x")
    score += 25
    reasons.append(f"당일 {intraday_chg:+.1f}%")

    if trend_ok:
        score += 20
        reasons.append("추세↑ EMA9>21")

    # RSI 선형 감점 (과열 근접)
    if rsi < 60:
        score += 15
    elif rsi < 65:
        score += 12
        reasons.append(f"RSI {rsi:.0f}")
    else:  # 65 <= rsi < vb_rsi_buy_max
        score += 6
        reasons.append(f"RSI {rsi:.0f}(주의)")

    # Donchian 20일 고가 동시 돌파 → 강건성 가산
    donch_n = int(cfg.get("vb_donchian_lookback", 20))
    d_upper, _ = ta.donchian(df_day["high"], df_day["low"], donch_n)
    if not d_upper.empty:
        d_val = float(d_upper.iloc[-1])
        if d_val > 0 and current_price >= d_val:
            score += int(cfg.get("vb_donchian_bonus", 5))
            reasons.append(f"Donchian{donch_n} 돌파")

    grade = _grade_from_score(score, "VB")

    # ── 레짐/ADX/Hurst 강등 (추세 전략 우위 검증) ──
    if cfg.get("vb_range_downgrade", True) and regime == "range":
        grade = _downgrade(grade)
        reasons.append("range 레짐 강등")

    adx_val = float("nan")
    adx_min = int(cfg.get("adx_min_for_trend", 0) or 0)
    if adx_min > 0:
        adx_series = ta.adx(df_day["high"], df_day["low"], df_day["close"],
                            cfg.get("adx_period", 14))
        if not adx_series.empty:
            adx_val = float(adx_series.iloc[-1])
            if adx_val < adx_min:
                grade = _downgrade(grade)
                reasons.append(f"ADX {adx_val:.0f} 약추세 강등")

    hurst_val = float("nan")
    if cfg.get("hurst_enabled", True):
        hurst_val = ta.hurst_exponent(df_day["close"],
                                      max_lag=int(cfg.get("hurst_lookback", 60)))
        if hurst_val < float(cfg.get("hurst_trend_min", 0.5)):
            grade = _downgrade(grade)
            reasons.append(f"Hurst {hurst_val:.2f} 평균회귀성 강등")

    ecfg = _effective_cfg(cfg, "VB")
    stop = max(prev["low"], current_price * (1 + ecfg["stop_loss_pct"] / 100))
    atr_val = float(ta.atr(df_day["high"], df_day["low"], df_day["close"]).iloc[-1])
    tp_pct_floor = current_price * (1 + ecfg["take_profit_pct"] / 100)
    tp_atr = current_price + ecfg.get("tp_atr_multiple", 4.0) * atr_val
    tp = max(tp_pct_floor, tp_atr)

    return Signal(
        symbol="",
        strategy="VB",
        action="BUY" if grade in ("A", "B") else "HOLD",
        grade=grade,
        score=score,
        reason=" / ".join(reasons),
        entry_price=current_price,
        stop_loss=stop,
        take_profit=tp,
        details={
            "target": target, "k": k, "noise": noise, "rsi": rsi,
            "regime": regime, "adx": adx_val, "hurst": hurst_val,
        },
    )


# ─── 전략 2: Momentum Breakout (15m/60m 봉) ───

def evaluate_mb(df: pd.DataFrame, cfg: dict, current_price: float) -> Optional[Signal]:
    """모멘텀 돌파.

    조건:
    - EMA_short > EMA_long
    - N-bar 최고가 돌파
    - 거래량 >= 평균의 N배
    - RSI 50~75
    """
    if df is None or len(df) < max(cfg["mb_ema_long"], cfg["mb_breakout_lookback"]) + 5:
        return None

    close = df["close"]
    es = ta.ema(close, cfg["mb_ema_short"]).iloc[-1]
    el = ta.ema(close, cfg["mb_ema_long"]).iloc[-1]
    trend_ok = es > el

    prev_high = ta.price_high_n(df["high"], cfg["mb_breakout_lookback"])
    breakout = current_price > prev_high

    vol_mult = ta.volume_spike(df["volume"], lookback=cfg["mb_breakout_lookback"])
    vol_ok = vol_mult >= cfg["mb_volume_spike_ratio"]

    rsi = ta.rsi(close).iloc[-1]
    rsi_ok = cfg["mb_rsi_min"] <= rsi <= cfg["mb_rsi_max"]

    _, _, hist = ta.macd(close)
    macd_ok = hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]

    score = 0
    reasons = []
    # EMA 추세는 필수 게이트 — 가중 상향 (20→30)
    if trend_ok:
        score += 30
        reasons.append(f"EMA{cfg['mb_ema_short']}>{cfg['mb_ema_long']}")
    if breakout:
        score += 25
        reasons.append(f"{cfg['mb_breakout_lookback']}봉 고가 돌파 ({prev_high:,.0f})")
    if vol_ok:
        score += 20
        reasons.append(f"거래량 {vol_mult:.1f}x")
    # RSI 선형 감점
    if cfg["mb_rsi_min"] <= rsi < 65:
        score += 15
        reasons.append(f"RSI {rsi:.0f}")
    elif 65 <= rsi < cfg["mb_rsi_max"]:
        score += 8
        reasons.append(f"RSI {rsi:.0f}(주의)")
    elif rsi >= cfg["mb_rsi_max"]:
        reasons.append(f"RSI {rsi:.0f}(과열)")
    if macd_ok:
        score += 10
        reasons.append("MACD상승")

    grade = _grade_from_score(score, "MB")
    ecfg = _effective_cfg(cfg, "MB")
    atr_val = ta.atr(df["high"], df["low"], close).iloc[-1]
    stop = current_price - ecfg["stop_atr_multiple"] * atr_val
    stop = max(stop, current_price * (1 + ecfg["stop_loss_pct"] / 100))
    tp_pct_floor = current_price * (1 + ecfg["take_profit_pct"] / 100)
    tp_atr = current_price + ecfg.get("tp_atr_multiple", 4.0) * float(atr_val)
    tp = max(tp_pct_floor, tp_atr)

    action = "BUY" if (grade in ("A", "B") and breakout and trend_ok) else "HOLD"

    return Signal(
        symbol="",
        strategy="MB",
        action=action,
        grade=grade,
        score=score,
        reason=" / ".join(reasons) or "조건 미충족",
        entry_price=current_price,
        stop_loss=stop,
        take_profit=tp,
        details={"rsi": rsi, "vol_mult": vol_mult, "prev_high": prev_high, "atr": atr_val},
    )


# ─── 전략 3: Mean Reversion (횡보 시만) ───

def evaluate_mr(df: pd.DataFrame, cfg: dict, current_price: float) -> Optional[Signal]:
    """평균회귀. 레짐이 range 일 때만 호출되어야 함."""
    if df is None or len(df) < cfg["mr_bb_period"] + 5:
        return None

    close = df["close"]
    upper, mid, lower, pctb = ta.bollinger_bands(close, cfg["mr_bb_period"], cfg["mr_bb_std"])
    rsi = ta.rsi(close).iloc[-1]
    vw = ta.vwap(df).iloc[-1]

    bb_bottom = pctb.iloc[-1] <= 0.1  # 하단 10% 이내
    rsi_oversold = rsi <= cfg["mr_rsi_oversold"]
    below_vwap = current_price < vw if cfg["mr_vwap_below_required"] else True

    score = 0
    reasons = []
    if bb_bottom:
        score += 35
        reasons.append(f"BB하단 %B={pctb.iloc[-1]:.2f}")
    if rsi_oversold:
        score += 30
        reasons.append(f"RSI {rsi:.0f}(과매도)")
    if below_vwap:
        score += 20
        reasons.append("VWAP 아래")

    # 가격이 최근 bottom 에 터치했으나 반등 시작(양봉 or MACD hist 상승) 확인
    _, _, hist = ta.macd(close)
    if hist.iloc[-1] > hist.iloc[-2]:
        score += 15
        reasons.append("MACD반등")

    grade = _grade_from_score(score, "MR")
    ecfg = _effective_cfg(cfg, "MR")
    stop = current_price * (1 + ecfg["stop_loss_pct"] / 100)
    # MR 은 BB 중단 or take_profit_pct 중 가까운 쪽 (떨어지는 칼, 빠른 익절)
    tp_mid = float(mid.iloc[-1])
    tp_pct_cap = current_price * (1 + ecfg["take_profit_pct"] / 100)
    tp = min(tp_mid, tp_pct_cap) if tp_mid > current_price else tp_pct_cap

    action = "BUY" if (grade in ("A", "B") and bb_bottom and rsi_oversold) else "HOLD"

    return Signal(
        symbol="",
        strategy="MR",
        action=action,
        grade=grade,
        score=score,
        reason=" / ".join(reasons) or "조건 미충족",
        entry_price=current_price,
        stop_loss=stop,
        take_profit=tp,
        details={"rsi": rsi, "pctb": pctb.iloc[-1], "vwap": vw, "bb_mid": mid.iloc[-1]},
    )


# ─── 전략 4: Volume Spike (레짐 무관, 섹터 로테이션 포착) ───

def evaluate_vs(df_day: pd.DataFrame, cfg: dict, current_price: float) -> Optional[Signal]:
    """Volume Spike / 급등 추종.

    학술 근거:
      - Jegadeesh & Titman (1993) — 단기 winners 매수 momentum 효과
      - Liu, Tsyvinski & Wu (2022) — 크립토 1-7일 horizon 양의 autocorrelation
      - attention-driven momentum 은 시장 regime 무관 발생

    조건:
      - 일봉 거래량 ≥ 20일 평균 × vs_spike_min_ratio
      - 일중 시가대비 +vs_intraday_change_min_pct~+max_pct (방향 확인 + 과열 회피)
      - RSI 50~78 (모멘텀 있으나 극과열 아님)

    VB 대비 차이: Hurst/ADX/regime 게이트 없음 — 스파이크 자체가 신호.
    """
    if df_day is None or len(df_day) < 21:
        return None

    last = df_day.iloc[-1]
    avg20 = float(df_day["volume"].iloc[-21:-1].mean())
    if avg20 <= 0:
        return None

    spike = float(last["volume"]) / avg20
    open_px = float(last["open"])
    chg_pct = (current_price - open_px) / open_px * 100 if open_px > 0 else 0.0
    rsi = float(ta.rsi(df_day["close"]).iloc[-1])

    score = 0
    reasons: list[str] = []

    min_ratio = float(cfg.get("vs_spike_min_ratio", 5.0))
    if spike < min_ratio:
        return Signal(
            symbol="", strategy="VS", action="HOLD", grade="D", score=0,
            reason=f"거래량 {spike:.1f}x < {min_ratio}x",
            details={"spike": spike, "intraday_change": chg_pct, "rsi": rsi},
        )
    # 스파이크 비율에 선형 가점 (5x=40, 10x=50, 20x+=60 max)
    spike_score = min(60, int(40 + (spike - min_ratio) * 2))
    score += spike_score
    reasons.append(f"거래량 {spike:.1f}x 급등")

    ch_min = float(cfg.get("vs_intraday_change_min_pct", 3.0))
    ch_max = float(cfg.get("vs_intraday_change_max_pct", 15.0))
    if not (ch_min <= chg_pct <= ch_max):
        return Signal(
            symbol="", strategy="VS", action="HOLD", grade="D", score=0,
            reason=f"일중 {chg_pct:+.2f}% 범위 밖 ({ch_min}~{ch_max}%)",
            details={"spike": spike, "intraday_change": chg_pct, "rsi": rsi},
        )
    # 일중 상승폭 가점: 5% 피크 구간에서 최고
    if 4 <= chg_pct <= 8:
        score += 30
    else:
        score += 20
    reasons.append(f"일중 {chg_pct:+.2f}%")

    rsi_min = float(cfg.get("vs_rsi_min", 50))
    rsi_max = float(cfg.get("vs_rsi_max", 78))
    if not (rsi_min <= rsi <= rsi_max):
        return Signal(
            symbol="", strategy="VS", action="HOLD", grade="D", score=0,
            reason=f"RSI {rsi:.0f} 범위 밖 ({rsi_min:.0f}~{rsi_max:.0f})",
            details={"spike": spike, "intraday_change": chg_pct, "rsi": rsi},
        )
    # RSI 60~70 sweet spot
    if 60 <= rsi <= 70:
        score += 20
    else:
        score += 12
    reasons.append(f"RSI {rsi:.0f}")

    # MACD histogram 확장 보너스
    _, _, hist = ta.macd(df_day["close"])
    if len(hist) >= 2 and hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]:
        score += 10
        reasons.append("MACD확장")

    grade = _grade_from_score(score, "VS")
    ecfg = _effective_cfg(cfg, "VS")
    atr_val = float(ta.atr(df_day["high"], df_day["low"], df_day["close"]).iloc[-1])
    # SL: 일중 저가 or -3.5% 중 유리한 쪽 (단, 너무 느슨하지 않게)
    stop_by_pct = current_price * (1 + ecfg["stop_loss_pct"] / 100)
    stop_by_low = float(last["low"])
    stop = max(stop_by_pct, stop_by_low) if stop_by_low < current_price else stop_by_pct
    tp_pct_floor = current_price * (1 + ecfg["take_profit_pct"] / 100)
    tp_atr = current_price + ecfg.get("tp_atr_multiple", 3.0) * atr_val
    tp = max(tp_pct_floor, tp_atr)

    return Signal(
        symbol="", strategy="VS",
        action="BUY" if grade in ("A", "B") else "HOLD",
        grade=grade, score=score,
        reason=" / ".join(reasons),
        entry_price=current_price, stop_loss=stop, take_profit=tp,
        details={"spike": spike, "intraday_change": chg_pct, "rsi": rsi, "atr": atr_val},
    )


# ─── 메인 진입 평가 ───

def evaluate_symbol(
    client: UpbitClient,
    symbol: str,
    cfg: dict,
) -> list[Signal]:
    """한 코인에 대해 활성화된 전략들을 모두 평가하고 Signal 리스트 반환."""
    cur = client.get_current_price(symbol)
    if not isinstance(cur, (int, float)):
        return []
    current_price = float(cur)

    signals: list[Signal] = []

    # 일봉 조회 (VB + 레짐 판정)
    df_day = client.get_ohlcv(symbol, interval="day", count=60)
    if df_day is None or df_day.empty:
        return []

    regime = ta.trend_regime(df_day["close"])

    # 1. VB — 일봉 필수
    if cfg["strategy_vb_enabled"]:
        s = evaluate_vb(df_day, cfg, current_price)
        if s:
            s.symbol = symbol
            s.details["regime"] = regime
            signals.append(s)

    # 2. MB — 선택 타임프레임
    if cfg["strategy_mb_enabled"]:
        df_m = client.get_ohlcv(symbol, interval=cfg["mb_timeframe"], count=100)
        if df_m is not None and not df_m.empty:
            s = evaluate_mb(df_m, cfg, current_price)
            if s:
                s.symbol = symbol
                s.details["regime"] = regime
                signals.append(s)

    # 3. MR — 횡보 레짐 시만
    if cfg["strategy_mr_enabled"] and regime == "range":
        df_m = client.get_ohlcv(symbol, interval="minute60", count=80)
        if df_m is not None and not df_m.empty:
            s = evaluate_mr(df_m, cfg, current_price)
            if s:
                s.symbol = symbol
                s.details["regime"] = regime
                signals.append(s)

    # 4. VS — Volume Spike. 레짐 무관 (bear 는 BTC 게이트가 사전 차단)
    if cfg.get("strategy_vs_enabled", True):
        s = evaluate_vs(df_day, cfg, current_price)
        if s:
            s.symbol = symbol
            s.details["regime"] = regime
            signals.append(s)

    return signals


def check_position_signals(
    client: UpbitClient,
    positions: list[dict],
    cfg: dict,
) -> list[dict]:
    """보유 포지션에 대한 청산 시그널.

    액션:
      SELL_STOP_LOSS / SELL_TAKE_PROFIT / SELL_TRAILING / SELL_BREAKEVEN
      / PARTIAL_TP (tp_level, exit_qty 포함)
      / STALE_CANDIDATE / HOLD
    """
    import json as _json
    from datetime import datetime

    out = []
    for pos in positions:
        sym = pos["symbol"]
        entry = pos.get("entry_price", 0) or 0
        # 잔량 우선 — 부분익절 후에도 계속 감시
        qty = pos.get("remaining_quantity") or pos.get("entry_quantity", 0) or 0
        entry_date = pos.get("entry_date", "")
        strategy = pos.get("strategy", "VB")
        if entry <= 0 or qty <= 0:
            continue

        ecfg = _effective_cfg(cfg, strategy)

        cur = client.get_current_price(sym)
        if not isinstance(cur, (int, float)):
            continue
        cur = float(cur)
        pnl_pct = (cur - entry) / entry * 100

        sig = {
            "symbol": sym,
            "current_price": cur,
            "entry_price": entry,
            "quantity": qty,
            "pnl_pct": round(pnl_pct, 2),
            "action": "HOLD",
            "reason": "정상 범위",
        }

        # 1) 손절 (전략별 SL 적용)
        if pnl_pct <= ecfg["stop_loss_pct"]:
            sig["action"] = "SELL_STOP_LOSS"
            sig["reason"] = f"손절 {pnl_pct:.2f}% (한도 {ecfg['stop_loss_pct']}%)"
            sig["urgency"] = "HIGH"
            out.append(sig)
            continue

        # 2) 브레이크이븐 청산 — 최고점 트리거 도달 후 본전 이탈 시
        #    max_favorable 가 breakeven_trigger_pct 이상이면 armed 간주
        if cfg.get("breakeven_enabled", True):
            mfe = pos.get("max_favorable") or 0.0
            armed = bool(pos.get("breakeven_armed")) or mfe >= cfg["breakeven_trigger_pct"]
            if armed and pnl_pct <= cfg.get("breakeven_buffer_pct", 0.2):
                sig["action"] = "SELL_BREAKEVEN"
                sig["reason"] = (
                    f"본전방어: 최고 +{mfe:.2f}% → 현재 {pnl_pct:+.2f}%"
                )
                sig["urgency"] = "HIGH"
                out.append(sig)
                continue

        # 3) 분할 익절 (Partial TP)
        if cfg.get("partial_tp_enabled", True):
            try:
                hits = _json.loads(pos.get("tp_hits") or "[]")
            except Exception:
                hits = []
            tp_levels = cfg.get("tp_levels") or []
            for idx, level in enumerate(tp_levels, start=1):
                if idx in hits:
                    continue
                if pnl_pct >= level["pct"]:
                    # 잔량의 size_ratio 만큼 매도
                    exit_qty = qty * level["size_ratio"]
                    sig["action"] = "PARTIAL_TP"
                    sig["tp_level"] = idx
                    sig["exit_quantity"] = exit_qty
                    sig["reason"] = (
                        f"분할익절 TP{idx} @+{pnl_pct:.2f}% — "
                        f"{int(level['size_ratio']*100)}% 청산"
                    )
                    sig["urgency"] = "MEDIUM"
                    out.append(sig)
                    break
            else:
                # break 안 탔으면 다음 단계로
                pass
            if sig["action"] == "PARTIAL_TP":
                continue

        # 4) 최종 익절 (파라미터 도달 — 잔량 전량)
        if pnl_pct >= ecfg["take_profit_pct"]:
            sig["action"] = "SELL_TAKE_PROFIT"
            sig["reason"] = f"익절 {pnl_pct:.2f}% (목표 {ecfg['take_profit_pct']}%)"
            sig["urgency"] = "MEDIUM"
            out.append(sig)
            continue

        # 5) 트레일링 — 전략별
        if pnl_pct >= ecfg["trailing_trigger_pct"]:
            df_short = client.get_ohlcv(sym, interval="minute15", count=20)
            if df_short is not None and len(df_short) >= 5:
                recent_high = df_short["high"].tail(10).max()
                drop_from_high = (cur - recent_high) / recent_high * 100
                if drop_from_high <= -ecfg["trailing_distance_pct"]:
                    sig["action"] = "SELL_TRAILING"
                    sig["reason"] = (
                        f"트레일링: 고점 {drop_from_high:.2f}% 하락, 수익 {pnl_pct:.2f}%"
                    )
                    sig["urgency"] = "HIGH"
                    out.append(sig)
                    continue

        # 6) STALE 후보 — MFE 유예 조건
        if cfg["stale_exit_enabled"] and entry_date:
            try:
                ed = datetime.strptime(entry_date, "%Y-%m-%d")
                hours = (datetime.now() - ed).total_seconds() / 3600
                mfe = float(pos.get("max_favorable") or 0.0)
                grace_pct = float(cfg.get("stale_exit_mfe_grace_pct", 0.5))
                mfe_grace = mfe >= grace_pct
                band = float(cfg["stale_exit_pnl_band_pct"])
                in_band = abs(pnl_pct) <= band
                if (hours >= cfg["stale_exit_hours"]
                        and in_band
                        and not mfe_grace):
                    sig["action"] = "STALE_CANDIDATE"
                    sig["reason"] = (
                        f"정체: {hours:.0f}시간 보유, PnL {pnl_pct:+.2f}% "
                        f"(MFE {mfe:+.2f}% < 유예 {grace_pct}%)"
                    )
                    sig["urgency"] = "LOW"
                    sig["hours_held"] = hours
                    out.append(sig)
                    continue
            except Exception:
                pass

        out.append(sig)

    return out


# ─── CLI ───
if __name__ == "__main__":
    import json
    import sys

    from config import get_api_keys, load_config

    cfg = load_config()
    c = UpbitClient(*get_api_keys(), dry_run=True)

    if len(sys.argv) < 2:
        print("Usage: signal_engine.py <KRW-SYMBOL>")
        sys.exit(1)

    sym = sys.argv[1]
    sigs = evaluate_symbol(c, sym, cfg)
    print(f"=== {sym} ===")
    for s in sigs:
        print(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
