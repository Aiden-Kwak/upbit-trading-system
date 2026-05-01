"""Upbit API 클라이언트 — pyupbit 래퍼 + 레이트리밋 + 재시도.

안전 규칙:
- 레이트리밋: 주문 초당 8회, 조회 초당 10회. 위반 시 Upbit 페널티.
- 지수 백오프 재시도 (최대 3회).
- dry_run 모드에서는 주문 함수가 mock 응답만 반환.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional

import pyupbit
import pandas as pd

log = logging.getLogger(__name__)


class RateLimiter:
    """초당 요청 수 제한. sliding window."""

    def __init__(self, max_per_sec: int):
        self.max_per_sec = max_per_sec
        self.ts: list[float] = []
        self.lock = Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            # 1초 이전 기록 제거
            self.ts = [t for t in self.ts if now - t < 1.0]
            if len(self.ts) >= self.max_per_sec:
                sleep_for = 1.0 - (now - self.ts[0]) + 0.01
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self.ts.append(time.time())


# 전역 리미터 — 업비트 공식 한도보다 여유있게
_quote_limiter = RateLimiter(max_per_sec=8)
_order_limiter = RateLimiter(max_per_sec=6)


def _retry(fn, *args, retries: int = 3, base_delay: float = 0.5, **kwargs):
    """지수 백오프 재시도. 네트워크 오류만 재시도 (비즈니스 에러는 즉시 반환)."""
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # 재시도해도 소용없는 에러는 즉시 중단
            if any(k in msg for k in ["invalid", "unauthorized", "forbidden", "insufficient"]):
                raise
            if i < retries - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_err  # type: ignore


@dataclass
class UpbitClient:
    """pyupbit.Upbit 래퍼. access/secret 이 없으면 조회 전용 모드.

    모드 조합:
    - dry_run=False, paper=False: 실거래
    - dry_run=True,  paper=False: mock 주문 + 실제 계정 잔고 조회 (지금까지 기본값)
    - dry_run=True,  paper=True:  가상 잔고 + mock 주문 + 체결 가정 (end-to-end 검증)
    """

    access_key: str = ""
    secret_key: str = ""
    dry_run: bool = True
    paper: bool = False
    paper_initial_krw: float = 1_000_000.0

    def __post_init__(self):
        self._upbit: Optional[pyupbit.Upbit] = None
        if self.access_key and self.secret_key:
            self._upbit = pyupbit.Upbit(self.access_key, self.secret_key)
        self._ticker_cache: dict[str, tuple[float, float]] = {}  # symbol → (ts, price)
        # ─── paper 잔고 ───
        self._paper_krw: float = self.paper_initial_krw if self.paper else 0.0
        self._paper_coins: dict[str, float] = {}  # symbol → quantity
        if self.paper:
            log.info(f"[paper] 가상 잔고 초기화: {self._paper_krw:,.0f}원")

    # ─── 시세 조회 ───

    def get_tickers(self, fiat: str = "KRW") -> list[str]:
        """KRW 마켓 전체 심볼."""
        _quote_limiter.wait()
        return _retry(pyupbit.get_tickers, fiat=fiat)

    def get_current_price(self, symbol: str | list[str]) -> float | dict[str, float] | None:
        """현재가 조회. list 입력 시 dict 반환."""
        _quote_limiter.wait()
        return _retry(pyupbit.get_current_price, symbol)

    def get_ohlcv(
        self,
        symbol: str,
        interval: str = "day",
        count: int = 200,
        to: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        """OHLCV 조회.

        interval: day, minute1, minute3, minute5, minute15, minute30, minute60, minute240, week, month
        """
        _quote_limiter.wait()
        df = _retry(pyupbit.get_ohlcv, symbol, interval=interval, count=count, to=to)
        return df

    def get_orderbook(self, symbol: str) -> Optional[dict]:
        _quote_limiter.wait()
        return _retry(pyupbit.get_orderbook, symbol)

    # ─── 계좌 조회 ───

    def get_balance_krw(self) -> float:
        """원화 잔고."""
        if self.paper:
            return self._paper_krw
        if not self._upbit:
            return 0.0
        _quote_limiter.wait()
        return float(_retry(self._upbit.get_balance, "KRW") or 0.0)

    def get_balance(self, symbol: str) -> float:
        """특정 코인 보유 수량."""
        if self.paper:
            return self._paper_coins.get(symbol, 0.0)
        if not self._upbit:
            return 0.0
        _quote_limiter.wait()
        return float(_retry(self._upbit.get_balance, symbol) or 0.0)

    def get_balances(self) -> list[dict]:
        """모든 보유 자산."""
        if not self._upbit:
            return []
        _quote_limiter.wait()
        res = _retry(self._upbit.get_balances) or []
        return res if isinstance(res, list) else []

    def get_avg_buy_price(self, symbol: str) -> float:
        if not self._upbit:
            return 0.0
        _quote_limiter.wait()
        return float(_retry(self._upbit.get_avg_buy_price, symbol) or 0.0)

    # ─── 주문 실행 (dry_run 분기) ───

    def buy_limit(self, symbol: str, price: float, quantity: float) -> dict:
        """지정가 매수."""
        if self.dry_run or not self._upbit:
            self._paper_apply_buy(symbol, price, quantity)
            return self._mock_order("buy_limit", symbol, price, quantity)
        _order_limiter.wait()
        return _retry(self._upbit.buy_limit_order, symbol, price, quantity)

    def sell_limit(self, symbol: str, price: float, quantity: float) -> dict:
        if self.dry_run or not self._upbit:
            self._paper_apply_sell(symbol, price, quantity)
            return self._mock_order("sell_limit", symbol, price, quantity)
        _order_limiter.wait()
        return _retry(self._upbit.sell_limit_order, symbol, price, quantity)

    def buy_market(self, symbol: str, krw_amount: float) -> dict:
        """시장가 매수. krw_amount = 쓸 원화 금액."""
        if self.dry_run or not self._upbit:
            # paper: 시장가 매수는 현재가 기준으로 수량 산출
            if self.paper:
                cur = self.get_current_price(symbol)
                if isinstance(cur, (int, float)) and cur > 0:
                    qty = krw_amount / float(cur)
                    self._paper_apply_buy(symbol, float(cur), qty)
            return self._mock_order("buy_market", symbol, None, krw_amount)
        _order_limiter.wait()
        return _retry(self._upbit.buy_market_order, symbol, krw_amount)

    def sell_market(self, symbol: str, quantity: float) -> dict:
        if self.dry_run or not self._upbit:
            if self.paper:
                cur = self.get_current_price(symbol)
                if isinstance(cur, (int, float)) and cur > 0:
                    self._paper_apply_sell(symbol, float(cur), quantity)
            return self._mock_order("sell_market", symbol, None, quantity)
        _order_limiter.wait()
        return _retry(self._upbit.sell_market_order, symbol, quantity)

    # ─── paper 잔고 적용 ───

    def _paper_apply_buy(self, symbol: str, price: float, quantity: float) -> None:
        """paper 모드에서 매수 체결 가정: 원화 차감, 코인 증가.
        수수료 0.05% (업비트 KRW 마켓 기본) 반영.
        """
        if not self.paper:
            return
        fee_rate = 0.0005
        krw_cost = price * quantity * (1 + fee_rate)
        if krw_cost > self._paper_krw + 1:  # 1원 여유
            log.warning(f"[paper] 잔고 부족: 필요 {krw_cost:,.0f} > 보유 {self._paper_krw:,.0f}")
            return
        self._paper_krw -= krw_cost
        self._paper_coins[symbol] = self._paper_coins.get(symbol, 0.0) + quantity
        log.info(f"[paper] BUY {symbol} {quantity:.6f}@{price:,.0f} "
                 f"= {krw_cost:,.0f}원 (잔고 {self._paper_krw:,.0f})")

    def _paper_apply_sell(self, symbol: str, price: float, quantity: float) -> None:
        """paper 모드에서 매도 체결 가정: 코인 차감, 원화 증가."""
        if not self.paper:
            return
        held = self._paper_coins.get(symbol, 0.0)
        sell_qty = min(held, quantity)
        if sell_qty <= 0:
            return
        fee_rate = 0.0005
        krw_gain = price * sell_qty * (1 - fee_rate)
        self._paper_coins[symbol] = held - sell_qty
        if self._paper_coins[symbol] < 1e-10:
            self._paper_coins.pop(symbol, None)
        self._paper_krw += krw_gain
        log.info(f"[paper] SELL {symbol} {sell_qty:.6f}@{price:,.0f} "
                 f"= {krw_gain:,.0f}원 (잔고 {self._paper_krw:,.0f})")

    def paper_summary(self) -> dict:
        """현재 가상 포지션 평가금액 포함 요약."""
        total_coin_value = 0.0
        holdings = {}
        for sym, qty in self._paper_coins.items():
            cur = self.get_current_price(sym)
            px = float(cur) if isinstance(cur, (int, float)) else 0.0
            value = px * qty
            total_coin_value += value
            holdings[sym] = {"qty": qty, "price": px, "value_krw": value}
        return {
            "krw": self._paper_krw,
            "coin_value_krw": total_coin_value,
            "total_krw": self._paper_krw + total_coin_value,
            "holdings": holdings,
        }

    def cancel_order(self, uuid: str) -> dict:
        if self.dry_run or not self._upbit:
            return {"dry_run": True, "uuid": uuid, "cancelled": True}
        _order_limiter.wait()
        return _retry(self._upbit.cancel_order, uuid)

    def get_order(self, uuid: str) -> Optional[dict]:
        if not self._upbit:
            return None
        _quote_limiter.wait()
        return _retry(self._upbit.get_order, uuid)

    # ─── 유틸 ───

    @staticmethod
    def _mock_order(kind: str, symbol: str, price: Optional[float], qty: float) -> dict:
        return {
            "dry_run": True,
            "kind": kind,
            "market": symbol,
            "price": price,
            "quantity": qty,
            "timestamp": time.time(),
            "uuid": f"mock-{int(time.time() * 1000)}",
        }

    @staticmethod
    def round_to_tick(price: float) -> float:
        """업비트 KRW 마켓 호가 단위(tick size) 정렬.

        실제 Upbit 오더북 기반 empirical 검증 (2026-04-21):
          BCH@658k→500, AAVE@134k→100, COMP@37990→10, EGLD@6180→5,
          ATOM@2663→1, UNI@4788→1, CPOOL@41.1→0.1, XEC@0.0106→0.0001.
        5,000원 기준으로 tick 1→5 전환 (기존 코드에서 누락되어 EGLD 등
        5,000~10,000원 코인 매수가 전부 invalid_price_bid 로 거절됨).
        100,000~500,000원 구간도 tick=50→100 으로 보정.
        """
        if price >= 2_000_000:
            tick = 1000
        elif price >= 1_000_000:
            tick = 500
        elif price >= 500_000:
            tick = 500
        elif price >= 100_000:
            tick = 100
        elif price >= 10_000:
            tick = 10
        elif price >= 5_000:
            tick = 5
        elif price >= 1_000:
            tick = 1
        elif price >= 100:
            tick = 1
        elif price >= 10:
            tick = 0.1
        elif price >= 1:
            tick = 0.01
        elif price >= 0.1:
            tick = 0.001
        elif price >= 0.01:
            tick = 0.0001
        else:
            tick = 0.00001
        return round(round(price / tick) * tick, 8)


# ─── CLI 테스트용 ───
if __name__ == "__main__":
    import sys
    from config import get_api_keys

    access, secret = get_api_keys()
    c = UpbitClient(access, secret, dry_run=True)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "tickers"
    if cmd == "tickers":
        t = c.get_tickers()
        print(f"KRW markets: {len(t)} coins")
        print(t[:10])
    elif cmd == "price":
        sym = sys.argv[2] if len(sys.argv) > 2 else "KRW-BTC"
        print(f"{sym}: {c.get_current_price(sym):,} KRW")
    elif cmd == "ohlcv":
        sym = sys.argv[2] if len(sys.argv) > 2 else "KRW-BTC"
        df = c.get_ohlcv(sym, interval="day", count=5)
        print(df)
    elif cmd == "balance":
        print(f"KRW: {c.get_balance_krw():,.0f}")
        for b in c.get_balances():
            print(b)
