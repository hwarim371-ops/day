import copy
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_manager import fixture, FakeCollector
from test_cloud import FakeBridge
from booking_manager import Manager
from cloud.runner import execute
import naver_room_booking_upgraded as core
from room_stays import combine_stays, required_stays, unit_title, verified_dates
from room_classifier import filtered_snapshot


DAY = date(2026, 11, 17)
WORDS = core.normalize_words(core.DEFAULT_STATUS_WORDS)


def product(unit=1, nights=1, state="closed", suffix=" 자갈/9mX6m", identity=None):
    title = (f"({nights}박)" if nights > 1 else "") + f"*사이트-{unit}*" + suffix
    return {"schemaVersion": 3, "itemKey": "product:" + (identity or f"/{unit}/{nights}"),
            "title": title, "titleKey": core.normalize_name(title), "unitEvidence": True,
            "overnightEvidence": True, "statusLabels": ["예약마감"] if state == "closed" else [],
            "reserved": state == "closed", "availabilityEvidence": state == "available",
            "text": f"{title}\n캠핑사이트, 기준 2인\n최소 {nights}박\n" +
                    ("80,000원" if state == "available" else "예약마감"), "priceAmount": 80000}


def view(cards, nights=1, day=DAY, **extra):
    end = day + timedelta(days=nights)
    return {"roomCards": copy.deepcopy(cards), "strictRoomCards": True, "listingStable": True,
            "url": core.build_naver_room_url("1038800159", day, nights),
            "selectedStayText": f"{day.month}월 {day.day}일 부터 {end.month}월 {end.day}일 까지\n{nights}박",
            **extra}


def combined(one="closed", two="closed", **extra):
    base = view([product(), product(nights=2)])
    base["roomCards"][0] = product(state=one)
    second = view([product(), product(nights=2, state=two)], 2)
    return combine_stays(base, {1: base, 2: second}, DAY, WORDS, **extra)


