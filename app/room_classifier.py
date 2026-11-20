"""Classify sale items, not every piece of text on the room page."""
import re
from collections import defaultdict

PARSER_VERSION = "room-items-4"

_PROMOTION = re.compile(r"쿠폰|프로모션|이벤트|할인|혜택|특가")
_PROMO_PREFIX = re.compile(r"^\[([^\[\]]+)\]\s*")
_NUMBERED_SLOT = re.compile(r"(?:(?:사이트|객실|글램핑|카라반|텐트|캐빈)[-_]?\d+|[a-z가-힣]+[-_]\d+|\d+(?:번|호|동))$")
_BOOKING_PRODUCT = re.compile(r"product:(/booking/\d+/bizes/\d+)/items/\d+")


def normalize(value):
    return re.sub(r"\s+|\u200b", "", str(value or "")).casefold()


def item_key(card):
    return card.get("itemKey") or "name:" + normalize(card.get("title"))


def classify(card, rules=None):
    title = str(card.get("title", "")).strip()
    key = normalize(title)
    rule = (rules or {}).get(item_key(card), {})
    if rule.get("mode") == "exclude":
        return "excluded", "사용자가 제외한 상품"
    if not key:
        return "review", "상품명 확인 불가"
    if rule.get("mode") == "include":
        return "room", "사용자가 객실로 확인한 상품"
    if re.search(r"공지|필독|이용안내|예약안내|안내사항|이용규칙|이용수칙|유의사항|주의사항|환불규정|배치도|오시는길|문의|상담|알림받기|더보기|\*{2,}", key):
        return "excluded", "공지·안내·문의·혜택 상품"
    if re.fullmatch(r"예약(?:하기|가능)?|상세보기|펼쳐보기|접기|날짜선택|사진(?:보기)?|리뷰(?:보기)?|공유|찜하기", key):
        return "excluded", "객실이 아닌 화면 버튼"
    if re.search(r"데이유즈|dayuse|당일(?:치기|이용|캠크닉|피크닉)|당일권|숙박불가|비숙박|캠크닉|피크닉|시간이용|이용권|입장권|오전권|오후권|바베큐장|바비큐장|캠핑식당", key):
        return "excluded", "숙박 객실이 아닌 당일·시간제 이용 상품"
    if re.fullmatch(r"(?:단독사용|개별|공용|추가)*(?:샤워실|화장실|개수대|주차권|주차비|장작|숯|바비큐세트|바베큐세트)(?:이용|대여|추가|만)?", key):
        return "excluded", "객실이 아닌 부대시설·추가 상품"
    if _PROMOTION.search(key):
        # A promotion on a numbered room is not a separate coupon product.
        named_unit = re.search(r"(?:사이트|객실|글램핑|카라반|텐트|동|호)\s*[-_]?\s*\d+|[a-z가-힣]+[-_]\d+|\d+\s*(?:번|호|동)(?:\b|$)", title, re.I)
        if not (named_unit and card.get("unitEvidence") and card.get("overnightEvidence")
                and str(card.get("itemKey", "")).startswith("product:")):
            if named_unit or (card.get("overnightEvidence") and re.search(r"사이트|객실|글램핑|카라반|펜션", title)):
                return "review", "할인 객실인지 독립 혜택 상품인지 확인 필요"
            return "excluded", "독립 이벤트·쿠폰·혜택 안내"
    if re.match(r"^(?:캠핑사이트|글램핑|카라반|펜션|방갈로|객실)?[,，]?(?:기준|최대)\d+인", key):
        return "excluded", "상품명이 아닌 객실 사양 설명"
    if re.search(r"공사중|운영중단|운영중지|준비중|오픈예정|전체이용|전체대관|[a-z]~[a-z]구역|구역\d+박예약", key):
        return "review", "공사·묶음·대체 상품 여부 확인 필요"
    text = str(card.get("text", ""))
    if re.search(r"(?:^|\n)\s*(?:데이유즈|day\s*use)\s*(?:\n|$)|\d+\s*시간\s*이용", text, re.I):
        return "excluded", "시간 단위로 이용하는 비숙박 상품"
    if card.get("schemaVersion") == 3 and not card.get("unitEvidence"):
        return "review", "개별 판매 항목 근거 부족"
    if card.get("schemaVersion") == 3 and not card.get("overnightEvidence"):
        return "review", "숙박·캠핑사이트 근거 부족"
    return "room", "개별 객실 상품 확인"


