# Upbit Trading System

업비트 KRW 마켓 자동매매 시스템.

---

## 목차

1. [실행 방법](#실행-방법)
2. [환경 변수 (.env)](#환경-변수-env)
3. [운용 모드](#운용-모드)
4. [대시보드](#대시보드)
5. [폴더 구조](#폴더-구조)
6. [트레이딩 로직 전체 설명](#트레이딩-로직-전체-설명)
7. [용어 사전](#용어-사전)
8. [안전 수칙](#안전-수칙)
9. [참고 자료](#참고-자료)

---

## 실행 방법

### 최초 설치

```bash
cd /Users/aiden/Desktop/Auto-trader/upbit-trading-system

# 1) 가상환경
python3 -m venv .venv
source .venv/bin/activate

# 2) 의존성
pip install -r requirements.txt

# 3) 환경 변수
cp .env.example .env
# .env 열어서 UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY 입력
```

### 데몬 실행

```bash
# 페이퍼 모드 (가상잔고 시뮬레이션 — 추천 시작점)
.venv/bin/python3 scripts/autotrade_daemon.py --paper

# 드라이런 모드 (실계좌 조회 + 주문 스킵)
.venv/bin/python3 scripts/autotrade_daemon.py --dry-run

# 실거래 모드 (충분한 검증 후에만!)
.venv/bin/python3 scripts/autotrade_daemon.py

# 백그라운드 실행
nohup .venv/bin/python3 scripts/autotrade_daemon.py --paper > /dev/null 2>&1 &

# 인터벌 지정 (기본 60초)
.venv/bin/python3 scripts/autotrade_daemon.py --paper --interval 60
```

### 상태 확인 / 중지

```bash
# 실행 중 데몬 찾기
ps aux | grep autotrade_daemon | grep -v grep

# 로그
tail -f logs/daemon.log

# 종료 (PID 치환)
kill -TERM <PID>
```

### CLI 유틸

```bash
# 스크리너 단독 실행 (후보 확인)
.venv/bin/python3 scripts/screener.py

# 특정 심볼 시그널 평가
.venv/bin/python3 scripts/signal_engine.py KRW-BTC

# DB 정리 (오래된 레코드 삭제 + VACUUM)
.venv/bin/python3 scripts/db.py cleanup --vacuum

# 백테스트
.venv/bin/python3 scripts/backtest.py
```

---

## 환경 변수 (.env)

`.env.example`을 `.env`로 복사 후 값 채우세요.

```ini
# ── 필수 ──
# Upbit Open API 키 (https://upbit.com/mypage/open_api_management)
# 권한: "자산조회 + 주문조회/생성/취소"만 활성.
UPBIT_ACCESS_KEY=your_access_key_here
UPBIT_SECRET_KEY=your_secret_key_here

# ── 선택 ──
# 기본 운용 모드 (CLI --paper / --dry-run 가 우선)
# 값: dry_run / live
UPBIT_MODE=dry_run

# Discord 웹훅 (매수/매도/에러 알림). 비우면 알림 비활성
DISCORD_WEBHOOK_URL=
```

### 보안 필수 사항

- **IP 화이트리스트 등록**: Upbit Open API 관리 페이지에서 현재 서버 IP만 허용
- **출금 권한 절대 금지**: 키 생성 시 "출금"은 체크하지 말 것
- **`.env` 절대 커밋 금지**: `.gitignore`에 포함되어 있어야 함

### 런타임 설정 (`data/user-config.json`)

API 키와 별개로 전략/리스크 파라미터는 JSON으로 관리. 데몬은 매 사이클 이 파일을 다시 읽으므로 **재시작 없이 반영** 가능.

```json
{
  "max_positions": 5,
  "reentry_cooldown_hours": 12,
  "breakeven_trigger_pct": 2.0,
  "trailing_trigger_pct": 3.0,
  "trailing_distance_pct": 1.0,
  "screener_top_by_volume_n": 0,
  "screener_top_by_spike_n": 25,
  "screener_spike_min_ratio": 2.0,
  "screener_min_volume_krw": 1000000000,
  "screener_full_scan_interval_sec": 180,
  "paper_initial_krw": 1000000
}
```

---

## 운용 모드

| 모드 | 실주문 | 잔고 | 용도 |
|------|--------|------|------|
| **live** | O | 실계좌 | 실거래 |
| **dry** | X | 실계좌 조회 | 로직 검증 (주문만 스킵) |
| **paper** | X | 가상잔고 100만원 | end-to-end 시뮬레이션 |

실거래 전환 전 반드시 **paper 모드에서 최소 1주일 검증**.

---

## 대시보드

```bash
.venv/bin/python3 dashboard/server.py
# http://localhost:8766
```

탭 구성:
- **개요** — 핵심 지표 + 누적 PnL 차트
- **포지션** — 현재 보유 포지션
- **거래 이력** — 과거 청산된 거래 목록
- **시그널** — 매수 시그널 기록
- **사이클** — 데몬 사이클 로그 (선별저장)
- **에러** — 에러 로그
- **로직** — 전략/지표/용어 설명 (README와 동일 내용)

---

## 폴더 구조

```
upbit-trading-system/
├── scripts/                 # 핵심 로직
│   ├── upbit_client.py      # pyupbit 래퍼 + 재시도/레이트리밋
│   ├── indicators.py        # TA (RSI, MACD, BB, VWAP, EMA, ATR, ADX, Hurst)
│   ├── signal_engine.py     # VB + MB + MR + VS + VP 5전략 앙상블
│   ├── position_monitor.py  # 10초 STOP_LOSS 감시 스레드 (메인 사이클 보조)
│   ├── screener.py          # 거래대금 spike 기반 후보 선별
│   ├── risk.py              # BTC 레짐 / 일일손실 / MDD 게이트
│   ├── order_executor.py    # 주문 실행 + 세이프티
│   ├── autotrade_daemon.py  # 메인 데몬 루프
│   ├── backtest.py          # 역사 백테스트
│   ├── notify.py            # Discord 알림
│   ├── config.py            # 설정 로드
│   └── db.py                # SQLite 저장소
├── dashboard/
│   ├── index.html           # 대시보드 UI
│   └── server.py            # HTTP API
├── docs/
│   ├── strategy.md          # 전략 상세
│   └── architecture.md      # 시스템 구조
├── data/
│   ├── trading.db           # SQLite (primary 저장소)
│   ├── user-config.json     # 런타임 설정
│   └── protected-coins.json # 자동매매 제외 종목
├── logs/
│   └── daemon.log
├── .env                     # API 키 (커밋 금지)
├── .env.example
└── requirements.txt
```

---

## 트레이딩 로직 전체 설명

### 1. 전체 흐름

데몬은 **60초 주기**로 아래 단계를 반복합니다. 단, **스크리닝 풀스캔**은 API 부하 때문에 **3분(180초)마다 1회**만 돌고, 다른 사이클은 직전 결과를 캐시로 사용합니다.

```
1. BTC 레짐 체크
   ↓
2. 포지션 감시 (손절/익절/본전/트레일링/부분익절/스테일)
   ↓
3. 리스크 게이트
   ↓
4. 스크리닝 (3분주기)
   ↓
5. 매수 실행
   ↓
6. DB 저장 (선별)
```

> **BTC 레짐 우선**: BTC가 bear 레짐이면 **모든 매수 차단**. 매도(청산)는 계속 진행합니다.

### 2. 스크리닝 — 후보 선별

Upbit KRW 전체 247개 마켓 중 **거래대금이 평소보다 급증한 코인**만 골라냅니다. 대장주(BTC/ETH 등)는 변동폭이 작아 단타에 부적합해 채널 A(거래대금 상위)는 폐지했습니다.

**단계**:
1. **보호/스테이블코인 제외** — `protected-coins.json`, USDT/USDC/DAI 등
2. **사전필터** — 24h 거래대금 ≥ `10억원` (유동성 함정 방지)
3. **거래대금 spike 계산** — `today_value / avg20_value` (오늘 거래대금 ÷ 20일 평균)
4. **채널 B 선정** — spike ≥ `2.0x` 중 상위 `25개`
5. **signal_engine 평가** — 5개 전략 계산 → 등급 A/B만 후보
6. **스프레드 검사** — 0.35% 초과 시 후보 탈락, 0.2% 초과 시 등급 한단계 강등

> **왜 거래량이 아닌 거래대금?** 거래량만 보면 가격 폭락 + 거래 폭증(패닉 매도)까지 잡힘. 거래대금(가격×거래량)은 "실제 돈이 몰리는가"를 반영해 진짜 관심을 측정합니다.

### 3. 5가지 전략 (signal_engine)

각 코인에 대해 5개 전략을 독립적으로 평가. 등급 A/B만 매수 후보가 됩니다. **VP는 청산 룰이 EMA 종가 기반이라 다른 전략의 BE/Trailing/STALE/PartialTP 룰과 격리됩니다.**

#### VB — Volatility Breakout (변동성 돌파)
*Larry Williams 전략. 일봉 기준. 주력 전략.*
- 매수: 오늘 시가 + K × 전일 Range 돌파
- K 동적 조정: 노이즈비 높을수록 낮아짐 (현재 `0.5` 고정)
- 게이트: bear 레짐 차단 / RSI < `68` / 거래량 ≥ 20일평균 × `3.0x` / ADX 약추세 강등 / Hurst < 0.5 강등
- 손절: `-5%` · 익절: ATR×4 또는 +6% 중 큰 쪽

#### MB — Momentum Breakout (모멘텀 돌파)
*단기 추세 추종. 15분/60분봉 기준.*
- 매수: EMA9 > EMA21 + N봉 고가 돌파 + 거래량 1.5x + RSI 50-75 + MACD 히스토그램 상승
- 손절: ATR 기반 또는 `-3%` · 익절: ATR×4 또는 `+9%`

#### MR — Mean Reversion (평균회귀)
*횡보 레짐 전용. BB 하단 + 과매도 포착.*
- 매수: BB %B ≤ 0.1 + RSI ≤ 30 + VWAP 아래 + MACD 반등
- 익절: BB 중단 또는 +3% 중 가까운 쪽 (떨어지는 칼 빠른 익절)
- 손절: `-3%`

#### VS — Volume Spike (거래량 급등 추종)
*레짐 무관. 섹터 로테이션 포착.*
- 매수: 일봉 거래량 ≥ 20일 평균 × `5.0x` + 일중 +3~15% + RSI 50-78
- 학술근거: Jegadeesh-Titman momentum, Liu-Tsyvinski 크립토 단기 autocorrelation
- 손절: `-3.5%` 또는 일중 저가 중 유리한 쪽

#### VP — VWAP Pullback (눌림목, 격리 청산)
*VWAP 위 + EMA9 눌림목 + 위쪽 매물대 비어있음. 15분봉. 롱 전용.*
- **방향 (VWAP)**: 현재가 > 세션 VWAP, 거리 0~3% (과확장 회피)
- **눌림목 (EMA9)**: 현재가 ≥ EMA9 + 1% 밴드, 직전 5봉 안에 EMA 터치 흔적
- **매물대 (간이 Volume Profile)**: 현재가 위쪽 누적 거래량 비중 ≤ 30%
- **회피 (힘겨루기)**: 최근 20봉 동안 close가 VWAP 위/아래로 4회 이상 교차 시 → SKIP
- **청산 (EMA 종가 기반)**: vp_timeframe(15분) 마지막 봉 close < EMA9 → `SELL_EMA_EXIT`
- **안전망 SL**: `-10%` (catastrophic 보호용. 1차 청산은 EMA 룰)
- **격리**: BE/Trailing/Partial TP/STALE 룰 일체 적용 안 함

### 4. 포지션 관리 (보유 중 자동 감시)

매 사이클(60초)마다 보유 포지션 각각에 대해 6단계 순서로 체크. 앞 단계에서 걸리면 뒷 단계 생략.

**① 손절 (최우선)**
PnL ≤ 전략별 손절선 → 즉시 `SELL_STOP_LOSS`

| 전략 | 손절선 |
|------|--------|
| VB | -5.0% |
| MB | -3.0% |
| MR | -3.0% |
| VS | -3.5% |
| VP | -10.0% (안전망 — EMA 종가 청산이 1차) |

**② 본전 방어 (Breakeven)**
한 번이라도 `+2%` 이상 갔으면 `breakeven_armed`. 이후 PnL이 `+0.5%` 아래로 떨어지면 `SELL_BREAKEVEN`으로 이탈. 이익 물리는 것을 방지. (**VP 전략 제외** — EMA 종가 룰만 사용)

> **중요**: MFE(최고수익)는 HOLD 상태 포함 모든 사이클에서 갱신됩니다.

**③ 분할 익절 (Partial TP)**
목표가 도달 시 일부 수량만 매도. 잔량은 계속 보유하며 더 큰 익절 노림. `tp_levels` 설정에 따라 동작.

**④ 최종 익절**
PnL ≥ 전략별 익절 목표 → 잔량 전량 `SELL_TAKE_PROFIT`

**⑤ 트레일링 스톱**
PnL ≥ `+3%` 도달 후, 최근 15분봉 고점 대비 `-1%` 하락 시 `SELL_TRAILING`. 익절 보존.

**⑥ 스테일 청산 (기회비용)**
진입 후 `24시간` 경과, PnL ±1% 이내 횡보, MFE < 0.5%면 "신호 재평가" 후 여전히 정체면 `SELL_STALE`. 자본 회전율 향상.

### 5. 리스크 게이트

매 사이클 매수 전 점검. 하나라도 차단(block)이면 신규 매수 스킵.

- **BTC 레짐** — bear일 때 차단
- **일일 손실 한도** — 오늘 누적 -X% 초과 시 차단
- **주간 MDD** — 최근 7일 최대낙폭 초과 시 차단
- **연속 손절** — 짧은 시간 내 N회 연속 손절 시 쿨다운
- **유동성 경고** — 얕은 새벽 시간대 등 → 포지션 사이즈 축소(warn, 차단은 아님)

### 6. 매수 실행

- **포지션 한도**: 최대 `5개` 동시 보유 (`max_positions`)
- **보유/쿨다운 제외**: 이미 보유 중이거나 `12시간` 내 청산한 코인은 제외
- **사이즈**: 총자산 × 리스크비율 ÷ (진입가 - 손절가) 공식. 얇은 유동성 경고 시 축소
- **같은 사이클 중복 방지**: 한 코인에 여러 전략이 동시에 BUY 신호 내도 1회만 진입
- **주문 방식**: 지정가 우선 (슬리피지 방지). 실거래 모드에서만 실주문, 현재는 페이퍼(가상잔고)

### 7. 쿨다운 & 중복 방지

- **재진입 쿨다운**: 청산 후 `12시간` 동안 같은 코인 재진입 금지. 손절 직후 반복매수 방지.
- **보호 코인**: `data/protected-coins.json`에 등록된 코인은 매수 대상에서 완전 제외 (사용자 외부 보유분과 충돌 방지).

### 8. DB 저장 정책

모든 사이클을 저장하면 노이즈가 많아져 선별 저장합니다.

- **의미있는 사이클** 저장: 포지션 보유 / 신호 생성 / 매수 / 매도 / 에러 중 하나라도 있을 때
- **Heartbeat**: 위 조건 모두 없어도 `30사이클(30분)마다` 1회 저장 (데몬 생존 확인용)
- **자동 정리**: 신호 14일, 사이클 30일, 에러 90일, 스크리닝 결과 14일 지나면 삭제 (하루 1회)

### 9. 모드 구분

| 모드 | 실주문 | 잔고 | 용도 |
|------|--------|------|------|
| **live** | O | 실계좌 | 실거래 |
| **dry** | X | 실계좌 조회 | 로직 검증 (주문만 스킵) |
| **paper** | X | 가상잔고 100만원 | end-to-end 시뮬레이션 |

> 데몬은 모드를 `UPBIT_MODE` 환경변수로 자체 갱신하므로 Discord 푸터·DB 컬럼 모두 런타임 모드와 일치합니다.

### 10. Position Monitor (10초 STOP_LOSS 감시)

메인 사이클(60초)이 스크리닝 등으로 길어질 때 갭다운 손실을 막기 위해 별도 스레드가 **10초마다** 보유 포지션을 감시. STOP_LOSS만 처리(시급도 1순위), 다른 룰은 메인 사이클이 담당. 메인 사이클과 동시 매도 race를 막기 위해 per-symbol Lock + pending_exits set + DB status 재확인 3중 보호.

LIVE 모드에서만 활성. 모니터 청산도 Discord 알림이 발송됩니다.

### 11. Discord 알림

`DISCORD_WEBHOOK_URL` 설정 시 매수/매도/분할익절/에러/데몬 시작·종료를 임베드 메시지로 통지.

- 가격 자릿수 적응 포맷 (BTC 95M부터 SHIB 0.025까지 정확)
- 매도 알림의 PnL은 **DB 최종값** 기반 — 분할익절 누적분(`realized_partial_krw`)까지 반영해 부분청산-잔량청산 케이스에서도 정확
- 푸터에 런타임 모드(LIVE/DRY/PAPER) 표기
- 전략명 표기 (`SELL_EMA_EXIT [VP]` 등 청산 사유 즉시 식별)

> **안전 원칙**: 실거래 전환 전 반드시 paper 모드에서 최소 1주일 검증. 실거래 모드에서도 하드 리스크 게이트(BTC 레짐, 일일 손실한도)는 항상 활성.

---

## 용어 사전

### 거래 동작 용어

#### 트레일링 스톱 (Trailing Stop)
*"이익을 따라다니며 고점 대비 일정 % 하락 시 청산"*

고정 손절선과 달리 **움직이는 손절선**. 수익이 커질수록 손절선도 함께 올라감. 예: +3% 도달 후 고점 1050원 기록 → 1040원(-1%) 찍으면 매도. 상승 추세 끝까지 타고 꺾이는 순간 이익 확정.

#### 스테일 (Stale) 청산
*"오래 물려서 기회비용 발생한 포지션 청산"*

진입 후 일정 시간(24h) 지났는데 수익도 손실도 거의 없는 횡보 상태 = 자본이 놀고 있음. 이 자본을 다른 기회에 쓰기 위해 청산. "죽지도 살지도 않는 포지션"을 정리하는 로직.

#### 본전 방어 (Breakeven Stop)
*"한 번 수익 난 포지션은 최소 본전 지키기"*

진입 후 +2% 이상 갔으면 `armed=1` 상태가 되어, 이후 본전 이하로 떨어지면 즉시 청산. "이익이 손실로 바뀌는" 최악의 경험을 막음.

### 시장 상태 용어

#### 레짐 (Regime)
*"시장이 지금 상승장인가, 하락장인가, 횡보장인가"*

종가 이동평균(EMA) 기울기와 가격 위치로 판정: `bull`(상승)·`bear`(하락)·`range`(횡보). 전략마다 유리한 레짐이 다름 — 돌파 전략(VB/MB)은 bull, 평균회귀(MR)는 range에서만 작동.

#### 스파이크 (Spike)
*"오늘 거래대금이 평소의 몇 배인가"*

공식: `today_value / 20일_평균_value`. 2x 이상이면 "뭔가 일어나고 있다", 10x 이상이면 "대폭발". 단타의 가장 빠른 신호.

#### 채널 A / 채널 B
*"후보 선정 경로"*

- **채널 A**(폐지): 24h 거래대금 상위 종목 — BTC/ETH 등 대장주. 변동성 작아 단타 부적합.
- **채널 B**(활성): 거래대금 spike 상위 종목 — 오늘 갑자기 활발해진 소/중형주. 현재 이 채널만 사용.

#### 스프레드 (Spread)
*"매수호가와 매도호가의 차이(%)"*

매수 즉시 발생하는 손실. 스프레드 0.5% = 매수하자마자 -0.5% 출발. 단타 평균 목표 수익 1-3%인데 스프레드 크면 이익의 상당 부분이 날아감. 그래서 `0.35%` 이상은 거부, `0.2%` 이상은 등급 강등.

### 기술 지표 (Technical Indicators)

#### EMA (Exponential Moving Average, 지수이동평균)
최근 가격에 더 큰 가중치를 둔 이동평균. 단순 평균(SMA)보다 반응 빠름. **EMA9 > EMA21**이면 단기 추세가 상승세. 골든크로스/데드크로스 개념의 기초.

#### RSI (Relative Strength Index, 상대강도지수)
0-100 범위. 최근 N일 상승/하락 비율로 계산. **70 이상 = 과매수(조정 위험)**, **30 이하 = 과매도(반등 가능)**. 돌파 매수 시 68 이상이면 추격매수 차단. *J. Welles Wilder (1978).*

#### MACD (Moving Average Convergence Divergence)
EMA12 - EMA26 = MACD 라인. MACD의 EMA9 = 시그널 라인. 둘의 차이 = 히스토그램. **히스토그램이 음→양으로 바뀌거나 확장 중**이면 상승 모멘텀. 추세 전환 포착. *Gerald Appel (1970s).*

#### ATR (Average True Range, 평균 실질 변동폭)
N일간 일평균 고저 변동폭. 코인이 얼마나 출렁이는가 측정. 손절/익절 폭을 코인 특성에 맞춰 동적으로 정할 때 사용 — 예: 변동성 큰 코인은 손절 넓게, 작은 코인은 타이트하게. "-5%" 같은 고정값보다 과학적.

#### BB (Bollinger Bands, 볼린저 밴드)
이동평균 ± 표준편차×2 의 상/하단 밴드. 가격이 **밴드 하단 터치 = 과매도**, 상단 터치 = 과매수로 해석. `%B` = 가격이 밴드의 어느 위치(0=하단, 1=상단). 평균회귀(MR) 전략의 핵심. *John Bollinger (1980s).*

#### VWAP (Volume Weighted Average Price, 거래량 가중평균가)
Σ(가격×거래량) / Σ(거래량). 기관 트레이더의 "본전가" 개념. 현재가가 VWAP 아래 = 오늘 대부분의 거래자보다 싸게 매수하는 셈. MR 전략에서 추가 확인용.

#### ADX (Average Directional Index, 평균 방향성 지수)
추세의 "강도"를 측정 (방향 무관). **25 이상 = 뚜렷한 추세**, 20 이하 = 약추세/횡보. 돌파 전략은 ADX가 약하면 신뢰도 낮음 → 등급 강등. *J. Welles Wilder (1978).*

#### Hurst 지수 (Hurst Exponent)
0~1 범위. **> 0.5 = 추세성**(한번 움직이면 계속), **< 0.5 = 평균회귀성**(튀어도 돌아옴), 0.5 = 랜덤워크. 돌파 매수인데 코인이 평균회귀 성향이면 함정 → 강등. *Harold Hurst (1951, 나일강 수위 연구에서 유래).*

#### Donchian Channel (돈치안 채널)
최근 N일 최고가/최저가 밴드. **N일 최고가 돌파 = 명확한 추세 시작 신호**. Turtle Traders(리차드 데니스)가 유명하게 만든 지표. VB에서 Donchian 20일 고가 동시 돌파 시 보너스 점수.

### 학술 근거 (간단 요약)

#### Larry Williams — Volatility Breakout (1980s)
"오늘 시가에서 전일 변동폭의 K배 이상 돌파하면 그날 추세가 살아있다." K=0.5가 통계적으로 가장 안정. VB 전략의 근본. 1987년 세계 트레이딩 챔피언십에서 +11,376% 수익으로 유명.

#### Jegadeesh & Titman (1993)
"Returns to Buying Winners..." — 최근 3-12개월 상승 종목을 매수하는 momentum 전략이 통계적으로 **초과수익** 발생을 주식시장에서 최초로 실증. VS/MB 전략의 이론적 배경. *Journal of Finance.*

#### Liu, Tsyvinski & Wu (2022)
"Common Risk Factors in Cryptocurrency" — 크립토 시장에서 **1-7일 단기**에 강한 양의 autocorrelation(추세 지속성) 존재. 주식시장의 월단위 momentum이 크립토에선 일 단위로 압축된다는 의미. 단타 momentum 전략의 학술적 정당화. *Review of Financial Studies.*

#### Mandelbrot & Peters — Hurst Exponent
금융시계열이 랜덤워크가 아니라 장기 기억(long memory)을 가진다는 프랙탈 이론. Edgar Peters의 "Chaos and Order in the Capital Markets"(1991)가 실무 적용을 대중화. VB의 Hurst 강등 게이트 근거.

#### Kelly Criterion / 포지션 사이징
"한 번에 얼마 베팅해야 장기 기대값 최대인가." 승률과 페이오프로 결정. 실무에선 Full Kelly는 과적합 위험 → 1/2 Kelly 또는 fixed fractional(1-2% 리스크) 방식. 현재 시스템은 **리스크 고정 비율 × 손절 폭** 방식.

> **왜 학술 근거가 중요한가?** 백테스트 과적합(과거 데이터에 맞춘 우연한 결과)을 피하기 위해. 수십 년 누적된 논문에서 **반복 검증된 효과**만 사용하면 생존 편향(survivorship bias) 위험 감소.

---

## 안전 수칙

- **최초 2주간 반드시 드라이런/페이퍼**. 크립토 변동성에 물리면 회복 어려움.
- **IP 화이트리스트** 등록 필수 (Upbit Open API 설정).
- **출금 권한 절대 부여 금지**. 매수/매도/조회만.
- **2% 룰**: 1 포지션 리스크 ≤ 총자산 2%.
- **BTC -7% 이상 급락 시 신규매수 차단** 자동 게이트.
- **`.env` 파일 절대 커밋 금지**.
- **실거래 전환 시** 소액(100만원 이하)으로 2-3주 추가 검증 후 스케일업.

---

## 참고 자료

- [pyupbit](https://github.com/sharebook-kr/pyupbit)
- [Upbit Open API 공식 문서](https://docs.upbit.com)
- [Volatility Breakout 전략 (TVExtBot)](https://tvextbot.github.io/post/indicator_vbi/)
- `docs/strategy.md` — 전략 파라미터 근거
- `docs/architecture.md` — 사이클 흐름, DB 스키마, ER 다이어그램

---

*last updated: 2026-05-07*
