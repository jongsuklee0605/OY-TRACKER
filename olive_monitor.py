"""
올리브영 재고 / 랭킹 모니터 → 슬랙 알림
MODE=stock : 상품별 재고(옵션별 품절 + 남은 수량) 체크
MODE=rank  : 랭킹 페이지에서 상품 순위 체크

환경변수
  MODE               (필수) stock 또는 rank
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook 주소
  GOODS_NO           (필수) 상품번호, 쉼표로 여러 개
  RANK_URLS          (선택) 랭킹 페이지, "이름|URL" 을 쉼표로 여러 개. 안 넣으면 전체 랭킹만
  ALWAYS_NOTIFY      (선택) 1이면 변화 없어도 매번 상품별 요약 알림
  NOTIFY_NO_CHANGE   (선택) 1이면 '변경점 없음' 알림도 보냄 (기본 0 = 안 보냄)
  LOW_STOCK          (선택) 구매수량 칸에 이 숫자를 넣어 보고, 안 들어가면 재고 부족 알림 (기본 20)
  SUMMARY_FILE       (선택) 재고 모드에서 지정하면 '변경점 없음'은 보내지 않고 이 파일에 결과를 기록
                            (여러 묶음을 합쳐 한 번에 보내는 통합 job 용)
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

MODE = os.environ.get("MODE", "stock").strip().lower()
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
RAW_GOODS = os.environ.get("GOODS_NO", "")
GOODS = list(dict.fromkeys(g.strip() for g in RAW_GOODS.split(",") if g.strip()))
ALWAYS = os.environ.get("ALWAYS_NOTIFY") == "1"
NOTIFY_NO_CHANGE = os.environ.get("NOTIFY_NO_CHANGE", "0") == "1"
SUMMARY_FILE = os.environ.get("SUMMARY_FILE", "").strip()
LOW_STOCK = int(os.environ.get("LOW_STOCK", "20"))   # 이 수량이 안 들어가면 재고 부족

DEFAULT_RANK = "전체|https://www.oliveyoung.co.kr/store/main/getBestList.do"
MAIN_URL = "https://www.oliveyoung.co.kr/store/main/main.do"
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / f"olive_{MODE}_state.json"
STOCK_STATE_FILE = BASE_DIR / "olive_stock_state.json"   # 랭킹 모드에서 상품명 참고용
SLACK_CHUNK = 3000

NAV_TIMEOUT = 25000      # 페이지 하나를 여는 데 기다리는 최대 시간(ms)
TIME_LIMIT = 9 * 60      # 전체 실행 최대 시간(초)
MAX_CONSEC_FAIL = 4      # 연속 실패가 이만큼이면 접속 차단으로 보고 중단
RANK_RETRY = 3           # 랭킹 페이지 최대 시도 횟수
RETRY_429 = 2            # 429가 뜨면 같은 상품을 최대 몇 번 다시 시도할지
WAIT_429 = (20, 40)      # 429가 떴을 때 기다리는 시간 범위(초)
GAP = (1.0, 2.0)         # 평소 상품 사이 대기 시간 범위(초)
MAX_OPT_REMAIN = 15      # 상품 하나에서 남은 수량을 확인할 옵션 최대 개수
MAX_OPT_SELECT_FAIL = 2  # 옵션 선택이 처음부터 이만큼 연속 실패하면 그 상품은 남은 수량 확인 포기

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']}); window.chrome = {runtime: {}};"
KST = timezone(timedelta(hours=9))

T0 = time.time()
PRICE_RE = re.compile(r"\d[\d,]*\s*원")
NOISE_RE = re.compile(r"^(예약|일시품절|품절|재입고|오늘드림|옵션을 선택|배송|구매수량|총\s)|배송시작|^\d+\s*%|^\(")
REMAIN_RE = re.compile(r"재고가\s*([\d,]+)\s*개\s*남았")
LIMIT_RE = re.compile(r"최대|제한|1인|구매\s*가능")
DIALOGS = []             # 브라우저 기본 알림창 문구 기록

# 화면에 잠깐 뜨는 안내 문구를 기록해 두는 감시 장치
WATCH_JS = r"""() => {
  window.__remain = [];
  window.__msgs = [];
  if (window.__remainObs) window.__remainObs.disconnect();
  const re = /재고가\s*([\d,]+)\s*개\s*남았/;
  const kw = /재고|최대|제한|1인|구매\s*가능/;
  const check = node => {
    const el = node && node.nodeType === 3 ? node.parentElement : node;
    if (!el || el.nodeType !== 1) return;
    if ((el.textContent || '').length > 300) return;
    if (!el.getClientRects || el.getClientRects().length === 0) return;
    const txt = (el.innerText || el.textContent || '').trim();
    const m = txt.match(re);
    if (m) window.__remain.push(m[1]);
    if (txt.length < 120 && kw.test(txt)) window.__msgs.push(txt);
  };
  window.__remainObs = new MutationObserver(list => {
    for (const m of list) {
      if (m.type === 'childList') m.addedNodes.forEach(check);
      else check(m.target);
    }
  });
  window.__remainObs.observe(document.body, {childList: true, subtree: true, characterData: true,
                                             attributes: true, attributeFilter: ['class', 'style']});
}"""

# 화면에 보이는 구매수량 칸(값이 1인 입력칸)을 찾아 표시하고 번호를 돌려준다 (없으면 -1)
MARK_QTY_JS = """() => {
  document.querySelectorAll('[data-qty-target]').forEach(e => e.removeAttribute('data-qty-target'));
  const skip = ['checkbox', 'radio', 'hidden', 'button', 'submit'];
  const all = [...document.querySelectorAll('input')];
  const idx = all.findIndex(i => i.offsetParent !== null && !skip.includes(i.type) && (i.value || '').trim() === '1');
  if (idx < 0) return -1;
  all[idx].setAttribute('data-qty-target', '1');
  return idx;
}"""

READ_INPUT_JS = """(i) => { const el = document.querySelectorAll('input')[i]; return el ? el.value : null; }"""

# 옵션 글자를 감싼 클릭 가능한 칸을 자바스크립트로 클릭
JS_CLICK = """(el) => {
  const t = el.closest('li, button, a, label, [role=option], [onclick]') || el;
  t.click();
  return true;
}"""

# 옵션 선택이 안 될 때 원인을 알기 위한 진단 정보
DIAG_JS = """(name) => {
  const skip = ['checkbox', 'radio', 'hidden', 'button', 'submit'];
  const inputs = [...document.querySelectorAll('input')]
    .filter(i => i.offsetParent !== null && !skip.includes(i.type))
    .map(i => i.type + ':' + i.value).slice(0, 6);
  const leaves = [...document.querySelectorAll('body *')]
    .filter(e => e.children.length === 0 && (e.textContent || '').trim() === name);
  const shown = leaves.filter(e => e.getClientRects().length > 0).length;
  let html = '';
  if (leaves.length) {
    const box = leaves[0].closest('li, button, a, [role=option]') || leaves[0].parentElement || leaves[0];
    html = box.outerHTML.replace(/\\s+/g, ' ').slice(0, 300);
  }
  const sel = document.querySelectorAll('select').length;
  return {inputs, total: leaves.length, shown, html, sel};
}"""


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def now_ts():
    return datetime.now(KST).strftime("%m/%d %H:%M")


def load_state(path):
    """상태 파일 형식: {"items": {상품번호: {...}}, "errors": {키: 메시지}}"""
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "items" in data:
            data.setdefault("errors", {})
            return data
    except Exception:
        pass
    return {"items": {}, "errors": {}}


def parse_rank_urls():
    raw = os.environ.get("RANK_URLS") or DEFAULT_RANK
    pages = []
    for item in raw.split(","):
        if not item.strip():
            continue
        if "|" in item:
            name, url = item.split("|", 1)
        else:
            name, url = item, item
        pages.append((name.strip(), url.strip()))
    return pages


def detail_url(goods_no):
    return f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={goods_no}"


def open_page(page, url):
    """페이지를 열고 HTTP 상태코드를 돌려준다. 400 이상이면 예외."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    status = resp.status if resp else None
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    page.wait_for_timeout(2000)
    return status


