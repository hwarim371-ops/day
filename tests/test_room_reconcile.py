import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import test_manager as fixtures
from test_room_classifier import card
import booking_manager as manager
import naver_room_booking_upgraded as core
from booking_store import entry_key
from room_classifier import classify
from room_reconcile import exact_bindings, plan_changes


def snapshot(*items, stable=True):
    return {"roomCards": list(items), "listingStable": stable, "strictRoomCards": True}


class Changes(unittest.TestCase):
    def test_promotion_on_a_real_room_is_not_coupon(self):
        for name in ["여름특가 A-1", "[쿠폰 할인] 글램핑 1", "이벤트 카라반 2호", "당일특가 A-1"]:
            self.assertEqual(classify(card(name))[0], "room")
        self.assertEqual(classify(card("글램핑 할인 이벤트"))[0], "review")
        for name in ["할인쿠폰 받기", "이벤트", "더보기", "상세보기"]:
            self.assertEqual(classify(card(name))[0], "excluded")

    def test_day_use_with_coupon_words_is_excluded_before_promotion_review(self):
        for name in ["평일 당일치기 글램핑(숙박 불가 상품/쿠폰 사용 불가)",
                     "휴일 당일치기 글램핑(숙박 불가 상품/쿠폰 사용 불가)"]:
            self.assertEqual(classify(card(name))[0], "excluded")

    def test_standalone_shower_is_not_campsite_with_shower(self):
        self.assertEqual(classify(card("단독사용 개별샤워실"))[0], "excluded")
        self.assertEqual(classify(card("개별샤워실 20사이트(8미터10미터)"))[0], "room")

    def test_stable_id_rename_is_not_new_inventory(self):
        old = card("A-1")
        bindings = exact_bindings(["A-1"], [old], {})
        p = plan_changes(["A-1"], snapshot(card("숲속-1")), {}, bindings)
        self.assertEqual(p["rooms"], ["숲속-1"])
        self.assertEqual(len(p["renamed"]), 1)
        self.assertEqual(p["added"], [])

    def test_manual_name_uses_identity_without_overwrite(self):
        p = plan_changes(["A-1"], snapshot(card("숲속-1")), {}, {"a-1": "product:one"}, manual=True)
        self.assertEqual(p["rooms"], ["A-1"])
        self.assertFalse(p["changed"])
        e = core.DbEntry(2, "123456789", "", "Test", "", ["A-1"])
        s = snapshot(card("숲속-1", labels=["예약마감"]))
        s["roomAliases"] = {"a-1": "product:one"}
        r = core.analyze_entry_snapshot(e, s, core.normalize_words(core.DEFAULT_STATUS_WORDS), "matched", "")
        self.assertEqual((r.matched_count, r.reserved_count), (1, 1))

    def test_absence_never_deletes_room_or_marks_sold_out(self):
        p = plan_changes(["A-1", "A-2"], snapshot(card("A-1")), {}, {})
        self.assertEqual(p["rooms"], ["A-1", "A-2"])
        self.assertEqual(p["missing"], ["A-2"])
        p = plan_changes(["A-1"], snapshot(stable=False), {}, {})
        self.assertIn("수집 화면 불완전", p["summary"])
        self.assertFalse(p["changed"])

    def test_numbered_same_family_addition_only(self):
        s = snapshot(card("A-1", "1"), card("A-2", "2"), card("A-3", "3"))
        for item in s["roomCards"]:
            item["priceAmount"] = 50000
        p = plan_changes(["A-1", "A-2"], s, {}, {})
        self.assertEqual(p["rooms"], ["A-1", "A-2", "A-3"])
        for options in [{"manual": True}, {"split": True}]:
            self.assertFalse(plan_changes(["A-1", "A-2"], s, {}, {}, **options)["changed"])
        s["listingStable"] = False
        self.assertFalse(plan_changes(["A-1", "A-2"], s, {}, {})["changed"])

    def test_unknown_or_different_price_not_auto_added(self):
        s = snapshot(card("A-1", "1"), card("A-2", "2"), card("A-3", "3"))
        self.assertFalse(plan_changes(["A-1", "A-2"], s, {}, {})["changed"])
        for index, item in enumerate(s["roomCards"]):
            item["priceAmount"] = 50000 if index < 2 else 250000
        self.assertFalse(plan_changes(["A-1", "A-2"], s, {}, {})["changed"])

    def test_mixed_types_and_packages_never_added(self):
        s = snapshot(card("A-1", "1"), card("A-2", "2"), card("글램핑 3", "3"))
        p = plan_changes(["A-1", "A-2"], s, {}, {})
        self.assertFalse(p["changed"])
        self.assertEqual(p["uncertain_new"], ["글램핑 3"])

    def test_duplicate_identity_or_title_never_auto_rename(self):
        for items in [[card("새이름", "1"), card("다른이름", "1")],
                      [card("새이름", "1"), card("새이름", "2")]]:
            p = plan_changes(["A-1"], snapshot(*items), {}, {"a-1": "product:1"})
            self.assertFalse(p["changed"])

    def test_noise_cleanup_respects_manual_and_inclusion(self):
        s = snapshot(card("A-1", "1"), card("쿠폰받기", "2"))
        self.assertEqual(plan_changes(["A-1", "쿠폰받기"], s, {}, {})["rooms"], ["A-1"])
        self.assertFalse(plan_changes(["A-1", "쿠폰받기"], s, {}, {}, manual=True)["changed"])
        self.assertFalse(plan_changes(["A-1", "쿠폰받기"], s, {"product:2": {"mode": "include"}}, {})["changed"])
        self.assertFalse(plan_changes(["쿠폰받기"], snapshot(card("쿠폰받기")), {}, {})["changed"])

    def test_eight_rooms_four_closed_and_three_notices(self):
        items = [card(f"A-{i}", str(i), ["예약마감"] if i <= 4 else []) for i in range(1, 9)]
        items += [card(n, n, ["예약마감"]) for n in ["공지사항", "쿠폰받기", "더보기"]]
        e = core.DbEntry(2, "123456789", "", "Test", "", [f"A-{i}" for i in range(1, 9)])
        result = core.analyze_entry_snapshot(e, snapshot(*items), core.normalize_words(core.DEFAULT_STATUS_WORDS), "matched", "")
        self.assertEqual((result.matched_count, result.reserved_count, result.saved_total), (8, 4, 8))

    def test_user_exclusion_does_not_claim_a_real_room_is_a_notice(self):
        p = plan_changes(["A-1"], snapshot(card("A-1", "1"), card("글램핑 1", "2")), {"product:2": {"mode": "exclude"}}, {})
        self.assertNotIn("공지·혜택·비숙박", p["summary"])
        self.assertEqual(p["selected_exclusions"], 1)


