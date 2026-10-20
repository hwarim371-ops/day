"""Conservative room changes: stable product identity, never deletion by absence."""
import re
from collections import Counter

from room_classifier import classify, item_key, normalize, partition
from naver_room_booking_upgraded import choose_best_room_card, normalize_name


def stable_id(card):
    value = item_key(card)
    return value if value.startswith("product:") and len(value) > 8 else ""


def family(title):
    # Only an explicit numbered unit can be added to an existing homogeneous group.
    title = normalize(title)
    match = re.fullmatch(r"([a-z가-힣]+[-_]?)\d+(번|호|동|사이트)?", title)
    return (match.group(1), match.group(2)) if match else None


def exact_bindings(rooms, cards, rules, saved=None):
    allowed = partition({"roomCards": cards}, rules)["room"]
    counts = Counter(stable_id(c) for c in cards)
    bindings = {normalize(n): v for n, v in (saved or {}).items()
                if normalize(n) in {normalize(r) for r in rooms} and str(v).startswith("product:")}
    for name in rooms:
        key = normalize(name)
        matches = [c for c in allowed if normalize(c.get("title")) == key and stable_id(c)]
        if len(matches) == 1 and counts[stable_id(matches[0])] == 1:
            bindings[key] = stable_id(matches[0])
    return bindings


def plan_changes(rooms, snapshot, rules, bindings, *, manual=False, split=False):
    groups = partition(snapshot, rules)
    raw = snapshot.get("roomCards", [])
    counts = Counter(stable_id(c) for c in raw)
    titles = Counter(normalize(c.get("title")) for c in groups["room"])
    eligible = {stable_id(c): c for c in groups["room"]
                if stable_id(c) and counts[stable_id(c)] == 1 and titles[normalize(c.get("title"))] == 1
                and c.get("schemaVersion") == 3 and c.get("unitEvidence") and c.get("overnightEvidence")}
    renamed, noise, missing, matched = [], [], [], []
    next_rooms = []
    used = set()
    for name in rooms:
        key = normalize(name)
        bound = bindings.get(key)
        exact = [c for c in groups["room"] if normalize(c.get("title")) == key]
        target = eligible.get(bound) if bound else None
        if target is None and len(exact) == 1:
            target = exact[0]
        if target is None:
            target, _ = choose_best_room_card(normalize_name(name), groups["room"], [],
                                             [normalize_name(n) for n in rooms])
        excluded = [c for c in groups["excluded"] if normalize(c.get("title")) == key]
        if target is not None and item_key(target) not in used:
            used.add(item_key(target))
            matched.append(target)
            title = target["title"].strip()
            if title != name and bound and target is eligible.get(bound):
                renamed.append({"before": name, "after": title, "itemKey": bound})
                next_rooms.append(name if manual else title)
            else:
                next_rooms.append(name)
        elif excluded and not manual:
            noise.append({"title": name, "reason": excluded[0]["reason"]})
        else:
            next_rooms.append(name)
            missing.append(name)
    added = [c for c in groups["room"] if item_key(c) not in used]
    numbered = [family(c["title"]) for c in matched]
    common = numbered[0] if numbered and all(f == numbered[0] for f in numbered) else None
    prices = [c.get("priceAmount") for c in matched]
    common_price = prices[0] if prices and all(isinstance(p, (int, float)) and p > 0 and p == prices[0] for p in prices) else None
    safe_additions = (not manual and not split and not missing and not groups["review"]
                      and common is not None and common_price is not None and snapshot.get("listingStable") is True)
    accepted = [c for c in added if safe_additions and family(c["title"]) == common
                and c.get("schemaVersion") == 3 and c.get("unitEvidence")
                and c.get("overnightEvidence") and stable_id(c) in eligible and c.get("priceAmount") == common_price]
    # Even one ambiguous new product could be an alternative tariff for the same unit.
    if len(accepted) != len(added):
        accepted = []
    if not manual:
        next_rooms.extend(c["title"].strip() for c in accepted)
    if not next_rooms or len({normalize(n) for n in next_rooms}) != len(next_rooms):
        next_rooms = list(rooms)
        accepted = []
    uncertain_new = [c["title"] for c in added if c not in accepted] if not manual else []
    automatic_exclusions = [c for c in groups["excluded"] if c["mode"] != "exclude"]
    selected_exclusions = [c for c in groups["excluded"] if c["mode"] == "exclude"]
    reasons = []
    if renamed:
        reasons.append(f"객실명 변경 {len(renamed)}개" + (" · 직접 지정한 이름 유지" if manual else ""))
    if accepted:
        reasons.append(f"동일 유형 신규 객실 {len(accepted)}개")
    if noise:
        reasons.append(f"DB 비객실 후보 {len(noise)}개")
    if automatic_exclusions:
        reasons.append(f"공지·혜택·비숙박 {len(automatic_exclusions)}개 제외")
    if selected_exclusions:
        reasons.append(f"지정 목록 밖·사용자 제외 {len(selected_exclusions)}개")
    if missing:
        label = "수집 화면 불완전" if snapshot.get("listingStable") is False or not raw else "이름·판매 여부 확인"
        reasons.append(f"{label} {len(missing)}개 · DB 유지")
    if uncertain_new:
        reasons.append(f"신규·분류·금액 확인 {len(uncertain_new)}개")
    if groups["review"]:
        reasons.append(f"상품 판별 확인 {len(groups['review'])}개")
    return {"rooms": next_rooms, "renamed": renamed, "added": [c["title"] for c in accepted],
            "excluded_db": noise, "missing": missing, "uncertain_new": uncertain_new,
            "excluded_count": len(groups["excluded"]), "review_count": len(groups["review"]),
            "automatic_exclusions": len(automatic_exclusions), "selected_exclusions": len(selected_exclusions),
            "summary": " / ".join(reasons) or "객실 구성 동일", "manual": manual,
            "changed": next_rooms != rooms, "bindings": exact_bindings(next_rooms, raw, rules, bindings)}


def catalog_signature(snapshot):
    return sorted((item_key(c), str(c.get("title", "")), bool(c.get("unitEvidence")),
                   bool(c.get("overnightEvidence")), str(c.get("priceAmount")), classify(c)[0])
                  for c in snapshot.get("roomCards", []))
