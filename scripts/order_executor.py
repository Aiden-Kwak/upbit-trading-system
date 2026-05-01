"""주문 실행기.

규칙:
- 알트코인은 지정가 우선 (시장가 슬리피지 방지)
- 대형주(BTC, ETH)는 시장가 허용
- tick size 정렬 필수
- 주문 후 DB 즉시 반영
- dry_run 모드 완벽 지원
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import db
from signal_engine import Signal
from upbit_client import UpbitClient

log = logging.getLogger(__name__)

MAJORS = {"KRW-BTC", "KRW-ETH"}  # 시장가 허용 대상


@dataclass
class OrderResult:
    ok: bool
    symbol: str
    side: str
    price: float = 0.0
    quantity: float = 0.0
    krw: float = 0.0
    uuid: str = ""
    reason: str = ""


def execute_buy(
    client: UpbitClient,
    signal: Signal,
    quantity: float,
    krw_amount: float,
    dry_run: bool = False,
) -> OrderResult:
    """매수 실행.

    - 대형주 + dry_run=False: 시장가 매수 (krw_amount 사용)
    - 알트: 지정가 매수 (현재가 tick 맞춰서)
    """
    sym = signal.symbol
    if quantity <= 0 or krw_amount <= 0:
        return OrderResult(False, sym, "buy", reason="사이즈 0")

    price_raw = signal.entry_price
    price = client.round_to_tick(price_raw)

    if sym in MAJORS and not dry_run:
        resp = client.buy_market(sym, krw_amount)
        actual_qty = krw_amount / price
    else:
        # 지정가 — 현재가에 0.3% 프리미엄으로 즉시체결 유도 (반드시 tick 정렬)
        bid_price = client.round_to_tick(price * 1.003)
        actual_qty = krw_amount / bid_price
        # 소수점 8자리 제한
        actual_qty = round(actual_qty, 8)
        resp = client.buy_limit(sym, bid_price, actual_qty)
        price = bid_price

    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "unknown"
        db.log_error("order", f"매수 실패 {sym}: {err}")
        return OrderResult(False, sym, "buy", reason=str(err))

    uuid = resp.get("uuid", "")

    # ─── 라이브 체결 reconcile ───────────────────────────────
    # 업비트에서 실체결 데이터(평균가/수량/금액) 조회해 추정치 대체.
    # 주문가 * (1 ± 프리미엄) 로 placement 하지만 실 체결가는 오더북 호가에 의존.
    final_price = price
    final_qty = actual_qty
    final_krw = krw_amount
    reconciled = False
    if not dry_run and uuid:
        for wait_s in (0.7, 1.3, 2.0):
            time.sleep(wait_s)
            info = client.get_order(uuid)
            if not isinstance(info, dict):
                continue
            trades_list = info.get("trades") or []
            vol_sum = sum(float(t.get("volume") or 0) for t in trades_list)
            funds_sum = sum(float(t.get("funds") or 0) for t in trades_list)
            if vol_sum > 0 and funds_sum > 0:
                final_price = funds_sum / vol_sum
                final_qty = vol_sum
                final_krw = funds_sum
                reconciled = True
                log.info(
                    f"{sym} 실체결 reconcile: {final_qty:.6f} @ {final_price:,.4f} "
                    f"= {final_krw:,.0f}원 (state={info.get('state')})"
                )
                break
        if not reconciled:
            log.warning(
                f"{sym} 체결 확인 실패(대기/부분체결) — 추정가 {price:,.4f} 로 기록, "
                f"다음 사이클에 잔고 기반 재조정 필요"
            )

    # DB 기록 (mode: live/dry/paper)
    mode = "paper" if getattr(client, "paper", False) else ("dry" if dry_run else "live")
    trade_id = db.insert_trade(
        symbol=sym,
        strategy=signal.strategy,
        entry_price=final_price,
        entry_quantity=final_qty,
        entry_krw=final_krw,
        entry_grade=signal.grade,
        entry_uuid=uuid,
        notes=signal.reason[:500],
        mode=mode,
    )

    return OrderResult(
        ok=True,
        symbol=sym,
        side="buy",
        price=final_price,
        quantity=final_qty,
        krw=final_krw,
        uuid=uuid,
        reason=f"trade_id={trade_id}" + (" reconciled" if reconciled else ""),
    )


def execute_partial_sell(
    client: UpbitClient,
    position: dict,
    exit_qty: float,
    tp_level: int,
    reason: str,
    dry_run: bool = False,
) -> OrderResult:
    """분할 익절 — trade 를 close 하지 않고 잔량만 감소."""
    sym = position["symbol"]
    trade_id = position.get("id")
    if exit_qty <= 0 or not trade_id:
        return OrderResult(False, sym, "sell", reason="부분매도 수량 0")

    cur = client.get_current_price(sym)
    if not isinstance(cur, (int, float)) or cur <= 0:
        return OrderResult(False, sym, "sell", reason="현재가 조회 실패")
    cur = float(cur)

    exit_qty = round(exit_qty, 8)

    if sym in MAJORS and not dry_run:
        resp = client.sell_market(sym, exit_qty)
        price = cur
    else:
        ask_price = client.round_to_tick(cur * 0.997)
        resp = client.sell_limit(sym, ask_price, exit_qty)
        price = ask_price

    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "unknown"
        db.log_error("order", f"분할매도 실패 {sym}: {err}")
        return OrderResult(False, sym, "sell", reason=str(err))

    uuid = resp.get("uuid", "")
    exit_krw = exit_qty * price
    db.apply_partial_exit(
        trade_id=int(trade_id),
        tp_level=tp_level,
        sold_qty=exit_qty,
        sold_krw=exit_krw,
    )
    return OrderResult(
        ok=True, symbol=sym, side="sell", price=price,
        quantity=exit_qty, krw=exit_krw, uuid=uuid, reason=reason,
    )


def execute_sell(
    client: UpbitClient,
    position: dict,
    reason: str,
    dry_run: bool = False,
) -> OrderResult:
    """청산 실행. 부분익절 잔량이 있으면 잔량만 매도."""
    sym = position["symbol"]
    # 부분익절 후 잔량이 있으면 그만큼만 매도 (없으면 entry_quantity)
    qty = float(
        position.get("remaining_quantity")
        or position.get("entry_quantity", 0)
        or 0
    )
    trade_id = position.get("id")
    if qty <= 0 or not trade_id:
        return OrderResult(False, sym, "sell", reason="수량 0 or trade_id 없음")

    # 실제 보유 수량 확인 (체결 수수료 등으로 미세 차이 가능)
    actual_holding = client.get_balance(sym)
    if actual_holding > 0:
        # 체결 수수료로 인해 DB 수량보다 실보유가 적을 수 있음 — 실보유로 축소
        sell_qty = min(qty, actual_holding)
        if actual_holding < qty * 0.99:
            log.warning(f"{sym}: DB 수량({qty}) vs 실보유({actual_holding}) 차이 1%+ "
                        f"— 실보유로 매도")
    else:
        # paper/dry 가 아니고 실제 보유가 0이면 이미 청산됨 → DB만 마감
        if not client.paper and not dry_run:
            log.warning(f"{sym}: 실보유 0 — 외부 청산된 것으로 간주, DB 마감")
            cur = client.get_current_price(sym)
            px = float(cur) if isinstance(cur, (int, float)) else 0.0
            db.close_trade(
                trade_id=trade_id, exit_price=px, exit_quantity=0,
                exit_krw=0, exit_reason=f"{reason} (external_sync)",
                exit_uuid="external",
            )
            return OrderResult(False, sym, "sell", reason="실보유 0 → DB만 동기화")
        sell_qty = qty
    # 소수점 8자리
    sell_qty = round(sell_qty, 8)

    cur = client.get_current_price(sym)
    if not isinstance(cur, (int, float)) or cur <= 0:
        return OrderResult(False, sym, "sell", reason="현재가 조회 실패")
    cur = float(cur)

    # STOP_LOSS 는 모든 코인 시장가 매도 (지정가가 폭락 추격 못해 -8.85% 슬리피지 발생 사례)
    # — API3 케이스 (2026-04-25): MFE -1%, MAE -8.18%, 실현 -8.85% (mb override -4% 무시됨)
    is_stop_loss = "STOP_LOSS" in (reason or "")
    use_market = (sym in MAJORS or is_stop_loss) and not dry_run

    if use_market:
        resp = client.sell_market(sym, sell_qty)
        price = cur
    else:
        # 지정가 — 즉시체결 유도로 0.3% 디스카운트
        ask_price = client.round_to_tick(cur * 0.997)
        resp = client.sell_limit(sym, ask_price, sell_qty)
        price = ask_price

    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "unknown"
        db.log_error("order", f"매도 실패 {sym}: {err}")
        return OrderResult(False, sym, "sell", reason=str(err))

    uuid = resp.get("uuid", "")
    # 시장가는 즉시 다중 호가 체결 가능 → reconcile 시도
    final_price = price
    final_qty = sell_qty
    if use_market and uuid:
        time.sleep(0.8)
        sinfo = client.get_order(uuid)
        if isinstance(sinfo, dict):
            strades = sinfo.get("trades") or []
            svol = sum(float(t.get("volume") or 0) for t in strades)
            sfunds = sum(float(t.get("funds") or 0) for t in strades)
            if svol > 0 and sfunds > 0:
                final_price = sfunds / svol
                final_qty = svol
                log.info(
                    f"{sym} 시장가 매도 reconcile: {final_qty:.6f} @ {final_price:,.4f} "
                    f"(reason={reason})"
                )
    exit_krw = final_qty * final_price
    price = final_price
    sell_qty = final_qty

    # DB 업데이트
    db.close_trade(
        trade_id=trade_id,
        exit_price=price,
        exit_quantity=sell_qty,
        exit_krw=exit_krw,
        exit_reason=reason,
        exit_uuid=uuid,
    )

    return OrderResult(
        ok=True,
        symbol=sym,
        side="sell",
        price=price,
        quantity=sell_qty,
        krw=exit_krw,
        uuid=uuid,
        reason=reason,
    )
