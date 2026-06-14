"""
항공권 가격 모니터링 - GitHub Actions용
requests 기반 (Playwright 없음)
"""
import json
import os
import re
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 설정 ────────────────────────────────────────────────
CONFIG = json.loads(Path("config.json").read_text(encoding="utf-8"))
PRICES_FILE = Path("prices.json")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

DEP  = CONFIG["dates"]["departure"].replace("-", "")   # 20250722
RET  = CONFIG["dates"]["return"].replace("-", "")      # 20250726
DEP_DASH = CONFIG["dates"]["departure"]                # 2025-07-22
RET_DASH = CONFIG["dates"]["return"]                   # 2025-07-26

# ── 텔레그램 ─────────────────────────────────────────────
def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {e}")

def price_alert(name: str, route: str, cur: int, prev: int, source: str, url: str = ""):
    diff = prev - cur
    pct  = diff / prev * 100
    emoji = {"몰디브":"🏝️","발리":"🌴","몰타":"🏰","카자흐스탄":"🏔️","몽골":"🐎"}.get(name, "✈️")
    link = f'\n🔗 <a href="{url}">바로가기</a>' if url else ""
    tg(
        f"✈️ <b>항공권 가격 하락!</b>\n\n"
        f"{emoji} <b>{name}</b> ({route})\n"
        f"📅 {DEP_DASH} 출발 / {RET_DASH} 귀국\n\n"
        f"💰 현재: <b>{cur:,}원</b>\n"
        f"📉 이전: {prev:,}원  →  -{diff:,}원 ({pct:.1f}% 하락)\n\n"
        f"📡 출처: {source}{link}"
    )

# ── 가격 이력 ─────────────────────────────────────────────
def load_prices() -> dict:
    return json.loads(PRICES_FILE.read_text(encoding="utf-8")) if PRICES_FILE.exists() else {}

def save_prices(data: dict):
    PRICES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ── 스크래퍼들 ────────────────────────────────────────────

def scrape_naver(frm: str, to: str) -> dict | None:
    """네이버 항공권 내부 API"""
    # 네이버 항공권 모바일 API (JSON 응답)
    url = (
        f"https://flight.naver.com/flights/international/"
        f"{frm}-{to}-{DEP}/{to}-{frm}-{RET}?adult=1"
    )
    # 네이버 항공 검색 API
    api_url = (
        "https://airline-api.naver.com/graphql"
    )
    payload = {
        "operationName": "getInternationalList",
        "variables": {
            "isDirect": False,
            "adult": 1, "child": 0, "infant": 0,
            "fareType": "Y",
            "itinerary": [
                {"departureAirport": frm, "arrivalAirport": to,  "departureDate": DEP_DASH},
                {"departureAirport": to,  "arrivalAirport": frm, "departureDate": RET_DASH},
            ]
        },
        "query": """
        query getInternationalList($itinerary:[ItineraryInput]!,$adult:Int!,$child:Int!,$infant:Int!,$fareType:String!,$isDirect:Boolean){
          internationalList(itinerary:$itinerary,adult:$adult,child:$child,infant:$infant,fareType:$fareType,isDirect:$isDirect){
            fare { price }
          }
        }
        """
    }
    try:
        r = requests.post(
            api_url,
            json=payload,
            headers={**HEADERS, "Referer": url, "Content-Type": "application/json"},
            timeout=20,
        )
        data = r.json()
        fares = data.get("data", {}).get("internationalList", {}).get("fare", [])
        prices = [f["price"] for f in fares if f.get("price", 0) > 10000]
        if prices:
            return {"price": min(prices), "url": url}
    except Exception as e:
        print(f"  [네이버] {e}")
    return None


