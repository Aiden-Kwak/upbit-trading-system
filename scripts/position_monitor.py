"""포지션 가격 감시 전용 스레드.

목적: 메인 사이클(60s)이 스크리닝 등으로 길어질 때
보유종목의 STOP_LOSS 트리거를 10s 단위로 감지/즉시청산.

설계:
- STOP_LOSS 만 처리 (시급도 1순위, 갭다운 보호)
- TRAILING/BREAKEVEN/STALE/PARTIAL_TP 는 메인 사이클 그대로 (시급도 낮음)
- 메인 사이클과의 양방향 동시성 보호:
  1) per-symbol Lock — 동일 심볼 동시 매도 차단
  2) pending_exits set — in-flight 매도 중복 발사 차단
  3) DB status 재확인 — 다른 스레드가 이미 closed 시 skip

사용:
  position_monitor.start(client, lambda: load_config(), interval=10)
  ...
  position_monitor.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import db
import order_executor
from order_executor import OrderResult

log = logging.getLogger(__name__)

# ─── 동시성 프리미티브 ─────────────────────────────────
_pending_exits: set[str] = set()
_pending_lock = threading.Lock()  # protects _pending_exits

_symbol_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()  # protects _symbol_locks dict

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def _get_symbol_lock(sym: str) -> threading.Lock:
    with _locks_meta:
        lk = _symbol_locks.get(sym)
        if lk is None:
            lk = threading.Lock()
            _symbol_locks[sym] = lk
        return lk


def is_pending(sym: str) -> bool:
    """다른 스레드가 이 심볼의 매도를 진행 중인지."""
    with _pending_lock:
        return sym in _pending_exits


def _claim_exit(sym: str) -> bool:
    with _pending_lock:
        if sym in _pending_exits:
            return False
        _pending_exits.add(sym)
        return True


def _release_exit(sym: str) -> None:
    with _pending_lock:
        _pending_exits.discard(sym)


def _get_trade_by_id(trade_id: int) -> Optional[dict]:
    from db import get_conn
    with get_conn() as c:
        row = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None


def safe_execute_sell(
    client,
    position: dict,
    reason: str,
    dry_run: bool = False,
) -> Optional[OrderResult]:
    """동시성-안전 매도. 메인 사이클 / 모니터 스레드 양쪽에서 호출 가능.

    None 반환 = 다른 스레드가 이미 처리 중이거나 이미 closed → skip.
    """
    sym = position["symbol"]
    trade_id = position.get("id")
    if not trade_id:
        # trade_id 없으면 보호 의미 없음 — 직접 호출
        return order_executor.execute_sell(client, position, reason, dry_run=dry_run)

    lk = _get_symbol_lock(sym)
    # 락 timeout — 데드락 방지 (정상 매도는 수초 내 완료)
    acquired = lk.acquire(timeout=15.0)
    if not acquired:
        log.warning(f"[safe_sell] {sym} 심볼락 타임아웃 — skip")
        return None
    try:
        fresh = _get_trade_by_id(int(trade_id))
        if fresh is None or fresh.get("status") != "open":
            log.info(f"[safe_sell] {sym} skip — DB 상태 closed (이미 다른 스레드 처리)")
            return None
        if not _claim_exit(sym):
            log.info(f"[safe_sell] {sym} skip — pending_exits 중복")
            return None
        try:
            return order_executor.execute_sell(client, position, reason, dry_run=dry_run)
        finally:
            _release_exit(sym)
    finally:
        lk.release()


# ─── 모니터 스레드 ─────────────────────────────────────

def _monitor_loop(client, get_cfg: Callable[[], dict], interval: int) -> None:
    log.info(f"[Monitor] 시작 — interval={interval}s, STOP_LOSS 전용")
    while not _stop_event.is_set():
        loop_start = time.time()
        try:
            cfg = get_cfg()
            positions = db.get_open_positions()
            for pos in positions:
                if _stop_event.is_set():
                    break
                sym = pos["symbol"]
                if is_pending(sym):
                    continue
                try:
                    _check_one(client, pos, cfg)
                except Exception as e:
                    log.error(f"[Monitor] {sym} 처리 예외: {e}")
        except Exception as e:
            log.exception(f"[Monitor] 루프 예외: {e}")
        elapsed = time.time() - loop_start
        wait_for = max(1.0, interval - elapsed)
        _stop_event.wait(wait_for)
    log.info("[Monitor] 종료")


def _check_one(client, pos: dict, cfg: dict) -> None:
    sym = pos["symbol"]
    cur = client.get_current_price(sym)
    if not isinstance(cur, (int, float)) or cur <= 0:
        return
    entry = float(pos.get("entry_price") or 0)
    if entry <= 0:
        return
    pnl_pct = (float(cur) / entry - 1) * 100
    db.update_mfe_mae(sym, pnl_pct)

    strat = (pos.get("strategy") or "").upper()
    overrides = (cfg.get("strategy_overrides") or {}).get(strat, {})
    sl = float(overrides.get("stop_loss_pct", cfg.get("stop_loss_pct", -5.0)))
    if pnl_pct > sl:
        return

    log.warning(
        f"[Monitor] {sym} STOP_LOSS 트리거 — "
        f"pnl={pnl_pct:+.2f}% ≤ {sl:.2f}% (전략={strat or '?'})"
    )
    reason = f"SELL_STOP_LOSS [Monitor10s] pnl={pnl_pct:+.2f}%"
    result = safe_execute_sell(client, pos, reason, dry_run=False)
    if result and result.ok:
        log.warning(
            f"[Monitor] {sym} 청산 완료 @ {result.price:,.4f} = {result.krw:,.0f}원"
        )


def start(client, get_cfg: Callable[[], dict], interval: int = 10) -> None:
    """모니터 스레드 시작 (멱등)."""
    global _thread
    if _thread and _thread.is_alive():
        log.info("[Monitor] 이미 실행 중 — skip")
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_monitor_loop,
        args=(client, get_cfg, interval),
        daemon=True,
        name="PositionMonitor",
    )
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())
