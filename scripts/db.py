"""SQLite 저장소. data/trading.db 가 primary store."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

REPO_DIR = Path(__file__).resolve().parent.parent
DB_FILE = REPO_DIR / "data" / "trading.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,            -- VB / MB / MR
    side TEXT NOT NULL,                -- buy / sell
    status TEXT NOT NULL,              -- open / closed / cancelled
    entry_price REAL,
    entry_quantity REAL,
    entry_krw REAL,                    -- 매수 금액
    entry_date TEXT,
    entry_grade TEXT,
    entry_uuid TEXT,
    exit_price REAL,
    exit_quantity REAL,
    exit_krw REAL,
    exit_date TEXT,
    exit_reason TEXT,
    exit_uuid TEXT,
    pnl_krw REAL,
    pnl_pct REAL,
    max_favorable REAL,                -- MFE (최고 수익률)
    max_adverse REAL,                  -- MAE (최악 수익률)
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_date ON trades(entry_date);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,              -- BUY / HOLD / SELL_xxx / SKIP
    grade TEXT,                        -- A / B / C / D
    score INTEGER,
    score_max INTEGER,
    reason TEXT,
    details TEXT,                      -- JSON
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

CREATE TABLE IF NOT EXISTS daemon_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_num INTEGER,
    status TEXT,                       -- ok / error / paused
    positions_count INTEGER,
    signals_generated INTEGER,
    buys_attempted INTEGER,
    buys_filled INTEGER,
    sells_executed INTEGER,
    today_pnl_pct REAL,
    btc_regime TEXT,
    duration_sec REAL,
    error_msg TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_cycles_created ON daemon_cycles(created_at);

CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    total_krw REAL,                    -- 원화 + 코인평가액
    krw_balance REAL,
    coin_value_krw REAL,
    n_positions INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_date ON equity_curve(snapshot_date);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- daemon / signal / order / api
    message TEXT,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_errors_created ON errors(created_at);

CREATE TABLE IF NOT EXISTS screener_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT,
    grade TEXT,
    score INTEGER,
    score_max INTEGER,
    current_price REAL,
    volume_24h_krw REAL,
    change_24h_pct REAL,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_screener_scan ON screener_results(scan_time);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, coldef: str) -> None:
    """sqlite 는 ADD COLUMN IF NOT EXISTS 미지원 → PRAGMA 체크."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")