def on_dialog(dialog):
    DIALOGS.append(dialog.message or "")
    try:
        dialog.accept()
    except Exception:
        pass


# ---------- 슬랙 ----------

def post_slack(text):
    if not WEBHOOK:
        log("[경고] SLACK_WEBHOOK_URL 이 비어 있어 슬랙 대신 로그에 출력합니다.")
        print(text)
        return
    req = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:
        log(f"[슬랙] 전송 완료 ({res.status})")


def send_slack(header, blocks):
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) > SLACK_CHUNK and cur != header:
            chunks.append(cur)
            cur = header
        cur += "\n\n" + b
    chunks.append(cur)
    for c in chunks:
        post_slack(c)
        time.sleep(1)


# ======================================================================
# 랭킹
# ======================================================================

def collect_goods(page):
    """페이지에 보이는 상품번호를 순서대로, 그리고 링크 글자(상품명 후보)를 수집"""
    pairs = page.eval_on_selector_all("a[href*='goodsNo=']", "els => els.map(e => [e.href, (e.innerText || '').trim()])")
    ids, names = [], {}
    for href, text in pairs:
        m = re.search(r"goodsNo=([A-Za-z0-9]+)", href)
        if not m:
            continue
        g = m.group(1)
        if g not in ids:
            ids.append(g)
        line = max(text.split("\n"), key=len).strip() if text else ""
        if len(line) > len(names.get(g, "")):
            names[g] = line[:60]
    return ids, names


