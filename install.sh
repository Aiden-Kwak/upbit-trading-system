#!/bin/bash
# 설치 스크립트
set -e

cd "$(dirname "$0")"

echo "▶ Python 가상환경 생성"
python3 -m venv .venv

echo "▶ 의존성 설치"
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "▶ DB 초기화"
.venv/bin/python scripts/db.py init

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  .env 파일을 편집하여 UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY 입력하세요."
  echo "    업비트 마이페이지 > Open API 관리: https://upbit.com/mypage/open_api_management"
  echo "    권한: 자산조회 + 주문조회/생성/취소 (출금 권한 금지)"
fi

echo ""
echo "✅ 설치 완료"
echo ""
echo "사용법:"
echo "  .venv/bin/python scripts/upbit_client.py tickers       # KRW 마켓 목록"
echo "  .venv/bin/python scripts/indicators.py KRW-BTC          # 지표 테스트"
echo "  .venv/bin/python scripts/signal_engine.py KRW-BTC       # 시그널 테스트"
echo "  .venv/bin/python scripts/backtest.py KRW-BTC --days 200 # 백테스트"
echo "  .venv/bin/python scripts/autotrade_daemon.py --dry-run  # 드라이런 실행"
echo "  .venv/bin/python dashboard/server.py                    # 대시보드 (localhost:8766)"
