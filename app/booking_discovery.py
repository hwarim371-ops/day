"""Read public place links; never infer booking support from a search hit."""
import re
from urllib.parse import parse_qs, quote, urlparse

import naver_room_booking_upgraded as core
from room_classifier import partition

BLOCK_WORDS = ("서비스 이용이 제한", "접근이 제한", "자동입력 방지", "보안문자를 입력", "비정상적인 접근")


def check_access(text):
    if any(word in text for word in BLOCK_WORDS):
        raise RuntimeError("네이버가 접근을 제한했습니다. 자동 재시도는 중단했습니다. 잠시 후 직접 네이버 접속 상태를 확인해 주세요.")


def place_id(value):
    value = value.strip()
    if re.fullmatch(r"\d{5,15}", value):
        return value
    url = urlparse(value)
    if url.scheme != "https" or url.hostname not in {"m.place.naver.com", "pcmap.place.naver.com", "map.naver.com"}:
        return ""
    match = re.search(r"/(?:accommodation|place|camping)/(\d{5,15})(?:/|$)", url.path)
    return match.group(1) if match else ""


def search_places(query, limit, progress, stop):
    if not query.strip() or len(query) > 100:
        raise ValueError("지역과 검색어를 입력해 주세요.")
    progress("네이버 지역 검색: " + query)
    with core.import_playwright()[0]() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="ko-KR", viewport={"width": 420, "height": 900})
            page.goto("https://m.place.naver.com/place/list?query=" + quote(query), wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2500)
            check_access(page.inner_text("body"))
            for _ in range(2):
                if stop.is_set():
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            links = page.eval_on_selector_all("a[href]", "els => els.map(a=>({url:a.href,name:(a.innerText||a.getAttribute('aria-label')||'').trim()}))")
            found = {}
            for item in links:
                company = place_id(item["url"])
                name = item["name"].splitlines()[0][:100] if item["name"] else ""
                if company and name and company not in found:
                    found[company] = {"company_id": company, "name": name, "url": item["url"], "region": query}
            if not found:
                raise RuntimeError("공개 검색 화면에서 업체 링크를 찾지 못했습니다. '링크로 추가'에 네이버 플레이스 링크를 입력할 수 있습니다.")
            return list(found.values())[:limit]
        finally:
            browser.close()


def verify_place(collector, candidate):
    url, snapshot = collector.get_snapshot(candidate["company_id"])
    page = collector._page
    check_access(page.inner_text("body"))
    rooms = core.extract_detected_rooms(snapshot, collector.status_words)
    uncertain = partition(snapshot)["review"]
    links = page.eval_on_selector_all("a[href]", "els=>els.map(a=>a.href)")
    booking = next((link for link in links if urlparse(link).hostname in {"booking.naver.com", "m.booking.naver.com"}), "")
    title = page.title().split(":")[0].strip()
    return {**candidate, "name": candidate.get("name") or title, "url": url, "rooms": rooms,
            "booking_url": booking, "verified": bool(booking and rooms and not uncertain),
            "status": "상품 판별 필요" if uncertain else "예약 링크·객실 확인" if booking and rooms else "예약 지원 확인 필요"}