def fetch_ranking(page, url):
    """403이 와도 보안 확인이 자동 통과될 수 있으므로 기다려 보고, 실패하면 메인 페이지를 거쳐 재시도"""
    last_err = None
    for attempt in range(1, RANK_RETRY + 1):
        try:
            if attempt > 1:
                log(f"랭킹 재시도 {attempt}/{RANK_RETRY} - 메인 페이지 경유")
                try:
                    page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                except Exception as e:
                    log(f"메인 페이지 열기 실패(무시): {str(e)[:60]}")
                page.wait_for_timeout(random.randint(3000, 6000))

            resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            status = resp.status if resp else None

            ids = []
            for _ in range(15):
                page.wait_for_timeout(1000)
                ids, _ = collect_goods(page)
                if ids:
                    break
            if not ids:
                raise RuntimeError(f"HTTP {status}, 제목 '{page.title()[:30]}'")

            for _ in range(4):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(400)
            ids, names = collect_goods(page)
            if attempt > 1:
                log(f"랭킹 재시도 {attempt}회차에 성공")
            return ids, names
        except Exception as e:
            last_err = e
            log(f"랭킹 시도 {attempt}/{RANK_RETRY} 실패: {str(e)[:80]}")
    raise RuntimeError(str(last_err)[:100])


def rank_text(r):
    return f"{r}위" if r else "권 밖"


def run_rank(page, prev):
    rank_pages = parse_rank_urls()
    log(f"[랭킹 모드] 상품 {len(GOODS)}개, 랭킹 {len(rank_pages)}개")

    rankings, rank_names, fails = {}, {}, {}
    for name, url in rank_pages:
        try:
            ids, names = fetch_ranking(page, url)
            rankings[name] = ids
            for g, n in names.items():
                rank_names.setdefault(g, n)
            log(f"랭킹 '{name}' 완료 ({len(ids)}개 상품 보임)")
        except Exception as e:
            fails[f"랭킹 '{name}'"] = str(e)[:100]
            log(f"랭킹 '{name}' 최종 실패: {str(e)[:100]}")
        time.sleep(random.uniform(1.0, 2.0))

    stock_names = {g: v.get("name", "") for g, v in load_state(STOCK_STATE_FILE)["items"].items()}
    old_items = prev["items"]

    now_items, blocks = {}, []
    rank_changed = False
    for g in GOODS:
        old = old_items.get(g, {})
        old_ranks = old.get("ranks", {})
        ranks, diffs = {}, []
        for name, _ in rank_pages:
            if name in rankings:
                lst = rankings[name]
                r = lst.index(g) + 1 if g in lst else None
                ranks[name] = r
                if g in old_items and old_ranks.get(name) != r:
                    diffs.append(f"  · {name}: {rank_text(old_ranks.get(name))} → *{rank_text(r)}*")
                    rank_changed = True
                elif g not in old_items and r:
                    diffs.append(f"  · {name}: *{rank_text(r)}*")
                    rank_changed = True
                elif ALWAYS:
                    diffs.append(f"  · {name}: {rank_text(r)}")
            else:
                ranks[name] = old_ranks.get(name)
        pname = stock_names.get(g) or old.get("name") or rank_names.get(g, "")
        now_items[g] = {"name": pname, "ranks": ranks}
        if diffs:
            title = f"{pname} ({g})" if pname else g
            blocks.append(title + "\n" + "\n".join(diffs) + f"\n  {detail_url(g)}")

    # 오류: 새로 생긴 것만 알림, 복구되면 알림
    prev_errors = prev["errors"]
    err_lines = []
    for k, msg in fails.items():
        if k not in prev_errors:
            err_lines.append(f"❗ {k} 확인 실패: {msg} (이전 순위 유지)")
    for k in prev_errors:
        if k not in fails and k.replace("랭킹 '", "").rstrip("'") in rankings:
            err_lines.append(f"✅ {k} 다시 정상 확인됨")
    if err_lines:
        blocks.append("\n".join(err_lines))

    ts = now_ts()
    if rank_changed or err_lines:
        send_slack(f"📊 [올리브영 랭킹 {ts}] 순위 변동", blocks)
    elif ALWAYS or NOTIFY_NO_CHANGE:
        in_rank = sum(1 for v in now_items.values() if any(v["ranks"].values()))
        note = f" (확인 실패 중: {', '.join(fails)})" if fails else ""
        msg = f"✅ [올리브영 랭킹 {ts}] 순위 변동 없음. 랭킹 내 상품 {in_rank}개{note}"
        if ALWAYS:
            lines = []
            for g in GOODS:
                it = now_items[g]
                rs = ", ".join(f"{n} {rank_text(r)}" for n, r in it["ranks"].items())
                lines.append(f"· {it['name'] or g}: {rs}")
            msg += "\n" + "\n".join(lines)
        post_slack(msg)
    else:
        log("변화 없음 (알림 생략)")

    return {"items": now_items, "errors": fails}