def init_db() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as c:
        c.executescript(SCHEMA)
        # 부분 익절 / 브레이크이븐 추적용 컬럼 (마이그레이션)
        _ensure_column(c, "trades", "tp_hits", "TEXT DEFAULT '[]'")        # JSON 배열, 예: [1,2]
        _ensure_column(c, "trades", "remaining_quantity", "REAL")           # 부분 매도 후 잔량
        _ensure_column(c, "trades", "realized_partial_krw", "REAL DEFAULT 0")  # 부분 실현 누적
        _ensure_column(c, "trades", "breakeven_armed", "INTEGER DEFAULT 0")  # 1=트리거 도달
        _ensure_column(c, "trades", "exit_time", "TEXT")                    # 재진입 쿨다운용 (YYYY-MM-DD HH:MM:SS)
        # mode: live / dry / paper — 기본값 'live' 이므로 기존 mock uuid 건은 교정 필요
        fresh = "mode" not in {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        _ensure_column(c, "trades", "mode", "TEXT DEFAULT 'live'")
        if fresh:
            # 과거 mock 주문 기록을 기반으로 기존 레코드 재분류
            c.execute("UPDATE trades SET mode='dry' WHERE entry_uuid LIKE 'mock-%'")
        # signals 테이블에도 mode 기록 (모드별 필터 대시보드용)
        _ensure_column(c, "signals", "mode", "TEXT DEFAULT 'live'")
        # daemon_cycles 에도 mode 기록 — paper/dry/live 기록 격리
        _ensure_column(c, "daemon_cycles", "mode", "TEXT DEFAULT 'live'")

    # TZ 마이그레이션 — 기존 created_at (UTC) 을 KST 로 일회성 보정
    _migrate_tz_to_kst()


def _migrate_tz_to_kst() -> None:
    """과거 CURRENT_TIMESTAMP(UTC) 로 기록된 created_at 을 KST(+9h) 로 보정.

    플래그 파일 존재 시 스킵 — 중복 실행으로 인한 18시간 이중 이동 방지.
    """
    flag = DB_FILE.parent / ".tz_migrated_kst"
    if flag.exists():
        return
    with get_conn() as c:
        # created_at 이 있는 모든 테이블 + screener_results 의 scan_time 은 원래 KST 였음
        for table in ("trades", "signals", "daemon_cycles",
                      "equity_curve", "errors", "screener_results"):
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = datetime(created_at, '+9 hours') "
                    f"WHERE created_at IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass  # 테이블이 아직 없거나 컬럼 없음
    flag.write_text(f"migrated at {datetime.now().isoformat()}\n")


def _now_kst() -> str:
    """KST 기준 현재 시각 문자열. DB 기록은 한국 시간으로 통일."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─── 조회 헬퍼 ───

def get_open_positions() -> list[dict[str, Any]]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_date"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_position(symbol: str) -> Optional[dict[str, Any]]:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM trades WHERE symbol=? AND status='open' LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def insert_trade(
    symbol: str,
    strategy: str,
    entry_price: float,
    entry_quantity: float,
    entry_krw: float,
    entry_grade: str,
    entry_uuid: str,
    notes: str = "",
    mode: str = "live",
) -> int:
    now = _now_kst()
    date_only = now.split()[0]
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO trades
               (symbol,strategy,side,status,entry_price,entry_quantity,entry_krw,
                entry_date,entry_grade,entry_uuid,notes,remaining_quantity,tp_hits,realized_partial_krw,mode,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                symbol, strategy, "buy", "open",
                entry_price, entry_quantity, entry_krw,
                date_only, entry_grade, entry_uuid, notes,
                entry_quantity, "[]", 0.0, mode, now,
            ),
        )
        return int(cur.lastrowid)


def apply_partial_exit(
    trade_id: int,
    tp_level: int,
    sold_qty: float,
    sold_krw: float,
) -> None:
    """부분 익절 반영 — trade 를 close 하지 않고 잔량만 감소."""
    with get_conn() as c:
        row = c.execute(
            "SELECT remaining_quantity, tp_hits, realized_partial_krw, entry_price FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            return
        try:
            hits = json.loads(row["tp_hits"] or "[]")
        except Exception:
            hits = []
        if tp_level not in hits:
            hits.append(tp_level)
        new_remaining = max(0.0, (row["remaining_quantity"] or 0.0) - sold_qty)
        new_realized = (row["realized_partial_krw"] or 0.0) + sold_krw
        c.execute(
            "UPDATE trades SET remaining_quantity=?, tp_hits=?, realized_partial_krw=? WHERE id=?",
            (new_remaining, json.dumps(hits), new_realized, trade_id),
        )


def arm_breakeven(trade_id: int) -> None:
    """브레이크이븐 트리거 도달 표시."""
    with get_conn() as c:
        c.execute("UPDATE trades SET breakeven_armed=1 WHERE id=?", (trade_id,))


def update_trade_entry(trade_id: int, entry_price: float, entry_quantity: float, entry_krw: float) -> None:
    """실체결 데이터로 진입가/수량/금액 보정. reconcile 용."""
    with get_conn() as c:
        c.execute(
            "UPDATE trades SET entry_price=?, entry_quantity=?, entry_krw=? WHERE id=?",
            (entry_price, entry_quantity, entry_krw, trade_id),
        )


def close_trade(
    trade_id: int,
    exit_price: float,
    exit_quantity: float,
    exit_krw: float,
    exit_reason: str,
    exit_uuid: str,
) -> None:
    with get_conn() as c:
        row = c.execute(
            "SELECT entry_krw,entry_quantity,realized_partial_krw FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            return
        entry_krw = row["entry_krw"] or 0.0
        realized_partial = row["realized_partial_krw"] or 0.0
        # 분할익절로 이미 실현된 금액 + 최종 청산액 = 총 회수금
        total_recovered = exit_krw + realized_partial
        pnl_krw = total_recovered - entry_krw
        pnl_pct = (pnl_krw / entry_krw * 100) if entry_krw > 0 else 0.0
        now = _now_kst()
        c.execute(
            """UPDATE trades
               SET status='closed',exit_price=?,exit_quantity=?,exit_krw=?,
                   exit_date=?,exit_time=?,exit_reason=?,exit_uuid=?,pnl_krw=?,pnl_pct=?
               WHERE id=?""",
            (exit_price, exit_quantity, exit_krw, now.split()[0], now,
             exit_reason, exit_uuid, pnl_krw, pnl_pct, trade_id),
        )


def update_mfe_mae(symbol: str, pnl_pct: float) -> None:
    """열린 포지션의 MFE/MAE 업데이트."""
    with get_conn() as c:
        c.execute(
            """UPDATE trades SET
                 max_favorable = CASE
                   WHEN max_favorable IS NULL OR ? > max_favorable THEN ?
                   ELSE max_favorable END,
                 max_adverse = CASE
                   WHEN max_adverse IS NULL OR ? < max_adverse THEN ?
                   ELSE max_adverse END
               WHERE symbol=? AND status='open'""",
            (pnl_pct, pnl_pct, pnl_pct, pnl_pct, symbol),
        )


def insert_signal(
    symbol: str,
    strategy: str,
    action: str,
    grade: str,
    score: int,
    score_max: int,
    reason: str,
    details: dict | None = None,
    mode: str = "live",
) -> None:
    with get_conn() as c:
        c.execute(
            """INSERT INTO signals (symbol,strategy,action,grade,score,score_max,reason,details,mode,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, strategy, action, grade, score, score_max, reason,
             json.dumps(details, ensure_ascii=False) if details else None,
             mode, _now_kst()),
        )


