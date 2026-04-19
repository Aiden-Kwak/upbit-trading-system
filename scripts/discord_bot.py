#!/usr/bin/env python3
"""Upbit 자동매매용 Discord 명령 봇.

채널에서 ! 명령어로 시스템을 조회/제어합니다.

사용법:
  .venv/bin/python3 scripts/discord_bot.py

명령어:
  !help       — 명령어 목록
  !positions  — 보유 포지션 (현재가 + 수익률 실시간)
  !trades [N] — 최근 청산 N건 (기본 5)
  !today      — 오늘 거래 요약
  !status     — 데몬 상태
  !scan       — 최근 스크리닝 결과
  !ping       — 봇 응답 테스트
  !daemon start|stop — 데몬 제어
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import discord

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# ─── .env 로드 ───
_env_file = REPO_DIR / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

import db  # noqa: E402
from config import get_api_keys, load_config  # noqa: E402
from upbit_client import UpbitClient  # noqa: E402

# ─── 색상 ───
GREEN = 0x22c55e
RED = 0xef4444
YELLOW = 0xeab308
BLUE = 0x3b82f6
PURPLE = 0xa855f7

# ─── 공용 클라이언트 (가격 조회용) ───
_client = UpbitClient(*get_api_keys(), dry_run=True)


def mode_label() -> str:
    m = os.environ.get("UPBIT_MODE", "dry_run").lower()
    return {"dry_run": "DRY RUN", "paper": "PAPER", "live": "LIVE"}.get(m, m.upper())


def footer() -> dict:
    return {"text": f"upbit-trading-system | {mode_label()}"}


# ─── 명령 핸들러 ───

def cmd_help() -> discord.Embed:
    desc = "\n".join([
        "**조회:**",
        "`!positions` / `!pos` — 보유 포지션 (실시간 수익률)",
        "`!trades [N]` — 최근 N건 청산 (기본 5)",
        "`!today` — 오늘 거래 요약",
        "`!status` — 데몬 상태",
        "`!scan` — 최근 스크리닝 결과",
        "",
        "**제어:**",
        "`!daemon start [--paper]` — 데몬 시작",
        "`!daemon stop` — 데몬 종료",
        "",
        "**기타:**",
        "`!ping` — 봇 응답",
        "`!help` — 이 도움말",
    ])
    return discord.Embed(
        title="📖 Upbit 트레이딩 봇", description=desc, color=PURPLE
    ).set_footer(**footer())


def cmd_positions() -> discord.Embed:
    positions = db.get_open_positions()
    if not positions:
        return discord.Embed(
            title="📋 보유 포지션",
            description="보유 중인 포지션이 없습니다.",
            color=PURPLE,
        ).set_footer(**footer())

    # 현재가 일괄 조회
    symbols = [p["symbol"] for p in positions]
    prices = {}
    try:
        r = _client.get_current_price(symbols)
        if isinstance(r, dict):
            prices = r
        elif isinstance(r, (int, float)) and len(symbols) == 1:
            prices = {symbols[0]: r}
    except Exception:
        pass

    embed = discord.Embed(
        title=f"📋 보유 포지션 ({len(positions)}건)", color=PURPLE
    )
    total_pnl = 0.0
    total_entry = 0.0
    for p in positions[:25]:
        sym = p["symbol"]
        entry = float(p.get("entry_price") or 0)
        qty = float(p.get("remaining_quantity") or p.get("entry_quantity") or 0)
        entry_krw = float(p.get("entry_krw") or 0)
        cur = prices.get(sym)
        grade = p.get("entry_grade", "?")
        strategy = p.get("strategy", "")

        if isinstance(cur, (int, float)) and cur > 0 and entry > 0:
            pnl_pct = (cur - entry) / entry * 100
            pnl_krw = (cur - entry) * qty
            total_pnl += pnl_krw
            total_entry += entry_krw
            icon = "🟢" if pnl_pct >= 0 else "🔴"
            value = (
                f"{strategy}/{grade} | {qty:.4f}개\n"
                f"진입 {entry:,.0f} → 현재 {cur:,.0f}\n"
                f"{icon} {pnl_pct:+.2f}% ({pnl_krw:+,.0f}원)"
            )
        else:
            value = f"{strategy}/{grade} | {qty:.4f}개\n진입 {entry:,.0f}\n(현재가 조회 실패)"
        embed.add_field(name=sym, value=value, inline=True)

    if total_entry > 0:
        total_pct = total_pnl / total_entry * 100
        icon = "🟢" if total_pnl >= 0 else "🔴"
        embed.description = f"**{icon} 미실현: {total_pct:+.2f}% ({total_pnl:+,.0f}원)**"
    return embed.set_footer(**footer())


def cmd_trades(count: int = 5) -> discord.Embed:
    with db.get_conn() as c:
        all_closed = c.execute(
            "SELECT symbol,strategy,pnl_pct,pnl_krw,exit_reason,entry_date,exit_date "
            "FROM trades WHERE status='closed' ORDER BY id DESC"
        ).fetchall()
    all_closed = [dict(r) for r in all_closed]
    recent = all_closed[:count]
    if not recent:
        return discord.Embed(
            title="📜 최근 거래", description="청산된 거래가 없습니다.", color=PURPLE
        ).set_footer(**footer())

    wins = sum(1 for t in all_closed if (t.get("pnl_pct") or 0) > 0)
    total_pnl = sum(t.get("pnl_krw") or 0 for t in all_closed)
    win_rate = wins / len(all_closed) * 100 if all_closed else 0
    desc = (
        f"전체 {len(all_closed)}건 | 승률 {win_rate:.0f}% | "
        f"누적 {total_pnl:+,.0f}원\n\n"
    )
    for t in recent:
        pnl = t.get("pnl_pct") or 0
        icon = "🟢" if pnl >= 0 else "🔴"
        desc += (
            f"{icon} **{t['symbol']}** ({t.get('strategy','')}) "
            f"{pnl:+.2f}% — {t.get('exit_reason') or ''} "
            f"({t.get('exit_date') or ''})\n"
        )
    return discord.Embed(
        title=f"📜 최근 청산 {len(recent)}건", description=desc, color=PURPLE
    ).set_footer(**footer())


def cmd_today() -> discord.Embed:
    today = datetime.now().strftime("%Y-%m-%d")
    with db.get_conn() as c:
        rows = c.execute(
            "SELECT symbol,strategy,pnl_pct,pnl_krw,status,exit_reason "
            "FROM trades WHERE entry_date=? OR exit_date=?",
            (today, today),
        ).fetchall()
    rows = [dict(r) for r in rows]
    opened = [r for r in rows if r.get("status") == "open"]
    closed = [r for r in rows if r.get("status") == "closed"]
    pnl_krw = sum(r.get("pnl_krw") or 0 for r in closed)
    wins = sum(1 for r in closed if (r.get("pnl_pct") or 0) > 0)

    desc = f"**매수 {len(opened)}건 · 청산 {len(closed)}건**\n"
    if closed:
        desc += f"수익: **{pnl_krw:+,.0f}원** | 승률 {wins}/{len(closed)}\n\n"
        for t in closed[:10]:
            p = t.get("pnl_pct") or 0
            icon = "🟢" if p >= 0 else "🔴"
            desc += f"{icon} {t['symbol']} {p:+.2f}% — {t.get('exit_reason') or ''}\n"
    color = GREEN if pnl_krw > 0 else (RED if pnl_krw < 0 else PURPLE)
    return discord.Embed(
        title=f"📅 오늘 거래 | {today}", description=desc, color=color
    ).set_footer(**footer())


def _daemon_running() -> bool:
    try:
        ps = subprocess.run(
            ["pgrep", "-f", "autotrade_daemon.py"], capture_output=True, text=True
        )
        return ps.returncode == 0
    except Exception:
        return False


def cmd_status() -> discord.Embed:
    with db.get_conn() as c:
        cycle_row = c.execute(
            "SELECT cycle_num,status,positions_count,today_pnl_pct,btc_regime,"
            "signals_generated,buys_filled,sells_executed,created_at "
            "FROM daemon_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        err_row = c.execute(
            "SELECT source,message,created_at FROM errors ORDER BY id DESC LIMIT 1"
        ).fetchone()

    alive = _daemon_running()
    proc_label = "🟢 프로세스 활성" if alive else "⚠️ 프로세스 없음"
    desc = f"{proc_label}\n"
    if cycle_row:
        r = dict(cycle_row)
        desc += (
            f"**사이클**: #{r['cycle_num']} ({r.get('status')})\n"
            f"**BTC 레짐**: {r.get('btc_regime','?')}\n"
            f"**보유**: {r.get('positions_count',0)}개 | "
            f"신호 {r.get('signals_generated',0)} | "
            f"매수 {r.get('buys_filled',0)} | 매도 {r.get('sells_executed',0)}\n"
            f"**오늘 PnL**: {r.get('today_pnl_pct') or 0:+.2f}%\n"
            f"**마지막**: {r.get('created_at','-')}\n"
        )
    else:
        desc += "사이클 기록 없음\n"
    if err_row:
        e = dict(err_row)
        desc += f"\n**최근 에러** ({e['source']}): {e['message'][:150]}\n`{e['created_at']}`"

    color = GREEN if alive else RED
    return discord.Embed(title="🤖 데몬 상태", description=desc, color=color).set_footer(**footer())


def cmd_scan() -> discord.Embed:
    with db.get_conn() as c:
        last = c.execute(
            "SELECT DISTINCT scan_time FROM screener_results ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not last:
            return discord.Embed(
                title="🔍 스크리닝", description="스캔 기록 없음.", color=PURPLE
            ).set_footer(**footer())
        rows = c.execute(
            "SELECT symbol,strategy,grade,score,score_max,volume_24h_krw,change_24h_pct "
            "FROM screener_results WHERE scan_time=? ORDER BY score DESC",
            (last["scan_time"],),
        ).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        return discord.Embed(
            title=f"🔍 스크리닝 {last['scan_time']}",
            description="A/B 후보 없음.", color=PURPLE
        ).set_footer(**footer())

    desc = ""
    for r in rows[:15]:
        icon = "🅰️" if r["grade"] == "A" else "🅱️"
        vol_eok = (r.get("volume_24h_krw") or 0) / 1e8
        desc += (
            f"{icon} **{r['symbol']}** [{r['strategy']}] "
            f"{r['score']}/{r['score_max']} · {vol_eok:.0f}억 · "
            f"{r.get('change_24h_pct') or 0:+.1f}%\n"
        )
    return discord.Embed(
        title=f"🔍 스크리닝 결과 | {last['scan_time']}",
        description=desc, color=GREEN,
    ).set_footer(**footer())


async def cmd_daemon(action: str, args: list[str]) -> discord.Embed:
    if action == "start":
        if _daemon_running():
            return discord.Embed(
                title="🤖 데몬 제어",
                description="이미 실행 중. 먼저 `!daemon stop`.", color=YELLOW,
            ).set_footer(**footer())
        paper = "--paper" in args
        dry = "--dry-run" in args or paper
        cmd = [sys.executable, str(SCRIPTS_DIR / "autotrade_daemon.py")]
        if paper:
            cmd.append("--paper")
        elif dry:
            cmd.append("--dry-run")
        log_path = REPO_DIR / "logs" / "daemon-stdout.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            cmd, stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        label = "PAPER" if paper else ("DRY" if dry else "LIVE")
        return discord.Embed(
            title="🟢 데몬 시작", description=f"모드 **{label}**", color=GREEN
        ).set_footer(**footer())
    elif action == "stop":
        if not _daemon_running():
            return discord.Embed(
                title="🤖 데몬 제어", description="실행 중인 데몬 없음.", color=YELLOW
            ).set_footer(**footer())
        subprocess.run(["pkill", "-f", "autotrade_daemon.py"], capture_output=True)
        await asyncio.sleep(2)
        still = _daemon_running()
        return discord.Embed(
            title="⬛ 데몬 종료",
            description="종료 완료." if not still else "종료 실패 — 프로세스 잔존.",
            color=RED if not still else YELLOW,
        ).set_footer(**footer())
    else:
        return discord.Embed(
            title="🤖 데몬 제어",
            description="사용법: `!daemon start [--paper|--dry-run]` · `!daemon stop`",
            color=YELLOW,
        ).set_footer(**footer())


# ─── Bot 실행 ───

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[Bot] {client.user} 로그인 완료 (guilds: {[g.name for g in client.guilds]})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content = message.content.strip()
    if not content.startswith("!"):
        return
    parts = content[1:].split()
    if not parts:
        return
    cmd = parts[0].lower()

    try:
        if cmd == "help":
            await message.channel.send(embed=cmd_help())
        elif cmd in ("positions", "pos"):
            await message.channel.send(embed=cmd_positions())
        elif cmd == "trades":
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
            await message.channel.send(embed=cmd_trades(n))
        elif cmd == "today":
            await message.channel.send(embed=cmd_today())
        elif cmd == "status":
            await message.channel.send(embed=cmd_status())
        elif cmd == "scan":
            await message.channel.send(embed=cmd_scan())
        elif cmd == "daemon":
            action = parts[1].lower() if len(parts) > 1 else ""
            await message.channel.send(embed=await cmd_daemon(action, parts[2:]))
        elif cmd == "ping":
            await message.channel.send(embed=discord.Embed(
                title="🏓 Pong!",
                description=f"지연 {round(client.latency * 1000)}ms",
                color=GREEN,
            ).set_footer(**footer()))
    except Exception as e:
        await message.channel.send(embed=discord.Embed(
            title="❌ 오류", description=str(e)[:500], color=RED,
        ).set_footer(**footer()))


if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_BOT_TOKEN 환경변수가 없습니다. .env 확인.")
        sys.exit(1)
    client.run(TOKEN)
