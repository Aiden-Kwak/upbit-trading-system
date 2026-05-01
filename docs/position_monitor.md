# Position Monitor — 보유종목 STOP_LOSS 전용 감시 스레드

작성: 2026-04-26
관련 파일: `scripts/position_monitor.py`, `scripts/autotrade_daemon.py`, `scripts/test_position_monitor.py`

---

## 1. 동기

기존 데몬은 단일 스레드로 다음을 직렬 처리한다:

```
[60s 사이클] BTC 레짐 → 보유 모니터링 → 스크리닝 → 시그널 → 매수
```

문제: **보유종목 가격 감지 주기가 곧 사이클 주기 = 60s**.

실제 발생한 사례 (2026-04-25 KRW-API3):

| 항목 | 값 |
|---|---|
| 진입 | 599 KRW |
| MB stop_loss override | -4.0% |
| MAE (최대 적자) | -8.18% |
| 실현 손실 | **-8.85%** |

원인 추정:
1. 사이클 사이(60s)에 폭락 발생
2. 손절 트리거 시 지정가 -0.3% 디스카운트 매도가 추격 못함

이미 (1) STOP_LOSS 시 시장가 매도 적용함. 그러나 사이클 60s 자체는 그대로 → **별도 스레드로 감시 주기를 10s 단축**.

---

## 2. 설계 원칙

### 2.1 책임 분리

| 항목 | 모니터 스레드 | 메인 사이클 |
|---|---|---|
| 감시 주기 | 10s | 60s |
| 처리 사유 | **STOP_LOSS 만** | TRAILING / BREAKEVEN / STALE / PARTIAL_TP / 신규매수 |
| 이유 | 갭다운 즉시 반응이 핵심 | 다른 사유는 분 단위 변동에 둔감 |

STOP_LOSS 만 분리한 이유:
- TRAILING: peak 추적 후 거리만 벗어나면 됨 (1분 단위로도 충분)
- BREAKEVEN: MFE 도달 후 본전 이탈 — 마찬가지로 분 단위
- STALE: 6시간 타이머 기반 — 초 단위 의미 없음
- PARTIAL_TP: +8/+20% 익절은 큰 가격이라 슬리피지 비중 적음

### 2.2 동시성 보호

두 스레드가 동일 포지션을 보고 매도를 시도하는 race window 가 존재한다.
대비책 3중:

```
┌──────────────────────────────────────────────┐
│ ① per-symbol Lock (threading.Lock)           │
│    동일 심볼 매도는 직렬화                     │
└──────────────────────────────────────────────┘
              ↓ 락 획득 후
┌──────────────────────────────────────────────┐
│ ② DB status 재확인                            │
│    SELECT status FROM trades WHERE id=?      │
│    이미 'closed' → skip                       │
└──────────────────────────────────────────────┘
              ↓ status='open' 확인 후
┌──────────────────────────────────────────────┐
│ ③ pending_exits set 에 mark                  │
│    in-flight 매도 중복 발사 방지              │
└──────────────────────────────────────────────┘
              ↓ execute_sell 호출 후
              finally: pending_exits 제거
              락 해제
```

추가: **메인 사이클의 PARTIAL_TP** 는 매도 전 `is_pending(sym)` 체크 — True 면 그 사이클 스킵 (이중 매도 방지).

### 2.3 락 timeout

`lock.acquire(timeout=15.0)` — 정상 매도는 수초 내 완료. 15초 초과 시 데드락 가능성 → skip + 경고 로그.

---

## 3. 발생 가능한 race scenarios 와 방어

### Scenario A: 양쪽 스레드 동시 STOP_LOSS 트리거

```
T=0.0  Monitor:  pnl=-3.5% ≤ -3% → safe_execute_sell 호출
T=0.0  Main:     같은 사이클에서도 pnl=-3.5% 감지 → safe_execute_sell
T=0.0  둘 다 lk.acquire() 시도
T=0.0  Monitor 가 lock 획득 → DB status 'open' 확인 → pending_exits 추가 → 매도 진행
T=0.0  Main 은 lock 대기
T=2.0  Monitor 매도 완료 → DB closed → pending_exits 제거 → lock 해제
T=2.0  Main lock 획득 → DB status 'closed' 확인 → return None (skip)
```

**결과**: execute_sell 정확히 1회 호출. 테스트 T1 검증.

### Scenario B: 외부에서 이미 청산됨

```
T=0  사용자가 모바일 앱에서 수동 매도
T=0  DB 는 여전히 'open' (수동 매도는 데몬 모름)
T=10 Monitor 가 STOP_LOSS 트리거 → safe_execute_sell
T=10 lock 획득 → DB 'open' 확인 (실보유 0)
T=10 execute_sell 진입 → client.get_balance() = 0 감지
     → "external_sync" 로 DB만 closed 처리
```

