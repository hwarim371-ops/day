"""Conservative nightly list-price extraction from matched accommodation cards."""
import re

from room_classifier import classify

NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
PRICE_LINE = re.compile(
    rf"({NUMBER})\s*(?:원\s*)?(?:[~～–-]\s*({NUMBER})\s*)?원"
    r"(?:\s*(?:Npay|네이버페이)\s*\+?)?", re.I)


def nightly_max(card, *, confirmed_room=False):
    decision, reason = classify(card)
    missing_type = (confirmed_room and card.get("unitEvidence")
                    and reason == "숙박·캠핑사이트 근거 부족")
    if decision != "room" and not missing_type:
        return None
    text = str(card.get("text", ""))
    title = str(card.get("title", ""))
    # A two-night package is not a nightly tariff.
    nights = re.findall(r"최소\s*(\d+)\s*박|\((\d+)박\)", text + "\n" + title)
    if any(int(a or b) > 1 for a, b in nights):
        return None
    for line in text.splitlines():
        match = PRICE_LINE.fullmatch(line.strip())
        if match:
            values = [int(v.replace(",", "")) for v in match.groups() if v]
            return max(values) if min(values) > 0 else None
    return None


def only_price_changed(before, after):
    return (before.company_id, before.major, before.sheet_title, before.rooms) == (
        after.company_id, after.major, after.sheet_title, after.rooms)
