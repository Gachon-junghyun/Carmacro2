
import http.client
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from tkinter import messagebox, ttk

KST = timezone(timedelta(hours=9))
PORT = 9222
# 시계는 **루트**에서 잰다. Date 헤더는 같은 서버가 찍지만 응답이 훨씬 싸다.
# 구간 폭은 곧 RTT 다(제약 하나가 1.0+RTT 초 폭) — 느린 엔드포인트로 재면
# 아무리 많이 던져도 폭이 안 좁아진다. 접수 몰릴 때 실측:
#   루트 /                폭 386ms · 최소RTT 256ms · 실패 0
#   sellerApplyInfo      폭 917ms · 최소RTT 336ms · 실패 0   (TTFB 최대 10.8s 관측)
# 게다가 정작 필요한 신청 경로에 부하를 얹지 않는 이점도 있다.
CLOCK_URL = "https://ev.or.kr/"
LIST_URL = "https://ev.or.kr/ev_ps/ps/seller/sellerApplyInfo"
PERIOD_URL = "https://ev.or.kr/ev_ps/ps/main/statLocalPeriod"
PROBE_TIMEOUT = 20     # 부하 때 seller TTFB 10.8s 관측 — 8s 는 너무 짧았다

KEEP_INTERVAL = 30 * 60   # 자동 유지 주기(s) — 세션 60분의 절반
KEEP_GAP = 5.0            # 새로고침 **완료 후** 연장까지 두는 간격(s)
KEEP_RETRY = 60           # 발사/팝업 때문에 걸렀을 때 재시도(s)
PREFLIGHT_EVERY = 3.0     # 발사 전 점검 주기(s) — 드라이버 1왕복이 든다
CLOCK_STALE = 600         # 시계 측정이 이보다 오래되면 경고(s)

# 목록의 단계 탭은 btnSearch('<코드들>', ...) 로 갈린다. 실측(2026-08-20):
#     제출전(1건)        btnSearch('100', 'ALL')
#     지원신청(1건)       btnSearch('101,102,103', 'N')
#     보완요청(승인전)     btnSearch('110', 'N')
# 그리고 행 버튼은 app_accept('<신청번호>', '<app_step>', '') 이다.
# 즉 행이 자기 단계 코드를 들고 있다 — 라벨("제출전")로 탭을 찾으면 그 행이
# 다른 단계에 있을 때 영영 못 찾는다. 코드로 찾는 게 맞다.
STAGE_MARK = "제출전"   # 코드 매칭이 실패했을 때만 쓰는 라벨 폴백

# 대상 행이 보이는지 확인하고, 없으면 그 행의 단계 탭을 눌러 준다. 한 왕복.
# 반환: 'row' / 'clicked:<라벨>' / 'nostage' / 'nolist'
STAGE_JS = r"""
var seq = arguments[0], step = arguments[1], mark = arguments[2];
if (typeof app_accept !== 'function') return 'nolist';
var acc = document.querySelectorAll("[onclick*='app_accept']");
for (var i = 0; i < acc.length; i++) {
  if ((acc[i].getAttribute('onclick') || '').indexOf("'" + seq + "'") >= 0) return 'row';
}
var tabs = document.querySelectorAll("[onclick*='btnSearch']");
// 1순위: 단계 코드가 그 탭의 코드 목록에 든 버튼
for (var j = 0; j < tabs.length; j++) {
  var oc = tabs[j].getAttribute('onclick') || '';
  var m = oc.match(/btnSearch\('([^']*)'/);
  if (!m) continue;
  var codes = m[1].split(',');
  for (var k = 0; k < codes.length; k++) {
    if (codes[k].replace(/\s/g, '') === step) {
      tabs[j].click();
      return 'clicked:' + (tabs[j].textContent || '').replace(/\s+/g, '');
    }
  }
}
// 2순위: 라벨 폴백
for (var n = 0; n < tabs.length; n++) {
  if ((tabs[n].textContent || '').replace(/\s+/g, '').indexOf(mark) === 0) {
    tabs[n].click();
    return 'clicked:' + (tabs[n].textContent || '').replace(/\s+/g, '');
  }
}
return 'nostage';
"""

POPUP_MARKS = ("RandomChk", "popupSellerRandom")
POPUP_INPUT = "randeomChk"
POPUP_POLL = 0.03      # 팝업 감시 간격(s) — 감지 지연의 상한
POPUP_WATCH = 10.0     # 발사 후 감시 시간(s)
FORM_POLL = 0.01       # 신청서 폼 로드 확인 간격(s) — 정각진입 때 임계경로에 든다
FORM_RELAX = 10.0      # 이 시간이 지나면 폴링을 0.1s 로 늦춘다. 정각 직후엔 10ms
                       # 정밀도가 의미 있지만, 몇 분째 서버가 안 받는 상황에서
                       # 10ms 로 계속 두드리면 드라이버가 먼저 지친다.
# 접수 몰릴 때 서버가 아예 연결을 끊는다(ERR_EMPTY_RESPONSE). 14분 넘게 그 상태인
# 걸 실제로 겪었다 — 40s 예산은 그 회차를 통째로 버리는 값이었다. 우회로는 없고
# 서버가 숨 쉬는 순간에 걸리는 수밖에 없으니, 지치지 않고 계속 두드리는 게 전부다.
FORM_WAIT = 1800.0     # 폼 로드 최대 대기(s) = 총 재시도 예산. 30분.
                       # 주의: 이 동안 자동 세션연장이 멈춘다(_keep_run 이 발사
                       # 스레드를 보고 건너뛴다). 사이트 세션이 60분이니 이미
                       # 오래 열어둔 창이면 재시도 도중 세션이 먼저 죽을 수 있다.
ENTER_TRIES = 2000     # 진입 재시도 횟수. **진입만** — 제출은 절대 재시도하지 않는다.
                       # 사실상 무제한이고 진짜 상한은 FORM_WAIT 다.
RETRY_GAP = 0.5        # 회차 사이 간격(s). 끊고 있는 서버를 더 빨리 두드려도
                       # 돌아오는 건 같다 — 예산만 태운다.
BACK_BUDGET = 15.0     # 목록 복귀 한 번에 줄 최대 시간(s). 예산 비율로 주면
                       # 900s 예산에서 한 번의 멈춤이 450s 를 삼킨다.
DEAD_CHECK = 0.3       # 페이지 사망 판정 주기(s). innerText 는 레이아웃을 강제해
                       # 비싸다 — FORM_POLL(10ms)로 돌리면 임계경로를 갉아먹는다

# 서버가 부하로 연결을 끊으면 크롬은 자체 오류 페이지를 띄운다. 그 위에서
# goApply 를 기다려 봐야 영원히 안 온다. 한 왕복으로 상태만 분류한다.
DEAD_JS = r"""
if (typeof goApply === 'function') return 'ok';
if (document.readyState !== 'complete') return 'loading';
var t = ((document.body && document.body.innerText) || '').trim();
if (/ERR_|작동하지 않습니다|연결할 수 없|시간이 너무 오래|Bad Gateway|Service Unavailable|Gateway Time-?out|HTTP Status (50|40)/i.test(t))
  return 'dead';
if (t.length < 80) return 'blank';
return 'other';
"""

# 확인코드 팝업 처리 전체를 브라우저에서 1왕복으로 끝낸다.
# URL 판정 → 코드 읽기 → 역순 입력 → (옵션)확인까지 한 번에.
# 왕복마다 붙는 드라이버 지연(수~수십 ms)을 곱하지 않으려는 것이다.
POPUP_JS = r"""
var marks = arguments[0], id = arguments[1], auto = arguments[2], act = arguments[3];
var href = location.href || '';
var hit = false;
for (var i = 0; i < marks.length; i++) {
  if (href.indexOf(marks[i]) >= 0) { hit = true; break; }
}
if (!hit) return {popup: false, url: href};
var code = '';
try {
  // 따옴표 종류는 사이트 스크립트에 달렸다 — 둘 다 받는다.
  var m = goCompare.toString().match(/=\s*(['"])([0-9A-Za-z]{6,16})\1\s*\.split/);
  if (m) code = m[2];
} catch (e) {}
if (!code) {
  var els = document.querySelectorAll('span.guide,span,div,b');
  for (var j = 0; j < els.length; j++) {
    var t = (els[j].textContent || '').trim();
    if (/^[0-9A-Za-z]{6,16}$/.test(t)) { code = t; break; }
  }
}
var out = {popup: true, url: href, code: code, filled: false, confirmed: false};
if (!act) return out;
var e = document.getElementById(id);
// 코드를 못 읽었어도 커서는 입력칸에 놔둔다 — 사람이 바로 이어 칠 수 있게.
if (e) { try { e.focus(); if (e.select) e.select(); } catch (e2) {} }
if (!code || !e) return out;
var rev = code.split('').reverse().join('');
e.value = rev;
e.dispatchEvent(new Event('input', {bubbles: true}));
out.rev = rev;
out.filled = true;
if (auto) {
  try { goCompare(); out.confirmed = true; } catch (e3) {}
}
return out;
"""

STEPS = [
    ("제출전", "100", "ALL"), ("지원신청", "101,102,103", "N"),
    ("보완요청(승인전)", "110", "N"), ("보완완료(승인전)", "111", "N"),
    ("자격부여", "120", "ALL"), ("지원가능확인", "121", "ALL"),
    ("대상자선정", "130", "ALL"), ("지급신청", "201,202", "ALL"),
    ("지급예정", "203", "ALL"), ("지급확인", "220", "ALL"),
    ("최종완료", "501", "ALL"),
]


