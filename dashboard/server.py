"""대시보드 HTTP 서버. http://localhost:8766"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "scripts"))

import db  # noqa: E402
from config import get_api_keys  # noqa: E402
from upbit_client import UpbitClient  # noqa: E402

PORT = 8766

# 현재가 조회용 클라이언트 (dry_run — 주문 안 함)
_client = UpbitClient(*get_api_keys(), dry_run=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        pass  # 조용히

    def do_GET(self):
        path = self.path.split("?")[0]
        mode = self._query_param("mode")  # live / dry / paper / all(기본)
        if path in ("/", "/index.html"):
            self._serve_file(REPO_DIR / "dashboard" / "index.html", "text/html")
        elif path == "/api/positions":
            self._json(self._positions_with_pnl(mode))
        elif path == "/api/recent-trades":
            where = self._mode_where(mode)
            self._json(self._fetch(f"SELECT * FROM trades{where} ORDER BY id DESC LIMIT 50"))
        elif path == "/api/cycles":
            self._json(self._fetch(
                "SELECT cycle_num,status,positions_count,signals_generated,"
                "buys_filled,sells_executed,today_pnl_pct,btc_regime,duration_sec,created_at "
                "FROM daemon_cycles ORDER BY id DESC LIMIT 30"))
        elif path == "/api/signals":
            where = self._mode_where(mode)
            self._json(self._fetch(
                "SELECT symbol,strategy,action,grade,score,score_max,reason,mode,created_at "
                f"FROM signals{where} ORDER BY id DESC LIMIT 30"))
        elif path == "/api/equity":
            self._json(self._fetch(
                "SELECT snapshot_date,total_krw,krw_balance,coin_value_krw,n_positions "
                "FROM equity_curve ORDER BY snapshot_date DESC LIMIT 90"))
        elif path == "/api/stats":
            self._json(self._stats(mode))
        elif path == "/api/pnl-history":
            days = 30
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in q.split("&"):
                if kv.startswith("days="):
                    try:
                        days = int(kv.split("=", 1)[1])
                    except ValueError:
                        pass
            self._json(self._pnl_history(days, mode))
        elif path == "/api/errors":
            self._json(self._fetch(
                "SELECT source,message,created_at FROM errors ORDER BY id DESC LIMIT 20"))
        else:
            self.send_error(404)

    def _query_param(self, key: str, default: str = "") -> str:
        if "?" not in self.path:
            return default
        q = self.path.split("?", 1)[1]
        for kv in q.split("&"):
            if kv.startswith(f"{key}="):
                return kv.split("=", 1)[1]
        return default

    @staticmethod
    def _mode_where(mode: str, column_prefix: str = "") -> str:
        """mode 파라미터 → WHERE 절. all/빈값 = 필터 없음."""
        if mode in ("live", "dry", "paper"):
            col = f"{column_prefix}mode" if column_prefix else "mode"
            return f" WHERE {col}='{mode}'"
        return ""

    def _positions_with_pnl(self, mode: str = "") -> list[dict]:
        positions = db.get_open_positions()
        if mode in ("live", "dry", "paper"):
            positions = [p for p in positions if p.get("mode") == mode]
        if not positions:
            return []
        symbols = [p["symbol"] for p in positions]
        try:
            prices = _client.get_current_price(symbols)
            if not isinstance(prices, dict):
                # 단일 심볼이면 float 반환 → dict 으로 감싸기
                prices = {symbols[0]: prices} if len(symbols) == 1 else {}
        except Exception:
            prices = {}
        for p in positions:
            entry = float(p.get("entry_price") or 0)
            qty = float(p.get("entry_quantity") or 0)
            cur = prices.get(p["symbol"])
            if isinstance(cur, (int, float)) and cur > 0 and entry > 0:
                p["current_price"] = float(cur)
                p["pnl_pct"] = (cur - entry) / entry * 100
                p["pnl_krw"] = (cur - entry) * qty
            else:
                p["current_price"] = None
                p["pnl_pct"] = None
                p["pnl_krw"] = None
        return positions

    def _fetch(self, sql: str) -> list[dict]:
        with db.get_conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def _stats(self, mode: str = "") -> dict:
        mode_and = f" AND mode='{mode}'" if mode in ("live", "dry", "paper") else ""
        with db.get_conn() as c:
            total = c.execute(f"SELECT COUNT(*) n FROM trades WHERE status='closed'{mode_and}").fetchone()["n"]
            row = c.execute(
                f"SELECT SUM(pnl_krw) pnl, SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) wins "
                f"FROM trades WHERE status='closed'{mode_and}"
            ).fetchone()
            open_n = c.execute(f"SELECT COUNT(*) n FROM trades WHERE status='open'{mode_and}").fetchone()["n"]
        m = mode if mode in ("live", "dry", "paper") else None
        return {
            "total_closed": total,
            "total_open": open_n,
            "cumulative_pnl_krw": row["pnl"] or 0,
            "win_rate": round((row["wins"] or 0) / total * 100, 1) if total else 0,
            "today_pnl_pct": db.today_pnl_pct(mode=m),
            "consecutive_losses": db.consecutive_losses(mode=m),
        }

    def _pnl_history(self, days: int, mode: str = "") -> list[dict]:
        """청산된 거래 기준 누적 PnL 시계열.

        exit_time 우선, 없으면 exit_date 23:59:59 로 대체.
        Returns: [{t, pnl_krw, cumulative}, ...] (시간 오름차순)
        """
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        mode_and = f" AND mode='{mode}'" if mode in ("live", "dry", "paper") else ""
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT COALESCE(exit_time, exit_date || ' 23:59:59') AS t, "
                "symbol, pnl_krw, pnl_pct FROM trades "
                "WHERE status='closed' AND pnl_krw IS NOT NULL "
                f"{mode_and} "
                "AND COALESCE(exit_time, exit_date || ' 23:59:59') >= ? "
                "ORDER BY t ASC",
                (cutoff_str,),
            ).fetchall()
        cum = 0.0
        out: list[dict] = []
        for r in rows:
            pnl = float(r["pnl_krw"] or 0)
            cum += pnl
            out.append({
                "t": r["t"],
                "symbol": r["symbol"],
                "pnl_krw": pnl,
                "pnl_pct": r["pnl_pct"],
                "cumulative": cum,
            })
        return out

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def _serve_file(self, path: Path, ctype: str):
        if not path.exists():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.end_headers()
        self.wfile.write(path.read_bytes())


def main() -> None:
    db.init_db()
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"대시보드: http://localhost:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
