"""
항공권 가격 모니터링 - GitHub Actions용 (Playwright 기반)
"""
import asyncio
import json
import os
import re
import random
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

# ── 설정 ────────────────────────────────────────────────────────────────────
CONFIG     = json.loads(Path("config.json").read_text(encoding="utf-8"))
PRICES_FILE = Path("prices.json")
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]

DEP_DASH = CONFIG["dates"]["departure"]   # 2025-07-22
RET_DASH = CONFIG["dates"]["return"]      # 2025-07-26
DEP      = DEP_DASH.replace("-", "")      # 20250722
RET      = RET_DASH.replace("-", "")      # 20250726

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

DEST_EMOJI = {
    "몰디브": "🏝️", "발리": "🌴", "몰타": "🏰",
    "카자흐스탄": "🏔️", "몽골": "🐎",
}

# ── 텔레그램 ─────────────────────────────────────────────────────────────────
def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"  [텔레그램] 전송 완료")
    except Exception as e:
        print(f"  [텔레그램] 실패: {e}")

def price_alert(name: str, route: str, cur: int, prev: int, source: str,
                url: str = "", result: dict = None):
    diff = prev - cur
    pct  = diff / prev * 100
    emoji = DEST_EMOJI.get(name, "✈️")
    link  = f'\n🔗 <a href="{url}">바로가기</a>' if url else ""

    # 항공편 상세 (카약에서 가져온 경우)
    detail = ""
    if result and result.get("top_flights"):
        flights = result["top_flights"]
        lines = ["\n✈️ <b>최저가 항공편</b>"]
        for i, f in enumerate(flights[:3], 1):
            stop_label = f["stops"] if "경유" in f.get("stops","") or "직항" in f.get("stops","") else ""
            lines.append(
                f"{i}. <b>{f['airline']}</b>  {f['price']:,}원\n"
                f"   ⏱ {f['duration']}  {stop_label}\n"
                f"   💺 {f['cabin']}"
            )
        detail = "\n".join(lines)
    elif result and result.get("airline"):
        detail = (
            f"\n\n✈️ <b>{result['airline']}</b>\n"
            f"⏱ {result.get('duration','')}  {result.get('stops','')}\n"
            f"💺 {result.get('cabin','')}"
        )

    tg(
        f"🚨 <b>항공권 가격 하락!</b>\n\n"
        f"{emoji} <b>{name}</b>  {route}\n"
        f"📅 {DEP_DASH} 출발 / {RET_DASH} 귀국\n\n"
        f"💰 현재 최저가: <b>{cur:,}원</b>\n"
        f"📉 이전가: {prev:,}원  →  -{diff:,}원 ({pct:.1f}% 하락)\n"
        f"📡 출처: {source}"
        f"{detail}"
        f"{link}"
    )

# ── 가격 이력 ─────────────────────────────────────────────────────────────────
DATE_KEY = f"{DEP_DASH}~{RET_DASH}"   # ex) "2026-07-22~2026-07-26"

def load_prices() -> dict:
    if not PRICES_FILE.exists():
        return {}
    data = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    # 날짜가 바뀌었으면 이전 가격 전부 초기화
    if data.get("__date__") != DATE_KEY:
        print(f"[INFO] 날짜 변경 감지 ({data.get('__date__')} → {DATE_KEY}), 가격 초기화")
        return {}
    return data

def save_prices(data: dict):
    data["__date__"] = DATE_KEY
    PRICES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Playwright 공통 브라우저 컨텍스트 ──────────────────────────────────────────
async def new_page(playwright) -> tuple:
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    ctx = await browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="ko-KR",
        viewport={"width": 1280, "height": 800},
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return browser, await ctx.new_page()

def extract_prices(text: str) -> list[int]:
    """텍스트에서 항공권 가격 범위(50,000~5,000,000원)의 숫자 추출"""
    return [
        int(m.replace(",", ""))
        for m in re.findall(r"[\d,]{6,9}", text)
        if 50_000 < int(m.replace(",", "")) < 5_000_000
    ]