PROBE_HEADERS = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}


class _ClockProbe:
    """서버 Date 를 읽는 HEAD 프로브. **연결을 유지한다.**

    제약 하나의 폭이 1.0+RTT 초라 구간 폭은 결국 RTT 바닥에 수렴한다.
    매번 새로 열면 TLS 핸드셰이크가 그 RTT 에 통째로 들어간다. 같은 25회 실측:
      새 연결 매번   폭 416.0ms · 최소RTT 239.0ms
      연결 유지      폭 150.6ms · 최소RTT  45.9ms
    선착순에서 이 차이는 곧 순번이다.

    (측정값, 실패사유) 를 돌려준다. 실패를 삼키면 SSL·타임아웃·차단이 전부
    '재측정 실패'로 뭉개져 원인을 못 찾는다.
    """

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        self.host, self.port = u.hostname, u.port or 443
        self.path = u.path or "/"
        self.c = None

    def close(self):
        try:
            if self.c:
                self.c.close()
        except Exception:
            pass
        self.c = None

    def __call__(self):
        # 유휴 연결은 서버가 끊는다(측정 주기 180s). 한 번은 다시 열고 재시도한다.
        last = None
        for _ in range(2):
            try:
                if self.c is None:
                    self.c = http.client.HTTPSConnection(
                        self.host, self.port, timeout=PROBE_TIMEOUT,
                        context=ssl.create_default_context())
                t0 = time.time()
                self.c.request("HEAD", self.path, headers=PROBE_HEADERS)
                r = self.c.getresponse()
                t1 = time.time()
                d = r.getheader("Date")
                r.read()                      # 다음 요청을 위해 반드시 비운다
                if not d:
                    return None, "Date 헤더 없음"
                return (parsedate_to_datetime(d).timestamp(), t0, t1), None
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
                self.close()
        return None, last


class ServerClock(threading.Thread):

    daemon = True

    def __init__(self, out_q, interval=180):
        super().__init__()
        self.q = out_q
        self.interval = interval
        self.offset = None
        self.lo = self.hi = None
        self.err = None
        self.rtt = None
        self.at = None
        self.mode = "safe"
        self.jumped = False
        self._kick = threading.Event()
        self.probe = _ClockProbe(CLOCK_URL)   # 연결 유지 — 측정 스레드 전용

    def remeasure_now(self):
        self._kick.set()

    def run(self):
        while True:
            self._measure()
            self._kick.wait(self.interval)
            self._kick.clear()

    def _measure(self, n=25):
        lo, hi, kept, best = -1e9, 1e9, 0, 9e9
        errs = []
        for _ in range(n):
            r, err = self.probe()
            if not r:
                if err and err not in errs:
                    errs.append(err)
                time.sleep(0.2)
                continue
            s, t0, t1 = r
            best = min(best, t1 - t0)
            a, b = s - t1, (s + 1.0) - t0
            na, nb = max(lo, a), min(hi, b)
            if na < nb:
                lo, hi, kept = na, nb, kept + 1
            else:
                lo, hi, kept = a, b, 1
            time.sleep(0.11)
        if kept >= 3:
            if errs:
                self.q.put(("clock", "   (%d/%d 실패 — %s)"
                            % (n - kept, n, " / ".join(errs[:2]))))
            prev = self.offset
            self.lo, self.hi = lo, hi
            self.offset, self.err = (lo + hi) / 2, (hi - lo) / 2
            self.rtt, self.at = best, time.time()
            self.jumped = prev is not None and abs(self.offset - prev) > 0.25
            self.q.put(("clock", "재측정: offset %+.3fs [%+.3f, %+.3f] 폭 %.0fms, RTT %.0fms%s"
                        % (self.offset, lo, hi, (hi - lo) * 1000, best * 1000,
                           "  ⚠ 이전 대비 %+.0fms 튐" % ((self.offset - prev) * 1000)
                           if self.jumped else "")))
        else:
            self.q.put(("clock", "재측정 실패 (%d/%d) — %s"
                        % (n - kept, n,
                           " / ".join(errs[:2]) if errs else "원인 미상")))

    def _off(self):
        """모드별 offset. safe 는 구간 하한(=서버시각을 낮게 봐서 늦게 쏨)."""
        if self.offset is None:
            return None
        return {"safe": self.lo, "fast": self.hi}.get(self.mode, self.offset)

    def now(self):
        """서버 기준 현재 epoch(모드 반영). 미측정이면 None."""
        o = self._off()
        return None if o is None else time.time() + o

    def band(self):
        """(가장 이른, 가장 늦은) 서버 현재시각 추정."""
        if self.lo is None:
            return None
        t = time.time()
        return t + self.lo, t + self.hi

    def local_for(self, server_epoch, lead=0.0):
        """서버시각 server_epoch 에 도달하려면 로컬시각 몇에 쏴야 하나."""
        o = self._off()
        return None if o is None else server_epoch - o - lead

    def early_risk(self, lead):
        """이 리드로 쐈을 때 '접수 시작 전 도착' 이 가능한가.

        도착 = T + (offset_true - offset_사용) + (편도지연 - 리드).
        safe 모드면 첫 항 ≥ 0 이라 (편도지연 - 리드) 만 보면 된다.
        """
        if self.lo is None:
            return None
        one_way = (self.rtt or 0) / 2
        if self.mode == "safe":
            worst = one_way - lead
        elif self.mode == "fast":
            worst = -(self.hi - self.lo) + one_way - lead
        else:
            worst = -(self.hi - self.lo) / 2 + one_way - lead
        return worst


# ── 크롬 기동 ───────────────────────────────────────────────────────────────
# Chrome 136+ 는 기본 프로필에 디버깅 포트를 열어주지 않는다 — --user-data-dir 로
# 전용 프로필을 강제해야 포트가 산다. 예전엔 run_chrome.sh/.bat 을 따로 들고
# 다녔는데, exe 로 배포하면 스크립트가 따라다니지 않아 앱 안으로 들여왔다.

def chrome_profile():
    p = os.environ.get("CARMACRO_PROFILE")
    if p:
        return p
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Carmacro/chrome-profile")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Carmacro", "chrome-profile")
    return os.path.expanduser("~/.config/carmacro/chrome-profile")


def find_chrome():
    c = os.environ.get("CHROME_BIN")
    if c and os.path.exists(c):
        return c
    if os.name == "nt":
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        cands = [os.path.join(b, "Google", "Chrome", "Application", "chrome.exe")
                 for b in bases if b]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                 "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"]
    else:
        cands = []
    for c in cands:
        if os.path.exists(c):
            return c
    for n in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        c = shutil.which(n)
        if c:
            return c
    return None