# ======================================================================
# 재고
# ======================================================================

def clean_name(title):
    t = (title or "").strip()
    t = re.sub(r"\s*[|\-–—:]\s*(올리브영|OLIVE\s*YOUNG).*$", "", t, flags=re.I)
    t = re.sub(r"^\[?(올리브영|OLIVE\s*YOUNG)\]?\s*[|\-–—:]?\s*", "", t, flags=re.I)
    return t.strip()[:60]


def get_name(page):
    try:
        og = page.evaluate("() => (document.querySelector('meta[property=\"og:title\"]') || {}).content || ''")
    except Exception:
        og = ""
    return clean_name(og or page.title())


def parse_options(text):
    """옵션 패널의 글자에서 {옵션이름: 품절여부} 를 뽑는다."""
    marker = "옵션을 선택해"
    if marker in text:
        rest = text.split(marker, 1)[1]
        text = rest.split("\n", 1)[1] if "\n" in rest else ""
    text = text.split("구매수량")[0]

    rows, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if PRICE_RE.search(line) or NOISE_RE.search(line):
            if cur is not None:
                cur["lines"].append(line)
        else:
            cur = {"name": line, "lines": []}
            rows.append(cur)

    options = {}
    for r in rows:
        joined = " ".join(r["lines"])
        if not PRICE_RE.search(joined):
            continue
        options[r["name"]] = ("품절" in joined) or ("재입고" in joined)
    return options


def find_option_header(page):
    loc = page.get_by_text("옵션을 선택해")
    for i in range(min(loc.count(), 5)):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def read_options(page):
    """옵션 상품이면 ({옵션: 품절여부}, 디버그글자), 옵션 없는 상품이면 None."""
    header = find_option_header(page)
    if header is None:
        return None

    def box_text():
        box = header.locator("xpath=ancestor::*[contains(., '원')][1]")
        return box.inner_text() if box.count() else ""

    text = box_text()
    options = parse_options(text)
    if options:
        return options, text[:80]

    header.click()
    page.wait_for_timeout(700)
    text = box_text()
    options = parse_options(text)
    if options:
        return options, text[:80]

    body = page.inner_text("body")
    return parse_options(body), text[:80]


# ---------- 남은 수량 ----------
# 결과 형식: {"low": False, "n": None}  → LOW_STOCK개 이상 (충분)
#            {"low": True,  "n": 15}    → 부족 (안내에서 읽은 숫자, 못 읽었으면 None)
#            None                       → 판단 못 함

def start_watch(page):
    try:
        page.evaluate(WATCH_JS)
    except Exception as e:
        log(f"안내 감시 시작 실패(무시): {str(e)[:60]}")