def insert_cycle(
    cycle_num: int, status: str, positions_count: int, signals_generated: int,
    buys_attempted: int, buys_filled: int, sells_executed: int,
    today_pnl_pct: float, btc_regime: str, duration_sec: float,
    error_msg: str = "",
    heartbeat_every: int = 30,
    mode: str = "live",
) -> bool:
    """사이클 기록.

    선별 저장 규칙: "변화가 있거나 heartbeat 주기(기본 30사이클=30분)에 걸릴 때"만 저장.
    무변화(포지션 0·시그널 0·매수/매도 0·에러 없음) 사이클은 생략해 누적을 줄인다.
    cycle_num 이 heartbeat_every 배수면 상태 확인용으로 기록.

    Returns: 실제 기록되었는지 여부.
    """
    is_interesting = (
        positions_count > 0
        or signals_generated > 0
        or buys_attempted > 0
        or buys_filled > 0
        or sells_executed > 0
        or (error_msg and error_msg.strip())
        or status != "ok"
    )
    is_heartbeat = heartbeat_every > 0 and cycle_num % heartbeat_every == 0
    if not (is_interesting or is_heartbeat):
        return False

    with get_conn() as c:
        c.execute(
            """INSERT INTO daemon_cycles
               (cycle_num,status,positions_count,signals_generated,buys_attempted,
                buys_filled,sells_executed,today_pnl_pct,btc_regime,duration_sec,error_msg,mode,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle_num, status, positions_count, signals_generated,
             buys_attempted, buys_filled, sells_executed, today_pnl_pct,
             btc_regime, duration_sec, error_msg, mode, _now_kst()),
        )
    return True


def insert_equity_snapshot(total_krw: float, krw_balance: float, coin_value_krw: float, n_positions: int) -> None:
    now = _now_kst()
    today = now.split()[0]
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO equity_curve
               (snapshot_date,total_krw,krw_balance,coin_value_krw,n_positions,created_at)
               VALUES (?,?,?,?,?,?)""",
            (today, total_krw, krw_balance, coin_value_krw, n_positions, now),
        )


def log_error(source: str, message: str, details: str = "") -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO errors (source,message,details,created_at) VALUES (?,?,?,?)",
            (source, message[:2000], details[:4000], _now_kst()),
        )