def port_version(timeout=1.0):
    """디버깅 포트가 살아 있으면 브라우저 이름을, 아니면 None."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
        c.request("GET", "/json/version")
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        c.close()
        if r.status != 200:
            return None
        m = re.search(r'"Browser"\s*:\s*"([^"]*)"', body)
        return m.group(1) if m else "Chrome"
    except Exception:
        return None


def launch_chrome(url=LIST_URL):
    exe = find_chrome()
    if not exe:
        raise RuntimeError("크롬을 찾지 못했다 — CHROME_BIN 환경변수로 경로를 지정해라")
    prof = chrome_profile()
    os.makedirs(prof, exist_ok=True)
    args = [exe,
            "--remote-debugging-port=%d" % PORT,
            "--user-data-dir=%s" % prof,
            "--no-first-run",
            "--no-default-browser-check",
            url]
    # 앱이 죽어도 크롬은 남아야 한다 — 부모에서 떼어 띄운다.
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    return exe, prof


def attach():
    from selenium import webdriver
    o = webdriver.ChromeOptions()
    o.add_experimental_option("debuggerAddress", "127.0.0.1:%d" % PORT)
    return webdriver.Chrome(options=o)


def ev_tab(d):
    for h in d.window_handles:
        d.switch_to.window(h)
        if "ev.or.kr" in d.current_url:
            return True
    return False


def read_page(d):

    info = {"url": d.current_url, "counts": {}, "rows": [], "session": ""}
    try:
        info["session"] = d.find_element("id", "ViewTimer").text.strip()
    except Exception:
        pass

    for b in d.find_elements("xpath", "//button[contains(@onclick,'btnSearch')]"):
        t = (b.text or "").replace("\n", " ").strip()
        m = re.match(r"^(.*)\((\d+)\s*건\)\s*$", t)
        if m:
            info["counts"][m.group(1).strip()] = m.group(2)

    best = {}
    for b in d.find_elements("xpath", "//button[contains(@onclick,'app_accept')]"):
        oc = b.get_attribute("onclick") or ""
        m = re.search(r"app_accept\('(\d+)',\s*'(\d+)'", oc)
        if not m:
            continue
        seq, step = m.group(1), m.group(2)
        cells = []
        try:
            tr = b.find_element("xpath", "./ancestor::tr[1]")
            cells = [c.text.strip() for c in tr.find_elements("tag name", "td")
                     if c.text.strip() and c.text.strip() != "지원신청조회"]
        except Exception:
            pass
        prev = best.get(seq)
        if prev is None or len(cells) > len(prev["cells"]):
            best[seq] = {"seq": seq, "step": step, "cells": cells}

    for seq, r in best.items():
        info["rows"].append({"seq": seq, "step": r["step"],
                             "desc": " · ".join(r["cells"][:8])})
    return info


def fetch_periods(d, start, end):
    """지자체 차종별 접수기간을 **새 탭에서** 읽고 탭을 닫는다.

    작업 중인 신청관리 탭을 건드리지 않으려고 새 탭을 쓴다(경기 중 이탈 방지).
    반환: [{'notice','year','car','local','start','end','cnt'}, …]
    """
    cur = d.current_window_handle
    d.switch_to.new_window("tab")
    rows = []
    try:
        d.get(PERIOD_URL)
        time.sleep(3)
        d.execute_script(
            "document.getElementById('startDay').value=arguments[0];"
            "document.getElementById('endDay').value=arguments[1];", start, end)
        d.execute_script("goSearch();")
        time.sleep(4)
        for tb in d.find_elements("xpath", "//table[contains(@class,'table-case1')]"):
            for tr in tb.find_elements("tag name", "tr"):
                tds = [c.text.strip() for c in tr.find_elements("tag name", "td")]
                if len(tds) >= 7 and tds[4]:
                    rows.append({"notice": tds[0], "year": tds[1], "car": tds[2],
                                 "local": tds[3], "start": tds[4], "end": tds[5],
                                 "cnt": tds[6]})
    finally:
        try:
            d.close()
        except Exception:
            pass
        d.switch_to.window(cur)
    return rows


def find_applicant(d, seq):
    """오른쪽 스크롤 표에서 해당 신청번호 행의 신청자명 등을 찾아본다(있으면)."""
    try:
        for tr in d.find_elements("xpath", "//table[@id='tablefix']//tr"):
            tds = [c.text.strip() for c in tr.find_elements("tag name", "td")]
            if any(t.isdigit() and len(t) > 4 for t in tds) or seq in tds:
                return " | ".join([t for t in tds if t][:8])
    except Exception:
        pass
    return ""


class PopupWatch:

    def __init__(self, d, log=None, mode=None):
        """log(msg, red=False) 는 UI 로그 콜백.

        mode 는 팝업을 어디까지 처리할지 담은 1칸짜리 리스트 —
          "watch" 감지만 (입력칸도 안 건드림)
          "fill"  역순 입력까지, goCompare **직전에 정지**
          "auto"  goCompare 까지 호출 (= 저장 확정)
        작업 스레드에서 읽으므로 tk 변수가 아니라 평범한 리스트를 쓴다.
        """
        self.d = d
        self.log = log or (lambda m, red=False: None)
        self.mode = mode if mode is not None else ["fill"]
        self.main = None
        self.known = set()
        self.popup = None
        self.rebase()

    def _all(self):
        try:
            return set(self.d.window_handles)
        except Exception:
            return set()

    def _cur(self):
        try:
            return self.d.current_window_handle
        except Exception:
            return None

    def _switch(self, h):
        """열려 있는 창일 때만 전환. 닫힌 핸들로 전환하면 예외가 난다."""
        if not h or h not in self._all():
            return False
        return self._switch_raw(h)

    def _switch_raw(self, h):
        """존재 확인 없이 바로 전환. 감시 루프용 — window_handles 왕복 한 번을
        아끼려는 것이고, 닫힌 핸들이면 어차피 예외로 걸러진다."""
        if not h:
            return False
        try:
            self.d.switch_to.window(h)
            return True
        except Exception:
            return False

    def rebase(self):
        """지금을 기준선으로. 이 뒤에 뜨는 창만 '새 창'이 된다."""
        self.main = self._cur() or self.main
        self.known = self._all()

    def _new_handles(self):
        """아직 정체를 모르는 새 핸들들. window_handles 왕복 1회."""
        now = self._all()
        if not now:
            return ()
        self.known &= now            # 닫힌 창 정리
        return tuple(sorted(now - self.known))

    def _scan(self, h, act):
        """새 창 하나를 검사. 전환 1왕복 + POPUP_JS 1왕복이 전부다.

        act=True 면 팝업일 경우 그 자리에서 mode 가 허용하는 데까지 처리한다.
        판정 결과 dict, 전환 자체가 실패하면 None.
        """
        if not self._switch_raw(h):
            return None
        mode = self.mode[0]
        try:
            r = self.d.execute_script(
                POPUP_JS, list(POPUP_MARKS), POPUP_INPUT,
                mode == "auto", bool(act) and mode != "watch")
        except Exception:
            return None
        if not isinstance(r, dict):
            return None
        if r.get("popup"):
            self.popup = h
            self.known.add(h)
        elif r.get("url") and r["url"] != "about:blank":
            self.known.add(h)        # 사용자가 연 다른 창 — 확정됐을 때만 제외
        return r

    def poll(self):
        """새로 뜬 창 중 확인코드 팝업이 있으면 그 핸들(전환된 상태), 없으면 None.
        입력은 하지 않는다.

        아직 URL 이 안 잡힌 창(about:blank)은 known 에 넣지 않는다. window.open
        직후를 밟으면 남의 창으로 오인해 영영 다시 안 보게 되기 때문이다.
        """
        new = self._new_handles()
        for h in new:
            r = self._scan(h, act=False)
            if r and r.get("popup"):
                return h
        if new:
            self._switch_raw(self.main)   # 남의 창을 봤을 때만 되돌린다
        return None

    def focus_input(self):
        """팝업의 확인코드 입력칸에 커서를 놓는다. 팝업이 없으면 False."""
        if not self._switch(self.popup):
            return False
        try:
            return bool(self.d.execute_script(
                "var e=document.getElementById(arguments[0]);"
                "if(!e) return false;"
                "e.focus(); if(e.select) e.select(); return true;", POPUP_INPUT))
        except Exception:
            return False

    def handle_random_popup(self):
        """새로 뜬 창만 검사. 보안 확인코드 팝업이면 코드 역순을 자동 입력한다.
        평소(새 창 없음)엔 창 전환을 하지 않아 포커스를 뺏지 않는다.

        어디까지 갈지는 mode 가 정한다. 반환 —
          None        팝업 없음
          "watched"   mode="watch" — 감지만 하고 손대지 않음
          "nocode"    코드나 입력칸을 못 잡음 (사람이 처리)
          "filled"    역순 입력까지, goCompare 직전 정지 (포커스는 팝업)
          "confirmed" goCompare 호출까지 = 저장 확정

        새 창이 없으면 window_handles 한 번으로 끝나고, 팝업이면 전환 1왕복 +
        POPUP_JS 1왕복으로 입력까지 끝난다.
        """
        new = self._new_handles()
        if not new:
            return None
        hit = None
        for h in new:
            r = self._scan(h, act=True)
            if r and r.get("popup"):
                hit = r
                break
        if hit is None:
            self._switch_raw(self.main)   # 남의 창만 떴다 — 포커스 되돌리고 빠진다
            return None

        if self.mode[0] == "watch":
            self.log("🔐 확인코드 팝업 감지 — '감지만' 모드다. 직접 입력하고 [확인] 눌러라", True)
            return "watched"

        code = hit.get("code") or ""
        if not code:
            self.log("🔐 확인코드 팝업 감지 — 코드를 못 읽었다. 화면 코드를 뒤집어 직접 입력해라", True)
            return "nocode"
        if not hit.get("filled"):
            self.log("🔐 확인코드 '%s' — 입력칸(%s)을 못 잡았다. 역순 '%s' 직접 입력해라"
                     % (code, POPUP_INPUT, code[::-1]), True)
            return "nocode"
        self.log("🔐 확인코드 '%s' 감지 → 역순 '%s' 자동 입력"
                 % (code, hit.get("rev") or code[::-1]))
        if hit.get("confirmed"):
            self.log("   goCompare() 호출 → 저장 진행됨", True)
            self.restore()   # 팝업은 닫힌다 — 메인으로 복귀
            return "confirmed"
        if self.mode[0] == "auto":
            self.log("   ⚠ goCompare() 호출이 실패했다 — [확인]을 직접 눌러라", True)
            return "filled"
        self.log("   역순 입력 완료 — goCompare 직전 정지. [확인]을 직접 눌러라(저장)")
        return "filled"      # 포커스는 팝업에 남긴다

    def alive(self):
        """마지막에 잡은 팝업이 아직 열려 있나."""
        return bool(self.popup) and self.popup in self._all()

    def restore(self):
        """메인(신청관리) 창으로 포커스 복귀."""
        return self._switch(self.main)


    


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CarMacro v2 — 서버시계 · 신청현황 · 발사")
        self.geometry("880x720")
        self.q = queue.Queue()
        self.clock = ServerClock(self.q)
        self.clock.start()
        self.d = None
        self.info = None
        self.armed = False
        self.fire_thread = None
        self.watch = None
        self.popup_mode = ["fill"]        # 팝업 정지 지점 — 작업 스레드가 읽는다
        self.entry_mode = ["at_target"]   # 정각에 무엇을 누를지 — 작업 스레드가 읽는다
        self.keep_job = None              # 다음 주기 after() 핸들
        self.keep_gap_job = None          # 새로고침 → 연장 사이 after() 핸들
        self._build()
        self.after(50, self._tick)
        self.after(200, self._pump)
        self.after(300, self._banner_tick)
        self.after(1000, self._preflight)

    def _build(self):
        # 최상단 상태 배너 — "지금 발사하면 어디까지 가나"를 한 줄로 못박는다.
        # 무장 여부와 팝업 모드를 곱해야 나오는 값이라 둘을 따로 보면 놓친다.
        self.v_banner = tk.StringVar(value="")
        self.lbl_banner = tk.Label(self, textvariable=self.v_banner,
                                   font=("맑은 고딕", 13, "bold"),
                                   fg="white", bg="#666", pady=7)
        self.lbl_banner.pack(fill="x", side="top")
        self._blink = False

        f1 = ttk.LabelFrame(self, text="  ev.or.kr 서버시계 (포트 %d 세션과 같은 서버)  " % PORT)
        f1.pack(fill="x", padx=10, pady=(10, 6))
        self.v_srv = tk.StringVar(value="--:--:--.---")
        tk.Label(f1, textvariable=self.v_srv, font=("Consolas", 34, "bold"),
                 fg="#0a3d62").pack(pady=(6, 0))
        self.v_meta = tk.StringVar(value="측정 중…")
        tk.Label(f1, textvariable=self.v_meta, font=("맑은 고딕", 9),
                 fg="#555").pack()
        r = ttk.Frame(f1)
        r.pack(pady=6)
        ttk.Label(r, text="목표(서버) ").pack(side="left")
        self.v_target = tk.StringVar(value="09:00:00")
        ttk.Entry(r, textvariable=self.v_target, width=20,
                  justify="center").pack(side="left")
        ttk.Button(r, text="접수기간에서 고르기",
                   command=self.open_periods).pack(side="left", padx=6)
        ttk.Label(r, text=" 리드(ms) ").pack(side="left")
        self.v_lead = tk.StringVar(value="50")
        ttk.Entry(r, textvariable=self.v_lead, width=6,
                  justify="center").pack(side="left")
        ttk.Label(r, text=" 선진입(s) ").pack(side="left")
        self.v_pre = tk.StringVar(value="30")
        ttk.Entry(r, textvariable=self.v_pre, width=5,
                  justify="center").pack(side="left")
        ttk.Button(r, text="시계 재측정",
                   command=self.clock.remeasure_now).pack(side="left", padx=8)

        # 입력한 문자열이 실제 몇 시로 환산됐는지 그대로 되비춘다.
        self.v_tdesc = tk.StringVar(value="")
        self.lbl_tdesc = tk.Label(f1, textvariable=self.v_tdesc,
                                  font=("Consolas", 9))
        self.lbl_tdesc.pack(pady=(0, 2))

        r1b = ttk.Frame(f1)
        r1b.pack(pady=(0, 2))
        ttk.Label(r1b, text="모드 ").pack(side="left")
        self.v_mode = tk.StringVar(value="safe")
        for val, lab in (("safe", "안전(늦게·튕김방지)"), ("mid", "중앙값"),
                         ("fast", "공격(빠르게)")):
            ttk.Radiobutton(r1b, text=lab, value=val, variable=self.v_mode,
                            command=self._apply_mode).pack(side="left", padx=3)
        self.v_risk = tk.StringVar(value="")
        self.lbl_risk = tk.Label(f1, textvariable=self.v_risk,
                                 font=("맑은 고딕", 9, "bold"))
        self.lbl_risk.pack()

        self.v_cd = tk.StringVar(value="")
        tk.Label(f1, textvariable=self.v_cd, font=("Consolas", 16),
                 fg="#b33").pack(pady=(0, 8))

        f2 = ttk.LabelFrame(self, text="  신청관리 현황 (열려 있는 창)  ")
        f2.pack(fill="both", expand=True, padx=10, pady=6)
        r2 = ttk.Frame(f2)
        r2.pack(fill="x", pady=4)
        self.btn_chrome = ttk.Button(r2, text="크롬 실행",
                                     command=self.open_chrome)
        self.btn_chrome.pack(side="left", padx=4)
        ttk.Button(r2, text="크롬 연결 / 새로고침",
                   command=self.refresh).pack(side="left", padx=4)
        self.v_sess = tk.StringVar(value="세션 --:--")
        ttk.Label(r2, textvariable=self.v_sess).pack(side="left", padx=10)
        ttk.Button(r2, text="세션 연장",
                   command=self.extend).pack(side="left")
        self.v_keep = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="%d분마다 자동 (새로고침 → %.0fs → 연장)"
                        % (KEEP_INTERVAL // 60, KEEP_GAP),
                        variable=self.v_keep,
                        command=self._apply_keep).pack(side="left", padx=12)
        self.v_keepnext = tk.StringVar(value="")
        ttk.Label(r2, textvariable=self.v_keepnext,
                  foreground="#080").pack(side="left")
        self.v_url = tk.StringVar(value="")
        ttk.Label(f2, textvariable=self.v_url, foreground="#666").pack(anchor="w", padx=6)

        self.counts = tk.Text(f2, height=4, wrap="word", state="disabled",
                              font=("맑은 고딕", 9))
        self.counts.pack(fill="x", padx=6, pady=4)

        ttk.Label(f2, text="제출전 목록 (발사 대상)").pack(anchor="w", padx=6)
        self.tree = ttk.Treeview(f2, columns=("seq", "desc"), show="headings", height=6)
        self.tree.heading("seq", text="신청번호")
        self.tree.heading("desc", text="내용")
        self.tree.column("seq", width=110, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)

        f3 = ttk.LabelFrame(self, text="  발사 — 지원신청 제출  ")
        f3.pack(fill="x", padx=10, pady=(6, 10))
        r3 = ttk.Frame(f3)
        r3.pack(fill="x", pady=4)
        ttk.Label(r3, text="대상 신청번호 ").pack(side="left")
        self.v_seq = tk.StringVar(value="")
        ttk.Entry(r3, textvariable=self.v_seq, width=14,
                  justify="center").pack(side="left")
        ttk.Button(r3, text="목록에서 가져오기",
                   command=self.pick).pack(side="left", padx=6)
        ttk.Button(r3, text="리허설 (조회 진입만)",
                   command=lambda: self.start_fire(False)).pack(side="left", padx=6)
        self.btn_arm = tk.Button(r3, text="무장 해제됨", bg="#ddd", width=14,
                                 command=self.toggle_arm)
        self.btn_arm.pack(side="left", padx=6)
        self.btn_fire = tk.Button(r3, text="예약 발사", bg="#eee", width=10,
                                  state="disabled",
                                  command=lambda: self.start_fire(True))
        self.btn_fire.pack(side="left")

        r3a = ttk.Frame(f3)
        r3a.pack(fill="x", pady=(0, 2))
        ttk.Label(r3a, text="정각(T)에 누를 것 ").pack(side="left")
        self.v_entry = tk.StringVar(value="at_target")
        for val, lab in (("at_target", "리스트에서 진입 (실제 접수와 같은 순서)"),
                         ("pre_enter", "선진입 후 지원신청만")):
            ttk.Radiobutton(r3a, text=lab, value=val, variable=self.v_entry,
                            command=self._apply_entry_mode).pack(side="left", padx=3)
        self.v_emode = tk.StringVar(value="")
        tk.Label(f3, textvariable=self.v_emode, font=("맑은 고딕", 9),
                 fg="#555").pack(anchor="w", padx=8)
        self._apply_entry_mode(quiet=True)

        r3b = ttk.Frame(f3)
        r3b.pack(fill="x", pady=(0, 2))
        ttk.Label(r3b, text="확인코드 팝업 — 어디서 멈출까 ").pack(side="left")
        self.v_popup = tk.StringVar(value="fill")
        for val, lab in (("watch", "감지만"),
                         ("fill", "역순 입력 후 goCompare 직전 정지"),
                         ("auto", "goCompare 까지 실행")):
            ttk.Radiobutton(r3b, text=lab, value=val, variable=self.v_popup,
                            command=self._apply_popup_mode).pack(side="left", padx=3)
        self.v_pmode = tk.StringVar(value="")
        self.lbl_pmode = tk.Label(f3, textvariable=self.v_pmode,
                                  font=("맑은 고딕", 9))
        self.lbl_pmode.pack(anchor="w", padx=8, pady=(0, 4))
        self._apply_popup_mode(quiet=True)

        self.v_pf = tk.StringVar(value="점검 대기…")
        self.lbl_pf = tk.Label(f3, textvariable=self.v_pf, font=("Consolas", 9),
                               justify="left", anchor="w")
        self.lbl_pf.pack(fill="x", padx=8, pady=(0, 6))

        self.log = tk.Text(self, height=9, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log.tag_config("red", foreground="#c00")
        self.log.tag_config("grn", foreground="#080")

    POPUP_DESC = {
        "watch": ("감지만 — 팝업을 띄워만 두고 입력칸도 안 건드린다. 코드 판독·입력 전부 직접.",
                  "#555"),
        "fill":  ("역순 입력 후 정지 — 코드를 뒤집어 넣고 goCompare 는 부르지 않는다. "
                  "[확인]을 눌러야 저장된다.", "#080"),
        "auto":  ("⚠ goCompare 까지 실행 — 사람 개입 없이 저장이 확정된다. "
                  "되돌리려면 사이트에서 취소해야 한다.", "#c00"),
    }

    ENTRY_DESC = {
        "at_target": "정각에 리스트에서 [지원신청조회]로 들어가고, 폼이 뜨는 즉시 지원신청. "
                     "페이지 이동(0.8~1.0s)이 임계경로에 들어온다.",
        "pre_enter": "선진입(s) 만큼 미리 폼에 들어가 두고 정각엔 지원신청만(38ms). "
                     "접수 시작 전 선진입이 서버에서 허용돼야 성립한다 — 미검증.",
    }

    def _apply_entry_mode(self, quiet=False):
        m = self.v_entry.get()
        self.entry_mode[0] = m
        self.v_emode.set(self.ENTRY_DESC[m])
        if not quiet:
            self._log("정각 동작: %s" % self.ENTRY_DESC[m])

    def _apply_popup_mode(self, quiet=False):
        m = self.v_popup.get()
        # 무장한 채로 auto 로 올리는 건 조용히 넘어가면 안 되는 승급이다.
        # 배너는 바뀌지만 라디오만 보고 있으면 못 본다.
        if not quiet and m == "auto" and self.armed:
            if not messagebox.askyesno(
                    "자동 확인 승급",
                    "이미 무장된 상태다.\n\n"
                    "auto 로 바꾸면 정각에 제출한 뒤 goCompare 까지 자동으로 눌러\n"
                    "사람 개입 없이 접수가 확정된다.\n\n"
                    "정말 바꿀까?", icon="warning"):
                self.v_popup.set(self.popup_mode[0])
                return
        self.popup_mode[0] = m
        desc, color = self.POPUP_DESC[m]
        self.v_pmode.set(desc)
        self.lbl_pmode.config(fg=color)
        if not quiet:
            self._log("팝업 정지 지점: %s" % desc, "red" if m == "auto" else None)

    def _mk_watch(self):
        """작업 스레드에서 나온 로그는 큐로 넘긴다(tk 위젯은 메인 스레드 전용)."""
        return PopupWatch(self.d,
                          log=lambda m, red=False: self.q.put(("f", m)),
                          mode=self.popup_mode)

    def _log(self, m, tag=None):
        ts = datetime.now(KST).strftime("%H:%M:%S")
        self.log.insert("end", "[%s] %s\n" % (ts, m), tag or ())
        self.log.see("end")

    def _pump(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                if kind == "btn":
                    self.btn_chrome.config(state="normal")
                    continue
                self._log(msg, {"clock": "grn", "chrome": "grn",
                                "err": "red"}.get(kind))
        except queue.Empty:
            pass
        self.after(200, self._pump)

    def _tick(self):
        n = self.clock.now()
        if n is None:
            self.v_srv.set("--:--:--.---")
        else:
            self.v_srv.set(datetime.fromtimestamp(n, KST).strftime("%H:%M:%S.%f")[:-3])
            age = time.time() - (self.clock.at or time.time())
            self.v_meta.set(
                "offset %+.3fs 구간[%+.3f, %+.3f] 폭%.0fms · 편도~%.0fms · %ds 전 측정%s"
                % (self.clock.offset, self.clock.lo, self.clock.hi,
                   (self.clock.hi - self.clock.lo) * 1000,
                   self.clock.rtt * 500, age,
                   "  ⚠시계 튐 감지" if self.clock.jumped else ""))
            t = self._target_epoch()
            if t:
                self.v_cd.set("목표까지  %s" % self._fmt_delta(t - n))
            self._update_risk()
        self._update_target_desc()
        self.after(50, self._tick)

    def _apply_mode(self):
        self.clock.mode = self.v_mode.get()
        self._log("시계 모드: %s" % self.v_mode.get())

    def _update_risk(self):
        try:
            lead = float(self.v_lead.get()) / 1000.0
        except Exception:
            return
        m = self.clock.early_risk(lead)
        if m is None:
            return
        if m >= 0:
            self.v_risk.set("✅ 일찍 도착 불가 — 최소 여유 %.0fms (튕길 일 없음)" % (m * 1000))
            self.lbl_risk.config(fg="#080")
        else:
            self.v_risk.set("⚠ 최대 %.0fms 일찍 도착 가능 — '접수 시작 전'으로 튕길 수 있다"
                            % (-m * 1000))
            self.lbl_risk.config(fg="#c00")

    # ── 상태 배너 ───────────────────────────────────────────────
    # 위험도는 (무장 여부 × 팝업 모드) 곱이다. 라디오 하나만 보고 있으면
    # "무장은 했는데 auto 인 줄 몰랐다"가 나온다. 그래서 곱해서 한 줄로 박는다.

    def _banner_tick(self):
        auto = self.popup_mode[0] == "auto"
        if not self.armed:
            txt = "🟢 안전 — 무장 해제됨. [예약 발사] 잠김. 리허설만 나간다."
            bg = "#4a7a5a"
        elif auto:
            self._blink = not self._blink
            txt = ("🔴 실발사 + 자동 확인 — 정각에 제출하고 goCompare 까지 눌러 "
                   "접수가 확정된다. 사람이 멈출 지점이 없다.")
            bg = "#cc0000" if self._blink else "#7a0000"
        else:
            stop = "감지만 한다" if self.popup_mode[0] == "watch" else "역순 입력 후 멈춘다"
            txt = ("🟠 실발사 무장됨 — 정각에 제출한다. 확인코드 팝업은 %s. "
                   "[확인]을 눌러야 저장된다." % stop)
            bg = "#c47f17"

        if self.armed:
            t, n = self._target_epoch(), self.clock.now()
            if t and n:
                txt += "   목표까지 %s" % self._fmt_delta(t - n)
        self.v_banner.set(txt)
        self.lbl_banner.config(bg=bg)
        self.after(500, self._banner_tick)

    # ── 발사 전 검증 루프 ───────────────────────────────────────

    @staticmethod
    def _sess_secs(s):
        """'54:47' → 3287. 못 읽으면 None."""
        m = re.match(r"^\s*(\d+):(\d{2})\s*$", s or "")
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None

    def _preflight(self):
        # 재예약을 먼저 건다 — 아래에서 뭐가 터져도 루프는 살아 있어야 한다.
        self.after(int(PREFLIGHT_EVERY * 1000), self._preflight)

        # 발사 시퀀스 중엔 드라이버를 절대 건드리지 않는다. 점검 왕복 하나가
        # 정각 임계경로에 끼는 것보다 점검을 쉬는 편이 낫다.
        if self.fire_thread and self.fire_thread.is_alive():
            self.v_pf.set("점검 중지 — 발사 시퀀스 진행 중")
            self.lbl_pf.config(fg="#555")
            return

        marks, bad, warn = [], 0, 0

        def add(ok, good, why):
            nonlocal bad, warn
            if ok is True:
                marks.append("✔" + good)
            elif ok is None:
                marks.append("△" + why)
                warn += 1
            else:
                marks.append("✖" + why)
                bad += 1

        add(self.d is not None, "연결", "연결없음")

        if self.d is None:
            add(False, "", "리스트?")
        else:
            try:
                fn = self.d.execute_script("return typeof app_accept")
                add(fn == "function", "리스트", "리스트아님")
            except Exception:
                add(False, "", "드라이버死")

        c = self.clock
        if c.offset is None:
            add(False, "", "시계미측정")
        else:
            age = time.time() - (c.at or 0)
            width = (c.hi - c.lo) * 1000
            add(None if age > CLOCK_STALE else True,
                "시계(%ds전·폭%.0fms)" % (age, width),
                "시계낡음(%dm전)" % (age // 60))

        seq = self.v_seq.get().strip()
        rows = (self.info or {}).get("rows", [])
        if not seq:
            add(False, "", "대상없음")
        elif rows and not any(r["seq"] == seq for r in rows):
            add(None, "", "대상 %s 목록에없음" % seq)
        else:
            add(True, "대상 %s" % seq, "")

        t, n = self._target_epoch(), c.now()
        if t is None or n is None:
            add(False, "", "목표형식")
        else:
            left = t - n
            add(left > 0, "목표 T-%s" % self._fmt_delta(left), "목표지남")
            sl = self._sess_secs((self.info or {}).get("session"))
            if sl is None:
                add(None, "", "세션?")
            else:
                add(None if sl < left else True,
                    "세션 %dm" % (sl // 60),
                    "세션 %dm < 목표까지 %dm" % (sl // 60, left // 60))

        self.v_pf.set("점검  " + "  ".join(marks))
        self.lbl_pf.config(fg="#c00" if bad else ("#c47f17" if warn else "#080"))

    @staticmethod
    def _fmt_delta(s):
        sign = "-" if s < 0 else ""
        s = abs(s)
        day, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, sec = divmod(r, 60)
        pre = "%dd " % day if day else ""
        return "%s%s%02d:%02d:%06.3f" % (sign, pre, h, m, sec)

    def _target_epoch(self):
        """목표 '서버시각' epoch.

        'YYYY-MM-DD HH:MM[:SS]' → 그 날짜 그대로.
        'HH:MM[:SS]'            → 오늘 기준, 이미 지났으면 내일.
        """
        r = self._resolve_target()
        return r["epoch"] if r["ok"] else None

    WEEKDAY = "월화수목금토일"

    def _resolve_target(self):
        """목표 입력을 실제 서버시각으로 환산하고 **어떻게 읽었는지**까지 돌려준다.

        날짜 없는 'HH:MM' 은 이미 지났으면 내일로 넘어간다 — 이게 조용히 일어나면
        오늘 10시에 쏠 생각이었는데 내일로 잡히는 사고가 난다. rolled 로 드러낸다.
        """
        s = self.v_target.get().strip()
        n = self.clock.now()
        if n is None:
            return {"ok": False, "epoch": None, "src": s,
                    "why": "시계 미측정 — 목표를 환산할 기준이 없다"}
        now_dt = datetime.fromtimestamp(n, KST)
        for fmt, dated in (("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d %H:%M", True),
                           ("%H:%M:%S", False), ("%H:%M", False)):
            try:
                t = datetime.strptime(s, fmt)
            except ValueError:
                continue
            if dated:
                dt = t.replace(tzinfo=KST)
                rolled = False
            else:
                dt = now_dt.replace(hour=t.hour, minute=t.minute,
                                    second=t.second, microsecond=0)
                rolled = dt.timestamp() < n
                if rolled:
                    dt += timedelta(days=1)
            return {"ok": True, "epoch": dt.timestamp(), "dt": dt,
                    "rolled": rolled, "fmt": fmt, "dated": dated, "src": s}
        return {"ok": False, "epoch": None, "src": s,
                "why": "형식을 못 읽는다 — HH:MM · HH:MM:SS · YYYY-MM-DD HH:MM[:SS]"}

    def _update_target_desc(self):
        r = self._resolve_target()
        if not r["ok"]:
            self.v_tdesc.set("목표 '%s' → ✖ %s" % (r["src"], r["why"]))
            self.lbl_tdesc.config(fg="#c00")
            return
        dt, n = r["dt"], self.clock.now()
        today = datetime.fromtimestamp(n, KST).date()
        day = ("오늘" if dt.date() == today else
               "내일" if (dt.date() - today).days == 1 else
               "%+d일" % (dt.date() - today).days)
        try:
            lead = float(self.v_lead.get()) / 1000.0
        except Exception:
            lead = 0.05
        fire = dt - timedelta(seconds=lead)
        txt = ("목표 '%s' → %s(%s) %s %s KST   ·   남은 %s   ·   "
               "리드 %.0fms 빼면 실제 발사 %s"
               % (r["src"], day, self.WEEKDAY[dt.weekday()],
                  dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S.%f")[:-3],
                  self._fmt_delta(r["epoch"] - n), lead * 1000,
                  fire.strftime("%H:%M:%S.%f")[:-3]))
        if r["rolled"]:
            txt = "⚠ 오늘 %s 은 이미 지났다 → 내일로 넘어갔다.  " % r["src"] + txt
            self.lbl_tdesc.config(fg="#c00")
        else:
            self.lbl_tdesc.config(fg="#080" if r["epoch"] > n else "#c00")
        self.v_tdesc.set(txt)

    def open_periods(self):
        if self.d is None:
            self._log("먼저 [크롬 연결 / 새로고침]", "red")
            return
        self._log("접수기간 조회 중… (새 탭에서 읽고 닫는다)")
        threading.Thread(target=self._periods_worker, daemon=True).start()

    def _periods_worker(self):
        n = self.clock.now() or time.time()
        d0 = datetime.fromtimestamp(n, KST)
        try:
            rows = fetch_periods(self.d, d0.strftime("%Y-%m-%d"),
                                 (d0 + timedelta(days=90)).strftime("%Y-%m-%d"))
        except Exception as e:
            self.q.put(("p", "접수기간 조회 실패: %r" % e))
            return
        self.q.put(("p", "접수기간 %d건 읽음" % len(rows)))
        self.after(0, lambda: PeriodDialog(self, rows))

    def open_chrome(self):
        """전용 프로필 크롬을 띄우고 포트가 뜰 때까지 기다린다(작업 스레드)."""
        self.btn_chrome.config(state="disabled")
        threading.Thread(target=self._chrome_worker, daemon=True).start()

    def _chrome_worker(self):
        try:
            v = port_version()
            if v:
                self.q.put(("chrome", "%d 포트가 이미 열려 있다 — 그대로 쓴다 (%s)" % (PORT, v)))
                return
            exe, prof = launch_chrome()
            self.q.put(("f", "크롬 실행: %s" % exe))
            self.q.put(("f", "프로필: %s" % prof))
            for _ in range(40):
                v = port_version()
                if v:
                    self.q.put(("chrome",
                                "크롬 준비 완료 (%s) — 로그인 후 [크롬 연결 / 새로고침]" % v))
                    return
                time.sleep(0.25)
            self.q.put(("err", "10초 안에 %d 포트가 열리지 않았다. 크롬 창을 확인해라" % PORT))
        except Exception as e:
            self.q.put(("err", "크롬 실행 실패: %s" % e))
        finally:
            self.q.put(("btn", ""))

    def refresh(self):
        try:
            if self.d is None:
                self.d = attach()
            if not ev_tab(self.d):
                self._log("ev.or.kr 탭을 못 찾았다", "red")
                return
            self.watch = self._mk_watch()
            self.info = read_page(self.d)
        except Exception as e:
            self.d = None
            self._log("크롬 연결 실패: %r" % e, "red")
            return

        self.v_url.set(self.info["url"])
        self.v_sess.set("세션 %s" % (self.info["session"] or "--:--"))
        self.counts.config(state="normal")
        self.counts.delete("1.0", "end")
        self.counts.insert("end", "  ".join(
            "%s %s건" % (k, v) for k, v in self.info["counts"].items()) or "(단계 버튼 없음)")
        self.counts.config(state="disabled")

        self.tree.delete(*self.tree.get_children())
        for row in self.info["rows"]:
            self.tree.insert("", "end", values=(row["seq"], row["desc"]))
        self._log("현황 갱신: %d행, 세션 %s" % (len(self.info["rows"]), self.info["session"]))

    def extend(self):
        if self.d is None:
            self._log("세션 연장 실패: 크롬에 연결돼 있지 않다", "red")
            return
        try:
            self.d.execute_script("getSessionCheck();")
            self._log("세션 연장 요청")
        except Exception as e:
            self._log("세션 연장 실패: %r" % e, "red")

    # ── 자동 유지 ───────────────────────────────────────────────
    # 새로고침은 탭을 전환하고(ev_tab) 버튼·행마다 드라이버를 왕복해서
    # 목록이 길수록 오래 걸린다. 그래서 연장은 refresh() 가 **반환한 뒤**
    # KEEP_GAP 초를 세고 쏜다 — 걸린 시간과 무관하게 간격이 보장된다.
    # 대기는 전부 after() 다. time.sleep 을 쓰면 시계 표시가 멈춘다.

    def _apply_keep(self):
        if self.v_keep.get():
            self._log("자동 유지 켬 — %d분마다 새로고침 → %.0fs 후 연장"
                      % (KEEP_INTERVAL // 60, KEEP_GAP))
            self._keep_schedule(KEEP_INTERVAL)
        else:
            self._keep_cancel()
            self.v_keepnext.set("")
            self._log("자동 유지 끔")

    def _keep_cancel(self):
        for job in (self.keep_job, self.keep_gap_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self.keep_job = self.keep_gap_job = None

    def _keep_schedule(self, secs):
        if self.keep_job:
            try:
                self.after_cancel(self.keep_job)
            except Exception:
                pass
        self.keep_job = self.after(int(secs * 1000), self._keep_run)
        nxt = datetime.now(KST) + timedelta(seconds=secs)
        self.v_keepnext.set("· 다음 %s" % nxt.strftime("%H:%M:%S"))

    def _keep_run(self):
        self.keep_job = None
        if not self.v_keep.get():
            return
        # 발사 대기 중엔 절대 건드리지 않는다. refresh() 는 탭을 전환하고
        # PopupWatch 를 새로 만든다 — 정각 임계경로에 끼면 발사가 통째로 어긋난다.
        if self.fire_thread and self.fire_thread.is_alive():
            self._log("자동 유지 건너뜀 — 발사 대기 중. %ds 후 다시 본다" % KEEP_RETRY)
            self._keep_schedule(KEEP_RETRY)
            return
        if self.watch and self.watch.alive():
            self._log("자동 유지 건너뜀 — 확인코드 팝업이 열려 있다. %ds 후 다시 본다"
                      % KEEP_RETRY)
            self._keep_schedule(KEEP_RETRY)
            return

        t0 = time.time()
        self.refresh()
        self._log("자동: 새로고침 %.1fs 소요 → %.0fs 후 연장"
                  % (time.time() - t0, KEEP_GAP))
        if self.keep_gap_job:
            try:
                self.after_cancel(self.keep_gap_job)
            except Exception:
                pass
        self.keep_gap_job = self.after(int(KEEP_GAP * 1000), self._keep_extend)
        self._keep_schedule(KEEP_INTERVAL)

    def _keep_extend(self):
        self.keep_gap_job = None
        if not self.v_keep.get():
            return
        self._log("자동: 세션 연장")
        self.extend()

    def pick(self):
        sel = self.tree.selection()
        if not sel:
            self._log("목록에서 행을 먼저 선택해라", "red")
            return
        self.v_seq.set(self.tree.item(sel[0], "values")[0])

    def toggle_arm(self):
        if self.armed:
            self.armed = False
            self.btn_arm.config(text="무장 해제됨", bg="#ddd")
            self.btn_fire.config(state="disabled", bg="#eee")
            self._log("무장 해제")
            return
        seq = self.v_seq.get().strip()
        if not seq:
            messagebox.showwarning("무장", "대상 신청번호가 비어 있다.")
            return
        row = next((r for r in (self.info or {}).get("rows", []) if r["seq"] == seq), None)
        desc = row["desc"] if row else "(목록에 없음 — 직접 입력한 번호)"
        auto = self.popup_mode[0] == "auto"
        if not messagebox.askyesno(
                "무장 확인",
                "아래 신청건을 실제로 제출한다.\n\n"
                "신청번호 : %s\n%s\n\n"
                "목표(서버) %s · 리드 %sms\n"
                "확인코드 팝업 : %s\n\n"
                "%s\n\n"
                "되돌리려면 사이트에서 취소해야 한다. 무장할까?"
                % (seq, desc, self.v_target.get(), self.v_lead.get(),
                   {"watch": "감지만", "fill": "역순 입력 후 정지",
                    "auto": "⚠ goCompare 까지 실행"}[self.popup_mode[0]],
                   "⚠⚠ 자동 확인이 켜져 있다 — 사람 개입 없이 접수가 확정된다."
                   if auto else "확인코드 팝업의 [확인]은 네가 눌러야 저장된다."),
                icon="warning" if auto else "question"):
            return
        self.armed = True
        self.btn_arm.config(text="⚠ 무장됨", bg="#f2c1c1")
        self.btn_fire.config(state="normal", bg="#f8d7da")
        self._log("무장: 신청번호 %s" % seq, "red")

    def start_fire(self, real):
        if real and not self.armed:
            messagebox.showwarning("발사", "무장 먼저.")
            return
        seq = self.v_seq.get().strip()
        if not seq:
            self._log("대상 신청번호가 비어 있다", "red")
            return
        if self.fire_thread and self.fire_thread.is_alive():
            self._log("이미 대기 중이다", "red")
            return
        t = self._target_epoch()
        if t is None:
            self._log("시계 측정 전이거나 목표 형식이 잘못됐다", "red")
            return
        try:
            lead = float(self.v_lead.get()) / 1000.0
        except Exception:
            lead = 0.05
        try:
            pre = max(3.0, float(self.v_pre.get()))
        except Exception:
            pre = 30.0
        n = self.clock.now()
        if self.entry_mode[0] == "pre_enter" and t - n < pre:
            pre = max(2.0, t - n - 1.0)
            self._log("목표가 가까워 선진입을 T-%.0fs 로 당겼다" % pre)
        step = self._step_for(seq)
        self.fire_thread = threading.Thread(
            target=self._fire_worker, args=(seq, step, t, lead, real, pre), daemon=True)
        self.fire_thread.start()

    def _step_for(self, seq):
        """목록 버튼이 실제로 넘기던 app_accept 두 번째 인자.

        read_page() 가 onclick 에서 뽑아 rows 에 넣어둔 값이다. 예전엔 이걸
        버리고 '100' 을 하드코딩했는데, 행마다 값이 다르면 버튼과 다른 단계로
        들어간다 — 버튼을 눌렀을 때와 결과가 갈리는 지점이었다.
        """
        for r in (self.info or {}).get("rows", []):
            if r["seq"] == seq:
                if r.get("step") and r["step"] != "100":
                    self._log("app_accept 2번째 인자 %s (목록 버튼에서 읽음)" % r["step"])
                return r.get("step") or "100"
        self._log("목록에 %s 가 없다 — app_accept 인자를 100 으로 가정한다" % seq, "red")
        return "100"

    def _wait_until(self, server_epoch):
        """서버시각 server_epoch 까지 대기. 마지막 1초는 5ms 간격으로 조인다."""
        while True:
            n = self.clock.now()
            if n is None:
                time.sleep(0.2)
                continue
            remain = server_epoch - n
            if remain <= 0:
                return
            time.sleep(0.005 if remain < 1 else
                       (0.05 if remain < 3 else min(remain - 1, 5)))

    def _drain_alerts(self):
        """삼켜둔 사이트 alert 를 꺼내 비운다. 팝업이 안 뜨는 이유가 대개 여기 있다."""
        try:
            return self.d.execute_script(
                "var a=window.__alerts||[]; window.__alerts=[]; return a;") or []
        except Exception:
            return []

    def _restore_dialogs(self):
        """confirm/alert 원복. 팝업 창에 가 있을 수 있으니 메인으로 돌아가서 하고,
        수동 확인 대기 중이면 포커스를 다시 팝업에 돌려준다."""
        keep = self.watch.popup if (self.watch and self.watch.alive()) else None
        try:
            if self.watch:
                self.watch.restore()
            self.d.execute_script(
                "if(window.__oc) window.confirm=window.__oc;"
                "if(window.__oa) window.alert=window.__oa;")
        except Exception:
            pass
        if keep:
            try:
                self.d.switch_to.window(keep)
            except Exception:
                pass

    def _srv_str(self):
        n = self.clock.now()
        return datetime.fromtimestamp(n, KST).strftime("%H:%M:%S.%f")[:-3] if n else "?"

    def _ensure_stage(self, seq, step, budget):
        """대상 행의 단계 탭을 띄워 그 행이 실제로 렌더되게 한다.

        app_accept 가 정의돼 있다고 목록이 준비된 게 아니다 — 새로 받은 목록은
        기본 탭이라 대상 행이 없다. 사람이 손으로 단계 탭을 눌러 확인하고
        들어가던 그 단계를 재시도에서도 똑같이 밟는다.

        어느 탭인지는 행이 들고 있는 app_step 으로 정한다. 라벨로 찾으면
        그 행이 '제출전'이 아닌 단계(예: 지원신청 101)에 있을 때 못 찾는다.
        """
        end = time.time() + max(2.0, budget)
        tab = None
        while time.time() < end:
            try:
                st = self.d.execute_script(STAGE_JS, seq, step, STAGE_MARK)
            except Exception:
                time.sleep(0.1)
                continue
            if st == "row":
                if tab:
                    self.q.put(("f", "   [%s] 눌러 대상 행 %s 확인" % (tab, seq)))
                return True
            if isinstance(st, str) and st.startswith("clicked:"):
                tab = st.split(":", 1)[1]      # 조회가 돌아올 때까지 기다린다
            elif st == "nostage":
                self.q.put(("f", "   ⚠ 단계 %s 탭을 못 찾았다" % step))
                return False
            elif st == "nolist":
                return False
            time.sleep(0.1)
        self.q.put(("f", "   ⚠ 대상 행 %s (단계 %s) 이 목록에 안 뜬다 (%.0fs)"
                    % (seq, step, budget)))
        return False

    def _back_to_list(self, seq, step, budget):
        """죽은 페이지에서 목록으로 복귀. app_accept 는 목록에만 정의돼 있어서
        (EV_apply.md §3) 재진입하려면 반드시 여기를 먼저 밟아야 한다.

        뒤로가기를 먼저 쓴다 — bfcache 를 타면 재요청보다 훨씬 싸고, 부하 중엔
        그 차이가 크다. 실패하면 리스트를 새로 받는다.
        """
        try:
            self.d.set_page_load_timeout(max(2.0, budget))
        except Exception:
            pass
        try:
            for how, act in (("뒤로가기", self.d.back),
                             ("리스트 재요청", lambda: self.d.get(LIST_URL))):
                try:
                    act()
                except Exception as e:
                    self.q.put(("f", "   복귀(%s) 실패: %s" % (how, type(e).__name__)))
                try:
                    if self.d.execute_script("return typeof app_accept") != "function":
                        continue
                except Exception:
                    continue
                # 목록에 선 것만으로는 부족하다 — 대상 행이 보여야 진짜 복귀다.
                if self._ensure_stage(seq, step, max(2.0, budget * 0.5)):
                    self.q.put(("f", "   복귀 성공(%s)" % how))
                    return True
            return False
        finally:
            try:
                self.d.set_page_load_timeout(300)
            except Exception:
                pass

    def _enter_form(self, seq, step, critical):
        """리스트에서 [지원신청조회] = app_accept 로 신청서 폼에 들어간다.

        critical=True 면 정각 임계경로 위다 — 폼 로드 확인을 FORM_POLL 로 조인다.
        (선진입일 땐 어차피 여유가 있으니 왕복을 아껴 느슨하게 본다.)

        부하로 서버가 연결을 끊으면(ERR_EMPTY_RESPONSE 관측) 크롬 오류 페이지가
        뜨는데, 예전엔 그 위에서 goApply 를 FORM_WAIT 내내 기다리다 그 회차를
        통째로 날렸다. 이제 사망을 판정해 목록으로 되돌아가 다시 들어간다.
        재시도는 **진입에만** 건다 — goApply 재시도는 중복 신청이 된다.

        반환: (성공?, 소요초)
        """
        t0 = time.time()
        end = t0 + FORM_WAIT
        step = FORM_POLL if critical else 0.1

        for attempt in range(1, ENTER_TRIES + 1):
            if attempt > 1:
                left = end - time.time()
                if left <= 1.0:
                    break
                self.q.put(("f", "   진입 %d회차 — 목록으로 복귀 (남은 %.0fs)"
                            % (attempt, left)))
                time.sleep(RETRY_GAP)
                if not self._back_to_list(seq, step, min(BACK_BUDGET, left * 0.5)):
                    self.q.put(("f", "   목록 복귀 실패 — 화면을 직접 봐라"))
                    break

            try:
                self.d.execute_script(
                    "app_accept(arguments[0], arguments[1]);", seq, step)
            except Exception as e:
                self.q.put(("f", "조회 진입 실패(%d회차): %r" % (attempt, e)))
                continue

            next_dead = time.time() + DEAD_CHECK
            while time.time() < end:
                try:
                    if self.d.execute_script("return typeof goApply") == "function":
                        if attempt > 1:
                            self.q.put(("f", "   %d회차에서 진입 성공" % attempt))
                        return True, time.time() - t0
                except Exception:
                    pass                      # 페이지 교체 중이면 튄다 — 정상

                now = time.time()
                if now >= next_dead:
                    next_dead = now + DEAD_CHECK
                    try:
                        st = self.d.execute_script(DEAD_JS)
                    except Exception:
                        st = None
                    if st in ("dead", "blank"):
                        self.q.put(("f", "   ⚠ 페이지가 죽었다(%s) — %.1fs 만에 감지"
                                    % (st, now - t0)))
                        break                 # 바깥 for 로 → 복귀 후 재진입
                if step < 0.1 and now - t0 > FORM_RELAX:
                    step = 0.1
                time.sleep(step)
            else:
                break                         # 시간 예산 소진

        self.q.put(("f", "폼 로드 확인 실패 (%.1fs, %d회 시도) — 화면을 직접 봐라"
                    % (time.time() - t0, attempt)))
        return False, time.time() - t0

    def _fire_worker(self, seq, step, target_srv, lead, real, pre):
        """발사. 정각에 무엇을 누르느냐가 entry_mode 로 갈린다.

        at_target : 정각에 **리스트에서 진입**(app_accept) → 폼 뜨자마자 지원신청.
                    실제 접수는 리스트에서 들어가는 순간이 경쟁 지점이라 이게 기본이다.
                    페이지 이동(실측 0.8~1.0s)이 임계경로에 들어온다.
        pre_enter : 목표 pre 초 전에 미리 폼에 들어가 두고 정각엔 goApply(실측 38ms)만.
                    빠르지만, 접수 시작 전 선진입이 서버에서 허용돼야 성립한다.
        """
        mode = "실발사" if real else "리허설"
        at_target = self.entry_mode[0] == "at_target"
        self.q.put(("f", "%s 대기 — 신청번호 %s · app_accept(%s,%s) · %s · 리드 %.0fms"
                    % (mode, seq, seq, step,
                       "정각에 리스트에서 진입" if at_target else "선진입 T-%.0fs" % pre,
                       lead * 1000)))

        if at_target:
            # 정각 전에 리스트 페이지에 서 있는지 확인해 둔다(진입 자체는 정각에).
            # T 이전이라 왕복이 공짜다 — 대상 행이 실제로 보이는지까지 본다.
            try:
                ready = self.d.execute_script("return typeof app_accept") == "function"
            except Exception:
                ready = False
            if not ready:
                self.q.put(("f", "⚠ 리스트 페이지가 아니다 — [크롬 연결/새로고침] 후 다시 무장해라"))
                return
            if not self._ensure_stage(seq, step, 10.0):
                self.q.put(("f", "⚠ 목록에 %s(단계 %s) 행이 안 보인다 — "
                                 "해당 단계 탭을 눌러 확인해라" % (seq, step)))
                return
            self.q.put(("f", "리스트 대기 중 · 서버 %s · 진입까지 %.1fs"
                        % (self._srv_str(), target_srv - self.clock.now())))
            self._wait_until(target_srv - lead)
            if not real:
                self.q.put(("f", "리허설 종료 (서버 %s). 리스트에서 들어가지 않았다. "
                                 "실발사였다면 지금 app_accept 가 나갔다." % self._srv_str()))
                return
            t_enter = self._srv_str()
            ok, dt = self._enter_form(seq, step, critical=True)
            if not ok:
                return
            self.q.put(("f", "▶ 정각 진입 — 지시 %s · 폼 준비 %s (%.0fms)"
                        % (t_enter, self._srv_str(), dt * 1000)))
        else:
            self._wait_until(target_srv - pre)
            ok, dt = self._enter_form(seq, step, critical=False)
            if not ok:
                return
            n = self.clock.now()
            self.q.put(("f", "선진입 완료 (%.0fms 소요) · 서버 %s · 발사까지 %.1fs"
                        % (dt * 1000, self._srv_str(), target_srv - n)))
            if target_srv - n <= 0:
                self.q.put(("f", "⚠ 선진입에 시간을 다 썼다 — 이미 목표를 지났다"))
            self._wait_until(target_srv - lead)
            if not real:
                self.q.put(("f", "리허설 종료 (서버 %s). 지원신청은 누르지 않았다. "
                                 "실발사였다면 지금 goApply 가 나갔다." % self._srv_str()))
                return

        if self.watch is None:
            self.watch = self._mk_watch()
        self.watch.rebase()

        # confirm 은 항상 통과시키고 alert 은 삼켜서 모아둔다.
        # 복원을 여기서 하면 안 된다 — execApply 의 confirm() 은 finishChkJson 응답
        # **뒤에** 불리므로(goApply 반환 시점엔 아직 안 불렸다) 지금 되돌리면
        # 네이티브 창이 떠서 드라이버가 통째로 멈춘다. 팝업 처리까지 끝낸 뒤 복원한다.
        try:
            self.d.execute_script(
                "window.__oc=window.confirm; window.confirm=function(){return true;};"
                "window.__oa=window.alert; window.__alerts=[];"
                "window.alert=function(m){window.__alerts.push(String(m));};")
            issued = self._srv_str()
            self.d.execute_script("goApply('101', '지원신청서를 제출 하시겠습니까?');")
            self.q.put(("f", "★ 지원신청 발사 — 지시 %s · 반환 %s" % (issued, self._srv_str())))
        except Exception as e:
            self.q.put(("f", "발사 실패: %r" % e))
            self._restore_dialogs()
            return

        t_fire = time.time()
        deadline = t_fire + POPUP_WATCH
        while time.time() < deadline:
            r = self.watch.handle_random_popup()   # 상세 로그는 watch 가 큐로 넣는다
            if r is None:
                al = self._drain_alerts()
                if al:
                    # 팝업 대신 알림이 떴다 = 서버가 발사를 막았다. 10초 헛기다리지 않는다.
                    for m in al:
                        self.q.put(("f", "⚠ 사이트 알림: %s" % m))
                    self.q.put(("f", "→ 확인코드 창은 뜨지 않는다. 접수는 안 됐다."))
                    break
                time.sleep(POPUP_POLL)
                continue
            self.q.put(("f", "   팝업 대응 %.0fms (발사 → %s)"
                        % ((time.time() - t_fire) * 1000,
                           "저장 확정" if r == "confirmed" else "감지" if r == "watched"
                           else "입력 완료")))
            if r in ("filled", "watched", "nocode"):
                self.q.put(("f", "→ 팝업에 포커스를 뒀다. [확인]을 누르면 저장된다."))
            break
        else:
            self.q.put(("f", "확인코드 창은 뜨지 않았다(%.0fs)." % POPUP_WATCH))
            for m in self._drain_alerts():
                self.q.put(("f", "⚠ 사이트 알림: %s" % m))
            self.watch.restore()
        self._restore_dialogs()
        self.q.put(("f", "완료. 결과는 목록 새로고침으로 확인해라."))


class PeriodDialog(tk.Toplevel):
    """접수기간 목록에서 하나 골라 목표시각으로 넣는다."""

    def __init__(self, app, rows):
        super().__init__(app)
        self.app, self.rows = app, rows
        self.title("지자체 차종별 접수기간 — 목표시각 선택")
        self.geometry("980x520")

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="검색(지자체/차종/공고) ").pack(side="left")
        self.v_f = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.v_f, width=26)
        e.pack(side="left")
        e.bind("<KeyRelease>", lambda _: self.fill())
        self.v_future = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="앞으로 열리는 것만", variable=self.v_future,
                        command=self.fill).pack(side="left", padx=10)
        ttk.Button(top, text="이 시각을 목표로",
                   command=self.apply).pack(side="right")

        cols = ("start", "local", "car", "notice", "cnt", "end")
        self.tv = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in (("start", "접수시작일시", 140), ("local", "지자체", 120),
                        ("car", "차종", 90), ("notice", "공고", 70),
                        ("cnt", "공고대수", 80), ("end", "접수종료일시", 140)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="center")
        self.tv.pack(fill="both", expand=True, padx=8, pady=6)
        self.tv.bind("<Double-1>", lambda _: self.apply())
        self.fill()

    def _future_only(self):
        n = self.app.clock.now() or time.time()
        out = []
        for r in self.rows:
            try:
                t = datetime.strptime(r["start"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            except ValueError:
                continue
            if not self.v_future.get() or t.timestamp() > n:
                out.append(r)
        return out

    def fill(self):
        f = self.v_f.get().strip()
        self.tv.delete(*self.tv.get_children())
        for r in self._future_only():
            if f and f not in (r["local"] + r["car"] + r["notice"]):
                continue
            self.tv.insert("", "end", values=(r["start"], r["local"], r["car"],
                                              r["notice"], r["cnt"], r["end"]))

    def apply(self):
        sel = self.tv.selection()
        if not sel:
            return
        v = self.tv.item(sel[0], "values")
        self.app.v_target.set(v[0] + ":00")
        self.app._log("목표시각 설정: %s (%s %s %s)" % (v[0], v[1], v[2], v[3]))
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
