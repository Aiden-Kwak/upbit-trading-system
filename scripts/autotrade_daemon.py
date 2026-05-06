"""자동매매 데몬 — 메인 사이클 루프.

흐름 (1사이클):
1. API 키 검증
2. BTC 레짐 체크 → 차단 시 매수 스킵 (매도는 진행)
3. 보유 포지션 모니터 → 손절/익절/STALE 처리
4. 리스크 게이트 실행
5. 스크리닝 (1시간 주기) → 시그널 생성 (매 사이클)
6. 매수 실행
7. 사이클 결과 DB 저장
"""
from __future__ import annotations

import argparse
import logging
import signal as sigmod
import sys
import time
from datetime import datetime
from pathlib import Path

# 같은 디렉토리 내 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import risk
from config import load_config, get_api_keys, LOGS_DIR
from notify import (notify_buy, notify_daemon, notify_error,
                    notify_partial_tp, notify_sell)
from order_executor import execute_buy, execute_sell, execute_partial_sell
import position_monitor
from screener import screen
from signal_engine import check_position_signals, evaluate_symbol
from upbit_client import UpbitClient

# ─── 로깅 ───
# FileHandler 로 daemon.log 에 기록. 대화형 실행(tty)일 때만 stdout 도 추가.
# 백그라운드 실행 시 nohup 이 stdout 을 daemon.log 로 리다이렉트하면 이중 기록이 되므로
# tty 여부를 구분해 중복 방지.
LOGS_DIR.mkdir(parents=True, exist_ok=True)
_handlers: list[logging.Handler] = [logging.FileHandler(LOGS_DIR / "daemon.log")]
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("daemon")