def insert_screener_results(rows: list[dict]) -> None:
    """스캔 결과 일괄 저장."""
    now = _now_kst()
    with get_conn() as c:
        for r in rows:
            c.execute(
                """INSERT INTO screener_results
                   (scan_time,symbol,strategy,grade,score,score_max,
                    current_price,volume_24h_krw,change_24h_pct,details,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (now, r["symbol"], r.get("strategy", ""),
                 r.get("grade", ""), r.get("score", 0), r.get("score_max", 100),
                 r.get("current_price"), r.get("volume_24h_krw"),
                 r.get("change_24h_pct"),
                 json.dumps(r.get("details", {}), ensure_ascii=False),
                 now),
            )


def today_pnl_pct(mode: str | None = None) -> float:
    """오늘 실현 손익률 (총자산 대비). mode 지정 시 해당 모드만 집계."""
    today = datetime.now().strftime("%Y-%m-%d")
    sql = "SELECT SUM(pnl_krw) as p, SUM(entry_krw) as e FROM trades WHERE exit_date=?"
    params: tuple = (today,)
    if mode in ("live", "dry", "paper"):
        sql += " AND mode=?"
        params = (today, mode)
    with get_conn() as c:
        row = c.execute(sql, params).fetchone()
    if not row or not row["e"]:
        return 0.0
    return float(row["p"] / row["e"] * 100)


def recently_closed_symbols(hours: float, mode: str | None = None) -> set[str]:
    """hours 시간 이내 청산된 심볼 집합. exit_time 기준 (없으면 exit_date 00:00 추정).

    재진입 쿨다운 필터에 사용. mode 지정 시 해당 모드 청산만 카운트 (paper↔live 격리).
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_date = cutoff.split()[0]
    sql = """SELECT DISTINCT symbol FROM trades
             WHERE status='closed'
               AND (
                 (exit_time IS NOT NULL AND exit_time >= ?)
                 OR (exit_time IS NULL AND exit_date >= ?)
               )"""
    params: tuple = (cutoff, cutoff_date)
    if mode in ("live", "dry", "paper"):
        sql += " AND mode=?"
        params = params + (mode,)
    with get_conn() as c:
        rows = c.execute(sql, params).fetchall()
    return {r[0] for r in rows}


def cleanup_old_records(
    days_signals: int = 14,
    days_cycles: int = 30,
    days_errors: int = 90,
    days_screener: int = 14,
    vacuum: bool = False,
) -> dict[str, int]:
    """보존 기간 초과 레코드 삭제.

    trades/equity_curve 는 영구 보존 → 건드리지 않음.
    vacuum=True 면 끝에 VACUUM 실행 (파일 크기 축소, 비용 큼 — 주 1회 권장).

    Returns: 테이블별 삭제 건수.
    """
    from datetime import timedelta
    now = datetime.now()
    deleted: dict[str, int] = {}
    with get_conn() as c:
        for table, col, days in [
            ("signals", "created_at", days_signals),
            ("daemon_cycles", "created_at", days_cycles),
            ("errors", "created_at", days_errors),
            ("screener_results", "scan_time", days_screener),
        ]:
            if days <= 0:
                continue
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cur = c.execute(
                f"DELETE FROM {table} WHERE {col} < ?", (cutoff,),
            )
            deleted[table] = cur.rowcount
    if vacuum:
        # VACUUM 은 단독 트랜잭션 필요
        conn = sqlite3.connect(DB_FILE, timeout=30)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        deleted["_vacuum"] = 1
    return deleted


def consecutive_losses(within_hours: float | None = None, mode: str | None = None) -> int:
    """가장 최근 연속 손절 수.

    within_hours 지정 시 해당 시간 내 청산된 거래만 대상 — 하루 지나면 자동 리셋.
    mode 지정 시 해당 모드(live/dry/paper)만 집계.
    """
    sql = "SELECT pnl_pct, exit_time, exit_date FROM trades WHERE status='closed'"
    params: tuple = ()
    if mode in ("live", "dry", "paper"):
        sql += " AND mode=?"
        params = (mode,)
    sql += " ORDER BY id DESC LIMIT 20"
    with get_conn() as c:
        rows = c.execute(sql, params).fetchall()
    if within_hours is not None:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=within_hours)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_date = cutoff_str.split()[0]
        filtered = []
        for r in rows:
            t = r["exit_time"]
            if t and t >= cutoff_str:
                filtered.append(r)
            elif not t and r["exit_date"] and r["exit_date"] >= cutoff_date:
                filtered.append(r)
        rows = filtered
    n = 0
    for r in rows:
        if (r["pnl_pct"] or 0) < 0:
            n += 1
        else:
            break
    return n


# ─── CLI ───
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: db.py [init|positions|recent-trades|cycles|errors]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "init":
        init_db()
        print(f"DB 초기화 완료: {DB_FILE}")
    elif cmd == "cleanup":
        vacuum = "--vacuum" in sys.argv
        result = cleanup_old_records(vacuum=vacuum)
        print(f"정리 완료: {result}")
    elif cmd == "positions":
        for p in get_open_positions():
            print(p)
    elif cmd == "recent-trades":
        with get_conn() as c:
            rows = c.execute(
                "SELECT symbol,strategy,status,entry_price,exit_price,pnl_pct,entry_date,exit_date,exit_reason FROM trades ORDER BY id DESC LIMIT 20"
            ).fetchall()
            for r in rows:
                print(dict(r))
    elif cmd == "cycles":
        with get_conn() as c:
            rows = c.execute(
                "SELECT cycle_num,status,positions_count,sells_executed,buys_filled,today_pnl_pct,btc_regime,created_at FROM daemon_cycles ORDER BY id DESC LIMIT 10"
            ).fetchall()
            for r in rows:
                print(dict(r))
    elif cmd == "errors":
        with get_conn() as c:
            rows = c.execute(
                "SELECT source,message,created_at FROM errors ORDER BY id DESC LIMIT 10"
            ).fetchall()
            for r in rows:
                print(dict(r))