# ── 스크래퍼 ─────────────────────────────────────────────────────────────────

async def scrape_naver(pw, frm: str, to: str) -> dict | None:
    url = (
        f"https://flight.naver.com/flights/international/"
        f"{frm}-{to}-{DEP}/{to}-{frm}-{RET}?adult=1"
    )
    browser, page = await new_page(pw)
    price_from_api = []

    async def on_response(resp):
        # 네이버 항공 검색 결과 API 인터셉트
        if "airline-api.naver.com" in resp.url or "techminds.pstatic.net/flight" in resp.url:
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype:
                    body = await resp.json()
                    text = json.dumps(body)
                    nums = [int(n) for n in re.findall(r'\b(\d{5,7})\b', text)
                            if 50_000 < int(n) < 5_000_000]
                    if nums:
                        price_from_api.extend(nums)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(url, timeout=40_000, wait_until="domcontentloaded")
        # 항공권 결과 로딩 대기 (최대 30초)
        for _ in range(6):
            await asyncio.sleep(5)
            if price_from_api:
                break

        if price_from_api:
            valid = [p for p in price_from_api if 80_000 < p < 5_000_000]
            if valid:
                return {"price": min(valid), "url": url}

        # 폴백: DOM에서 '원' 단위 텍스트 추출
        content = await page.content()
        won_prices = [int(m.replace(",", "")) for m in re.findall(r"([\d,]{5,9})원", content)
                      if 80_000 < int(m.replace(",", "")) < 5_000_000]
        if won_prices:
            return {"price": min(won_prices), "url": url}

    except Exception as e:
        print(f"    [네이버] {e}")
    finally:
        await browser.close()
    return None


async def scrape_hanatour(pw, to: str, name: str) -> dict | None:
    keywords = {
        "MLE": ["몰디브"], "DPS": ["발리"], "MLA": ["몰타"],
        "ALA": ["카자흐", "알마티"], "TSE": ["카자흐", "아스타나"],
        "ULN": ["몽골", "울란"],
    }.get(to, [name])

    url = "https://www.hanatour.com/lastminute/air-special.do"
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=30_000, wait_until="networkidle")
        await asyncio.sleep(2)
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")

        for item in soup.find_all(True, class_=re.compile(r"item|product|card|list", re.I)):
            text = item.get_text()
            if any(k in text for k in keywords):
                prices = extract_prices(text)
                if prices:
                    a = item.find("a", href=True)
                    link = ("https://www.hanatour.com" + a["href"]) if a else url
                    return {"price": min(prices), "url": link}
    except Exception as e:
        print(f"    [하나투어] {e}")
    finally:
        await browser.close()
    return None


async def scrape_modutour(pw, to: str, name: str) -> dict | None:
    keywords = {
        "MLE": ["몰디브"], "DPS": ["발리"], "MLA": ["몰타"],
        "ALA": ["카자흐", "알마티"], "ULN": ["몽골"],
    }.get(to, [name])

    url = "https://www.modetour.com/Plan/AirTour/AirTourLastMinute"
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=30_000, wait_until="networkidle")
        await asyncio.sleep(2)
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")

        for item in soup.find_all(True, class_=re.compile(r"item|product|card|deal|list", re.I)):
            text = item.get_text()
            if any(k in text for k in keywords):
                prices = extract_prices(text)
                if prices:
                    a = item.find("a", href=True)
                    link = (
                        "https://www.modetour.com" + a["href"]
                        if a and a["href"].startswith("/") else url
                    )
                    return {"price": min(prices), "url": link}
    except Exception as e:
        print(f"    [모두투어] {e}")
    finally:
        await browser.close()
    return None


