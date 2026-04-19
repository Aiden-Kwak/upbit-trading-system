# 전략 문서

## 개요

Upbit Trading System은 **3종 전략 앙상블**을 사용합니다. 각 전략은 독립적으로 시그널을 생성하며, 레짐에 따라 활성 여부가 결정됩니다.

## 레짐 분류 (BTC 기반)

| 레짐 | 조건 | 활성 전략 |
|------|------|-----------|
| `bull` | EMA9 > EMA50, 가격 > EMA9 | VB, MB |
| `range` | 최근 20봉 변동폭 < 4% | MR |
| `bear` | EMA9 < EMA50, 가격 < EMA9 | **매수 차단** |
| `crisis` | BTC 24h < -7% | **전체 차단** |

## 1. VB: Volatility Breakout (래리 윌리엄스)

**근거**: 업비트 대상 가장 검증된 알고리즘 전략. 일봉 변동폭을 기반으로 당일 추세 돌파를 포착.

### 수식
```
매수가 = 당일시가 + K × 전일Range
Range  = 전일 고가 - 전일 저가
K      = max(0.3, min(0.7, 1.0 - 노이즈비율))
```

### 노이즈비율
```
noise = 1 - |close - open| / (high - low)
```
- 낮음 (0.2~0.4): 강한 추세 → K 0.6~0.8 (공격적 진입)
- 높음 (0.6~0.8): 횡보 → K 0.3~0.4 (진입 회피)

### 추가 필터
- EMA9 > EMA21 (상승 추세 확인)
- 당일 거래량 ≥ 20일 평균 × 1.2
- RSI < 80 (극단 과열 회피)

### 청산
- 손절: max(전일 저가, 진입가 × 0.95)
- 익절: 진입가 × 1.12 (크립토 기본 +12%)
- 트레일링: +8% 진입 후 고점 대비 -3.5%

### 점수 (최대 100)
| 항목 | 배점 |
|------|------|
| 돌파 확정 | 40 |
| 거래량 확인 | 25 |
| 추세 확인 | 20 |
| RSI 정상 | 15 |

## 2. MB: Momentum Breakout

**근거**: 크립토는 추세 지속성이 강함. N-bar 고가 돌파 시 진입하여 모멘텀 타기.

### 조건
- EMA9 > EMA21 (기본 추세)
- 현재가 > 20봉 최고가 (돌파)
- 거래량 ≥ 평균 × 1.5 (확인)
- 50 ≤ RSI ≤ 75 (과열 회피)
- MACD Histogram 양전환 or 상승

### 타임프레임
- 기본: 60분봉 (설정으로 15분봉 전환 가능)

### 청산
- 손절: max(-5%, 진입가 - 2.5 × ATR14)
- 익절: +12%
- 트레일링: 동일

### 점수 (최대 100)
| 항목 | 배점 |
|------|------|
| EMA 상승 | 20 |
| N-bar 돌파 | 30 |
| 거래량 확인 | 25 |
| RSI 정상 | 15 |
| MACD 상승 | 10 |

## 3. MR: Mean Reversion (횡보 시만)

**근거**: 횡보 구간에서 BB 하단 + RSI 과매도 조합의 반등 포착. 트렌드 구간에서는 절대 사용 금지 (강한 하락의 초입일 가능성).

### 활성 조건
- **레짐이 `range`** 일 때만 평가
- 최근 20봉 변동폭이 4% 미만인 구간

### 조건
- Bollinger Bands %B ≤ 0.1 (하단 10% 이내)
- RSI ≤ 28 (알트코인 기준, 주식 30 대비 타이트)
- 가격 < VWAP
- MACD Histogram 상승 전환

### BB 설정 (알트 특화)
- period 15 (기본 20 → 단축)
- std 2.0

### 청산
- 1차 목표: BB 중앙선 (SMA15)
- 2차 목표: BB 상단
- 손절: -5%

### 점수 (최대 100)
| 항목 | 배점 |
|------|------|
| BB 하단 | 35 |
| RSI 과매도 | 30 |
| VWAP 아래 | 20 |
| MACD 반등 | 15 |

## 리스크 게이트 (BUY 실행 전)

순서대로 평가, 하나라도 `block` 이면 차단:

1. **BTC 레짐 게이트** — 24h -7% 이상 or EMA cross 약세
2. **일일 손실 한도** — 오늘 실현 -3% 이상
3. **주간 MDD** — 7일 누적 -8% 이상
4. **연속 손절** — 3회 연속 손실 시 쿨다운
5. **포지션 한도** — 최대 3종목
6. **얕은 유동성** (경고만) — 02~06 KST → 사이즈 50% 축소

## 포지션 사이징

```python
risk_krw     = total_asset × 1.0%              # per_trade_risk_pct
price_risk   = current - stop_loss
qty_by_risk  = risk_krw / price_risk
krw_by_risk  = qty_by_risk × current
krw_by_cap   = total_asset × 20%               # max_position_pct
final_krw    = min(krw_by_risk, krw_by_cap)
if thin_liq: final_krw *= 0.5
```

**근거**: "2% 룰" 보다 더 보수적인 1%. 크립토 변동성 반영.

## STALE 청산 (기회비용)

**2단 게이트** (Toss에서 이식, 크립토 맞춤):

1. **1단** — 3시간 이상 보유 + PnL ±2% 이내 → `STALE_CANDIDATE`
2. **2단** — 신호 재평가 → 등급 A/B 밖이면 `SELL_STALE` 청산

크립토는 주식(3일)보다 훨씬 빠르게 판정 (24/7 시장 + 급격한 변동성 → 기회비용 회전율 극대화).

## 주문 실행 원칙

| 코인 | 주문 유형 | 이유 |
|------|-----------|------|
| BTC, ETH | 시장가 허용 | 유동성 충분, 슬리피지 미미 |
| 기타 알트 | **지정가 필수** | 얇은 호가, 시장가 사용 시 슬리피지 1%+ 가능 |

지정가 매수: `현재가 × 1.003` (즉시체결 유도)
지정가 매도: `현재가 × 0.997`

모든 가격은 **tick size 정렬** 필수 (`UpbitClient.round_to_tick`).

## 크립토 vs 주식 파라미터 비교

| 파라미터 | 주식 (Toss) | 크립토 (Upbit) | 근거 |
|----------|-------------|----------------|------|
| 손절 | -3% | -5% | 일일 변동폭 2배 |
| 익절 | +7% | +12% | 추세 지속성 강함 |
| 트레일링 트리거 | +3% | +8% | 노이즈 흡수 |
| RSI 과매수 | 70 | 75~80 | 강세장 RSI 장기 고정 |
| RSI 과매도 | 30 | 28 | 알트는 더 깊이 내려감 |
| 스탠 보유 한도 | 3일 | 3시간 | 24/7 시장 + 빠른 회전 |
| BB period | 20 | 15 (알트) | 빠른 신호 |
| 포지션 한도 | 2 | 3 | 분산 |
| 사이클 주기 | 5분 | 1분 | 빠른 시장 |

## 참고 문헌

- [Larry Williams Volatility Breakout 전략](https://tvextbot.github.io/post/indicator_vbi/)
- [Kraken: 24/7 Day Trading Strategies](https://www.kraken.com/learn/day-trading-strategies)
- [Mudrex: Bollinger Bands Formula](https://mudrex.com/learn/bollinger-bands-in-crypto-trading/)
- [Altcoin-tuned MACD/RSI/BB 전략](https://web3.gate.com/crypto-wiki/article/how-to-use-technical-indicators-macd-rsi-and-bollinger-bands-for-crypto-trading-in-2026-20260204)
