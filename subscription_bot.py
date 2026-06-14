# -*- coding: utf-8 -*-
"""
청약 자동 알림 봇 - GitHub Actions용
공공데이터포털 청약홈 API → 조건 필터링 → 텔레그램 전송
"""
import json
import os
import requests
import traceback
from datetime import datetime, timedelta

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_KEY          = os.environ["PUBLIC_DATA_API_KEY"]

FILTER = {
    "지역": ["서울", "인천", "경기"],
    "최대_분양가_만원": 75000,
    "최소_세대수": 500,
}

BASE_URL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"


def fetch_announcements() -> list:
    """모집공고 목록 조회"""
    url = f"{BASE_URL}/getAPTLttotPblancDetail?serviceKey={API_KEY}&numOfRows=100&pageNo=1"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", [])
    return items if isinstance(items, list) else []


def filter_items(items: list) -> list:
    """필터 조건 적용"""
    allowed_regions = FILTER["지역"]
    min_units       = FILTER["최소_세대수"]
    result = []

    for item in items:
        if item.get("HOUSE_DTL_SECD_NM") != "민영":
            continue

        region = item.get("SUBSCRPT_AREA_CODE_NM", "")
        if not any(r in region for r in allowed_regions):
            continue

        try:
            units = int(item.get("TOT_SUPLY_HSHLDCO", 0))
        except (ValueError, TypeError):
            units = 0
        if units < min_units:
            continue

        result.append(item)

    return result


def build_report(items: list) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일")
    lines = [f"📅 *청약 일일 리포트 ({today})*\n"]

    if not items:
        lines.append("오늘은 조건에 맞는 청약 단지가 없습니다.")
        return "\n".join(lines)

    lines.append("🔥 *조건 충족 단지*\n")
    for item in items:
        name        = item.get("HOUSE_NM", "알 수 없음")
        address     = item.get("HSSPLY_ADRES", "-")
        units       = item.get("TOT_SUPLY_HSHLDCO", "-")
        rcpt_start  = item.get("RCEPT_BGNDE", "-")
        rcpt_end    = item.get("RCEPT_ENDDE", "-")
        announce    = item.get("RCRIT_PBLANC_DE", "-")
        winner_date = item.get("PRZWNER_PRESNATN_DE", "-")
        url         = item.get("PBLANC_URL", "")

        lines.append(f"🏠 *{name}*")
        lines.append(f"  • 위치: {address}")
        lines.append(f"  • 총 세대수: {units}세대")
        lines.append(f"  • 모집공고일: {announce}")
        lines.append(f"  • 청약 접수: {rcpt_start} ~ {rcpt_end}")
        lines.append(f"  • 당첨자 발표: {winner_date}")
        if url:
            lines.append(f"  • 상세: {url}")
        lines.append("")

    lines.append(f"📌 총 {len(items)}개 단지 조건 충족")
    return "\n".join(lines)


def send_telegram(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    r.raise_for_status()
    print("[OK] Telegram sent")


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 청약 리포트 시작...")
    try:
        items = fetch_announcements()
        print(f"[INFO] 조회된 단지 수: {len(items)}")

        filtered = filter_items(items)
        print(f"[INFO] 필터 통과 단지 수: {len(filtered)}")

        report = build_report(filtered)
        print(report)
        send_telegram(report)

    except Exception as e:
        traceback.print_exc()
        msg = f"⚠️ 청약 봇 오류 ({datetime.now().strftime('%Y-%m-%d')})\n{str(e)}"
        try:
            send_telegram(msg)
        except Exception:
            pass


if __name__ == "__main__":
    main()
