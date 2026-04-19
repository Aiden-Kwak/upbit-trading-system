"""시스템 설정 기본값 + 런타임 오버라이드 로딩.

사용자 오버라이드는 data/user-config.json 에 저장되며,
DEFAULT_CONFIG 와 병합하여 사용됩니다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_DIR / "data"
LOGS_DIR = REPO_DIR / "logs"
USER_CONFIG_FILE = DATA_DIR / "user-config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    # ─── 운용 한도 ───
    "max_positions": 3,                   # 동시 보유 코인 수
    "max_position_pct": 20.0,             # 1 코인 최대 비중 (총자산 %)
    "min_order_krw": 5_500,               # 업비트 최소 주문금액 5000원 + 버퍼
    "per_trade_risk_pct": 1.0,            # 1트레이드 리스크 (총자산 %) — 2% 룰보다 보수적

    # ─── 크립토 맞춤 리스크 (주식 대비 완화) ───
    "stop_loss_pct": -5.0,                # 기본 손절 (크립토 변동성)
    "take_profit_pct": 12.0,              # 기본 익절
    "trailing_trigger_pct": 6.0,          # 트레일링 발동 수익률 (8→6, 조기 보호)
    "trailing_distance_pct": 2.5,         # 트레일링 거리 (3.5→2.5)
    "stop_atr_multiple": 2.5,             # ATR 기반 손절 배수
    "tp_atr_multiple": 4.0,               # ATR 기반 익절 배수 (변동성 큰 종목 수익 극대화)

    # ─── 브레이크이븐 (Phase 1) ───
    "breakeven_enabled": True,
    "breakeven_trigger_pct": 3.0,         # +3% 도달 시 본전 방어 가동
    "breakeven_buffer_pct": 0.2,          # 본전 +0.2% 이탈 시 청산 (수수료 상쇄)

    # ─── 분할 익절 (Phase 2) ───
    "partial_tp_enabled": True,
    "tp_levels": [                        # 순서대로 적용
        {"pct": 8.0, "size_ratio": 0.5},   # +8% 도달 → 50% 익절
        {"pct": 20.0, "size_ratio": 0.3},  # +20% 도달 → 추가 30% 익절
        # 잔여 20% 는 trailing 에서 처리
    ],

    # ─── 전략별 SL/TP 오버라이드 (Phase 3) ───
    "strategy_overrides": {
        "VB": {},                                        # 기본값 사용
        "MB": {"stop_loss_pct": -4.0},                   # 모멘텀은 빠른 손절
        "MR": {"stop_loss_pct": -3.0,                    # MR 은 떨어지는 칼 → tight
               "take_profit_pct": 5.0,                   # BB 중단까지만
               "trailing_trigger_pct": 3.0,
               "trailing_distance_pct": 1.5},
        "VS": {"stop_loss_pct": -3.5,                    # Volume Spike: 타이트 손절
               "take_profit_pct": 10.0,
               "trailing_trigger_pct": 3.0,
               "trailing_distance_pct": 1.5,
               "tp_atr_multiple": 3.0},
    },

    # ─── 일일/주간 서킷 ───
    "daily_loss_limit_pct": -3.0,         # 일일 -3% 초과 시 당일 신규매수 차단
    "weekly_mdd_limit_pct": -8.0,         # 주간 MDD -8% 시 냉각
    "cooldown_after_consecutive_losses": 3,
    "consecutive_loss_window_hours": 12,  # 최근 12h 내 청산만 카운트 (시간 지나면 자동 리셋)

    # ─── BTC 레짐 게이트 ───
    "btc_crash_threshold_pct": -7.0,      # BTC 24h -7% 이상 급락 시 모든 매수 차단
    "btc_regime_lookback_days": 30,       # 레짐 판정 기간
    "btc_crisis_ema_short": 9,
    "btc_crisis_ema_long": 50,

    # ─── 전략 활성화 ───
    "strategy_vb_enabled": True,          # 변동성 돌파 (일봉)
    "strategy_mb_enabled": True,          # 모멘텀 돌파 (15/60분봉)
    "strategy_mr_enabled": True,          # 평균회귀 (횡보 시만)
    "strategy_vs_enabled": True,          # Volume Spike (레짐 무관 급등 추종)

    # ─── VS 파라미터 (Volume Spike, 섹터 로테이션/급등 포착) ───
    # 학술: Jegadeesh-Titman momentum(1993) + Liu-Tsyvinski(2022) crypto 단기 autocorrelation
    "vs_spike_min_ratio": 5.0,            # 20일 평균 거래량 대비 5배
    "vs_intraday_change_min_pct": 3.0,    # 일중 시가대비 +3% 이상 (방향 확인)
    "vs_intraday_change_max_pct": 15.0,   # +15% 초과는 극과열 추격 회피
    "vs_rsi_min": 50,
    "vs_rsi_max": 78,
    "vs_max_spread_pct": 0.3,             # 스프레드 타이트만
    "vs_require_bull_regime": False,      # 핵심: range 레짐에서도 허용

    # ─── VB 파라미터 (Larry Williams) ───
    "vb_k_min": 0.3,
    "vb_k_max": 0.7,
    "vb_noise_lookback": 20,              # 노이즈비율 평균 기간
    "vb_volume_confirm_ratio": 1.2,       # 돌파 시점 거래량 확인
    "vb_rsi_buy_max": 70,                 # RSI 70↑ 이면 BUY 차단 (과열 추격 방지)
    "vb_bear_skip": True,                 # bear 레짐에선 VB 스킵
    "vb_range_downgrade": True,           # range 레짐에선 등급 한 단계 강등
    "vb_donchian_lookback": 20,           # 20일 Donchian 고가 동시 돌파 보너스
    "vb_donchian_bonus": 5,

    # ─── 추세 강도 필터 (VB/MB 공통, 학술: ADX, Hurst) ───
    "adx_period": 14,
    "adx_min_for_trend": 15,              # ADX<15 면 약추세 강등
    "hurst_enabled": True,
    "hurst_lookback": 20,                 # R/S max_lag — 일봉 60개 중 2~20 lag 스캔
    # 크립토 일봉 60개로 측정한 KRW 시장 median ≈ 0.26, 상위 13% 가 0.40+.
    # 0.40 기준은 "시장 평균 대비 추세 지속성이 더 강한 종목" 만 통과시킴.
    "hurst_trend_min": 0.40,

    # ─── MB 파라미터 (Momentum Breakout, 15분/60분) ───
    "mb_timeframe": "minute60",           # minute15 / minute60
    "mb_breakout_lookback": 20,           # N-bar 최고가 돌파
    "mb_volume_spike_ratio": 1.5,         # 평균 거래량 1.5배
    "mb_rsi_min": 50,                     # 매수 RSI 하한
    "mb_rsi_max": 75,                     # 매수 RSI 상한 (주식 70 → 75)
    "mb_ema_short": 9,
    "mb_ema_long": 21,

    # ─── MR 파라미터 (Mean Reversion, 횡보 시만) ───
    "mr_bb_period": 15,                   # 알트용 짧게
    "mr_bb_std": 2.0,
    "mr_rsi_oversold": 28,                # 알트: 30 → 28
    "mr_vwap_below_required": True,

    # ─── 스크리닝 ───
    "screener_top_n": 15,                 # (legacy) 단일 채널 호환용
    "screener_top_by_volume_n": 15,       # 채널A: 거래대금 상위 (안정 대장주)
    "screener_top_by_spike_n": 10,        # 채널B: 거래량 급등 (소형주 각성)
    "screener_spike_min_ratio": 3.0,      # 채널B 필터: 20일 평균 대비 3배 이상만
    "screener_min_volume_krw": 3_000_000_000,  # 공통 하한: 24h 거래대금 30억 (유동성 함정 방지)
    "screener_scan_interval_sec": 300,    # 5분 간격 재채점
    "screener_full_scan_interval_sec": 1800,  # 30분 풀스캔
    "screener_spread_downgrade_pct": 0.2, # 0.2%↑ 스프레드 → 한 단계 강등
    "screener_spread_reject_pct": 0.4,    # 0.4%↑ 스프레드 → 완전 거부 (왕복비용 0.8%+)

    # ─── 재진입 쿨다운 ───
    "reentry_cooldown_hours": 4,          # 청산 후 동일 심볼 N시간 재진입 금지

    # ─── STALE 청산 (완화: MFE 유예) ───
    "stale_exit_enabled": True,
    "stale_exit_hours": 6,                # 3→6 (MFE 없을 때만 공격적)
    "stale_exit_pnl_band_pct": 1.0,       # 2.0→1.0 (밴드 좁혀 애매한 손절 방지)
    "stale_exit_mfe_grace_pct": 0.5,      # 한 번이라도 +0.5% 찍었으면 STALE 유예
    "stale_exit_signal_recheck": True,

    # ─── 진입 등급 ───
    "entry_grades": ["A", "B"],

    # ─── 시간대 게이트 (KST 기준, 24/7이지만 얕은 유동성 회피) ───
    "thin_liquidity_hours": [2, 3, 4, 5],  # 02~06시 신규매수 억제
    "thin_liquidity_max_pct": 50.0,        # 얕은 시간대엔 사이징 50%만

    # ─── 운영 ───
    "cycle_interval_sec": 60,              # 1분 사이클
    "dry_run_default": True,
    "discord_notify": True,

    # ─── 페이퍼 트레이딩 (가상 잔고로 end-to-end 검증) ───
    "paper_trading_enabled": False,        # True 시 dry_run 강제 + 가상 잔고
    "paper_initial_krw": 1_000_000.0,      # 가상 시작 자본
}


def load_config() -> dict[str, Any]:
    """DEFAULT_CONFIG 에 data/user-config.json 내용을 덮어씌운 최종 설정을 반환.

    user-config.json 이 단일 편집 파일 역할. '_' 로 시작하는 키는 섹션 구분/주석용이므로
    코드에서는 무시하고 제거합니다 (JSON 이 주석을 지원하지 않아 대체 방식).
    """
    cfg = dict(DEFAULT_CONFIG)
    if USER_CONFIG_FILE.exists():
        try:
            override = json.loads(USER_CONFIG_FILE.read_text())
            if isinstance(override, dict):
                # '_' prefix 키(주석/섹션 구분자)는 드롭
                filtered = {k: v for k, v in override.items() if not k.startswith("_")}
                cfg.update(filtered)
        except Exception as e:
            print(f"[config] user-config.json 읽기 실패: {e} → 기본값 사용")
    return cfg


def save_config(overrides: dict[str, Any]) -> None:
    """user-config.json 에 오버라이드 저장 (기존 값과 병합)."""
    existing = {}
    if USER_CONFIG_FILE.exists():
        try:
            existing = json.loads(USER_CONFIG_FILE.read_text())
        except Exception:
            existing = {}
    existing.update(overrides)
    USER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def get_api_keys() -> tuple[str, str]:
    """환경변수에서 Upbit API 키를 로드. .env 파일도 지원."""
    # .env 파일 로드 시도
    env_file = REPO_DIR / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            # dotenv 없으면 수동 파싱
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    access = os.environ.get("UPBIT_ACCESS_KEY", "").strip()
    secret = os.environ.get("UPBIT_SECRET_KEY", "").strip()
    return access, secret


if __name__ == "__main__":
    import sys
    cfg = load_config()
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    access, secret = get_api_keys()
    print(f"\nAPI key loaded: {'Y' if access else 'N'} / {'Y' if secret else 'N'}", file=sys.stderr)