**결과**: 이미 `execute_sell` 내부에서 처리됨 (line 208-219). 추가 매도 없음.

### Scenario C: PARTIAL_TP 와 STOP_LOSS 충돌

```
T=0  Monitor: 가격 급락 → STOP_LOSS pnl=-3.2% → pending_exits 추가, 매도 시작
T=1  Main 사이클: 1초 전 가격 +8% → PARTIAL_TP 시그널 (캐시)
T=1  Main: is_pending('KRW-X') = True 감지 → SKIP_PARTIAL 로그 후 스킵
T=2  Monitor 매도 완료
T=61 다음 메인 사이클: 이미 closed 라 PARTIAL_TP 도 자동 무시
```

**결과**: 이중 매도 방지. 테스트 T3 검증.

### Scenario D: 이론상 데드락?

per-symbol lock 만 사용. 단일 락만 잡으므로 **deadlock 불가능**.
다중 심볼 락을 동시에 잡는 경로 없음 (의도적).

---

## 4. 통합 지점

### 4.1 Daemon 시작/종료
```python
# autotrade_daemon.py Daemon.__init__
if not dry_run and not paper:
    mon_iv = int(self.cfg.get("monitor_interval_sec", 10))
    position_monitor.start(self.client, load_config, interval=mon_iv)

# Daemon.stop
position_monitor.stop(timeout=5.0)
```

LIVE 모드만 활성. dry/paper 는 즉시반응 가치 없음.

### 4.2 메인 사이클의 매도 호출

| 위치 | 변경 전 | 변경 후 |
|---|---|---|
| 일반 SELL (TRAILING/BE/STALE/STOP_LOSS) | `execute_sell(...)` | `position_monitor.safe_execute_sell(...)` |
| PARTIAL_TP | `execute_partial_sell(...)` | `is_pending(sym)` 체크 후 호출 |

`safe_execute_sell` 가 `None` 반환 시 → "모니터가 처리 중/완료" 로 간주, 그 사이클 무시.

### 4.3 Config

`data/user-config.json` 또는 `config.py` DEFAULT_CONFIG 에 추가 가능:
```json
"monitor_interval_sec": 10
```
미설정 시 기본 10초.

---

## 5. 레이트리밋 영향

Upbit 시세 한도: **600 req/min**.

| 호출원 | 분당 호출 |
|---|---|
| Monitor (5종목 × 6회/분) | 30 |
| Main 사이클 시세/스크리닝/OHLCV | ~50 |
| 기타 (Discord, 잔고 등) | ~10 |
| **합계** | **~90** = **15% 사용** |

→ 안전. monitor_interval 을 5초로 줄여도 25% 수준.

---

## 6. 테스트 결과

`scripts/test_position_monitor.py` — 5종 테스트 전부 통과 (2026-04-26).

```
test_t1_concurrent_double_sell_only_one_wins ... ok
test_t2_already_closed_in_db ... ok
test_t3_is_pending_observed_during_sell ... ok
test_t4_stress_no_deadlock ... ok
test_t5_different_symbols_no_blocking ... ok

Ran 5 tests in 0.553s — OK
```

| 테스트 | 시나리오 | 검증 항목 |
|---|---|---|
| T1 | 같은 심볼 2 스레드 동시 매도 (race window 0.3초) | execute_sell 1회만, 한쪽은 None 반환, pending_exits 정리 |
| T2 | DB 가 이미 closed | execute_sell 0회 |
| T3 | 매도 진행 중 is_pending() 관찰 | 진행 중 True, 완료 후 False |
| T4 | 50 스레드 stress, barrier 동기화 | 1회만 매도, 데드락 없음 |
| T5 | 5종목 병렬 매도 (다른 심볼) | 모두 동시 진입 가능 — 락 분리 검증 |

테스트는 임시 SQLite 파일 + `unittest.mock` 으로 `execute_sell` 가로채 실거래 발생 안 함.

---

## 7. 운영 노트

### 7.1 모니터 죽음 감지
모니터 스레드 예외는 루프 내부 `try/except` 로 흡수 후 다음 iteration. 단,
**스레드 자체가 죽으면 자동복구 없음** — 향후 Daemon 사이클에서 `position_monitor.is_running()` 체크 + restart 추가 권장.

### 7.2 갭다운 경계
모니터 주기 < 폭락 속도면 여전히 슬리피지 발생.
업비트는 단일 거래소 → 페일오버/헷지 불가. 시장가 매도가 마지막 방어선.

### 7.3 추후 확장 후보
- TRAILING 도 모니터로 이관 (peak 기록은 in-memory 캐시 필요)
- 모니터 인터벌 동적 (보유종목 변동성 기반: 큰 코인은 30s, 알트는 5s)
- WebSocket 기반 가격 푸시로 폴링 제거 (Upbit 공식 WS 지원)