def _identity(card, title):
    product = _BOOKING_PRODUCT.fullmatch(item_key(card))
    if not (product and card.get("schemaVersion") == 3 and card.get("unitEvidence")
            and card.get("overnightEvidence")):
        return None
    # The listing can truncate parenthesized amenities; require the whole numbered
    # room name, not just a number or a fuzzy match.
    if card.get("stayMembers") or re.search(r"\d+\s*박", title) or re.search(r"최소\s*[2-9]\d*\s*박|최소\s*1\d+\s*박", str(card.get("text", ""))):
        return None
    slot, _, description = title.partition("(")
    slot = normalize(slot)
    if not _NUMBERED_SLOT.search(slot):
        return None
    return (product.group(1), slot), normalize(description).rstrip(")")


def _promotion_exclusions(cards):
    standard = defaultdict(list)
    glamping_storefronts = set()
    for card in cards:
        if card["decision"] != "room" or _PROMOTION.search(card["title"]):
            continue
        identity = _identity(card, card["title"])
        if identity is None:
            continue
        unit, description = identity
        standard[unit].append((card, description))
        if "글램핑" in unit[1]:
            glamping_storefronts.add(unit[0])
    excluded = {}
    for card in cards:
        if card["mode"] != "auto" or card["decision"] == "excluded":
            continue
        title = card["title"].strip()
        prefix = _PROMO_PREFIX.match(title)
        if prefix and _PROMOTION.search(prefix[1]) and not re.search(r"\d+\s*박", prefix[1]):
            identity = _identity(card, title[prefix.end():])
            if identity is not None:
                unit, description = identity
                originals = standard[unit]
                if len(originals) == 1:
                    original, detail = originals[0]
                    same_detail = description.startswith(detail) or detail.startswith(description)
                    if same_detail:
                        # A closed promotional offer is not a sold room. If only
                        # the promotion appears open, don't assert occupancy either.
                        conflict = ((card.get("availabilityEvidence") is True or card.get("availabilityState") == "available") and
                                    (original.get("reserved") is True or original.get("availabilityState") == "closed"))
                        excluded[card["itemKey"]] = (
                            ("review", "기존 객실과 할인 상품의 예약 상태가 달라 확인 필요") if conflict else
                            ("excluded", "같은 번호의 기존 객실에 붙은 할인 상품 · 중복 제외")
                        )
                        continue
        key = normalize(title)
        product = _BOOKING_PRODUCT.fullmatch(card["itemKey"])
        if (card["decision"] == "review" and product and product.group(1) in glamping_storefronts
                and "수험생" in key and "할인" in key and "수험표필수" in key and "글램핑" in key
                and not _NUMBERED_SLOT.search(key.split("(", 1)[0])):
            excluded[card["itemKey"]] = "excluded", "동 번호가 없는 수험표 필수 할인 상품 · 객실 수에서 제외"
    return excluded


def partition(snapshot, rules=None):
    cards = []
    seen = set()
    for raw in snapshot.get("roomCards", []):
        key = item_key(raw)
        if key in seen:
            continue
        seen.add(key)
        decision, reason = classify(raw, rules)
        cards.append({**raw, "title": str(raw.get("title", "")), "itemKey": key,
                      "decision": decision, "reason": reason,
                      "mode": (rules or {}).get(key, {}).get("mode", "auto")})
    decisions = _promotion_exclusions(cards)
    result = {"room": [], "excluded": [], "review": []}
    for card in cards:
        if card["itemKey"] in decisions:
            card["decision"], card["reason"] = decisions[card["itemKey"]]
        result[card["decision"]].append(card)
    return result


def filtered_snapshot(snapshot, rules=None):
    groups = partition(snapshot, rules)
    # Generic page-text matching would reintroduce notices and unrelated room mentions.
    return {**snapshot, "roomCards": groups["room"], "candidates": [], "strictRoomCards": True}, groups
