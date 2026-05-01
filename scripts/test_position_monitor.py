"""position_monitor 동시성 테스트.

시나리오:
  T1) 모니터 스레드 + 메인 스레드가 동일 심볼 매도 동시 발사 → 1회만 실행
  T2) DB 가 이미 closed 상태 → 양쪽 모두 skip
  T3) 모니터가 sell 중일 때 PARTIAL_TP skip
  T4) 1000회 동시 호출 stress (라이브락/데드락 검출)

실제 거래는 발생하지 않음 (order_executor.execute_sell 모킹).
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _setup_temp_db():
    """테스트용 임시 DB 생성 + 환경변수로 경로 주입."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pm_test_")
    os.close(fd)
    os.environ["UPBIT_DB_FILE_OVERRIDE"] = path
    # db 모듈 reload 강제
    import importlib
    import db
    db.DB_FILE = Path(path)
    importlib.reload(db)
    db.DB_FILE = Path(path)  # reload 후 재주입
    db.init_db()
    return path


def _insert_open_position(symbol="KRW-TEST", strategy="MB", entry_price=1000.0, qty=10.0):
    import db
    return db.insert_trade(
        symbol=symbol, strategy=strategy,
        entry_price=entry_price, entry_quantity=qty,
        entry_krw=entry_price * qty, entry_grade="A",
        entry_uuid="test-uuid", notes="test", mode="live",
    )


class FakeClient:
    """주문 호출 카운트만 기록."""
    paper = False
    dry_run = False

    def __init__(self, current_price=900.0):
        self.current_price = current_price
        self.sell_calls = []

    def get_current_price(self, sym):
        return self.current_price

    def get_balance(self, sym):
        return 100.0

    def round_to_tick(self, p):
        return p

    def sell_market(self, sym, qty):
        self.sell_calls.append(("market", sym, qty))
        return {"uuid": f"sell-{len(self.sell_calls)}", "state": "done"}

    def sell_limit(self, sym, px, qty):
        self.sell_calls.append(("limit", sym, px, qty))
        return {"uuid": f"sell-{len(self.sell_calls)}", "state": "done"}

    def get_order(self, uuid):
        return {"trades": [{"volume": "10", "funds": "9000"}], "state": "done"}


