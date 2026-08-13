# EV_apply — 인수인계

대상: `new_app.py` (CarMacro v2) / ev.or.kr 신청관리
작성 시점: 2026-08-11 오전

---

## 1. 실행 환경

| 항목 | 값 |
|---|---|
| Chrome | 151.0.7922.77 (`C:\Program Files\Google\Chrome\Application\chrome.exe`) |
| selenium | 4.45.0 |
| 디버깅 포트 | 9222 |
| 전용 프로필 | `%LOCALAPPDATA%\Carmacro\chrome-profile` |

크롬 실행:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\Carmacro\chrome-profile" --no-first-run --no-default-browser-check https://ev.or.kr/ev_ps/ps/seller/sellerApplyInfo
```

- Chrome 136부터 **기본 프로필에는 디버깅 포트가 열리지 않는다.** 전용 `--user-data-dir` 필수.
- 팝업 허용 규칙이 이 프로필에 저장돼 있음: `Default/Preferences` → `profile.content_settings.exceptions.popups` 에 `https://ev.or.kr:443,*` → `setting: 1`.
- **크롬을 재시작하면 ev.or.kr 세션이 끊긴다**(재로그인 필요). 팝업 허용 규칙은 유지된다.

---

## 2. 계정에 실제로 발생한 변경 (2026-08-11)

| 신청번호 | 내용 | 발생한 일 | 현재 상태 |
|---|---|---|---|
| `1349523` | 수원시 · 코나 일렉트릭 · 박수경(더미) | 10:19:00 지원신청 접수됨 (팝업 auto 모드, `goCompare` 호출까지) | 취소됨. 취소 사유 메모 `내용 테스트 잘못 신청함`, 취소일 2026-08-11 |
| `1350440` | 수원시 · 코나 일렉트릭 · 박수경(더미) | 신규 신청서 작성 + 임시저장으로 생성 | **제출전에 남아 있음. 정리 필요** |
| `1221543` | 세종특별자치시 · 아이오닉6 · 정희경 | 목록 조회만. 폼 진입·발사 없음 | 지원신청 단계 그대로 |

- 취소 건수 25 → 26.
- `1350440`은 취소된 `1349523`의 입력값을 그대로 복제해 만들었다. 금액도 동일(총 7,480,000 / 국비 5,140,000 / 지방비 2,140,000).
- `1350440`에는 실행 중 첨부파일이 등록되었다(개인정보 동의서, 차량 구매계약서, 등본, 우선순위 증빙, 지방세 납세증명서).
- 취소 기능은 이 작업 중 호출한 적이 없다.

---

## 3. 사이트 동작 (소스·실행 관측)

### 지원신청 호출 체인

```
goApply('101', msg)
  └ goApply_1(checkYn, data1, msg, obj)
      └ doApply(data1, msg)
          ├ POST /ev_ps/ps/seller/finishChkJson        → {"CHK_YN":"Y","RETURNMSG":""}
          ├ POST /ev_ps/ps/seller/carApplyTimeChkJson  → chkYn=="Y" 이면 "요청이 너무 빠릅니다."
          └ execApply(data, msg)
              ├ confirm(msg)
              ├ $("#clickChk").val('Y')
              └ data=='101'|'121' → popup('/ev_ps/ps/comm/popupSellerApplyRandomChk', 'popupRandom')
```

- `data1='101'` 경로에는 `form.submit()` 이 없다. 실제 저장은 팝업의 `goCompare` 가 수행한다.
- `execApply` 의 `confirm()` 은 `finishChkJson` 응답 뒤(비동기)에 호출된다. `goApply()` 반환 시점에는 아직 호출 전이다.

### 임시저장 호출 체인

```
goSave()
  └ goSave_1(checkYn)
      └ checkFinish()
          └ POST /ev_ps/ps/seller/finishChkJson
              └ mode=='write' → popup('/ev_ps/ps/comm/popupSellerRandomChk', 'popupRandom')
```

- 신규 작성 임시저장도 확인코드 팝업을 거친다. 팝업 URL이 지원신청과 다르지만 둘 다 `RandomChk` 를 포함해 `POPUP_MARKS` 에 걸린다.
- 완료 시 네이티브 alert `임시저장 완료` 가 뜬다.

### 확인코드

- 팝업 소스의 `goCompare` 함수 리터럴에 코드가 들어 있다. 정규식: `= (['"])([0-9A-Za-z]{6,16})\1 .split`
- 관측된 코드는 모두 **10자리 영숫자 대소문자 혼합**: `yTtF5Vyt6E`, `udVpu1x3b5`, `oiO676Z5x9`, `WJzcY5zDNQ`, `qT6307S3G6`, `y8An0La746`, `bg3LdX25Di`
- 화면 표시 코드와 `goCompare` 소스 코드가 매번 일치했다.
- 입력칸 id: `randeomChk`. 코드를 뒤집어 넣는다.

