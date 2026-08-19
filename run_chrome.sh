#!/usr/bin/env bash
# 디버깅 포트를 연 전용 프로필 크롬을 띄운다.
# Chrome 136+ 는 기본 프로필에 포트를 열어주지 않는다 — --user-data-dir 은 필수다.
set -euo pipefail

PORT=9222
PROFILE="${CARMACRO_PROFILE:-$HOME/Library/Application Support/Carmacro/chrome-profile}"
START_URL="${1:-https://ev.or.kr/ev_ps/ps/seller/sellerApplyInfo}"

CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
  for c in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
  do
    [ -x "$c" ] && CHROME="$c" && break
  done
fi
if [ -z "$CHROME" ]; then
  echo "크롬을 찾지 못했다. CHROME_BIN 환경변수로 경로를 지정해라." >&2
  exit 1
fi

# 이미 포트가 열려 있으면 두 번 띄우지 않는다 — 새 인스턴스는 포트를 못 잡는다.
if curl -sf -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "이미 $PORT 포트가 열려 있다. 그대로 쓴다."
  exit 0
fi

mkdir -p "$PROFILE"
echo "크롬 실행: 포트 $PORT / 프로필 $PROFILE"

nohup "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "$START_URL" \
  >/dev/null 2>&1 &
disown || true

# 포트가 실제로 뜰 때까지 기다린다. 앱이 먼저 붙으려다 실패하는 걸 막는다.
for _ in $(seq 1 40); do
  if curl -sf -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "준비 완료 — $(curl -s "http://127.0.0.1:$PORT/json/version" | tr ',' '\n' | grep -i '"Browser"' | cut -d'"' -f4)"
    exit 0
  fi
  sleep 0.25
done

echo "10초 안에 $PORT 포트가 열리지 않았다. 크롬 창은 떴는지 확인해라." >&2
exit 1