class TestSafeExecuteSell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dbpath = _setup_temp_db()

    def setUp(self):
        # pending_exits / 락 초기화
        import position_monitor as pm
        with pm._pending_lock:
            pm._pending_exits.clear()
        with pm._locks_meta:
            pm._symbol_locks.clear()
        # DB 비우기
        import db
        with db.get_conn() as c:
            c.execute("DELETE FROM trades")

    def test_t1_concurrent_double_sell_only_one_wins(self):
        """양 스레드가 동시 매도 시도 → 정확히 1번만 execute_sell 호출."""
        import position_monitor as pm
        import db
        tid = _insert_open_position("KRW-T1")
        pos = db.get_open_position("KRW-T1")
        client = FakeClient()

        call_counter = {"n": 0}
        original = pm.order_executor.execute_sell

        def slow_sell(c, p, reason, dry_run=False):
            call_counter["n"] += 1
            time.sleep(0.3)  # 길게 잡아 race window 넓힘
            db.close_trade(p["id"], 900, 10, 9000, reason, "uuid-x")
            from order_executor import OrderResult
            return OrderResult(True, p["symbol"], "sell", 900, 10, 9000, "uuid-x", reason)

        with mock.patch.object(pm.order_executor, "execute_sell", side_effect=slow_sell):
            results = []
            barrier = threading.Barrier(2)

            def worker(label):
                barrier.wait()
                r = pm.safe_execute_sell(client, pos, f"SELL_TEST_{label}")
                results.append((label, r))

            t1 = threading.Thread(target=worker, args=("A",))
            t2 = threading.Thread(target=worker, args=("B",))
            t1.start(); t2.start()
            t1.join(); t2.join()

        self.assertEqual(call_counter["n"], 1, "execute_sell 은 정확히 1회만 호출돼야 함")
        ok_results = [r for (_, r) in results if r is not None]
        skip_results = [r for (_, r) in results if r is None]
        self.assertEqual(len(ok_results), 1, "한 스레드만 OrderResult 반환")
        self.assertEqual(len(skip_results), 1, "다른 스레드는 None(skip) 반환")
        self.assertNotIn("KRW-T1", pm._pending_exits, "pending_exits 자동 정리됨")

    def test_t2_already_closed_in_db(self):
        """DB 가 이미 closed 면 매도 호출 0회."""
        import position_monitor as pm
        import db
        tid = _insert_open_position("KRW-T2")
        pos = db.get_open_position("KRW-T2")
        # 미리 close
        db.close_trade(tid, 1000, 10, 10000, "EXTERNAL", "ext-uuid")

        client = FakeClient()
        call_counter = {"n": 0}

        def fake_sell(*a, **kw):
            call_counter["n"] += 1
            from order_executor import OrderResult
            return OrderResult(True, "x", "sell")

        with mock.patch.object(pm.order_executor, "execute_sell", side_effect=fake_sell):
            r = pm.safe_execute_sell(client, pos, "SELL_LATE")

        self.assertEqual(call_counter["n"], 0)
        self.assertIsNone(r)

    def test_t3_is_pending_observed_during_sell(self):
        """매도 진행 중 다른 스레드가 is_pending() 호출 시 True 반환."""
        import position_monitor as pm
        import db
        tid = _insert_open_position("KRW-T3")
        pos = db.get_open_position("KRW-T3")
        client = FakeClient()

        seen_pending = threading.Event()
        sell_started = threading.Event()
        sell_can_finish = threading.Event()

        def slow_sell(c, p, reason, dry_run=False):
            sell_started.set()
            sell_can_finish.wait(timeout=2)
            db.close_trade(p["id"], 900, 10, 9000, reason, "uuid-y")
            from order_executor import OrderResult
            return OrderResult(True, p["symbol"], "sell", 900, 10, 9000)

        with mock.patch.object(pm.order_executor, "execute_sell", side_effect=slow_sell):
            t = threading.Thread(
                target=lambda: pm.safe_execute_sell(client, pos, "SELL_T3")
            )
            t.start()
            sell_started.wait(timeout=2)
            # 매도 진행 중 — is_pending 은 True 여야 함
            if pm.is_pending("KRW-T3"):
                seen_pending.set()
            sell_can_finish.set()
            t.join()

        self.assertTrue(seen_pending.is_set(), "in-flight 동안 is_pending 이 True 여야 함")
        self.assertFalse(pm.is_pending("KRW-T3"), "완료 후 is_pending False")

    def test_t4_stress_no_deadlock(self):
        """동일 심볼에 대해 50 스레드 동시 호출 — 데드락/예외 없이 1회만 통과."""
        import position_monitor as pm
        import db
        tid = _insert_open_position("KRW-T4")
        pos = db.get_open_position("KRW-T4")
        client = FakeClient()

        call_counter = {"n": 0}
        lock = threading.Lock()

        def fast_sell(c, p, reason, dry_run=False):
            with lock:
                call_counter["n"] += 1
            db.close_trade(p["id"], 900, 10, 9000, reason, "uuid-z")
            from order_executor import OrderResult
            return OrderResult(True, p["symbol"], "sell", 900, 10, 9000)

        with mock.patch.object(pm.order_executor, "execute_sell", side_effect=fast_sell):
            barrier = threading.Barrier(50)
            threads = []

            def worker(i):
                barrier.wait()
                pm.safe_execute_sell(client, pos, f"SELL_S_{i}")

            for i in range(50):
                threads.append(threading.Thread(target=worker, args=(i,)))
            for t in threads: t.start()
            for t in threads: t.join(timeout=10)

        self.assertEqual(call_counter["n"], 1, "stress 에서도 정확히 1회만 매도")
        self.assertEqual(len(pm._pending_exits), 0, "stress 후 pending_exits 비어있음")

    def test_t5_different_symbols_no_blocking(self):
        """서로 다른 심볼은 동시 매도 가능 (lock 분리)."""
        import position_monitor as pm
        import db
        for i in range(5):
            _insert_open_position(f"KRW-T5{i}")
        positions = [db.get_open_position(f"KRW-T5{i}") for i in range(5)]
        client = FakeClient()

        active = threading.Semaphore(0)
        proceed = threading.Event()

        def gated_sell(c, p, reason, dry_run=False):
            active.release()  # 진입 알림
            proceed.wait(timeout=3)
            db.close_trade(p["id"], 900, 10, 9000, reason, "uuid-w")
            from order_executor import OrderResult
            return OrderResult(True, p["symbol"], "sell")

        with mock.patch.object(pm.order_executor, "execute_sell", side_effect=gated_sell):
            ts = [threading.Thread(target=pm.safe_execute_sell,
                                   args=(client, positions[i], f"SELL_DIV_{i}"))
                  for i in range(5)]
            for t in ts: t.start()
            # 5개 모두 동시에 execute_sell 진입 가능해야 함
            for _ in range(5):
                self.assertTrue(active.acquire(timeout=3), "다른 심볼들이 병렬 진입 못함")
            proceed.set()
            for t in ts: t.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
