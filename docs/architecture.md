# 시스템 아키텍처

## 컴포넌트 다이어그램

```mermaid
flowchart TB
    subgraph External["외부 시스템"]
        UPBIT[Upbit Open API<br/>REST + WebSocket]
    end

    subgraph Core["핵심 로직 (scripts/)"]
        CLIENT[upbit_client.py<br/>API 래퍼 + 레이트리밋]
        INDIC[indicators.py<br/>TA 지표]
        SIGNAL[signal_engine.py<br/>VB/MB/MR 3종 앙상블]
        SCREEN[screener.py<br/>KRW 마켓 스캔]
        RISK[risk.py<br/>게이트 6종 + 사이징]
        EXEC[order_executor.py<br/>주문 실행 + tick 정렬]
        DAEMON[autotrade_daemon.py<br/>메인 루프]
    end

    subgraph Config["설정"]
        CFG[config.py<br/>DEFAULT + user override]
        ENV[.env<br/>API keys]
        UCFG[(data/user-config.json)]
    end

    subgraph Storage["저장소"]
        DB[(data/trading.db<br/>SQLite)]
        LOGS[logs/daemon.log]
    end

    subgraph Dashboard["UI"]
        DASH[dashboard/server.py<br/>:8766]
        HTML[dashboard/index.html]
    end

    DAEMON --> CLIENT
    DAEMON --> SIGNAL
    DAEMON --> SCREEN
    DAEMON --> RISK
    DAEMON --> EXEC
    DAEMON --> DB
    DAEMON --> LOGS

    SIGNAL --> INDIC
    SIGNAL --> CLIENT
    SCREEN --> SIGNAL
    SCREEN --> CLIENT
    RISK --> CLIENT
    RISK --> DB
    EXEC --> CLIENT
    EXEC --> DB
    CLIENT --> UPBIT
    CFG --> ENV
    CFG --> UCFG
    DAEMON --> CFG

    DASH --> DB
    HTML --> DASH
```

## 데몬 사이클 흐름

```mermaid
flowchart TB
    START[사이클 시작] --> BTC[BTC 레짐 조회]
    BTC --> MONITOR[보유 포지션 모니터링]

    MONITOR --> MONCHK{청산 시그널?}
    MONCHK -->|손절/익절/트레일링| SELL1[즉시 매도]
    MONCHK -->|STALE_CANDIDATE| RECHECK[신호 재평가]
    RECHECK -->|무효| SELL2[기회비용 청산]
    RECHECK -->|유효| HOLD1[보유 유지]
    MONCHK -->|HOLD| HOLD2[보유 유지]

    SELL1 --> GATES
    SELL2 --> GATES
    HOLD1 --> GATES
    HOLD2 --> GATES

    GATES[리스크 게이트 6종]
    GATES --> GCHECK{매수 허용?}
    GCHECK -->|❌| SKIPBUY[매수 스킵]
    GCHECK -->|✅| SCAN{1h 풀스캔 주기?}

    SCAN -->|Y| FULLSCAN[스크리닝<br/>KRW 마켓 Top N]
    SCAN -->|N| USECACHE[캐시된 후보 사용]
    FULLSCAN --> CANDIDATES
    USECACHE --> CANDIDATES

    CANDIDATES[A/B 등급 후보] --> SIZE[포지션 사이징<br/>리스크 기반]
    SIZE --> BUY[매수 실행]
    BUY --> RECORD[DB 기록]
    SKIPBUY --> RECORD
    RECORD --> END[사이클 종료]
```

## 주문 실행 흐름

```mermaid
flowchart TB
    SIG[Signal with entry/stop/tp] --> QTY[사이징<br/>qty, krw_amount]
    QTY --> TICK[tick size 정렬<br/>round_to_tick]
    TICK --> TYPE{코인 종류}
    TYPE -->|BTC/ETH| MARKET[시장가]
    TYPE -->|기타 알트| LIMIT[지정가<br/>현재가×1.003]

    MARKET --> API[Upbit API 호출]
    LIMIT --> API

    API --> CHECK{응답 성공?}
    CHECK -->|❌| ERRLOG[errors 테이블 기록]
    CHECK -->|✅| DBINS[trades 테이블 INSERT<br/>status=open]
    DBINS --> UUID[주문 UUID 반환]
```

## DB 스키마

```mermaid
erDiagram
    trades {
        int id PK
        text symbol
        text strategy "VB/MB/MR"
        text side "buy/sell"
        text status "open/closed/cancelled"
        real entry_price
        real entry_quantity
        real entry_krw
        text entry_date
        text entry_grade
        text entry_uuid
        real exit_price
        real exit_quantity
        real exit_krw
        text exit_date
        text exit_reason
        text exit_uuid
        real pnl_krw
        real pnl_pct
        real max_favorable
        real max_adverse
    }
    signals {
        int id PK
        text symbol
        text strategy
        text action "BUY/HOLD/SELL_*"
        text grade "A/B/C/D"
        int score
        int score_max
        text reason
        text details "JSON"
    }
    daemon_cycles {
        int id PK
        int cycle_num
        text status
        int positions_count
        int signals_generated
        int buys_attempted
        int buys_filled
        int sells_executed
        real today_pnl_pct
        text btc_regime
        real duration_sec
    }
    equity_curve {
        int id PK
        text snapshot_date UK
        real total_krw
        real krw_balance
        real coin_value_krw
        int n_positions
    }
    screener_results {
        int id PK
        text scan_time
        text symbol
        text strategy
        text grade
        int score
        real current_price
        real volume_24h_krw
        real change_24h_pct
    }
    errors {
        int id PK
        text source
        text message
        text details
    }
```

## 레이트리밋 관리

| API 종류 | 공식 한도 | 이 시스템 설정 |
|---------|----------|----------------|
| 주문 | 초 8 / 분 200 | 초 6 (여유) |
| 시세 | 초 10 / 분 600 | 초 8 (여유) |
| WebSocket | 초 5 / 분 100 | 미사용 (REST만) |

`upbit_client.py:RateLimiter` 가 sliding window로 관리.

## 파일 역할 요약

| 파일 | 책임 |
|------|------|
| `config.py` | 설정 기본값 + user-config.json 병합 |
| `upbit_client.py` | API 호출 단일 진입점 (재시도/레이트리밋/dry_run 분기) |
| `indicators.py` | 기술 지표 순수 함수들 |
| `signal_engine.py` | 3종 전략 평가 + 청산 시그널 |
| `screener.py` | KRW 마켓 전체 스캔 |
| `risk.py` | 게이트 + 포지션 사이징 |
| `order_executor.py` | 주문 실행 + DB 반영 |
| `autotrade_daemon.py` | 메인 루프 |
| `backtest.py` | VB 전략 백테스터 |
| `db.py` | SQLite 헬퍼 |