async def scrape_skyscanner(pw, frm: str, to: str) -> dict | None:
    dep_fmt = DEP[2:]  # 250722
    ret_fmt = RET[2:]
    url = (
        f"https://www.skyscanner.co.kr/transport/flights/"
        f"{frm.lower()}/{to.lower()}/{dep_fmt}/{ret_fmt}/?adults=1&currency=KRW"
    )
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4, 7))
        try:
            await page.wait_for_selector(
                '[class*="Price"], [data-testid*="price"], [class*="price"]',
                timeout=20_000
            )
        except Exception:
            pass

        content = await page.content()
        # 원화 가격 추출
        won_prices = [
            int(m.replace(",", ""))
            for m in re.findall(r"₩\s*([\d,]+)", content)
            if 50_000 < int(m.replace(",", "")) < 5_000_000
        ]
        if not won_prices:
            won_prices = extract_prices(content)
        if won_prices:
            return {"price": min(won_prices), "url": url}
    except Exception as e:
        print(f"    [스카이스캐너] {e}")
    finally:
        await browser.close()
    return None


def parse_kayak_flights(body_text: str) -> list[dict]:
    """
    카약 body 텍스트에서 항공편 파싱.
    패턴: [경유정보] [경유공항] [소요시간] [항공사] [가격원] [좌석] 선택
    """
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    results = []
    for i, line in enumerate(lines):
        m = re.match(r"^([\d,]+)원$", line)
        if not m:
            continue
        price = int(m.group(1).replace(",", ""))
        if not (80_000 < price < 5_000_000):
            continue
        # 바로 위 줄 = 항공사, 2줄 위 = 소요시간, 3줄 위 = 경유정보
        airline  = lines[i-1] if i >= 1 else ""
        duration = lines[i-2] if i >= 2 else ""
        stops    = lines[i-3] if i >= 3 else ""
        # 다음 줄 = 좌석 등급
        cabin    = lines[i+1] if i+1 < len(lines) else ""

        # 소요시간 패턴 검증 (N시간 M분)
        if not re.search(r'\d+시간', duration):
            continue
        # 항공사 줄이 너무 길거나 이상하면 스킵
        if len(airline) > 50 or not airline:
            continue

        results.append({
            "price": price, "airline": airline,
            "duration": duration, "stops": stops, "cabin": cabin,
        })

    # 가격순 정렬, 중복 제거
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x["price"]):
        if r["price"] not in seen:
            seen.add(r["price"])
            unique.append(r)
    return unique[:5]  # 최저가 5개


async def scrape_kayak(pw, frm: str, to: str) -> dict | None:
    url = (
        f"https://www.kayak.co.kr/flights/{frm}-{to}/"
        f"{DEP_DASH}/{RET_DASH}?adults=1&sort=price_a"
    )
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector('[class*="esgW-price-holder"]', timeout=20_000)
        except Exception:
            await asyncio.sleep(random.uniform(6, 10))

        body_text = await page.evaluate("() => document.body.innerText")
        flights = parse_kayak_flights(body_text)

        if flights:
            best = flights[0]
            return {
                "price":    best["price"],
                "url":      url,
                "airline":  best["airline"],
                "duration": best["duration"],
                "stops":    best["stops"],
                "cabin":    best["cabin"],
                "top_flights": flights,  # 상위 5개 전달
            }
    except Exception as e:
        print(f"    [카약] {e}")
    finally:
        await browser.close()
    return None


