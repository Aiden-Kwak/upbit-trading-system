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

    # DB 기록 (mode: live/dry/paper)
    mode = "paper" if getattr(client, "paper", False) else ("dry" if dry_run else "live")
    trade_id = db.insert_trade(
        symbol=sym,
        strategy=signal.strategy,
        entry_price=price,
        entry_quantity=actual_qty,
        entry_krw=krw_amount,
        entry_grade=signal.grade,
        entry_uuid=uuid,
        notes=signal.reason[:500],
        mode=mode,
    )

    return OrderResult(
        ok=True,
        symbol=sym,
        side="buy",
        price=price,
        quantity=actual_qty,
        krw=krw_amount,
        uuid=uuid,
        reason=f"trade_id={trade_id}",
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

    if sym in MAJORS and not dry_run:
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
    exit_krw = sell_qty * price

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