def watched_remain(page):
    """감시 장치가 잡은 '재고가 N개 남았습니다'의 숫자. 없으면 None."""
    try:
        vals = page.evaluate("() => window.__remain || []")
        if vals:
            return int(str(vals[-1]).replace(",", ""))
    except Exception:
        pass
    try:
        t = page.get_by_text(REMAIN_RE).first
        if t.is_visible():
            m = REMAIN_RE.search(t.inner_text())
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def watched_msgs(page):
    try:
        return list(page.evaluate("() => window.__msgs || []"))
    except Exception:
        return []


def check_qty(page):
    """구매수량 칸에 LOW_STOCK개를 입력해 보고, 그대로 들어가는지로 재고가 충분한지 판단"""
    idx = page.evaluate(MARK_QTY_JS)
    if idx is None or idx < 0:
        return None
    DIALOGS.clear()
    want = str(LOW_STOCK)
    qty = page.locator("[data-qty-target]").first
    try:
        qty.click(timeout=3000)
        qty.press("Control+A")
        qty.type(want, delay=60)
        qty.press("Tab")
    except Exception as e:
        log(f"수량 입력 실패(무시): {str(e)[:60]}")
        return None

    page.wait_for_timeout(1500)
    n = watched_remain(page)
    try:
        val = (page.evaluate(READ_INPUT_JS, idx) or "").strip()
    except Exception:
        val = ""
    msgs = " / ".join(DIALOGS + watched_msgs(page))

    if n is not None:                          # "재고가 N개 남았습니다" 를 잡았으면 그 숫자가 정답
        return {"low": n < LOW_STOCK, "n": n}
    if val == want:                            # 그대로 들어갔으면 충분
        return {"low": False, "n": None}
    if msgs and "재고" not in msgs and LIMIT_RE.search(msgs):
        log(f"수량 {want}개가 구매 제한으로 막힌 것 같음 (재고 판단 불가): '{msgs[:80]}'")
        return None
    m = re.search(r"([\d,]+)\s*개", msgs) if "재고" in msgs else None
    left = int(m.group(1).replace(",", "")) if m else None
    log(f"수량 {want}개 입력 안 됨 (칸 값 '{val}') → 재고 부족 판단. 안내: '{msgs[:80]}'")
    return {"low": True, "n": left}


def remain_text(r):
    if not r:
        return ""
    if r.get("low"):
        return f"{r['n']}개 남음" if r.get("n") is not None else f"{LOW_STOCK}개 미만 남음"
    return f"{LOW_STOCK}개 이상"


def option_selected(page, ms=1500):
    """옵션이 실제로 선택됐는지 (수량 칸이 생겼거나 재고 안내가 떴는지) 확인"""
    end = time.time() + ms / 1000
    while True:
        try:
            idx = page.evaluate(MARK_QTY_JS)
            if (idx is not None and idx >= 0) or watched_remain(page) is not None:
                return True
        except Exception:
            pass
        if time.time() >= end:
            return False
        page.wait_for_timeout(250)


def find_visible_text(scope, text, exact=True):
    loc = scope.get_by_text(text, exact=exact)
    for i in range(min(loc.count(), 8)):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def select_option(page, oname):
    """옵션 목록을 열고 해당 옵션을 선택. 실제로 선택된 게 확인되면 True."""
    header = find_option_header(page)
    if header is None:
        return False
    box = header.locator("xpath=ancestor::*[contains(., '원')][1]")

    # (찾는 범위, 정확히 일치, 자바스크립트 클릭 여부) 순서대로 시도
    attempts = [
        (box, True, False),
        (box, True, True),
        (page, True, True),
        (page, False, True),
    ]
    for scope, exact, use_js in attempts:
        try:
            item = find_visible_text(scope, oname, exact)
            if item is None:
                h = find_option_header(page)
                if h is not None:
                    h.click()
                    page.wait_for_timeout(700)
                item = find_visible_text(scope, oname, exact)
            if item is None:
                continue
            if use_js:
                item.evaluate(JS_CLICK)
            else:
                item.click(timeout=3000)
            if option_selected(page):
                return True
        except Exception:
            continue
    return False