class StayUnits(unittest.TestCase):
    def test_alternative_availability_is_one_physical_unit(self):
        for one, two, expected in [("closed", "closed", "closed"),
                                   ("closed", "available", "available"),
                                   ("available", "closed", "available"),
                                   ("available", "available", "available")]:
            with self.subTest(one=one, two=two):
                snapshot = combined(one, two)
                self.assertEqual(len(snapshot["roomCards"]), 1)
                card = snapshot["roomCards"][0]
                self.assertEqual(card["availabilityState"], expected)
                self.assertEqual(len(card["stayMembers"]), 2)
                entry = core.DbEntry(2, "1038800159", "camp", "camp", "80000", [product()["title"]])
                result = core.analyze_entry_snapshot(entry, snapshot, WORDS, "matched", snapshot["url"])
                self.assertEqual((result.saved_total, result.reserved_count), (1, int(expected == "closed")))
                self.assertEqual(result.status, "정상")

    def test_second_tariff_on_one_night_page_is_not_used(self):
        base = view([product(), product(nights=2, state="available")])
        correct = view([product(), product(nights=2)], 2)
        result = combine_stays(base, {1: base, 2: correct}, DAY, WORDS)
        self.assertEqual(result["roomCards"][0]["availabilityState"], "closed")

    def test_missing_or_unverified_second_view_never_claims_closed(self):
        base = view([product(), product(nights=2)])
        failures = [view([], 2), view([product(nights=2)], 2, listingStable=False),
                    view([product(nights=2)], 2, selectedStayText=""),
                    view([product(nights=2)], 2, day=DAY + timedelta(days=1))]
        for second in failures:
            result = combine_stays(base, {1: base, 2: second}, DAY, WORDS)
            self.assertEqual(result["roomCards"][0]["availabilityState"], "unknown")
            entry = core.DbEntry(2, "1038800159", "camp", "camp", "80000", [product()["title"]])
            counted = core.analyze_entry_snapshot(entry, result, WORDS, "matched", base["url"])
            self.assertEqual((counted.reserved_count, counted.matched_count), (0, 0))
            self.assertIn("숙박기간별 예약상태 미확인", counted.status)

    def test_valid_open_tariff_wins_even_if_other_view_fails(self):
        base = view([product(state="available"), product(nights=2)])
        result = combine_stays(base, {1: base}, DAY, WORDS)
        self.assertEqual(result["roomCards"][0]["availabilityState"], "available")

    def test_single_two_night_only_unit_can_match_canonical_db_title(self):
        base = view([product(unit=6, nights=2)])
        second = view([product(unit=6, nights=2)], 2)
        result = combine_stays(base, {1: base, 2: second}, DAY, WORDS)
        card, _ = core.choose_best_room_card(core.normalize_name(product(unit=6)["title"]),
                                             result["roomCards"], WORDS, [])
        self.assertIsNotNone(card)
        self.assertEqual(card["availabilityState"], "closed")

    def test_aliases_for_either_product_resolve_same_unit(self):
        cards = combined()["roomCards"]
        for key in ["product:/1/1", "product:/1/2"]:
            card, _ = core.choose_best_room_card("custom", cards, WORDS, ["custom"], {"custom": key})
            self.assertEqual(card, cards[0])

    def test_numbers_types_and_specs_are_not_fuzzy_merged(self):
        items = [product(), product(nights=2), product(unit=10, nights=2),
                 product(nights=2, suffix=" 데크/9mX6m", identity="deck"),
                 product(nights=2, suffix=" 자갈/8mX6m", identity="size")]
        base = view(items)
        result = combine_stays(base, {1: base, 2: view(items, 2)}, DAY, WORDS)
        self.assertEqual(len(result["roomCards"]), 4)
        self.assertEqual(unit_title("(2박) 감성 패키지"), "(2박) 감성 패키지")

    def test_ambiguous_duplicate_tariffs_need_review(self):
        items = [product(), product(identity="duplicate"), product(nights=2)]
        base = view(items)
        result = combine_stays(base, {1: base, 2: view(items, 2)}, DAY, WORDS)
        self.assertEqual(len(result["roomCards"]), 3)
        self.assertTrue(all(c["availabilityState"] == "unknown" for c in result["roomCards"]))

    def test_previously_seen_alternative_missing_now_remains_unknown(self):
        known = combined()["roomCards"]
        base = view([product()])
        result = combine_stays(base, {1: base, 2: view([product()], 2)}, DAY, WORDS, known=known)
        self.assertEqual(result["roomCards"][0]["availabilityState"], "unknown")
        self.assertEqual(len(result["roomCards"][0]["stayMembers"]), 2)

    def test_pre_upgrade_raw_catalog_preserves_missing_alternative(self):
        base = view([product()])
        result = combine_stays(base, {1: base, 2: view([product()], 2)}, DAY, WORDS,
                               known=[product(), product(nights=2)])
        self.assertEqual(result["roomCards"][0]["availabilityState"], "unknown")

    def test_confirmed_unlisted_tariff_is_not_a_booking_and_returns_if_seen(self):
        old = combined()["roomCards"]
        proof = Mock(return_value={"redirectUrl": "storefront"})
        for other in ("available", "closed"):
            items = [product(nights=2, state=other)]
            base = view(items)
            card = combine_stays(base, {1: base, 2: view(items, 2)}, DAY, WORDS,
                                 old, confirm_unlisted=proof)["roomCards"][0]
            self.assertEqual([e["state"] for e in card["stayEvidence"]], ["not_offered", other])
            self.assertEqual(card["availabilityState"], other)
            self.assertEqual(len(card["memberItemKeys"]), 2)
        items = [product(state="available"), product(nights=2)]
        base = view(items)
        proof.reset_mock()
        restored = combine_stays(base, {1: base, 2: view(items, 2)}, DAY, WORDS,
                                 [card], confirm_unlisted=proof)["roomCards"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["availabilityState"], "available")
        proof.assert_not_called()

    def test_incomplete_views_do_not_trigger_unlisting(self):
        proof = Mock(return_value={"redirectUrl": "storefront"})
        old = combined()["roomCards"]
        base = view([product(nights=2)], listingStable=False)
        result = combine_stays(base, {1: base, 2: view([product(nights=2)], 2)}, DAY, WORDS,
                               old, confirm_unlisted=proof)
        self.assertEqual(result["roomCards"][0]["availabilityState"], "unknown")
        proof.assert_not_called()

    def test_unlisting_requires_same_business_catalog_and_storefront(self):
        col = core.BrowserCollector(DAY, WORDS, False, 25, lambda _: None)
        prefix = "product:/booking/3/bizes/123/items/"
        old, sibling = {"itemKey": prefix + "1"}, {"itemKey": prefix + "2"}
        col._page = Mock()
        col._page.url = "https://m.booking.naver.com/booking/3/bizes/123"
        snap = {"listingStable": True, "url": "catalog", "roomCards": [sibling]}
        with patch.object(col, "get_price_snapshot", return_value=("catalog", snap)):
            self.assertIsNone(col._confirm_unlisted("123", old, ["product:/booking/3/bizes/42/items/2"], 1))
            for cards, stable in [([sibling, old], True), ([], True), ([sibling], False)]:
                snap.update(roomCards=cards, listingStable=stable)
                self.assertIsNone(col._confirm_unlisted("123", old, [sibling["itemKey"]], 1))
            col._page.goto.assert_not_called()
            snap.update(roomCards=[sibling], listingStable=True)
            confirmation = col._confirm_unlisted("123", old, [sibling["itemKey"]], 1)
            self.assertIn("startDate=2026-11-17", confirmation["detailUrl"])
            self.assertIn("endDate=2026-11-18", confirmation["detailUrl"])
            col._page.url = "https://nid.naver.com/login"
            self.assertIsNone(col._confirm_unlisted("123", old, [sibling["itemKey"]], 1))

    def test_explicit_exclusion_is_not_restored_as_an_alternative(self):
        rules = {"product:/1/2": {"mode": "exclude"}}
        result = combined(rules=rules, known=combined()["roomCards"])
        filtered, groups = filtered_snapshot(result, rules)
        self.assertEqual(len(filtered["roomCards"]), 1)
        self.assertEqual(len(groups["excluded"]), 1)
        self.assertEqual(required_stays([product(nights=2)], rules), [])

    def test_user_confirmed_room_with_floorplan_note_gets_long_query(self):
        room = product(nights=2, suffix=" (배치도 확인)")
        self.assertEqual(required_stays([room]), [])
        self.assertEqual(required_stays([room], {room["itemKey"]: {"mode": "include"}}), [2])

    def test_single_night_new_unit_only_seen_on_long_view_is_unknown(self):
        base = view([product(nights=2)])
        second = view([product(nights=2), product(unit=9)], 2)
        result = combine_stays(base, {1: base, 2: second}, DAY, WORDS)
        ninth = next(c for c in result["roomCards"] if c["itemKey"] == "product:/9/1")
        self.assertEqual(ninth["availabilityState"], "unknown")

    def test_date_verification_includes_year_and_month_boundaries(self):
        for day in [date(2026, 11, 30), date(2026, 12, 31)]:
            snapshot = view([], 2, day)
            self.assertTrue(verified_dates(snapshot, day, 2))
            self.assertFalse(verified_dates(snapshot, day.replace(year=2025), 2))
        self.assertFalse(verified_dates(view([], 2), DAY, 1))

    def test_eight_regular_units_four_closed_is_unchanged(self):
        items = [product(unit=i, state="closed" if i < 5 else "available") for i in range(1, 9)]
        base = view(items)
        entry = core.DbEntry(2, "1038800159", "camp", "camp", "80000", [p["title"] for p in items])
        result = core.analyze_entry_snapshot(entry, base, WORDS, "matched", base["url"])
        self.assertEqual((result.saved_total, result.reserved_count, result.status), (8, 4, "정상"))

    def test_collector_queries_each_duration_once_and_refreshes_all(self):
        base = view([product(), product(nights=2)])
        second = view([product(), product(nights=2)], 2)
        collector = core.BrowserCollector(DAY, WORDS, False, 25, lambda _: None)
        with patch.object(collector, "_read_snapshot", side_effect=[base, second, base, second]) as read:
            first = collector.get_snapshot("1038800159")[1]
            self.assertIs(first, collector.get_snapshot("1038800159")[1])
            self.assertEqual(read.call_count, 2)
            self.assertIn("checkout=20261119", read.call_args.args[0])
            collector.refresh_snapshot("1038800159")
            self.assertEqual(read.call_count, 4)

    def test_failed_long_query_is_unknown_but_access_block_is_propagated(self):
        base = view([product(), product(nights=2)])
        for message in ["timeout", "네이버 접근 제한"]:
            collector = core.BrowserCollector(DAY, WORDS, False, 25, lambda _: None)
            with patch.object(collector, "_read_snapshot", side_effect=[base, RuntimeError(message)]):
                if "제한" in message:
                    with self.assertRaisesRegex(RuntimeError, "제한"):
                        collector.get_snapshot("1038800159")
                else:
                    self.assertEqual(collector.get_snapshot("1038800159")[1]["roomCards"][0]["availabilityState"], "unknown")

    def test_stay_limit_is_bounded(self):
        self.assertEqual(required_stays([product(nights=8)]), [8])
        with self.assertRaises(ValueError):
            core.build_naver_room_url("1038800159", DAY, 8)