async def scrape_google_flights(pw, frm: str, to: str) -> dict | None:
    url = (
        f"https://www.google.com/travel/flights/search?"
        f"tfs=CBwQAhoeEgoyMDI1LTA3LTIyagcIARIDSUNOcgcIARIDTUxFGh4SCjIwMjUtMDctMjZqBwgBEgNNTEVyBwgBEgNJQ04qAggB"
    )
    # 간단한 URL 방식
    url = (
        f"https://www.google.com/travel/flights?q="
        f"flights+from+{frm}+to+{to}+on+{DEP_DASH}+returning+{RET_DASH}"
    )
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(5, 9))

        content = await page.content()
        # 구글 항공권 원화 가격
        won_prices = [
            int(m.replace(",", ""))
            for m in re.findall(r"₩([\d,]+)", content)
            if 50_000 < int(m.replace(",", "")) < 5_000_000
        ]
        # aria-label에서 추출
        labels = re.findall(r'aria-label="[^"]*?([\d,]+)\s*원[^"]*"', content)
        for lbl in labels:
            p = int(lbl.replace(",", ""))
            if 50_000 < p < 5_000_000:
                won_prices.append(p)

        if won_prices:
            return {"price": min(won_prices), "url": url}
    except Exception as e:
        print(f"    [구글플라이트] {e}")
    finally:
        await browser.close()
    return None


async def scrape_koreanair(pw, frm: str, to: str) -> dict | None:
    url = (
        f"https://www.koreanair.com/booking/flight-search?"
        f"departureCity={frm}&arrivalCity={to}"
        f"&departureDate={DEP_DASH}&returnDate={RET_DASH}&adults=1&tripType=RT"
    )
    browser, page = await new_page(pw)
    try:
        await page.goto(url, timeout=40_000, wait_until="domcontentloaded")
        await asyncio.sleep(4)
        try:
            await page.wait_for_selector('[class*="price"], [class*="fare"]', timeout=20_000)
        except Exception:
            pass

        content = await page.content()
        prices = extract_prices(content)
        if prices:
            return {"price": min(prices), "url": url, "airline": "대한항공"}
    except Exception as e:
        print(f"    [대한항공] {e}")
    finally:
        await browser.close()
    return None


# ── 메인 ────────────────────────────────────────────────────────────────────
async def check_route(pw, route: dict, prices: dict, threshold: int):
    frm, to, name = route["from"], route["to"], route["name"]
    key = f"{frm}-{to}"
    print(f"\n[{name}] {key}")

    scrapers = {
        "네이버항공권":  lambda: scrape_naver(pw, frm, to),
        "하나투어":      lambda: scrape_hanatour(pw, to, name),
        "모두투어":      lambda: scrape_modutour(pw, to, name),
        "스카이스캐너":  lambda: scrape_skyscanner(pw, frm, to),
        "카약":          lambda: scrape_kayak(pw, frm, to),
        "구글플라이트":  lambda: scrape_google_flights(pw, frm, to),
        "대한항공":      lambda: scrape_koreanair(pw, frm, to),
    }

    for source, fn in scrapers.items():
        try:
            result = await fn()
            if not result:
                print(f"  [{source}] 결과 없음")
                continue

            price    = result["price"]
            url      = result.get("url", "")
            store_key = f"{key}-{source}"
            prev     = prices.get(store_key)

            if prev:
                diff = prev - price
                print(f"  [{source}] {price:,}원  (이전: {prev:,}원, 변동: {'+' if diff<0 else ''}{-diff:,}원)")
                if price < prev - threshold:
                    price_alert(name, key, price, prev, source, url, result)
            else:
                print(f"  [{source}] {price:,}원  (첫 확인)")

            prices[store_key] = price

        except Exception as e:
            print(f"  [{source}] 오류: {e}")

        await asyncio.sleep(random.uniform(1, 2))


async def main():
    prices    = load_prices()
    threshold = CONFIG.get("price_drop_threshold", 5000)
    now       = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== 항공권 모니터링 시작 [{now}] ===")
    print(f"노선 수: {len(CONFIG['routes'])}개  |  임계값: {threshold:,}원 이상 하락 시 알림")

    async with async_playwright() as pw:
        for route in CONFIG["routes"]:
            await check_route(pw, route, prices, threshold)

    save_prices(prices)
    print(f"\n=== 완료. prices.json 저장됨 ===")


if __name__ == "__main__":
    asyncio.run(main())
