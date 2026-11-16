import copy
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_manager import fixture, FakeCollector
from test_cloud import FakeBridge
from booking_manager import Manager
from booking_store import entry_key
from booking_pricing import nightly_max
import naver_room_booking_upgraded as core
from cloud.runner import execute
from cloud.snapshot import pack, unpack


def card(title, price, detail="최소 1박", **extra):
    return {"title": title, "text": f"{title}\n{price}\n캠핑사이트, 기준 2인\n{detail}", **extra}


class PriceParsing(unittest.TestCase):
    def test_undated_price_cache_never_replaces_dated_booking_snapshot(self):
        collector = core.BrowserCollector(core.parse_date("2026-11-15"), [], False, 25, lambda _: None)
        bookings, prices = {"source": "bookings"}, {"source": "prices"}
        with patch.object(collector, "_read_snapshot", side_effect=[bookings, prices]) as read:
            dated_url, current = collector.get_snapshot("123456789")
            price_url, listed = collector.get_price_snapshot("123456789")
            self.assertIn("checkin=20261115", dated_url)
            self.assertNotIn("checkin=", price_url)
            self.assertIs(collector.get_snapshot("123456789")[1], current)
            self.assertIs(collector.get_price_snapshot("123456789")[1], listed)
            self.assertEqual(read.call_count, 2)

    def test_range_and_single(self):
        for text, expected in [("70,000 ~ 80,000원", 80000), ("70,000원~80,000원", 80000),
                               ("80000원", 80000), ("70,000～80,000원 Npay+", 80000),
                               ("70,000 ~\n부터\n 80,000원\n네이버페이", 80000)]:
            self.assertEqual(nightly_max(card("A-1", text)), expected)

    def test_no_guess_from_addons_discounts_and_specs(self):
        for text in ["추가 인원 30000원", "쿠폰 30000원", "80,000원부터", "9m X 6m", "예약마감", "0원"]:
            self.assertIsNone(nightly_max(card("A-1", text)))
        self.assertEqual(nightly_max(card("A-1", "80,000원\n추가요금\n100,000원")), 80000)

    def test_noise_and_multi_night_excluded(self):
        for title, detail in [("공지사항", "최소 1박"), ("캠크닉 G-11", "8시간 이용"),
                              ("쿠폰", ""), ("(2박) 사이트-6", "최소 2박"), ("A-1", "최소 3박")]:
            self.assertIsNone(nightly_max(card(title, "200,000원", detail)))

    def test_explicit_multi_night_price_units(self):
        self.assertEqual(nightly_max(card("(2박) 사이트-6", "1박 기준: 80000원", "최소 2박")), 80000)
        self.assertEqual(nightly_max(card("(2박) 사이트-6", "2박 총액: 140000~160000원", "최소 2박")), 80000)
        self.assertIsNone(nightly_max(card("[2박] 사이트-6", "160000원", "")))
        self.assertIsNone(nightly_max(card("(2박) 사이트-6", "2박 총액: 160001원", "최소 2박")))
        self.assertIsNone(nightly_max(card("쿠폰", "1박 기준: 80000원")))

    def test_known_room_can_omit_type_but_not_unit_or_day_use_evidence(self):
        known = card("A8", "70,000 ~\n부터\n80,000원", "", schemaVersion=3,
                     unitEvidence=True, overnightEvidence=False)
        self.assertIsNone(nightly_max(known))
        self.assertEqual(nightly_max(known, confirmed_room=True), 80000)
        self.assertIsNone(nightly_max({**known, "unitEvidence": False}, confirmed_room=True))
        self.assertIsNone(nightly_max({**known, "title": "캠크닉"}, confirmed_room=True))
        self.assertIsNone(nightly_max({**known, "title": "쿠폰"}, confirmed_room=True))

    def test_floorplan_note_on_confirmed_room_is_not_a_notice_product(self):
        known = card("T5 글램핑 (더블,트윈베드/배치도 확인)", "199,000 ~\n부터\n289,000원",
                     schemaVersion=3, unitEvidence=True, overnightEvidence=True)
        self.assertIsNone(nightly_max(known))
        self.assertEqual(nightly_max(known, confirmed_room=True), 289000)
        for title in ["배치도 확인", "(배치도 확인)", "공지사항 (배치도 확인)"]:
            self.assertIsNone(nightly_max({**known, "title": title}, confirmed_room=True))


class PricingIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.m = Manager(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, index=0, **changes):
        row = self.m.state()["entries"][index]
        return self.m.apply({"revision": self.m.store.entries()[1], "edits": [{**row, **changes}]})

    def collect(self, collector=FakeCollector):
        row = self.m.state()["entries"][0]
        with patch.object(core, "BrowserCollector", collector):
            execute(FakeBridge(), self.m, {"kind": "collect", "payload": {
                "keys": [row["key"]], "revision": self.m.store.entries()[1], "date": "2026-11-15"}})

    def test_price_only_updates_excel_and_preserves_collection(self):
        self.collect()
        key = self.m.state()["entries"][0]["key"]
        before = copy.deepcopy(self.m.data["results"][key])
        old_history = self.m.store.history(key)[0]
        self.save(price="80000")
        row = self.m.state()["entries"][0]
        self.assertFalse(row["changed"])
        self.assertFalse(row["result"]["stale"])
        self.assertEqual(row["result"]["checked_at"], before["checked_at"])
        self.assertEqual((row["result"]["reserved"], row["result"]["total"]), (1, 2))
        history = self.m.store.history(key)
        self.assertEqual(history[0], old_history)
        self.assertEqual((history[-1]["price"], history[-1]["revenue"]), (80000, 80000))
        wb = core.load_workbook(self.m.store.result)
        self.assertEqual(wb[core.STATUS_SHEET_NAME].cell(2, 10).value, 80000)
        wb.close()
        with tempfile.TemporaryDirectory() as directory:
            unpack(pack(self.m), directory)
            reopened = Manager(directory)
            self.assertFalse(reopened.state()["entries"][0]["result"]["stale"])
            self.assertEqual(reopened.store.meta["price_policy"][key]["mode"], "manual")

    def test_price_edit_does_not_hide_existing_issues_or_pending_rooms(self):
        self.collect()
        key = self.m.state()["entries"][0]["key"]
        self.m.data["results"][key]["status"] = "객실 확인 필요"
        self.save(price="80000")
        self.assertEqual(self.m.state()["entries"][0]["result"]["status"], "객실 확인 필요")
        self.save(rooms=["A-1", "new room"])
        self.save(price="90000")
        row = self.m.state()["entries"][0]
        self.assertTrue(row["changed"])
        self.assertTrue(row["result"]["superseded"])

    def test_price_and_rooms_still_need_collection(self):
        self.collect()
        self.save(price="80000", rooms=["A-1"])
        self.assertTrue(self.m.state()["entries"][0]["changed"])

    def test_identical_manual_confirmation_still_clears_old_proposal(self):
        self.save(room_authority="manual")
        self.collect()
        key = self.m.state()["entries"][0]["key"]
        self.m.data["proposals"][key] = {"error": "old review"}
        self.save()
        self.assertIsNone(self.m.state()["entries"][0]["proposal"])

    def test_manual_price_survives_rename_and_clear_releases(self):
        self.save(price="80000")
        old = self.m.state()["entries"][0]["key"]
        self.save(sheet_title="renamed", price="90000")
        key = self.m.state()["entries"][0]["key"]
        self.assertNotIn(old, self.m.store.meta["price_policy"])
        self.assertEqual(self.m.store.meta["price_policy"][key]["mode"], "manual")
        self.save(price="")
        self.assertEqual(self.m.store.meta["price_policy"][key]["mode"], "auto_max")

    def test_auto_max_uses_own_rooms_only_and_preserves_inventory(self):
        class PricedCollector(FakeCollector):
            def get_price_snapshot(self, company):
                return "https://m.place.naver.com", {"listingStable": True, "roomCards": [
                    card("A-1", "60000~80000원"), card("A-2", "90000원"),
                    card("G-1", "300000원"), card("G-2", "350000원"),
                    card("캠크닉", "999000원"), card("공지사항", "999000원")]}
        self.collect(PricedCollector)
        row = self.m.state()["entries"][0]
        self.assertEqual(row["price"], "90000")
        self.assertEqual(row["rooms"], ["A-1", "A-2"])
        self.assertEqual(row["result"]["status"], "정상")
        self.assertFalse(row["result"]["stale"])
        self.assertEqual(row["price_policy"]["mode"], "auto_max")
        self.assertEqual(self.m.store.history(row["key"])[-1]["revenue"], 90000)
        self.save(price="80000")
        self.collect(PricedCollector)
        self.assertEqual(self.m.state()["entries"][0]["price"], "80000")

    def test_unconfirmed_or_missing_price_keeps_previous(self):
        class EmptyCollector(FakeCollector):
            def get_price_snapshot(self, company):
                return "", {"listingStable": True, "roomCards": [card("A-1", "예약마감")]}
        self.collect(EmptyCollector)
        self.assertEqual(self.m.state()["entries"][0]["price"], "5만원")

    def test_known_dated_room_with_untyped_undated_price(self):
        class UntypedCollector(FakeCollector):
            def get_price_snapshot(self, company):
                return "", {"listingStable": True, "roomCards": [
                    card("A-1", "70000~80000원", "", schemaVersion=3,
                         unitEvidence=True, overnightEvidence=False)]}
        self.collect(UntypedCollector)
        self.assertEqual(self.m.state()["entries"][0]["price"], "80000")

    def test_price_does_not_turn_partial_room_matching_into_success(self):
        class PartialCollector(FakeCollector):
            def get_snapshot(self, company):
                url, snapshot = super().get_snapshot(company)
                snapshot["roomCards"] = [c for c in snapshot["roomCards"] if c["title"] != "A-2"]
                return url, snapshot
            def get_price_snapshot(self, company):
                return "", {"listingStable": True, "roomCards": [card("A-1", "80000원")]}
        self.collect(PartialCollector)
        row = self.m.state()["entries"][0]
        self.assertEqual(row["price"], "80000")
        self.assertEqual(row["result"]["missing"], 1)
        self.assertNotEqual(row["result"]["status"], "정상")
        self.assertEqual(self.m.store.history(row["key"])[-1]["date"], "2026-08-01")

    def test_price_save_failure_rolls_back_workbooks_and_policy(self):
        import os
        self.collect()
        db, result = self.m.store.db.read_bytes(), self.m.store.result.read_bytes()
        state = copy.deepcopy(self.m.data["results"])
        replace = os.replace
        def fail(source, target):
            if Path(target) == self.m.store.result:
                raise PermissionError("locked workbook")
            return replace(source, target)
        with patch("booking_store.os.replace", side_effect=fail), self.assertRaises(PermissionError):
            self.save(price="80000")
        self.assertEqual(self.m.store.db.read_bytes(), db)
        self.assertEqual(self.m.store.result.read_bytes(), result)
        self.assertEqual(self.m.data["results"], state)
        self.assertFalse(self.m.store.meta.get("price_policy"))


if __name__ == "__main__":
    unittest.main()
