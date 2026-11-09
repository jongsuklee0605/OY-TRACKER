"""
올리브영 재고 / 랭킹 모니터 → 슬랙 알림
MODE=stock : 상품별 재고(옵션별 품절) 체크
MODE=rank  : 랭킹 페이지에서 상품 순위 체크

환경변수
  MODE               (필수) stock 또는 rank
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook 주소
  GOODS_NO           (필수) 상품번호, 쉼표로 여러 개
  RANK_URLS          (선택) 랭킹 페이지, "이름|URL" 을 쉼표로 여러 개. 안 넣으면 전체 랭킹만
  ALWAYS_NOTIFY      (선택) 1이면 변화 없어도 매번 상세 알림
  NOTIFY_NO_CHANGE   (선택) 0이면 '변경점 없음' 알림을 보내지 않음 (기본 1)
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
NOTIFY_NO_CHANGE = os.environ.get("NOTIFY_NO_CHANGE", "1") != "0"

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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']}); window.chrome = {runtime: {}};"
KST = timezone(timedelta(hours=9))

T0 = time.time()
PRICE_RE = re.compile(r"\d[\d,]*\s*원")
NOISE_RE = re.compile(r"^(예약|일시품절|품절|재입고|오늘드림|옵션을 선택|배송|구매수량|총\s)|배송시작|^\d+\s*%|^\(")


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
                elif g not in old_items and r:
                    diffs.append(f"  · {name}: *{rank_text(r)}*")
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
    if blocks:
        send_slack(f"📊 [올리브영 랭킹 {ts}] 순위 변동", blocks)
    elif NOTIFY_NO_CHANGE:
        in_rank = sum(1 for v in now_items.values() if any(v["ranks"].values()))
        note = f" (확인 실패 중: {', '.join(fails)})" if fails else ""
        post_slack(f"✅ [올리브영 랭킹 {ts}] 순위 변동 없음. 랭킹 내 상품 {in_rank}개{note}")
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


def read_options(page):
    """옵션 상품이면 ({옵션: 품절여부}, 디버그글자), 옵션 없는 상품이면 None."""
    loc = page.get_by_text("옵션을 선택해")
    header = None
    for i in range(min(loc.count(), 5)):
        if loc.nth(i).is_visible():
            header = loc.nth(i)
            break
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
            return {"name": name, "soldout": False, "options": {}}
        if any("품절" in t for t in btns):
            return {"name": name, "soldout": True, "options": {}}
        raise NoStockInfo("재고 상태를 판단할 수 없음 (구매 버튼을 못 찾음)")

    options, debug = res
    if not options:
        raise RuntimeError(f"옵션 목록을 읽지 못함: {debug!r}")
    return {"name": name, "soldout": all(options.values()), "options": options}


def stock_label(so):
    return "확인 못함" if so is None else ("🔴 품절" if so else "🟢 판매중")


def run_stock(page, prev):
    log(f"[재고 모드] 상품 {len(GOODS)}개")
    stock, fails = {}, {}
    consec_fail = 0
    stopped = None

    for i, g in enumerate(GOODS, 1):
        if time.time() - T0 > TIME_LIMIT:
            stopped = f"시간 제한 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
            break
        if consec_fail >= MAX_CONSEC_FAIL:
            stopped = f"연속 {consec_fail}회 실패 → 접속 차단 의심 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
            break
        try:
            stock[g] = get_stock(page, g)
            consec_fail = 0
            s = stock[g]
            if s["options"]:
                desc = ", ".join(f"{n}({'품절' if v else '판매중'})" for n, v in s["options"].items())
                log(f"[{i}/{len(GOODS)}] {s['name']} ({g}) 옵션 {len(s['options'])}개: {desc}")
            else:
                log(f"[{i}/{len(GOODS)}] {s['name']} ({g}) {'품절' if s['soldout'] else '판매중'} (옵션 없음)")
        except NoStockInfo as e:
            consec_fail = 0
            fails[g] = str(e)[:100]
            log(f"[{i}/{len(GOODS)}] {g} 판단 불가: {str(e)[:100]}")
        except Exception as e:
            consec_fail += 1
            fails[g] = str(e)[:100]
            log(f"[{i}/{len(GOODS)}] {g} 실패: {str(e)[:100]}")
        time.sleep(random.uniform(0.5, 1.2))

    old_items = prev["items"]
    prev_errors = prev["errors"]
    now_items, now_errors, blocks = {}, {}, []

    for g in GOODS:
        old = old_items.get(g, {})
        if g in stock:
            s = stock[g]
            cur = {"name": s["name"] or old.get("name", ""), "soldout": s["soldout"], "options": s["options"]}
        else:
            cur = {"name": old.get("name", ""), "soldout": old.get("soldout"), "options": old.get("options", {})}
            if g in fails:
                now_errors[g] = fails[g]
            elif g in prev_errors:
                now_errors[g] = prev_errors[g]
        now_items[g] = cur

        changed = (old.get("soldout"), old.get("options", {})) != (cur["soldout"], cur["options"])
        if not (changed or ALWAYS) or g not in stock:
            continue
        tag = "⚠️ 변화 " if changed and g in old_items else ""
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
                lines.append(f"      - {oname}: {stock_label(v)}{mark}")
        else:
            prev_label = f" (이전: {stock_label(old.get('soldout'))})" if changed and g in old_items else ""
            lines.append(f"  · 재고: {stock_label(cur['soldout'])}{prev_label}")
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

    ts = now_ts()
    if blocks:
        send_slack(f"📦 [올리브영 재고 {ts}] 상품 {len(GOODS)}개 확인", blocks)
    elif NOTIFY_NO_CHANGE:
        post_slack(f"✅ [올리브영 재고 {ts}] 상품 {len(GOODS)}개 체크했으나 변경점이 없습니다. (재고 확인 {len(stock)}개)")
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
