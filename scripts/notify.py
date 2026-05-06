"""Discord webhook 알림 — 데몬에서 import 하여 이벤트 전송.

사용:
    from notify import notify_buy, notify_sell, notify_error
    notify_buy(symbol="KRW-BTC", price=..., krw=..., grade="A", reason="...")
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

REPO_DIR = Path(__file__).resolve().parent.parent

# .env 로드 (이미 로드돼 있지 않으면)
_env_file = REPO_DIR / ".env"
if _env_file.exists() and not os.environ.get("DISCORD_WEBHOOK_URL"):
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 색상
GREEN = 0x22c55e
RED = 0xef4444
YELLOW = 0xeab308
BLUE = 0x3b82f6


def _mode() -> str:
    # 데몬이 런타임에 설정한 UPBIT_MODE 우선 (live/dry/paper),
    # 없으면 .env 의 초기값 사용.
    return os.environ.get("UPBIT_MODE", "dry_run").upper().replace("_", " ")


def _fmt_price(price: float) -> str:
    """가격 자릿수 적응 포맷 — KRW BTC(95M)부터 SHIB(0.025)까지 커버."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    if p >= 0.01:
        return f"{p:,.4f}"
    return f"{p:,.8f}".rstrip("0").rstrip(".")


def _fmt_qty(qty: float) -> str:
    """수량 자릿수 적응 포맷."""
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return str(qty)
    if q >= 1000:
        return f"{q:,.2f}"
    if q >= 1:
        return f"{q:.4f}"
    return f"{q:.8f}".rstrip("0").rstrip(".")


def _post(payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            WEBHOOK_URL, data=data,
            headers={
                "Content-Type": "application/json",
                # Discord 웹훅은 기본 urllib User-Agent 를 차단 → curl-like 로 위장
                "User-Agent": "upbit-trading-bot/1.0 (+https://discord.com/developers)",
            },
        )
        urlopen(req, timeout=5).close()
    except Exception as e:
        print(f"[notify] webhook 실패: {e}")


def _embed(title: str, description: str, color: int, fields: Optional[list] = None) -> dict:
    e = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": f"upbit-trading-system | {_mode()}"},
    }
    if fields:
        e["fields"] = fields
    return e


def notify_buy(
    symbol: str, price: float, krw: float, grade: str, strategy: str, reason: str
) -> None:
    desc = (
        f"**{symbol}** [{strategy}/{grade}]\n"
        f"가격: `{_fmt_price(price)}` · 금액: `{krw:,.0f}원`\n"
        f"사유: {(reason or '')[:300]}"
    )
    _post({"embeds": [_embed("🟢 매수 체결", desc, GREEN)]})


def notify_sell(
    symbol: str, price: float, pnl_pct: float, pnl_krw: float, reason: str,
    strategy: str = "",
) -> None:
    icon = "🟢" if pnl_pct >= 0 else "🔴"
    color = GREEN if pnl_pct >= 0 else RED
    head = f"**{symbol}**" + (f" [{strategy}]" if strategy else "")
    desc = (
        f"{head}\n"
        f"청산가: `{_fmt_price(price)}`\n"
        f"{icon} PnL: **{pnl_pct:+.2f}%** ({pnl_krw:+,.0f}원)\n"
        f"사유: {(reason or '')[:300]}"
    )
    _post({"embeds": [_embed("⚪ 매도 체결", desc, color)]})


def notify_partial_tp(
    symbol: str, tp_level: int, price: float, qty: float, pnl_pct: float,
    strategy: str = "",
) -> None:
    head = f"**{symbol}**" + (f" [{strategy}]" if strategy else "") + f" TP{tp_level}"
    desc = (
        f"{head}\n"
        f"청산가: `{_fmt_price(price)}` · 수량: `{_fmt_qty(qty)}`\n"
        f"PnL: **{pnl_pct:+.2f}%**"
    )
    _post({"embeds": [_embed("💰 분할 익절", desc, BLUE)]})


def notify_error(source: str, message: str) -> None:
    _post({"embeds": [_embed(f"⚠️ 오류 ({source})", message[:500], YELLOW)]})


def notify_daemon(event: str, description: str = "") -> None:
    color = GREEN if event in ("시작", "재시작") else RED
    _post({"embeds": [_embed(f"🤖 데몬 {event}", description, color)]})


if __name__ == "__main__":
    # 테스트 발송
    notify_daemon("테스트", "notify.py 테스트 메시지")
    print("테스트 완료." if WEBHOOK_URL else "WEBHOOK_URL 설정 없음.")
