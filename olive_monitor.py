"""
올리브영 여러 상품 x 여러 랭킹 페이지 모니터 → 슬랙 알림
(빠른 실패 + 진행 로그 + 옵션별 품절 + 상품명 표시 + 오류 1회 알림 + 무변화 알림 버전)

환경변수
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook 주소
  GOODS_NO           (선택) 상품번호, 쉼표로 여러 개. 안 넣으면 아래 DEFAULT_GOODS 사용
  RANK_URLS          (선택) 랭킹 페이지, "이름|URL" 을 쉼표로 여러 개. 안 넣으면 전체 랭킹만
  ALWAYS_NOTIFY      (선택) 1이면 변화 없어도 매번 상세 알림
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_GOODS = "A000000227445,A000000227451,A000000227439,A000000227443,A000000227446,A000000227449,A000000208718,A000000208721,A000000208732,A000000208733,A000000208731,A000000208730,A000000208726,A000000208723,A000000240361,A000000240368,A000000240371,A000000232342,A000000253343,A000000232341,A000000232362,A000000232364,A000001005542,A000001005349,A000000232358,A000000232361,A000000231899,A000000261287,A000000261290,A000000261292,A000000261090,A000000245944,A000000262006,A000000215317,A000000265442,A000000267402,A000000267403,A000000268404"

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
RAW_GOODS = os.environ.get("GOODS_NO") or DEFAULT_GOODS
GOODS = list(dict.fromkeys(g.strip() for g in RAW_GOODS.split(",") if g.strip()))
ALWAYS = os.environ.get("ALWAYS_NOTIFY") == "1"
DEFAULT_RANK = "전체|https://www.oliveyoung.co.kr/store/main/getBestList.do"
STATE_FILE = Path(__file__).with_name("olive_state.json")
SLACK_CHUNK = 3000

NAV_TIMEOUT = 25000      # 페이지 하나를 여는 데 기다리는 최대 시간(ms)
TIME_LIMIT = 9 * 60      # 전체 실행 최대 시간(초). 넘으면 중간 결과만 처리
MAX_CONSEC_FAIL = 4      # 연속 실패가 이만큼이면 접속 차단으로 보고 중단

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

T0 = time.time()
PRICE_RE = re.compile(r"\d[\d,]*\s*원")
# 옵션 이름이 아닌 줄(가격, 배지, 안내문구, 버튼 글자)
NOISE_RE = re.compile(r"^(예약|일시품절|품절|재입고|오늘드림|옵션을 선택|배송|구매수량|총\s)|배송시작|^\d+\s*%|^\(")


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def parse_rank_urls():
    raw = os.environ.get("RANK_URLS") or DEFAULT_RANK
    pages = []
    for item in raw.split(","):
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
    page.wait_for_timeout(2000)  # 동적 내용이 채워질 시간
    return status


def fetch_ranking(page, url):
    status = open_page(page, url)
    for _ in range(4):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)
    hrefs = page.eval_on_selector_all("a[href*='goodsNo=']", "els => els.map(e => e.href)")
    seen = []
    for h in hrefs:
        m = re.search(r"goodsNo=([A-Za-z0-9]+)", h)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    if not seen:
        raise RuntimeError(f"상품 못 찾음 (HTTP {status}, 제목 '{page.title()[:30]}')")
    return seen


# ---------- 상품명 ----------

def clean_name(title):
    """페이지 제목에서 '| 올리브영' 같은 꼬리표를 떼고 상품명만 남긴다."""
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


# ---------- 옵션 ----------

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
            continue  # 가격이 없으면 옵션이 아님
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

    header.click()  # 접혀 있던 옵션 창을 펼친다
    page.wait_for_timeout(700)
    text = box_text()
    options = parse_options(text)
    if options:
        return options, text[:80]

    body = page.inner_text("body")
    return parse_options(body), text[:80]


class NoStockInfo(Exception):
    """페이지는 열렸지만 재고 상태를 판단할 수 없는 경우 (판매종료 등). 접속 차단과 구분하기 위함"""
    pass


def get_stock(page, goods_no):
    """{'name', 'soldout'(상품 전체), 'options'} 를 돌려준다."""
    open_page(page, detail_url(goods_no))
    name = get_name(page)
    res = read_options(page)

    if res is None:  # 옵션 없는 상품: 구매 버튼으로 판단
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


def stock_label(so):
    return "확인 못함" if so is None else ("🔴 품절" if so else "🟢 판매중")


def core(d):
    """상품명, 오류 기록은 바뀌어도 '변화'로 치지 않으려고 비교에서 뺀다."""
    return {k: v for k, v in (d or {}).items() if k not in ("name", "error")}


def main():
    if not GOODS:
        sys.exit("상품번호가 없습니다.")
    rank_pages = parse_rank_urls()
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    log(f"시작: 상품 {len(GOODS)}개, 랭킹 {len(rank_pages)}개")

    rankings, stock = {}, {}
    rank_errors = []        # 랭킹 실패, 중단 사유 (매번 표시)
    goods_errors = {}       # 상품별 재고 실패 {상품번호: 메시지} (처음 한 번만 표시)
    consec_fail = 0
    stopped = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, locale="ko-KR").new_page()
        try:
            for name, url in rank_pages:
                if time.time() - T0 > TIME_LIMIT:
                    stopped = "시간 제한"
                    break
                try:
                    rankings[name] = fetch_ranking(page, url)
                    consec_fail = 0
                    log(f"랭킹 '{name}' 완료 ({len(rankings[name])}개 상품 보임)")
                except Exception as e:
                    consec_fail += 1
                    rank_errors.append(f"랭킹 '{name}' 실패: {str(e)[:100]}")
                    log(f"랭킹 '{name}' 실패: {str(e)[:100]}")
                time.sleep(random.uniform(0.5, 1.2))

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
                    consec_fail = 0   # 페이지는 정상적으로 열렸으므로 차단 아님
                    goods_errors[g] = str(e)[:100]
                    log(f"[{i}/{len(GOODS)}] {g} 판단 불가: {str(e)[:100]}")
                except Exception as e:
                    consec_fail += 1
                    goods_errors[g] = str(e)[:100]
                    log(f"[{i}/{len(GOODS)}] {g} 실패: {str(e)[:100]}")
                time.sleep(random.uniform(0.5, 1.2))
        finally:
            browser.close()

    if stopped:
        rank_errors.append(f"중단: {stopped}")
        log(f"중단: {stopped}")

    # 확인에 실패한 항목은 이전 값 유지(가짜 변화 알림 방지)
    now = {}
    for g in GOODS:
        old = prev.get(g, {})
        ranks = {}
        for name, _ in rank_pages:
            if name in rankings:
                lst = rankings[name]
                ranks[name] = lst.index(g) + 1 if g in lst else None
            else:
                ranks[name] = old.get("ranks", {}).get(name)
        if g in stock:
            s = stock[g]
            soldout, options, pname = s["soldout"], s["options"], s["name"] or old.get("name", "")
            err = None                      # 정상 확인 → 오류 기록 삭제
        else:
            soldout, options, pname = old.get("soldout"), old.get("options", {}), old.get("name", "")
            err = goods_errors.get(g, old.get("error"))
        now[g] = {"name": pname, "ranks": ranks, "soldout": soldout, "options": options, "error": err}

    STATE_FILE.write_text(json.dumps(now, ensure_ascii=False))

    # 상품 오류는 '이번에 새로 생긴 것'만 표시 (지난번과 같은 오류면 생략)
    new_goods_errors = []
    for g, msg in goods_errors.items():
        if prev.get(g, {}).get("error") == msg:
            continue
        label = now[g].get("name") or g
        new_goods_errors.append(f"{label} ({g}) 재고 실패: {msg}")
    errors = rank_errors + new_goods_errors

    ts = datetime.now().strftime("%m/%d %H:%M")
    blocks = []
    for g, cur in now.items():
        changed = core(prev.get(g)) != core(cur)
        if not (changed or ALWAYS):
            continue
        tag = "⚠️ 변화 " if changed and g in prev else ""
        title = f"{cur['name']} ({g})" if cur.get("name") else g
        lines = [f"{tag}{title}"]
        for name, r in cur["ranks"].items():
            lines.append(f"  · {name} 랭킹: {f'{r}위' if r else '권 밖'}")
        opts = cur.get("options") or {}
        if opts:
            sold = sum(1 for v in opts.values() if v)
            lines.append(f"  · 재고: 옵션 {len(opts)}개 중 {sold}개 품절 ({stock_label(cur['soldout'])})")
            old_opts = prev.get(g, {}).get("options") or {}
            for oname, v in opts.items():
                mark = ""
                if oname in old_opts and old_opts[oname] != v:
                    mark = f" ← 변화 (이전: {'품절' if old_opts[oname] else '판매중'})"
                lines.append(f"      - {oname}: {stock_label(v)}{mark}")
        else:
            lines.append(f"  · 재고: {stock_label(cur['soldout'])}")
        lines.append(f"  {detail_url(g)}")
        blocks.append("\n".join(lines))

    if errors:
        shown = errors[:10]
        extra = f"\n...외 {len(errors) - 10}건" if len(errors) > 10 else ""
        blocks.append("❗ 확인 실패/중단 (이전 값 유지)\n" + "\n".join(shown) + extra)

    if blocks:
        send_slack(f"[올리브영 {ts}] 상품 {len(GOODS)}개 확인", blocks)
    else:
        checked = len(stock)
        post_slack(f"✅ [올리브영 {ts}] 상품 {len(GOODS)}개 체크했으나 변경점이 없습니다. (재고 확인 {checked}개, 랭킹 {len(rankings)}개)")
        log("변화 없음 → 슬랙 알림 전송")
    log("끝")


if __name__ == "__main__":
    main()