### 검증·차단 메시지

| 상황 | alert 문구 |
|---|---|
| 사용자 제스처 없이 `window.open` | `차단된 팝업창을 허용해 주십시오.` |
| 동의서 미첨부 상태로 지원신청 | `보조금 구매지원 신청서 개인정보 동의 수집이용 및 제3자 위탁 제공 동의서 pdf파일을 등록해주세요.` |
| 폼 잠금 중 재호출 | `지원신청 제출중 입니다.` / `저장 중입니다.` |
| 재시도 간격 짧을 때 | `요청이 너무 빠릅니다. 잠시 후 다시 시도해주세요.` |

### 그 밖의 관측

- `#clickChk` 가 `'Y'` 면 `goApply`/`goSave` 가 즉시 return 한다. 목록으로 나갔다 폼에 재진입하면 해제된다.
- `goSave_1` 에 `local_cd` 가 서버 심어둔 값과 다르면 `전송된 파라미터의 위·변조 … 자동 로그아웃` 처리하는 분기가 있다.
- 팝업에 코드를 입력하고 약 26초 방치한 뒤 확인해 보니 팝업이 닫혀 있고 메인 창이 리스트로 돌아가 있었다. **1회 관측, 원인 미확인.**
- `app_accept` 는 목록 페이지에만 정의돼 있다. 폼 페이지에서 호출하면 `app_accept is not defined`.

---

## 4. `new_app.py` 변경 내역

### 추가된 상수

```python
POPUP_POLL  = 0.03   # 팝업 감시 간격
POPUP_WATCH = 10.0   # 발사 후 감시 시간
FORM_POLL   = 0.01   # 폼 로드 확인 간격(정각진입 시 임계경로)
FORM_WAIT   = 15.0   # 폼 로드 최대 대기
POPUP_JS             # 팝업 처리 전체를 1왕복으로 실행하는 JS
```

### `PopupWatch`

시그니처: `PopupWatch(d, log=None, mode=None)`
`mode` 는 1칸 리스트(작업 스레드가 읽으므로 tk 변수 아님).

| mode | 동작 |
|---|---|
| `watch` | 감지만. 입력칸·포커스 건드리지 않음 |
| `fill` | 역순 입력 후 `goCompare` **직전 정지** |
| `auto` | `goCompare()` 호출 = 저장 확정 |

반환값: `None` / `"watched"` / `"nocode"` / `"filled"` / `"confirmed"`

- `POPUP_JS` 한 번으로 URL 판정 → 코드 추출(goCompare 소스 우선, DOM 폴백) → 역순 입력 → 옵션 `goCompare` 까지 처리. 팝업당 왕복은 창 전환 1회 + 스크립트 1회.
- `_switch_raw()` 는 존재 확인 없이 전환(감시 루프용). `restore`/`focus_input` 은 검증하는 `_switch()` 사용.
- `poll()` 은 URL 미확정(`about:blank`) 창을 `known` 에 넣지 않는다.
- 이전 버전에 있던 `read_code()` / `fill_reversed()` 는 삭제(로직이 `POPUP_JS` 와 이중화되므로).

### `App`

- `popup_mode` / `entry_mode` (각각 1칸 리스트), 발사 프레임에 라디오 2세트.
- `_drain_alerts()` — 삼켜둔 site alert 를 꺼내 비운다.
- `_restore_dialogs()` — `confirm`/`alert` 원복. 메인으로 돌아가서 실행하고, 팝업이 살아 있으면 포커스를 되돌린다.
- `_mk_watch()` — 로그를 `self.q` 로 넘기는 `PopupWatch` 생성.
- `_enter_form(seq, critical)` — `app_accept` + 폼 로드 대기. `critical=True` 면 `FORM_POLL`(10ms), 아니면 0.1s.

### `_fire_worker` — 정각 동작 분기

| `entry_mode` | 정각에 하는 일 |
|---|---|
| `at_target` (기본) | 리스트에서 `app_accept` 진입 → 폼 뜨는 즉시 `goApply` |
| `pre_enter` | `pre` 초 전에 미리 진입해 두고 정각엔 `goApply` 만 |

- `at_target` 리허설은 리스트에 선 채 대기만 하고 진입하지 않는다.
- `confirm`/`alert` 오버라이드 복원 시점을 **팝업 처리 완료 후**로 옮겼다. `goApply` 직후 복원하면 비동기 `confirm()` 이 네이티브 창으로 떠서 드라이버가 멈춘다.
- 팝업 대기 루프에서 매 폴마다 alert 를 확인하고, 잡히면 `POPUP_WATCH` 를 채우지 않고 즉시 종료한다.

