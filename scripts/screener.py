"""KRW 마켓 스크리닝.

흐름:
1. 전체 KRW 마켓 (~200개) 중 24h 거래대금 상위 N개만 후보
2. 각 후보에 대해 signal_engine.evaluate_symbol 호출
3. 등급 A/B 만 반환
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from upbit_client import UpbitClient
from signal_engine import Signal, evaluate_symbol

PROTECTED_FILE = Path(__file__).resolve().parent.parent / "data" / "protected-coins.json"


def load_protected_symbols() -> set[str]:
    """protected-coins.json 에 등록된 심볼 집합 반환 (소문자/대문자 무관 KRW-XRP 등 원본 유지)."""
    try:
        if not PROTECTED_FILE.exists():
            return set()
        data = json.loads(PROTECTED_FILE.read_text(encoding="utf-8"))
        return {item["symbol"] for item in data.get("protected", []) if "symbol" in item}
    except Exception as e:
        print(f"[screener] protected-coins 로드 실패: {e}")
        return set()


def get_universe(
    client: UpbitClient,
    min_volume_krw: float,
    top_n: int,
    top_by_spike_n: int = 0,
    spike_min_ratio: float = 3.0,
    stablecoin_exclude: list[str] | None = None,
) -> list[dict]:
    """후보 수집.

    채널A(선택): 24h 거래대금 상위 top_n — top_n=0 이면 비활성
    채널B(주력): 거래대금 급등비율(today_value / avg20_value) 상위 top_by_spike_n
    공통: min_volume_krw 하한 통과해야 함 (유동성 함정 방지)
    """
    tickers = client.get_tickers(fiat="KRW")
    if not tickers:
        return []

    # 보호 코인 제외 (사용자 외부 보유분)
    protected = load_protected_symbols()
    if protected:
        tickers = [t for t in tickers if t not in protected]

    # 스테이블코인 배제 (USDT/USDC 등 — 돌파/모멘텀 전략에 부적합)
    if stablecoin_exclude:
        excl = set(stablecoin_exclude)
        tickers = [t for t in tickers if t not in excl]

    # ─── 1차 필터: /v1/ticker 단일 호출로 24h 거래대금 사전 조회 ───
    # (~0.1s 로 200+ 종목 일괄 처리 → 하한 미달 종목은 일봉 조회 자체를 스킵)
    prefilter_pass: list[str] = []
    ticker_stats: dict[str, dict] = {}
    try:
        # markets 파라미터는 한 번에 제한이 있을 수 있어 100개 청크로 분할
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            resp = requests.get(
                "https://api.upbit.com/v1/ticker",
                params={"markets": ",".join(chunk)},
                timeout=5,
            )
            if resp.status_code == 200:
                for d in resp.json():
                    ticker_stats[d["market"]] = d
        prefilter_pass = [
            s for s in tickers
            if ticker_stats.get(s, {}).get("acc_trade_price_24h", 0) >= min_volume_krw
        ]
    except Exception as e:
        print(f"[screener] /v1/ticker 사전필터 실패, 전체 대상: {e}")
        prefilter_pass = tickers  # 폴백: 모두 조회

    # 현재가 (사전필터 통과분만) — 사전필터 실패시 전체
    prices = client.get_current_price(prefilter_pass)
    if not isinstance(prices, dict):
        prices = {}

    # ─── 2차: 통과분만 일봉 21봉 병렬 조회 ───
    def _fetch_day21(sym: str) -> tuple[str, pd.DataFrame | None]:
        try:
            return sym, client.get_ohlcv(sym, interval="day", count=21)
        except Exception:
            return sym, None

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_fetch_day21, s) for s in prefilter_pass]
        for f in as_completed(futures):
            sym, df = f.result()
            if df is None or df.empty:
                continue
            last = df.iloc[-1]
            vol_krw = float(last["close"] * last["volume"])
            if vol_krw < min_volume_krw:
                continue

            # 20일 평균 대비 오늘 거래대금 배수 (채널B 핵심 지표)
            # 거래대금 = close * volume. 가격 폭락 + 거래량 폭증 케이스를 자연 필터링.
            spike_ratio = 1.0
            if len(df) >= 21:
                values = (df["close"] * df["volume"]).astype(float)
                avg20_value = float(values.iloc[-21:-1].mean())
                if avg20_value > 0:
                    spike_ratio = float(values.iloc[-1]) / avg20_value

            prev = df.iloc[-2] if len(df) >= 2 else last
            change_24h = (last["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] else 0

            rows.append({
                "symbol": sym,
                "current_price": float(prices.get(sym, last["close"])),
                "volume_24h_krw": vol_krw,
                "change_24h_pct": change_24h,
                "value_spike_ratio": spike_ratio,
            })

    # 채널A: 거래대금 순 (top_n=0 이면 비활성)
    picked: dict[str, dict] = {}
    if top_n > 0:
        by_volume = sorted(rows, key=lambda r: r["volume_24h_krw"], reverse=True)[:top_n]
        picked = {r["symbol"]: r for r in by_volume}

    # 채널B: 거래대금 급등 비율 순 (min_ratio 필터 통과분)
    if top_by_spike_n > 0:
        spike_candidates = [r for r in rows if r["value_spike_ratio"] >= spike_min_ratio]
        by_spike = sorted(spike_candidates, key=lambda r: r["value_spike_ratio"], reverse=True)[:top_by_spike_n]
        for r in by_spike:
            if r["symbol"] not in picked:
                picked[r["symbol"]] = r

    return list(picked.values())


def _spread_pct(client: UpbitClient, symbol: str) -> float:
    """호가 스프레드(%). 실패 시 99 (필터 아웃)."""
    try:
        ob = client.get_orderbook(symbol)
        if isinstance(ob, list) and ob:
            ob = ob[0]
        if not isinstance(ob, dict):
            return 99.0
        units = ob.get("orderbook_units") or []
        if not units:
            return 99.0
        ask = float(units[0]["ask_price"])
        bid = float(units[0]["bid_price"])
        if bid <= 0:
            return 99.0
        return (ask - bid) / bid * 100
    except Exception:
        return 99.0


def screen(client: UpbitClient, cfg: dict) -> list[dict]:
    """스크리닝 → Grade A/B 시그널 리스트.

    개선:
    - 스프레드 0.2%↑ 시 등급 한 단계 강등 (실행 가능성)
    - Tie-breaker 정렬: score → 거래대금 → 스프레드 순
    """
    universe = get_universe(
        client,
        min_volume_krw=cfg["screener_min_volume_krw"],
        top_n=cfg.get("screener_top_by_volume_n", cfg.get("screener_top_n", 15)),
        top_by_spike_n=cfg.get("screener_top_by_spike_n", 0),
        spike_min_ratio=cfg.get("screener_spike_min_ratio", 3.0),
        stablecoin_exclude=cfg.get("stablecoin_exclude"),
    )

    reject_pct = float(cfg.get("screener_spread_reject_pct", 0.4))
    downgrade_pct = float(cfg.get("screener_spread_downgrade_pct", 0.2))
    vs_spread_max = float(cfg.get("vs_max_spread_pct", 0.3))

    def _evaluate_one(u: dict) -> list[dict]:
        """단일 심볼 평가 + 스프레드 조회 + 등급 보정."""
        sym = u["symbol"]
        try:
            sigs = evaluate_symbol(client, sym, cfg)
        except Exception as e:
            print(f"[screener] {sym} 평가 실패: {e}")
            return []
        buy_sigs = [s for s in sigs if s.action == "BUY"]
        if not buy_sigs:
            return []
        spread_pct = _spread_pct(client, sym)
        # 스프레드 완전 거부
        if spread_pct >= reject_pct:
            return []
        out: list[dict] = []
        for s in buy_sigs:
            original_grade = s.grade
            if s.strategy == "VS" and spread_pct > vs_spread_max:
                continue
            if spread_pct >= downgrade_pct:
                s.grade = {"A": "B", "B": "C", "C": "D"}.get(s.grade, s.grade)
                s.reason = f"{s.reason} / 스프레드 {spread_pct:.2f}% 강등"
            if s.grade not in ("A", "B"):
                continue
            row = s.to_dict()
            row["current_price"] = s.entry_price
            row["volume_24h_krw"] = u["volume_24h_krw"]
            row["change_24h_pct"] = u["change_24h_pct"]
            row["value_spike_ratio"] = round(u.get("value_spike_ratio", 1.0), 2)
            row["spread_pct"] = round(spread_pct, 3)
            row["original_grade"] = original_grade
            out.append(row)
        return out

    results: list[dict] = []
    # 병렬 평가 — 레이트리밋은 client._quote_limiter 가 전역으로 관리
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_evaluate_one, u) for u in universe]
        for f in as_completed(futures):
            try:
                results.extend(f.result())
            except Exception as e:
                print(f"[screener] 평가 future 실패: {e}")

    # Tie-breaker: score 내림 → 거래대금 내림 → 스프레드 오름
    results.sort(key=lambda r: (
        -r["score"],
        -r["volume_24h_krw"],
        r.get("spread_pct", 99),
    ))
    return results


# ─── CLI ───
if __name__ == "__main__":
    import json
    from config import get_api_keys, load_config

    cfg = load_config()
    c = UpbitClient(*get_api_keys(), dry_run=True)
    res = screen(c, cfg)
    print(f"=== 스크리닝 결과: {len(res)}개 (A/B) ===")
    for r in res[:20]:
        print(f"{r['symbol']:<15} {r['strategy']:<4} {r['grade']} "
              f"{r['score_pct']}% | {r['reason']}")