def read_option_remains(page, goods_no, options):
    """판매중인 옵션을 하나씩 골라 수량을 넣어 본다. 두 번째 옵션부터는 페이지를 새로 연다."""
    remains = {}
    first = True
    select_ok = 0
    select_fail = 0
    targets = [n for n, sold in options.items() if not sold][:MAX_OPT_REMAIN]
    for oname in targets:
        if time.time() - T0 > TIME_LIMIT - 60:
            log(f"{goods_no} 시간 부족 → 옵션 남은 수량 확인 일부 생략")
            break
        if select_ok == 0 and select_fail >= MAX_OPT_SELECT_FAIL:
            log(f"{goods_no} 옵션 선택이 계속 안 됨 → 이 상품은 남은 수량 확인 생략")
            break
        try:
            if not first:
                time.sleep(random.uniform(*GAP))
                open_page(page, detail_url(goods_no))
            first = False
            start_watch(page)
            if not select_option(page, oname):
                select_fail += 1
                try:
                    d = page.evaluate(DIAG_JS, oname)
                    log(f"{goods_no} 옵션 '{oname[:20]}' 선택 안 됨 | 같은글자 {d['total']}개(보임 {d['shown']}) "
                        f"| select태그 {d['sel']}개 | 입력칸 {d['inputs']} | 구조 {d['html']}")
                except Exception as e:
                    log(f"{goods_no} 옵션 '{oname[:20]}' 선택 안 됨 (진단 실패: {str(e)[:60]})")
                continue
            select_ok += 1
            r = check_qty(page)
            if r is None:
                log(f"{goods_no} 옵션 '{oname[:20]}' 남은 수량 판단 못 함")
            remains[oname] = r
        except Exception as e:
            log(f"{goods_no} 옵션 '{oname[:20]}' 남은 수량 확인 실패(무시): {str(e)[:60]}")
    return remains


class NoStockInfo(Exception):
    """페이지는 열렸지만 재고 상태를 판단할 수 없는 경우 (판매종료 등)"""
    pass


def get_stock(page, goods_no):
    open_page(page, detail_url(goods_no))
    name = get_name(page)
    res = read_options(page)

    if res is None:
        btns = [t.strip() for t in page.locator("button").all_inner_texts()]
        if any(("바로구매" in t or "구매하기" in t) for t in btns):
            start_watch(page)
            return {"name": name, "soldout": False, "options": {}, "remains": {"": check_qty(page)}}
        if any("품절" in t for t in btns):
            return {"name": name, "soldout": True, "options": {}, "remains": {}}
        raise NoStockInfo("재고 상태를 판단할 수 없음 (구매 버튼을 못 찾음)")

    options, debug = res
    if not options:
        raise RuntimeError(f"옵션 목록을 읽지 못함: {debug!r}")
    remains = read_option_remains(page, goods_no, options)
    return {"name": name, "soldout": all(options.values()), "options": options, "remains": remains}


def stock_label(so):
    return "확인 못함" if so is None else ("🔴 품절" if so else "🟢 판매중")


def describe_stock(s, g):
    rm = s.get("remains", {})
    if s["options"]:
        parts = []
        for n, v in s["options"].items():
            st = "품절" if v else "판매중"
            if rm.get(n):
                st += f", {remain_text(rm[n])}"
            parts.append(f"{n}({st})")
        return f"{s['name']} ({g}) 옵션 {len(s['options'])}개: {', '.join(parts)}"
    rem = f", {remain_text(rm[''])}" if rm.get("") else ""
    return f"{s['name']} ({g}) {'품절' if s['soldout'] else '판매중'} (옵션 없음{rem})"


def get_stock_with_429_retry(page, g, label):
    """429가 뜨면 잠깐 기다렸다가 같은 상품을 다시 시도. 다른 오류는 그대로 올려보냄."""
    for attempt in range(RETRY_429 + 1):
        try:
            return get_stock(page, g)
        except NoStockInfo:
            raise
        except Exception as e:
            is_429 = "HTTP 429" in str(e)
            if not is_429 or attempt >= RETRY_429 or time.time() - T0 > TIME_LIMIT:
                raise
            wait = random.uniform(*WAIT_429)
            log(f"{label} {g} 요청 제한(429) → {wait:.0f}초 후 다시 시도 ({attempt + 1}/{RETRY_429})")
            time.sleep(wait)