---

## 5. 실측값

### 드라이버 왕복 (20회 중앙값, localhost CDP)

| 호출 | 중앙값 |
|---|---|
| `window_handles` | 0.9ms |
| `switch_to.window` | 1.9ms |
| `execute_script` | 2.9ms |
| PopupWatch 유휴 폴 1회 | 0.98ms |

### 발사 구간 (여러 회 실측)

| 구간 | 관측값 |
|---|---|
| `app_accept` + 폼 로드 | 713 / 726 / 760 / 791 / 1045 / 1190 ms |
| `goApply` 디스패치 | 14 / 32 / 36 / 38 / 52 ms |
| 발사 → 역순 입력 완료 | 223 / 224 / 239 / 243 ms |
| 폼 준비 → `goApply` 지시 | 5ms (`FORM_POLL` 10ms 적용 후) |

`at_target` 모드 1회 전체(11:27 실행): 진입 760ms + 디스패치 32ms + 팝업 224ms = **정각 지시부터 코드 입력 완료까지 1021ms**.

### 서버시계

| 실행 | offset | 구간 폭 | RTT |
|---|---|---|---|
| 11:06 | -0.353s | 169ms | 85ms |
| 11:13 | -0.345s | 190ms | 103ms |
| 11:26 | -0.413s | 212ms | 85ms |

`early_risk` (RTT 85ms, safe 모드): 리드 0ms → 여유 49ms* / 30ms → 여유 13ms / 40ms → 여유 3ms / 50ms → 8ms 일찍 도착 가능
(*리드 0 값은 RTT가 다른 실행에서 측정)

주의: 화면의 리스크 라벨은 50ms 주기 `_tick` 에서 갱신된다. 리드를 바꾼 직후 읽으면 이전 값이 나온다.

---

## 6. 검증 상태

### 확인된 것

- 팝업 감지 → `goCompare` 소스에서 코드 추출 → 역순 입력 → (옵션) `goCompare` 호출까지 실제 사이트에서 동작.
- `watch` / `fill` / `auto` 세 모드 모두 실제 팝업으로 동작 확인.
- 팝업 허용 후에는 `execute_script("goApply(...)")` 경로에서도 팝업이 열린다.
- alert 캡처가 실패 원인(동의서 미첨부)을 즉시 표면화.
- `at_target` 모드 실발사 1회 완주 (`goCompare` 직전 정지, 접수되지 않음).
- 단위 테스트: `PopupWatch` 9케이스, `POPUP_JS` 11케이스 통과.

### 확인 안 된 것

- **접수 시작 전에 `app_accept` 로 폼 진입이 가능한지.** 모든 테스트는 이미 열린 접수창(수원시 2026-08-10 ~ 09-30)에서 수행했다.
- **미리 진입해 둔 폼으로 정각 `goApply` 가 통과하는지.** `pre_enter` 모드의 전제.
- 확인코드 팝업의 유효시간.
- `carApplyTimeChkJson` 의 재시도 허용 간격.
- `at_target` 모드의 리허설 경로는 실행해 보지 않았다.
- 발사 후 페이지가 이동하는 경우 `_restore_dialogs` 가 정상 동작하는지(101 경로는 이동하지 않아 미발생).

---

## 7. 재현 방법

앱 실행:

```
python C:\Users\fivep\Carmacro2\new_app.py
```

순서: 크롬 연결/새로고침 → 목록에서 행 선택 → 목표시각·리드·정각동작·팝업모드 설정 → 무장 → 예약 발사.

테스트 스크립트(임시 디렉터리, 세션 종료 시 사라질 수 있음):

- `t_popup.py` — `PopupWatch` 가짜 드라이버 단위 테스트
- `gui_run.py` — GUI 를 띄우고 한 사이클 자동 조작 + 스크린샷

`gui_run.py` 는 무장 확인창(`messagebox.askyesno`)을 자동 승인으로 패치한다. 나머지(서버시계, 큐 펌프, 발사 스레드, 팝업 처리)는 실제 코드가 그대로 돈다.

---

## 8. 남은 작업

- `1350440` 정리(취소 또는 삭제).
- 접수 시작 전 `app_accept` 가능 여부 확인 → 결과에 따라 `pre_enter` 모드 존치 여부 결정.
- 확인코드 팝업 유효시간 측정.
- 실행 후 팝업이 닫히고 리스트로 돌아간 현상(1회 관측) 원인 확인.