class StayIntegration(unittest.TestCase):
    def test_manual_price_history_and_same_date_latest_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fixture(folder)
            manager = Manager(folder)
            row = manager.state()["entries"][0]
            manager.apply({"revision": manager.store.entries()[1], "edits": [
                {**row, "rooms": [product()["title"]], "price": "80000", "room_authority": "manual"}]})
            class Collector(FakeCollector):
                current = "closed"
                def get_snapshot(self, company):
                    snapshot = combined(two=self.current)
                    return snapshot["url"], snapshot
            def collect():
                with patch.object(core, "BrowserCollector", Collector):
                    execute(FakeBridge(), manager, {"kind": "collect", "payload": {
                        "keys": [row["key"]], "revision": manager.store.entries()[1], "date": DAY.isoformat()}})
            collect()
            state = manager.state()["entries"][0]
            self.assertEqual((state["result"]["status"], state["result"]["total"], state["result"]["reserved"]), ("정상", 1, 1))
            self.assertEqual(state["price"], "80000")
            self.assertEqual(len(state["result"]["stay_units"]), 1)
            history_before = manager.store.history(row["key"])
            Collector.current = "available"
            collect()
            history = manager.store.history(row["key"])
            self.assertEqual(len(history), len(history_before))
            self.assertEqual(history[0], history_before[0])
            self.assertEqual((history[-1]["reserved"], history[-1]["total"], history[-1]["revenue"]), (0, 1, 0))
            self.assertEqual(manager.state()["entries"][0]["price"], "80000")


if __name__ == "__main__":
    unittest.main()