def run_stock(page, prev):
    log(f"[재고 모드] 상품 {len(GOODS)}개, 재고 부족 기준 {LOW_STOCK}개 미만")
    stock, fails = {}, {}
    no_info = set()
    consec_fail = 0
    stopped = None

    for i, g in enumerate(GOODS, 1):
        if time.time() - T0 > TIME_LIMIT:
            stopped = f"시간 제한 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
            break
        if consec_fail >= MAX_CONSEC_FAIL:
            stopped = f"연속 {consec_fail}회 실패 → 접속 차단 의심 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
            break
        label = f"[{i}/{len(GOODS)}]"
        try:
            stock[g] = get_stock_with_429_retry(page, g, label)
            consec_fail = 0
            log(f"{label} {describe_stock(stock[g], g)}")
        except NoStockInfo as e:
            consec_fail = 0
            no_info.add(g)
            fails[g] = str(e)[:100]
            log(f"{label} {g} 판단 불가: {str(e)[:100]}")
        except Exception as e:
            consec_fail += 1
            fails[g] = str(e)[:100]
            log(f"{label} {g} 실패: {str(e)[:100]}")
        time.sleep(random.uniform(*GAP))

    # ---------- 실패한 상품 마지막에 한 번 더 시도 ----------
    retry = [g for g in GOODS if g in fails and g not in no_info]
    if retry and not stopped:
        log(f"실패 상품 {len(retry)}개 재시도")
        time.sleep(random.uniform(3, 5))
        for g in retry:
            if time.time() - T0 > TIME_LIMIT:
                log("재시도 중 시간 제한 → 재시도 중단")
                break
            try:
                stock[g] = get_stock_with_429_retry(page, g, "[재시도]")
                fails.pop(g, None)
                log(f"[재시도 성공] {describe_stock(stock[g], g)}")
            except Exception as e:
                fails[g] = str(e)[:100]
                log(f"[재시도 실패] {g}: {str(e)[:100]}")
            time.sleep(random.uniform(*GAP))

    old_items = prev["items"]
    prev_errors = prev["errors"]
    now_items, now_errors, blocks = {}, {}, []
    changed_cnt = 0
    low_cnt = 0

    for g in GOODS:
        old = old_items.get(g, {})
        old_lows = old.get("lows")
        if old_lows is None:                       # 이전 버전 상태 파일 호환
            old_lows = {"": True} if old.get("low") else {}
        low_msgs = []
        rm = {}
        if g in stock:
            s = stock[g]
            rm = s.get("remains", {})
            if s["options"]:
                valid = {k for k, sold in s["options"].items() if not sold}
            else:
                valid = set() if s["soldout"] else {""}
            # 품절됐거나 없어진 옵션은 '부족' 상태를 지움 (품절 알림이 따로 감)
            lows = {k: True for k, v in old_lows.items() if v and k in valid}
            for k, r in rm.items():
                if not r:
                    continue                       # 이번에 판단 못 했으면 이전 상태 유지
                was = old_lows.get(k, False)
                label = f"[{k}] " if k else ""
                if r["low"]:
                    lows[k] = True
                    if not was:
                        low_msgs.append(f"  · 🟠 재고 부족 {label}{remain_text(r)}")
                else:
                    lows.pop(k, None)
                    if was:
                        low_msgs.append(f"  · 🟢 재고 다시 넉넉함 {label}({LOW_STOCK}개 이상)")
            cur = {"name": s["name"] or old.get("name", ""), "soldout": s["soldout"], "options": s["options"],
                   "remains": {k: r for k, r in rm.items() if r}, "lows": lows}
        else:
            cur = {"name": old.get("name", ""), "soldout": old.get("soldout"), "options": old.get("options", {}),
                   "remains": old.get("remains", {}), "lows": old_lows}
            if g in fails:
                now_errors[g] = fails[g]
            elif g in prev_errors:
                now_errors[g] = prev_errors[g]
        now_items[g] = cur

        changed = (old.get("soldout"), old.get("options", {})) != (cur["soldout"], cur["options"])
        if changed and g in stock:
            changed_cnt += 1
        if low_msgs:
            low_cnt += 1
        if not (changed or low_msgs or ALWAYS) or g not in stock:
            continue
        if changed and g in old_items:
            tag = "⚠️ 변화 "
        elif any("🟠" in m for m in low_msgs):
            tag = "🟠 재고 부족 "
        else:
            tag = ""
        title = f"{cur['name']} ({g})" if cur["name"] else g
        lines = [f"{tag}{title}"]
        opts = cur["options"] or {}
        if opts:
            sold = sum(1 for v in opts.values() if v)
            lines.append(f"  · 재고: 옵션 {len(opts)}개 중 {sold}개 품절 ({stock_label(cur['soldout'])})")
            old_opts = old.get("options") or {}
            for oname, v in opts.items():
                mark = ""
                if oname in old_opts and old_opts[oname] != v:
                    mark = f" ← 변화 (이전: {'품절' if old_opts[oname] else '판매중'})"
                rem = f" · {remain_text(rm[oname])}" if rm.get(oname) else ""
                lines.append(f"      - {oname}: {stock_label(v)}{rem}{mark}")
        else:
            prev_label = f" (이전: {stock_label(old.get('soldout'))})" if changed and g in old_items else ""
            rem = f" · {remain_text(rm[''])}" if rm.get("") else ""
            lines.append(f"  · 재고: {stock_label(cur['soldout'])}{prev_label}{rem}")
        lines.extend(low_msgs)
        lines.append(f"  {detail_url(g)}")
        blocks.append("\n".join(lines))

    err_lines = []
    for g, msg in fails.items():
        if prev_errors.get(g) != msg:
            label = now_items[g].get("name") or g
            err_lines.append(f"{label} ({g}) 재고 실패: {msg}")
    if stopped:
        err_lines.append(f"중단: {stopped}")
        log(f"중단: {stopped}")
    if err_lines:
        shown = err_lines[:10]
        extra = f"\n...외 {len(err_lines) - 10}건" if len(err_lines) > 10 else ""
        blocks.append("❗ 확인 실패/중단 (이전 값 유지)\n" + "\n".join(shown) + extra)

    has_issue = bool(changed_cnt or low_cnt or err_lines)

    # ALWAYS_NOTIFY=1이면 변화가 없어도 상품별 한 줄 요약을 붙임
    summary_lines = []
    if ALWAYS:
        for g in GOODS:
            if g not in stock:
                continue
            it = now_items[g]
            opts = it["options"] or {}
            extra = f" (옵션 {len(opts)}개 중 {sum(opts.values())}개 품절)" if opts else ""
            r = stock[g].get("remains", {}).get("")
            if r:
                extra += f" ({remain_text(r)})"
            summary_lines.append(f"· {it['name'] or g}: {stock_label(it['soldout'])}{extra}")

    if SUMMARY_FILE:
        Path(SUMMARY_FILE).write_text(json.dumps(
            {"goods": len(GOODS), "checked": len(stock), "issue": has_issue, "lines": summary_lines},
            ensure_ascii=False))

    ts = now_ts()
    if has_issue:
        send_slack(f"📦 [올리브영 재고 {ts}] 상품 {len(GOODS)}개 확인", blocks)
    elif SUMMARY_FILE:
        log("변화 없음 → 통합 알림 job에서 한 번에 전송")
    elif ALWAYS or NOTIFY_NO_CHANGE:
        msg = f"✅ [올리브영 재고 {ts}] 상품 {len(GOODS)}개 체크했으나 변경점이 없습니다. (재고 확인 {len(stock)}개)"
        if summary_lines:
            msg += "\n" + "\n".join(summary_lines)
        post_slack(msg)
    else:
        log("변화 없음 (알림 생략)")

    return {"items": now_items, "errors": now_errors}


# ======================================================================

def main():
    if MODE not in ("stock", "rank"):
        sys.exit(f"MODE 값이 잘못됐습니다: {MODE!r} (stock 또는 rank)")
    if not GOODS:
        sys.exit("상품번호가 없습니다. (GOODS_NO Secret 확인)")

    prev = load_state(STATE_FILE)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1366, "height": 900})
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        page.on("dialog", on_dialog)
        try:
            if MODE == "rank":
                new_state = run_rank(page, prev)
            else:
                new_state = run_stock(page, prev)
        finally:
            browser.close()

    # 슬랙 전송이 성공한 뒤에만 상태 저장 (전송 실패 시 다음 실행에서 다시 알림)
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False))
    log(f"상태 저장 완료 ({STATE_FILE.name})")
    log("끝")


if __name__ == "__main__":
    main()