class Daemon:
    def __init__(self, dry_run: bool, interval_sec: int, paper: bool = False):
        self.cfg = load_config()
        # paper 모드면 dry_run 자동 True
        if paper:
            dry_run = True
        access, secret = get_api_keys()
        if not dry_run and (not access or not secret):
            log.error("실거래 모드인데 API 키가 없습니다. .env 확인 필요.")
            sys.exit(1)
        self.client = UpbitClient(
            access, secret,
            dry_run=dry_run,
            paper=paper,
            paper_initial_krw=float(self.cfg.get("paper_initial_krw", 1_000_000)),
        )
        self.dry_run = dry_run
        self.paper = paper
        self.mode = "paper" if paper else ("dry" if dry_run else "live")
        # notify.py 푸터가 런타임 모드를 정확히 반영하도록 환경변수 갱신.
        import os as _os
        _os.environ["UPBIT_MODE"] = self.mode
        self.interval_sec = interval_sec
        self.cycle_num = 0
        self.last_full_scan = 0.0
        self.last_maintenance = 0.0
        self.cached_candidates: list[dict] = []
        self.running = True

        db.init_db()
        mode = "PAPER" if paper else ("DRY" if dry_run else "LIVE")
        log.info(f"데몬 시작 [{mode}]. interval={interval_sec}s")
        if paper:
            log.info(f"가상 시작자본: {self.client.get_balance_krw():,.0f}원")
        log.info(
            f"활성 전략: VB={self.cfg['strategy_vb_enabled']}, "
            f"MB={self.cfg['strategy_mb_enabled']}, "
            f"MR={self.cfg['strategy_mr_enabled']}, "
            f"VS={self.cfg.get('strategy_vs_enabled', False)}, "
            f"VP={self.cfg.get('strategy_vp_enabled', False)}"
        )
        if self.cfg.get("discord_notify", True):
            notify_daemon("시작", f"모드 **{mode}** · interval={interval_sec}s")

        # 모니터 스레드 — LIVE 만 활성화 (dry/paper 는 즉시반응 가치 없음)
        if not dry_run and not paper:
            mon_iv = int(self.cfg.get("monitor_interval_sec", 10))
            position_monitor.start(self.client, load_config, interval=mon_iv)
            log.info(f"📡 PositionMonitor 스레드 시작 — interval={mon_iv}s (STOP_LOSS 전용)")

    def stop(self, *_):
        log.info("종료 신호 수신 — 현재 사이클 후 종료")
        self.running = False
        try:
            position_monitor.stop(timeout=5.0)
        except Exception as e:
            log.warning(f"모니터 스레드 종료 실패: {e}")

    def run(self) -> None:
        sigmod.signal(sigmod.SIGTERM, self.stop)
        sigmod.signal(sigmod.SIGINT, self.stop)

        while self.running:
            start = time.time()
            try:
                self._cycle()
                self._maybe_maintenance()
            except Exception as e:
                log.exception(f"사이클 에러: {e}")
                db.log_error("daemon", str(e))
            elapsed = time.time() - start
            sleep_for = max(1, self.interval_sec - elapsed)
            log.info(f"사이클 완료 {elapsed:.1f}s → {sleep_for:.0f}s 대기")
            for _ in range(int(sleep_for)):
                if not self.running:
                    break
                time.sleep(1)

        log.info("데몬 종료")

    # ─── 사이클 ───
    def _cycle(self) -> None:
        self.cycle_num += 1
        start = time.time()
        log.info(f"━━━ 사이클 #{self.cycle_num} ━━━")

        # config 핫리로드 — user-config.json 변경을 매 사이클 반영
        try:
            new_cfg = load_config()
            if new_cfg != self.cfg:
                changed = [k for k in set(new_cfg) | set(self.cfg)
                           if new_cfg.get(k) != self.cfg.get(k) and not k.startswith("_")]
                if changed:
                    log.info(f"[config] 변경 감지: {changed}")
                self.cfg = new_cfg
        except Exception as e:
            log.warning(f"[config] 리로드 실패 — 기존 설정 유지: {e}")

        status = "ok"
        error_msg = ""
        buys_attempted = 0
        buys_filled = 0
        sells_executed = 0
        signals_generated = 0

        # 1. BTC 레짐
        btc_regime = risk.btc_regime_label(self.client, self.cfg)
        log.info(f"BTC 레짐: {btc_regime}")

        # 2. 포지션 모니터 → 청산
        positions = db.get_open_positions()
        log.info(f"보유 포지션: {len(positions)}개")
        if positions:
            position_sigs = check_position_signals(self.client, positions, self.cfg)
            for sig in position_sigs:
                sym = sig["symbol"]

                # MFE/MAE 갱신 — HOLD 포함 모든 사이클에서 수행해야 함
                # (HOLD 상태에서도 가격이 +trigger 도달하면 추적해야 본전방어 가능)
                db.update_mfe_mae(sym, sig.get("pnl_pct", 0))

                # 본전 armed 트리거 — HOLD 도 포함해서 체크 (가격 정점이 HOLD 구간일 수 있음)
                if (sig.get("pnl_pct", 0) >= self.cfg.get("breakeven_trigger_pct", 3.0)
                        and not sig["action"].startswith("SELL")):
                    pos = next((p for p in positions if p["symbol"] == sym), None)
                    if pos and pos.get("id") and not pos.get("breakeven_armed"):
                        db.arm_breakeven(int(pos["id"]))

                if sig["action"] == "HOLD":
                    continue

                # 부분 익절 — trade close 하지 않고 잔량만 감소
                if sig["action"] == "PARTIAL_TP":
                    pos = next((p for p in positions if p["symbol"] == sym), None)
                    if not pos:
                        continue
                    # 모니터 스레드가 동일 심볼 청산 중이면 부분익절 보류 (이중 매도 방지)
                    if position_monitor.is_pending(sym):
                        log.info(f"  [SKIP_PARTIAL] {sym} — 모니터 스레드 청산 중")
                        continue
                    log.info(f"  PARTIAL_TP {sym}: {sig['reason']}")
                    res = execute_partial_sell(
                        self.client, pos,
                        exit_qty=float(sig["exit_quantity"]),
                        tp_level=int(sig["tp_level"]),
                        reason=sig["reason"],
                        dry_run=self.dry_run,
                    )
                    if res.ok:
                        log.info(
                            f"    ✅ {res.quantity} @ {res.price:,.0f} = {res.krw:,.0f}원 (잔량 유지)"
                        )
                        if self.cfg.get("discord_notify", True):
                            notify_partial_tp(
                                symbol=sym, tp_level=int(sig["tp_level"]),
                                price=res.price, qty=res.quantity,
                                pnl_pct=sig.get("pnl_pct", 0),
                                strategy=(pos.get("strategy") or "").upper(),
                            )
                    else:
                        log.warning(f"    ❌ 실패: {res.reason}")
                    continue

                if sig["action"] == "STALE_CANDIDATE":
                    # 2단 게이트: 신호 재평가
                    if self.cfg.get("stale_exit_signal_recheck", True):
                        revalidation = evaluate_symbol(self.client, sym, self.cfg)
                        still_valid = any(s.grade in self.cfg["entry_grades"]
                                          and s.action == "BUY" for s in revalidation)
                        if still_valid:
                            log.info(f"  [Stale] {sym} 정체이나 신호 유효 → 보유")
                            continue
                        log.info(f"  [Stale] {sym} 정체 + 신호 소멸 → 기회비용 청산")
                    sig["action"] = "SELL_STALE"
                    sig["reason"] = f"기회비용 청산: {sig['reason']}"

                # 포지션 정보 찾기
                pos = next((p for p in positions if p["symbol"] == sym), None)
                if not pos:
                    continue
                log.info(f"  SELL {sym}: {sig['action']} / {sig['reason']} / PnL={sig.get('pnl_pct', 0):+.2f}%")
                # 동시성 보호 wrapper — 모니터 스레드와 경쟁 시 한 쪽만 성공
                res = position_monitor.safe_execute_sell(
                    self.client, pos, sig["action"], dry_run=self.dry_run
                )
                if res is None:
                    log.info(f"    ↺ skip — 모니터 스레드가 이미 처리 중/완료")
                    continue
                if res.ok:
                    sells_executed += 1
                    log.info(f"    ✅ {res.quantity} @ {res.price:,.0f} = {res.krw:,.0f}원")
                    if self.cfg.get("discord_notify", True):
                        # DB의 최종 pnl_krw/pnl_pct 사용 — close_trade 가
                        # realized_partial_krw 까지 누적 반영하므로 분할익절 후
                        # 잔량 청산도 정확. 폴백은 직접 계산.
                        closed = db.get_trade(int(pos["id"])) or {}
                        pnl_pct_final = closed.get("pnl_pct")
                        pnl_krw_final = closed.get("pnl_krw")
                        if pnl_pct_final is None or pnl_krw_final is None:
                            entry_krw = float(pos.get("entry_krw") or 0)
                            realized_partial = float(pos.get("realized_partial_krw") or 0)
                            pnl_krw_final = (res.krw + realized_partial - entry_krw) if entry_krw else 0
                            pnl_pct_final = (pnl_krw_final / entry_krw * 100) if entry_krw > 0 else sig.get("pnl_pct", 0)
                        notify_sell(
                            symbol=sym, price=res.price,
                            pnl_pct=float(pnl_pct_final),
                            pnl_krw=float(pnl_krw_final),
                            reason=sig.get("reason", sig["action"]),
                            strategy=(pos.get("strategy") or "").upper(),
                        )
                else:
                    log.warning(f"    ❌ 실패: {res.reason}")

        # 3. 리스크 게이트 (모드별 기록만 기준)
        allow_buy, decisions = risk.run_all_gates(self.client, self.cfg, mode=self.mode)
        for d in decisions:
            if d.severity == "block":
                log.warning(f"  🚫 {d.reason}")
            elif d.severity == "warn":
                log.info(f"  ⚠️  {d.reason}")
            else:
                log.debug(f"  ✓ {d.reason}")

        if not allow_buy:
            log.info("리스크 게이트 차단 → 매수 스킵")
            self._save_cycle(start, status, len(positions), 0, 0, 0, sells_executed, btc_regime, error_msg)
            return

        # 4. 스크리닝 (1시간 주기 풀스캔)
        now = time.time()
        if now - self.last_full_scan >= self.cfg["screener_full_scan_interval_sec"]:
            log.info("🔍 풀스캔 시작")
            try:
                self.cached_candidates = screen(self.client, self.cfg)
                self.last_full_scan = now
                db.insert_screener_results(self.cached_candidates)
                log.info(f"  → {len(self.cached_candidates)}개 후보 (A/B)")
            except Exception as e:
                log.exception(f"스크리닝 실패: {e}")
                db.log_error("signal", f"스크리닝 실패: {e}")

        # 5. 매수 대상 선정 — 보유중/최근 청산 쿨다운 제외
        held_symbols = {p["symbol"] for p in positions}
        cooldown_hours = float(self.cfg.get("reentry_cooldown_hours", 0) or 0)
        cooldown_symbols = (db.recently_closed_symbols(cooldown_hours, mode=self.mode)
                            if cooldown_hours > 0 else set())
        if cooldown_symbols:
            log.info(f"재진입 쿨다운({cooldown_hours}h, {self.mode}): {sorted(cooldown_symbols)}")
        candidates = [c for c in self.cached_candidates
                      if c["symbol"] not in held_symbols
                      and c["symbol"] not in cooldown_symbols
                      and c.get("grade") in self.cfg["entry_grades"]]
        signals_generated = len(candidates)

        available_slots = self.cfg["max_positions"] - len(positions)
        if available_slots <= 0:
            log.info("포지션 한도 가득 → 신규매수 없음")
            self._save_cycle(start, status, len(positions), signals_generated, 0, 0, sells_executed, btc_regime, error_msg)
            return

        # 6. 매수 실행
        krw_balance = self.client.get_balance_krw()
        # 총자산 근사: 원화 + 포지션 entry_krw 합
        total_krw = krw_balance + sum(p.get("entry_krw", 0) or 0 for p in positions)
        log.info(f"원화: {krw_balance:,.0f} / 총자산 추정: {total_krw:,.0f}")

        thin_liq = decisions[-1].severity == "warn"

        # 같은 사이클 내 중복 진입 방지 — screen() 이 같은 심볼의 여러 전략(VB/VS/MB)
        # 시그널을 반환할 수 있어서 별도 가드 필요 (candidates 가 이미 score 내림 정렬)
        purchased_this_cycle: set[str] = set()
        buys_done = 0

        for c in candidates:
            if buys_done >= available_slots:
                break
            if c["symbol"] in purchased_this_cycle:
                log.info(f"  [SKIP] {c['symbol']} — 같은 사이클 중복 진입 방지")
                continue
            from signal_engine import Signal
            s = Signal(
                symbol=c["symbol"],
                strategy=c["strategy"],
                action=c["action"],
                grade=c["grade"],
                score=c["score"],
                score_max=c["score_max"],
                reason=c["reason"],
                entry_price=c["entry_price"],
                stop_loss=c["stop_loss"],
                take_profit=c["take_profit"],
                details=c.get("details", {}),
            )
            qty, krw_amt = risk.size_position(
                total_krw=total_krw,
                current_price=s.entry_price,
                stop_loss_price=s.stop_loss,
                cfg=self.cfg,
                thin_liquidity=thin_liq,
            )
            if qty <= 0 or krw_amt < self.cfg["min_order_krw"]:
                log.info(f"  [SKIP] {s.symbol} — 사이즈 부족 (krw={krw_amt:,.0f})")
                continue
            if krw_amt > krw_balance * 0.98:
                log.info(f"  [SKIP] {s.symbol} — 잔고 부족")
                continue

            buys_attempted += 1
            log.info(f"  BUY {s.symbol} [{s.strategy}/{s.grade}] "
                     f"price={s.entry_price:,.0f} stop={s.stop_loss:,.0f} tp={s.take_profit:,.0f} "
                     f"krw={krw_amt:,.0f}")
            res = execute_buy(self.client, s, qty, krw_amt, dry_run=self.dry_run)
            if res.ok:
                buys_filled += 1
                buys_done += 1
                purchased_this_cycle.add(s.symbol)
                log.info(f"    ✅ uuid={res.uuid}")
                krw_balance -= krw_amt
                db.insert_signal(
                    symbol=s.symbol, strategy=s.strategy, action="BUY",
                    grade=s.grade, score=s.score, score_max=s.score_max,
                    reason=s.reason, details=s.details, mode=self.mode,
                )
                if self.cfg.get("discord_notify", True):
                    notify_buy(
                        symbol=s.symbol, price=res.price, krw=res.krw,
                        grade=s.grade, strategy=s.strategy, reason=s.reason,
                    )
            else:
                log.warning(f"    ❌ 실패: {res.reason}")

        self._save_cycle(start, status, len(positions), signals_generated,
                         buys_attempted, buys_filled, sells_executed, btc_regime, error_msg)

    def _save_cycle(self, start: float, status: str, n_pos: int,
                    signals_gen: int, buys_att: int, buys_fil: int,
                    sells_exe: int, btc_regime: str, error: str) -> None:
        elapsed = time.time() - start
        pnl = db.today_pnl_pct(mode=self.mode)
        # 선별 저장 — 무변화 사이클은 건너뜀 (30사이클마다 heartbeat 만 기록)
        db.insert_cycle(
            cycle_num=self.cycle_num, status=status, positions_count=n_pos,
            signals_generated=signals_gen, buys_attempted=buys_att,
            buys_filled=buys_fil, sells_executed=sells_exe,
            today_pnl_pct=pnl, btc_regime=btc_regime,
            duration_sec=elapsed, error_msg=error,
            heartbeat_every=self.cfg.get("cycle_heartbeat_every", 30),
            mode=self.mode,
        )

    def _maybe_maintenance(self) -> None:
        """하루 1회 오래된 레코드 정리. 첫 실행 후 24h 마다."""
        now = time.time()
        interval = float(self.cfg.get("db_maintenance_interval_sec", 86400))
        if self.last_maintenance == 0.0:
            # 초기 시작 직후에는 24h 기준으로 밀어두고 스킵
            self.last_maintenance = now
            return
        if now - self.last_maintenance < interval:
            return
        try:
            result = db.cleanup_old_records(
                days_signals=int(self.cfg.get("retention_signals_days", 14)),
                days_cycles=int(self.cfg.get("retention_cycles_days", 30)),
                days_errors=int(self.cfg.get("retention_errors_days", 90)),
                days_screener=int(self.cfg.get("retention_screener_days", 14)),
                vacuum=bool(self.cfg.get("db_vacuum_on_maintenance", True)),
            )
            log.info(f"🧹 DB 유지보수: {result}")
        except Exception as e:
            log.exception(f"DB 유지보수 실패: {e}")
            db.log_error("daemon", f"db_maintenance: {e}")
        self.last_maintenance = now


# ─── 엔트리포인트 ───
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="실주문 없이 시그널만")
    ap.add_argument("--paper", action="store_true",
                    help="페이퍼 트레이딩: 가상 잔고 + 체결 가정 (end-to-end 검증)")
    ap.add_argument("--interval", type=int, default=None, help="사이클 주기(초)")
    args = ap.parse_args()

    cfg = load_config()
    interval = args.interval or cfg["cycle_interval_sec"]
    paper = args.paper or cfg.get("paper_trading_enabled", False)
    # paper → 자동 dry. 그 외엔 CLI 또는 config 기본
    dry = paper or args.dry_run or cfg.get("dry_run_default", True)

    d = Daemon(dry_run=dry, interval_sec=interval, paper=paper)
    d.run()


if __name__ == "__main__":
    main()
