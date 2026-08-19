#!/usr/bin/env bash
# new_app.py 실행. venv 준비 → selenium 확인 → 크롬 포트 확인 → 앱 기동.
set -euo pipefail

cd "$(dirname "$0")"

PORT=9222
VENV=".venv"

# tkinter 가 들어 있는 파이썬만 쓸 수 있다.
# pyenv/homebrew 파이썬은 _tkinter 없이 빌드된 경우가 많아 후보를 훑는다.
pick_python() {
  local cands=(
    "${PYTHON_BIN:-}"
    /usr/local/bin/python3
    /opt/homebrew/bin/python3
    "$(command -v python3 || true)"
    /usr/bin/python3
  )
  for p in "${cands[@]}"; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    if "$p" -c "import tkinter" >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

if [ ! -x "$VENV/bin/python" ]; then
  PY="$(pick_python)" || {
    echo "tkinter 가 있는 python3 를 찾지 못했다." >&2
    echo "  brew install python-tk  또는  PYTHON_BIN=/경로/python3 로 지정해라." >&2
    exit 1
  }
  echo "venv 생성 ($PY)"
  "$PY" -m venv "$VENV"
fi

PY="$VENV/bin/python"

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "venv 의 파이썬에 tkinter 가 없다. $VENV 를 지우고 다시 실행해라." >&2
  exit 1
fi

if ! "$PY" -c "import selenium" >/dev/null 2>&1; then
  echo "selenium 설치 중..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet selenium
fi

# python.org 빌드는 etc/openssl/cert.pem 이 없으면 트러스트 스토어가 비어
# HTTPS 가 전부 CERTIFICATE_VERIFY_FAILED 로 죽는다. _probe() 가 예외를 삼키는 탓에
# 증상은 "재측정 실패"로만 보인다. certifi 번들을 명시해 그 경로를 막는다.
if ! "$PY" -c "
import ssl, urllib.request
urllib.request.urlopen('https://ev.or.kr/', timeout=8)" >/dev/null 2>&1; then
  "$PY" -c "import certifi" >/dev/null 2>&1 || "$PY" -m pip install --quiet certifi
  CAFILE="$("$PY" -c "import certifi; print(certifi.where())")"
  export SSL_CERT_FILE="$CAFILE"
  export REQUESTS_CA_BUNDLE="$CAFILE"
  echo "인증서 번들 지정: $CAFILE"
fi

# 크롬이 먼저 떠 있어야 붙는다. 없으면 알려만 주고 계속 — 시계/조회는 포트 없이도 돈다.
if ! curl -sf -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "경고: $PORT 포트가 닫혀 있다. ./run_chrome.sh 를 먼저 실행해라." >&2
fi

exec "$PY" new_app.py