class Integration(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown

    def collect(self, m, entry, first, refreshed=None):
        class Collector(fixtures.FakeCollector):
            calls = 0
            def get_snapshot(self, company):
                return "https://m.place.naver.com", copy.deepcopy(first)
            def refresh_snapshot(self, company):
                Collector.calls += 1
                return "https://m.place.naver.com", copy.deepcopy(refreshed if refreshed is not None else first)
        with patch.object(core, "BrowserCollector", Collector):
            m.start("collect", {"keys": [entry_key(entry)], "revision": m.store.entries()[1], "date": "2026-10-20"})
            m.worker.join(15)
        self.assertFalse(m.busy())
        self.assertNotEqual(m.job["state"], "failed", m.job)
        return Collector.calls

    def test_rename_saved_with_backup_history_price_and_new_result(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        m.data["catalogs"][e.company_id] = {"snapshot": snapshot(card("A-1", "1"), card("A-2", "2"))}
        before = m.store.db.read_bytes()
        s = snapshot(card("숲속-1", "1", ["예약마감"]), card("A-2", "2"))
        self.assertEqual(self.collect(m, e, s), 1)
        updated = m.store.entries()[0][0]
        self.assertEqual(updated.rooms, ["숲속-1", "A-2"])
        self.assertEqual(updated.price, e.price)
        self.assertEqual(updated.sheet_title, e.sheet_title)
        self.assertEqual(m.job["normal"], 1)
        self.assertEqual(m.data["results"][entry_key(e)]["reserved"], 1)
        backups = list(m.store.home.glob("수정전_*/" + core.DB_FILENAME))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(len(m.store.history(entry_key(e))), 2)
        reopened = manager.Manager(self.folder)
        self.assertEqual(reopened.data["room_bindings"][entry_key(e)]["숲속-1"], "product:1")
        self.assertFalse(reopened.state()["entries"][0]["result"]["stale"])

    def test_changed_second_read_preserves_original_db(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        m.data["catalogs"][e.company_id] = {"snapshot": snapshot(card("A-1", "1"), card("A-2", "2"))}
        before = m.store.db.read_bytes()
        self.collect(m, e, snapshot(card("NEW", "1"), card("A-2", "2")), snapshot(card("OTHER", "1")))
        self.assertEqual(before, m.store.db.read_bytes())
        self.assertEqual(m.job["auto_changes"]["entries"], 0)

    def test_only_problem_company_rechecked_once_no_deletion(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        before = m.store.db.read_bytes()
        self.assertEqual(self.collect(m, e, snapshot(card("A-1", "1"))), 1)
        self.assertEqual(m.job["completed"], 1)
        self.assertEqual(m.job["issues"], 1)
        self.assertEqual(m.store.db.read_bytes(), before)
        self.assertIn("DB 유지", m.data["results"][entry_key(e)]["status"])

    def test_new_numbered_room_saved_and_other_categories_unchanged(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][2]
        s = snapshot(card("B-1", "1"), card("B-2", "2"), card("B-3", "3"))
        for item in s["roomCards"]:
            item["priceAmount"] = 50000
        self.assertEqual(self.collect(m, e, s), 1)
        self.assertEqual(m.store.entries()[0][2].rooms, ["B-1", "B-2", "B-3"])
        self.assertEqual(m.store.entries()[0][0].rooms, ["A-1", "A-2"])
        self.assertEqual(m.job["normal"], 1)

    def test_normal_entries_not_refetched(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        self.assertEqual(self.collect(m, e, snapshot(card("A-1", "1"), card("A-2", "2"))), 0)
        self.assertEqual(m.job["normal"], 1)

    def test_existing_sibling_rooms_are_not_reported_as_new(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        s = snapshot(card("A-1", "1"), card("A-2", "2"), card("G-1", "3"), card("G-2", "4"))
        self.collect(m, e, s)
        result = m.data["results"][entry_key(e)]
        self.assertEqual(result["diagnosis"]["uncertain_new"], [])
        self.assertEqual(result["diagnosis"]["summary"], "객실 구성 동일")
        self.assertEqual(m.job["normal"], 1)

    def test_manual_rename_is_collected_without_rewriting_user_names(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][0]
        m.store.meta["manual_rooms"] = {entry_key(e): {"rooms": list(e.rooms)}}
        m.store.meta["room_rules"] = {e.company_id: {"product:1": {"mode": "exclude"}}}
        m.data["catalogs"][e.company_id] = {"snapshot": snapshot(card("A-1", "1"), card("A-2", "2"))}
        before = m.store.db.read_bytes()
        self.collect(m, e, snapshot(card("숲속-1", "1", ["예약마감"]), card("A-2", "2")))
        self.assertEqual(before, m.store.db.read_bytes())
        self.assertEqual(m.job["normal"], 1)
        self.assertEqual(m.data["results"][entry_key(e)]["reserved"], 1)

    def test_aliases_cannot_double_count_across_categories(self):
        m = manager.Manager(self.folder)
        e, other = m.store.entries()[0][:2]
        m.data["room_bindings"][entry_key(other)] = {"g-1": "product:1"}
        self.collect(m, e, snapshot(card("A-1", "1"), card("A-2", "2"), card("G-2", "4")))
        self.assertEqual(m.job["normal"], 0)
        self.assertIn("분류 간 동일 상품 중복", m.data["results"][entry_key(e)]["status"])

    def test_noise_removal_is_backed_up_and_manual_include_is_preserved(self):
        m = manager.Manager(self.folder)
        e = m.store.entries()[0][2]
        m.apply({"revision": m.store.entries()[1],
                 "edits": [{"key": entry_key(e), "rooms_only": True, "rooms": ["B-1", "쿠폰받기"]}]})
        e = m.store.entries()[0][2]
        self.collect(m, e, snapshot(card("B-1", "1"), card("쿠폰받기", "2")))
        self.assertEqual(m.store.entries()[0][2].rooms, ["B-1"])
        self.assertEqual(m.job["normal"], 1)
        self.assertTrue(m.data["results"][entry_key(e)]["diagnosis"]["applied"])


if __name__ == "__main__":
    unittest.main()
