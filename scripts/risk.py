"""리스크 관리 게이트.

순서 (autotrade_daemon 에서 이 순서로 호출):
1. btc_regime_gate() — BTC 급락 시 모든 매수 차단
2. daily_loss_gate() — 일일 손실 한도
3. weekly_mdd_gate() — 주간 최대낙폭
4. consecutive_loss_gate() — 연속 손절 쿨다운
5. position_limit_gate() — 동시 보유 한도
6. size_position() — 포지션 사이징
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import db
import indicators as ta
from upbit_client import UpbitClient


@dataclass
class RiskDecision:
    allow: bool
    reason: str = ""
    severity: str = "info"  # info / warn / block


# ─── 게이트 ───

def btc_regime_gate(client: UpbitClient, cfg: dict) -> RiskDecision:
    """BTC 급락 시 신규매수 전면 차단.

    24시간 변동률 < -7% 이면 차단.
    """
    df = client.get_ohlcv("KRW-BTC", interval="day", count=cfg["btc_regime_lookback_days"])
    if df is None or len(df) < 2:
        return RiskDecision(True, "BTC 데이터 부족 — 통과")

    today = df.iloc[-1]
    yday = df.iloc[-2]
    ch_24h = (today["close"] - yday["close"]) / yday["close"] * 100 if yday["close"] else 0

    if ch_24h <= cfg["btc_crash_threshold_pct"]:
        return RiskDecision(
            allow=False,
            severity="block",
            reason=f"BTC 24h {ch_24h:+.2f}% 급락 → 전체 매수 차단 (한도 {cfg['btc_crash_threshold_pct']}%)",
        )

    # 추가 신호: EMA 교차로 위기 감지
    ema_s = ta.ema(df["close"], cfg["btc_crisis_ema_short"]).iloc[-1]
    ema_l = ta.ema(df["close"], cfg["btc_crisis_ema_long"]).iloc[-1]
    if ema_s < ema_l * 0.95 and today["close"] < ema_l * 0.92:
        return RiskDecision(
            allow=False,
            severity="block",
            reason=f"BTC 장기 약세 (EMA{cfg['btc_crisis_ema_short']}<{cfg['btc_crisis_ema_long']}) → 매수 차단",
        )

    return RiskDecision(True, f"BTC 24h {ch_24h:+.2f}%")


def btc_regime_label(client: UpbitClient, cfg: dict) -> str:
    """대시보드/로그용 BTC 레짐 문자열."""
    df = client.get_ohlcv("KRW-BTC", interval="day", count=60)
    if df is None or len(df) < 50:
        return "unknown"
    return ta.trend_regime(df["close"], cfg["btc_crisis_ema_short"], cfg["btc_crisis_ema_long"])


def daily_loss_gate(cfg: dict, mode: str | None = None) -> RiskDecision:
    pnl = db.today_pnl_pct(mode=mode)
    if pnl <= cfg["daily_loss_limit_pct"]:
        return RiskDecision(
            allow=False,
            severity="block",
            reason=f"일일 손실 {pnl:.2f}% — 한도 {cfg['daily_loss_limit_pct']}% 초과",
        )
    return RiskDecision(True, f"오늘 PnL {pnl:+.2f}%")


def weekly_mdd_gate(cfg: dict, mode: str | None = None) -> RiskDecision:
    """7일 누적 실현 손익의 MDD 계산."""
    from datetime import date, timedelta
    mode_and = " AND mode=?" if mode in ("live", "dry", "paper") else ""
    params: tuple = ((date.today() - timedelta(days=7)).strftime("%Y-%m-%d"),)
    if mode_and:
        params = params + (mode,)
    with db.get_conn() as c:
        rows = c.execute(
            "SELECT exit_date, SUM(pnl_krw) as pnl, SUM(entry_krw) as ent "
            f"FROM trades WHERE status='closed' AND exit_date >= ?{mode_and} "
            "GROUP BY exit_date ORDER BY exit_date",
            params,
        ).fetchall()
    if len(rows) < 3:
        return RiskDecision(True, "주간 거래 부족 — 통과")

    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in rows:
        if not r["ent"]:
            continue
        daily_pct = r["pnl"] / r["ent"] * 100
        cum += daily_pct
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    if mdd <= cfg["weekly_mdd_limit_pct"]:
        return RiskDecision(
            allow=False,
            severity="block",
            reason=f"주간 MDD {mdd:.2f}% — 한도 {cfg['weekly_mdd_limit_pct']}%",
        )
    return RiskDecision(True, f"주간 MDD {mdd:.2f}%")


def consecutive_loss_gate(cfg: dict, mode: str | None = None) -> RiskDecision:
    """연속 손절 쿨다운 — 최근 N시간 내 청산만 카운트 (날짜 지나면 자동 해제)."""
    window = float(cfg.get("consecutive_loss_window_hours", 12))
    n = db.consecutive_losses(within_hours=window, mode=mode)
    if n >= cfg["cooldown_after_consecutive_losses"]:
        return RiskDecision(
            allow=False,
            severity="block",
            reason=(f"최근 {window:.0f}h 연속 손절 {n}회 → 쿨다운 "
                    f"(한도 {cfg['cooldown_after_consecutive_losses']}회)"),
        )
    return RiskDecision(True, f"최근 {window:.0f}h 연속 손절 {n}회")


def position_limit_gate(cfg: dict) -> RiskDecision:
    n = len(db.get_open_positions())
    if n >= cfg["max_positions"]:
        return RiskDecision(
            allow=False,
            severity="warn",
            reason=f"포지션 한도 도달: {n}/{cfg['max_positions']}",
        )
    return RiskDecision(True, f"보유 {n}/{cfg['max_positions']}")


def thin_liquidity_gate(cfg: dict) -> RiskDecision:
    """얕은 유동성 시간대(02~06 KST) 진입 억제."""
    h = datetime.now().hour
    if h in cfg.get("thin_liquidity_hours", []):
        return RiskDecision(
            allow=True,
            severity="warn",
            reason=f"얕은 유동성 시간대 ({h}시) — 포지션 축소 권고",
        )
    return RiskDecision(True)


# ─── 포지션 사이징 ───

def size_position(
    total_krw: float,
    current_price: float,
    stop_loss_price: float,
    cfg: dict,
    thin_liquidity: bool = False,
) -> tuple[float, float]:
    """포지션 사이징.

    1. Vol Target (리스크 기반): position_risk = total × per_trade_risk_pct
       qty = position_risk / (current - stop)
    2. max_position_pct 한도로 제한
    3. 얕은 유동성 시간대면 50% 축소

    Returns: (quantity, krw_amount)
    """
    # 리스크 기반
    risk_krw = total_krw * cfg["per_trade_risk_pct"] / 100
    price_risk = current_price - stop_loss_price
    if price_risk <= 0:
        # 손절 미설정 시 비중 기반
        krw = total_krw * cfg["max_position_pct"] / 100
    else:
        qty_by_risk = risk_krw / price_risk
        krw_by_risk = qty_by_risk * current_price
        krw_by_cap = total_krw * cfg["max_position_pct"] / 100
        krw = min(krw_by_risk, krw_by_cap)

    if thin_liquidity:
        krw *= cfg.get("thin_liquidity_max_pct", 50) / 100

    # 최소 주문금액 미달 시 0 반환
    if krw < cfg["min_order_krw"]:
        return 0.0, 0.0

    qty = krw / current_price
    return qty, krw


# ─── 통합 체크 ───

def run_all_gates(
    client: UpbitClient, cfg: dict, mode: str | None = None,
) -> tuple[bool, list[RiskDecision]]:
    """모든 게이트 순차 실행. 첫 block 에서 중단 가능하나 모두 수집하여 반환."""
    decisions: list[RiskDecision] = []

    for gate in [
        lambda: btc_regime_gate(client, cfg),
        # daily_loss_gate 제거 — 연속손절·주간MDD 로 중복 보호됨
        lambda: weekly_mdd_gate(cfg, mode=mode),
        lambda: consecutive_loss_gate(cfg, mode=mode),
        lambda: position_limit_gate(cfg),
        lambda: thin_liquidity_gate(cfg),
    ]:
        try:
            d = gate()
        except Exception as e:
            d = RiskDecision(True, f"게이트 오류: {e}", "warn")
        decisions.append(d)

    # block 심각도가 하나라도 있으면 차단
    allow = all(d.allow or d.severity != "block" for d in decisions)
    return allow, decisions


# ─── CLI ───
if __name__ == "__main__":
    from config import get_api_keys, load_config

    cfg = load_config()
    c = UpbitClient(*get_api_keys(), dry_run=True)
    db.init_db()
    allow, decisions = run_all_gates(c, cfg)
    print(f"종합 결과: {'✅ 매수 허용' if allow else '❌ 매수 차단'}")
    for d in decisions:
        icon = "✅" if d.allow else "🚫"
        print(f"  {icon} [{d.severity}] {d.reason}")