def scrape_hanatour(to: str, name: str) -> dict | None:
    """하나투어 땡처리"""
    keywords = {
        "MLE": ["몰디브"], "DPS": ["발리"], "MLA": ["몰타"],
        "ALA": ["카자흐", "알마티"], "TSE": ["카자흐", "아스타나"],
        "ULN": ["몽골", "울란"],
    }.get(to, [name])
    try:
        url = "https://www.hanatour.com/lastminute/air-special.do"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.find_all(["li", "div"], class_=re.compile(r"item|product|list", re.I)):
            text = item.get_text()
            if any(k in text for k in keywords):
                m = re.search(r"([\d,]{5,})\s*원", text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    if 50_000 < price < 5_000_000:
                        a = item.find("a", href=True)
                        link = ("https://www.hanatour.com" + a["href"]) if a else url
                        return {"price": price, "url": link}
    except Exception as e:
        print(f"  [하나투어] {e}")
    return None


def scrape_modutour(to: str, name: str) -> dict | None:
    """모두투어 땡처리"""
    keywords = {
        "MLE": ["몰디브"], "DPS": ["발리"], "MLA": ["몰타"],
        "ALA": ["카자흐", "알마티"], "ULN": ["몽골"],
    }.get(to, [name])
    try:
        url = "https://www.modetour.com/Plan/AirTour/AirTourLastMinute"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.find_all(["li", "div"], class_=re.compile(r"item|product|list|deal", re.I)):
            text = item.get_text()
            if any(k in text for k in keywords):
                m = re.search(r"([\d,]{5,})\s*원", text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    if 50_000 < price < 5_000_000:
                        a = item.find("a", href=True)
                        link = ("https://www.modetour.com" + a["href"]) if (a and a["href"].startswith("/")) else url
                        return {"price": price, "url": link}
    except Exception as e:
        print(f"  [모두투어] {e}")
    return None


def scrape_skyscanner(frm: str, to: str) -> dict | None:
    """스카이스캐너 비공개 API"""
    dep_fmt = DEP[2:]   # 250722
    ret_fmt = RET[2:]
    url = f"https://www.skyscanner.co.kr/transport/flights/{frm.lower()}/{to.lower()}/{dep_fmt}/{ret_fmt}/?adults=1"
    try:
        # 스카이스캐너 내부 가격 API
        api = (
            f"https://www.skyscanner.net/g/conductor/v1/fps3/search/?country=KR&currency=KRW"
            f"&locale=ko-KR&originplace={frm}-sky&destinationplace={to}-sky"
            f"&outbounddate={DEP_DASH}&inbounddate={RET_DASH}&adults=1&children=0&infants=0&cabinclass=economy"
        )
        r = requests.get(api, headers={**HEADERS, "Referer": url}, timeout=20)
        data = r.json()
        quotes = data.get("Quotes", [])
        prices = [q["MinPrice"] for q in quotes if q.get("MinPrice", 0) > 10]
        if prices:
            won = int(min(prices))
            return {"price": won, "url": url}
    except Exception as e:
        print(f"  [스카이스캐너] {e}")
    return None


SCRAPERS = {
    "네이버항공권": lambda frm, to, name: scrape_naver(frm, to),
    "하나투어":    lambda frm, to, name: scrape_hanatour(to, name),
    "모두투어":    lambda frm, to, name: scrape_modutour(to, name),
    "스카이스캐너": lambda frm, to, name: scrape_skyscanner(frm, to),
}

# ── 메인 ─────────────────────────────────────────────────
def main():
    prices = load_prices()
    threshold = CONFIG.get("price_drop_threshold", 5000)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== 항공권 모니터링 시작 [{now}] ===")

    for route in CONFIG["routes"]:
        frm, to, name = route["from"], route["to"], route["name"]
        key = f"{frm}-{to}"
        print(f"\n[{name}] {key}")

        for source, fn in SCRAPERS.items():
            try:
                result = fn(frm, to, name)
                if not result:
                    print(f"  [{source}] 결과 없음")
                    continue

                price = result["price"]
                url   = result.get("url", "")
                store_key = f"{key}-{source}"
                prev  = prices.get(store_key)

                print(f"  [{source}] {price:,}원" + (f" (이전: {prev:,}원)" if prev else " (첫 확인)"))

                if prev and price < prev - threshold:
                    price_alert(name, key, price, prev, source, url)

                prices[store_key] = price
                time.sleep(random.uniform(1, 2))  # 요청 간격

            except Exception as e:
                print(f"  [{source}] 오류: {e}")

    save_prices(prices)
    print(f"\n=== 완료. prices.json 업데이트됨 ===")


if __name__ == "__main__":
    main()
