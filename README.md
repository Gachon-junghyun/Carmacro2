# Carmacro2

ev.or.kr 판매자 신청 보조 도구. 서버시계 측정 · 신청현황 조회 · 예약 발사.

## Windows — exe 로 받기

[최신 릴리스](https://github.com/Gachon-junghyun/Carmacro2/releases/latest/download/Carmacro-win.zip)
를 받아 압축을 풀고 `Carmacro.exe` 실행. 파이썬 설치는 필요 없다.

1. `Carmacro.exe` 실행
2. **[크롬 실행]** 버튼 — 전용 프로필 크롬이 뜬다
3. 그 창에서 ev.or.kr 로그인
4. **[크롬 연결 / 새로고침]**

크롬은 그 PC 에 설치돼 있어야 한다. 서명 없는 exe 라 SmartScreen 경고가 뜨면
"추가 정보" → "실행" 으로 통과시킨다.

## macOS — 소스로 실행

```sh
./run_chrome.sh     # 디버깅 포트를 연 전용 프로필 크롬
./run_app.sh        # venv 준비 → 의존성 설치 → 앱 기동
```

`run_app.sh` 가 venv 를 만들고 selenium 을 깐다. tkinter 가 들어 있는 파이썬이
필요하다(`brew install python-tk`, 또는 `PYTHON_BIN=/경로/python3` 로 지정).

앱 안의 **[크롬 실행]** 버튼도 macOS 경로를 처리하므로 `run_chrome.sh` 없이
버튼만 눌러도 된다. 스크립트는 앱을 띄우기 전에 크롬만 먼저 올리고 싶을 때 쓴다.

## 왜 전용 프로필인가

Chrome 136+ 는 기본 프로필에 원격 디버깅 포트를 열어주지 않는다. `--user-data-dir`
로 별도 프로필을 강제해야 포트(9222)가 산다. 그래서 평소 쓰던 크롬 창에는 붙을 수 없다.

환경변수로 바꿀 수 있다:

| 변수 | 뜻 | 기본값 |
| --- | --- | --- |
| `CHROME_BIN` | chrome 실행 파일 경로 | 플랫폼별 표준 경로 자동 탐색 |
| `CARMACRO_PROFILE` | 전용 프로필 위치 | win `%LOCALAPPDATA%\Carmacro\chrome-profile` / mac `~/Library/Application Support/Carmacro/chrome-profile` |

## 배포

태그를 밀면 GitHub Actions(`windows-latest`)가 exe 를 빌드해 릴리스에 붙인다.

```sh
git tag v1.0.0
git push origin v1.0.0
```

수동 빌드가 필요하면 Windows 에서:

```sh
pyinstaller --noconfirm --clean --onefile --windowed \
  --name Carmacro --collect-all selenium new_app.py
```

macOS 용 `.app` 은 macOS 에서 같은 명령을 돌려야 한다 — exe 는 mac 에서 돌지 않는다.
