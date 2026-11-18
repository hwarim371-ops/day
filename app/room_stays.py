"""Resolve stay-length tariffs into one physical unit, with dated evidence."""
import copy
import re
from collections import defaultdict
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from room_classifier import classify, item_key, normalize

STAY_TAG = re.compile(r"[\[(]\s*(\d+)\s*박(?:\s*전용)?\s*[\])]")
NUMBERED_UNIT = re.compile(r"(?:사이트|객실|글램핑|카라반|캐빈)\s*[-_]?\s*\d+|[a-z가-힣]+[-_]\d+|\d+\s*(?:번|호|동)", re.I)
MAX_NIGHTS = 7
BOOKING_ITEM = re.compile(r"product:(/booking/\d+/bizes/\d+)/items/\d+")


def unit_title(title):
    title = str(title or "").strip()
    cleaned = STAY_TAG.sub("", title).strip()
    return cleaned if NUMBERED_UNIT.search(cleaned) else title


def stay_length(card):
    tags = [int(n) for n in STAY_TAG.findall(str(card.get("title", "")))]
    minimum = [int(n) for n in re.findall(r"최소\s*(\d+)\s*박", str(card.get("text", "")))]
    return max(tags + minimum + [1])


def members(card):
    return card.get("stayMembers") or [card]


def required_stays(cards, rules=None):
    return sorted({stay_length(c) for card in cards for c in members(card)
                   if classify(c, rules)[0] == "room" and stay_length(c) > 1})


def verified_dates(snapshot, checkin, nights):
    checkout = checkin + timedelta(days=nights)
    query = parse_qs(urlparse(snapshot.get("url", "")).query)
    if query.get("checkin") != [checkin.strftime("%Y%m%d")] or query.get("checkout") != [checkout.strftime("%Y%m%d")]:
        return False
    text = snapshot.get("selectedStayText", "")
    dates = re.search(r"(\d+)월\s*(\d+)일.*?부터\s*(\d+)월\s*(\d+)일.*?까지", text, re.S)
    durations = re.findall(r"(?<!\d)(\d+)\s*박", text)
    return bool(dates and tuple(map(int, dates.groups())) == (checkin.month, checkin.day, checkout.month, checkout.day)
                and durations == [str(nights)])


def _state(card, snapshot, nights, checkin, status_words):
    if (card is None or not snapshot or not snapshot.get("listingStable")
            or not verified_dates(snapshot, checkin, nights)):
        return "unknown"
    if any(normalize(label) in {normalize(w) for w in status_words} for label in card.get("statusLabels", [])):
        return "closed"
    # A missing sold-out label alone is not evidence that an alternative tariff is open.
    return "available" if card.get("availabilityEvidence") is True else "unknown"


def combine_stays(base, views, checkin, status_words, known=(), rules=None, confirm_unlisted=None):
    """Use each tariff only in its own valid duration; retain unobserved alternatives."""
    rules = rules or {}
    by_id, observations = {}, {}
    for nights, snapshot in sorted(views.items()):
        for raw in snapshot.get("roomCards", []):
            identity = item_key(raw)
            by_id.setdefault(identity, copy.deepcopy(raw))
            observations[(identity, nights)] = raw
    families = defaultdict(list)
    unchanged = []
    for identity, card in by_id.items():
        if classify(card, rules)[0] == "room":
            families[normalize(unit_title(card.get("title")))].append(card)
        else:
            unchanged.append(card)
    # An alternative disappearing must not turn a site into "sold out".
    for old in known:
        for raw in members(old):
            family = normalize(unit_title(raw.get("title")))
            variant_family = family in families and (old.get("stayMembers") or stay_length(raw) > 1
                or any(stay_length(c) > 1 for c in families[family]))
            if (variant_family and item_key(raw) not in by_id
                    and classify(raw, rules)[0] == "room"):
                families[family].append(copy.deepcopy(raw))
                by_id[item_key(raw)] = raw
    cards, summaries = list(unchanged), []
    for family, products in families.items():
        durations = [stay_length(c) for c in products]
        tagged = any(STAY_TAG.search(str(c.get("title", ""))) for c in products)
        if not tagged and max(durations) == 1:
            cards.extend(c if (item_key(c), 1) in observations else {**c, "availabilityState": "unknown"}
                         for c in products)
            continue
        # Equal names alone are not enough to combine multiple products of the same tariff.
        ambiguous = (len(set(durations)) != len(durations)
                     or (len(products) > 1 and not NUMBERED_UNIT.search(unit_title(products[0].get("title")))))
        if ambiguous:
            cards.extend({**c, "availabilityState": "unknown"} for c in products)
            continue
        products.sort(key=lambda c: (stay_length(c), item_key(c)))
        evidence = []
        for product in products:
            nights = stay_length(product)
            snapshot = views.get(nights)
            observed = observations.get((item_key(product), nights))
            state = _state(observed, snapshot, nights, checkin, status_words)
            proof = {}
            if (state == "unknown" and confirm_unlisted and snapshot and
                    not any((item_key(product), n) in observations for n in views) and
                    all(v.get("listingStable") and verified_dates(v, checkin, n) for n, v in views.items())):
                sibling_keys = [item_key(c) for c in products if
                                (item_key(c), stay_length(c)) in observations and
                                _state(observations[item_key(c), stay_length(c)], views[stay_length(c)],
                                       stay_length(c), checkin, status_words) != "unknown"]
                if sibling_keys:
                    proof = confirm_unlisted(product, sibling_keys, nights) or {}
                    if proof:
                        state = "not_offered"
            evidence.append({"itemKey": item_key(product), "title": product["title"], "nights": nights,
                "checkin": checkin.isoformat(), "checkout": (checkin + timedelta(days=nights)).isoformat(),
                "state": state, "url": snapshot.get("url", "") if snapshot else "", **proof})
        active = [(p, e["state"]) for p, e in zip(products, evidence) if e["state"] != "not_offered"]
        primary = active[0][0] if active else products[0]
        states = [state for _, state in active]
        state = "available" if "available" in states else "closed" if states and all(s == "closed" for s in states) else "unknown"
        title = unit_title(primary["title"])
        labels = list(status_words[:1]) if state == "closed" else []
        details = ", ".join(f"{e['nights']}박 " + {"available": "가능", "closed": "마감", "unknown": "미확인",
                            "not_offered": "판매목록 미게시 확인"}[e["state"]] for e in evidence)
        cards.append({**primary, "title": title, "titleKey": re.sub(r"\s+", "", title),
            "statusLabels": labels, "reserved": state == "closed", "availabilityState": state,
            "stayMembers": products, "memberItemKeys": [item_key(c) for c in products], "stayEvidence": evidence,
            "text": title + "\n" + details, "textKey": normalize(title + details),
            "priceAmount": None, "schemaVersion": 3})
        summaries.append({"title": title, "products": len(products), "state": state, "detail": details})
    return {**base, "roomCards": cards, "staySummary": summaries,
            "listingStable": base.get("listingStable") is True and all(v.get("listingStable") for v in views.values()),
            "stayQueries": [{"nights": n, "verified": verified_dates(v, checkin, n)} for n, v in sorted(views.items())]}
