import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_manager import fixture, FakeCollector
from booking_business import POLICY, assess, annotate_history, workbook_bytes
from booking_manager import Manager
from booking_store import entry_key
from openpyxl import load_workbook
from cloud.snapshot import pack, unpack
import naver_room_booking_upgraded as core


def history(start=date(2026, 10, 19), count=28, weekday=8, weekend=4):
    return [{"date": (start + timedelta(days=i)).isoformat(), "total": 10,
             "reserved": weekend if (start + timedelta(days=i)).weekday() in (4, 5) else weekday,
             "price": "10000", "revenue": 10000 * (weekend if (start + timedelta(days=i)).weekday() in (4, 5) else weekday)}
            for i in range(count)]


class PolicyTests(unittest.TestCase):
    def test_sunday_through_thursday_are_weekdays_even_full(self):
        for offset in range(7):
            day = date(2026, 11, 15) + timedelta(days=offset)
            a = assess([], {"date": day.isoformat(), "reserved": 10, "total": 10, "revenue": 90000})
            self.assertEqual(a["assumed_closed"], offset < 5)
            self.assertEqual(a["revenue"], 0 if offset < 5 else 90000)
            self.assertEqual(a["rate"], None if offset < 5 else 1)
            self.assertEqual(a["total"], 0 if offset < 5 else 10)

    def test_full_weekday_is_assumption_without_history(self):
        a = assess([], {"date": "2026-11-15", "reserved": 1, "total": 1, "revenue": None})
        self.assertEqual(a["reason"], "weekday_full")
        self.assertFalse(a["sufficient"])
        self.assertEqual(a["revenue"], 0)

    def test_average_reversal(self):
        rows = history()
        before = copy.deepcopy(rows)
        a = assess(rows, rows[-1])
        self.assertEqual((a["weekday_days"], a["weekend_days"]), (20, 8))
        self.assertEqual((a["weekday_rate"], a["weekend_rate"]), (.8, .4))
        self.assertEqual(a["reason"], "weekday_above_weekend")
        self.assertEqual(a["revenue"], 0)
        self.assertEqual(rows, before)

    def test_equal_or_lower_is_not_closed(self):
        for weekday in (4, 2):
            rows = history(weekday=weekday)
            a = assess(rows, rows[-1])
            self.assertEqual(a["reason"], "unchanged")
            self.assertEqual(a["revenue"], rows[-1]["revenue"])

    def test_weekend_stays_open_when_averages_reverse(self):
        rows = history()
        a = assess(rows, rows[-2])
        self.assertEqual(a["reason"], "weekend")
        self.assertEqual(a["total"], 10)

    def test_requires_both_sample_minima(self):
        for count in (1, 7, 13):
            rows = history(count=count)
            rows[-1]["date"] = "2026-11-15"
            self.assertFalse(assess(rows, rows[-1])["sufficient"])
        rows = history(count=14)
        self.assertTrue(assess(rows, rows[-1])["sufficient"])

    def test_window_excludes_old_and_future_dates(self):
        current = history()[-1]
        rows = history(start=date(2026, 1, 1)) + history(start=date(2026, 11, 16))
        a = assess(rows, current)
        self.assertEqual((a["weekday_days"], a["weekend_days"]), (1, 0))
        self.assertEqual(a["reason"], "insufficient")

    def test_dedup_current_wins_and_invalid_counts_ignored(self):
        current = {"date":"2026-11-15", "reserved":2, "total":10, "revenue":None}
        rows = [{**current, "reserved":10}, {**current, "date":"2026-02-30"},
                {**current, "date":"2026-11-14", "reserved":11},
                {**current, "date":"2026-11-13", "reserved":True}]
        a = assess(rows, current)
        self.assertEqual(a["weekday_days"], 1)
        self.assertEqual(a["weekend_days"], 0)
        self.assertEqual(a["weekday_rate"], .2)
        self.assertIsNone(a["revenue"])

    def test_invalid_current_excluded(self):
        for change in ({"total":0}, {"reserved":11}, {"reserved":True}, {"date":"2026-02-30"}):
            self.assertIsNone(assess([], {**history()[-1], **change}))

    def test_historical_assessments_do_not_use_future_data(self):
        rows = history()
        points = annotate_history(rows)
        self.assertEqual(points[0]["business_adjustment"]["reason"], "insufficient")
        self.assertEqual(points[-1]["business_adjustment"]["reason"], "weekday_above_weekend")
        self.assertEqual(points[0]["reserved"], 8)
        self.assertEqual(points[-1]["revenue"], 80000)

    def test_average_is_room_day_weighted(self):
        rows = history(weekday=4, weekend=4)
        rows[0].update(total=100, reserved=90)
        a = assess(rows, rows[-1])
        self.assertAlmostEqual(a["weekday_rate"], (90+19*4)/(100+19*10))
        self.assertTrue(a["assumed_closed"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.manager = Manager(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_loads_workbook_once_and_returns_independent_values(self):
        from booking_store import load_workbook as loader
        with patch("booking_store.load_workbook", wraps=loader) as load:
            histories = self.manager.store.histories()
            first = load.call_count
            self.manager.store.histories()
            self.assertEqual(load.call_count, first)
        key = next(iter(histories))
        histories[key].clear()
        self.assertTrue(self.manager.store.history(key))

    def test_excel_appends_fields_and_keeps_original_cells(self):
        state = self.manager.state()
        before = self.manager.store.result.read_bytes()
        data = workbook_bytes(self.manager.store.result, state, self.manager.store.histories())
        self.assertEqual(before, self.manager.store.result.read_bytes())
        original = load_workbook(io.BytesIO(before))
        exported = load_workbook(io.BytesIO(data))
        try:
            for row in range(1, 3):
                self.assertEqual([original.active.cell(row,col).value for col in range(1,7)],
                                 [exported.active.cell(row,col).value for col in range(1,7)])
            self.assertEqual(exported.active.cell(1,7).value, "보정 가동률")
            self.assertEqual(exported.active.cell(2,9).value, "주말 · 원본 유지")
        finally:
            original.close()
            exported.close()

    def test_snapshot_round_trip_and_metadata(self):
        bundle = pack(self.manager)
        with ZipFile(io.BytesIO(bundle)) as z:
            state = json.loads(z.read("state.json"))
            records = json.loads(z.read("history.json"))
            self.assertEqual(state["business_policy"], POLICY)
            self.assertEqual(records[state["entries"][0]["key"]][0]["business_adjustment"]["reason"], "weekend")
        with tempfile.TemporaryDirectory() as temp:
            unpack(bundle, temp)
            restored = Manager(temp)
            self.assertEqual(restored.store.histories(), self.manager.store.histories())

    def test_current_invalid_result_cannot_get_closed_assumption(self):
        state = self.manager.state()
        entry = self.manager.store.entries()[0][0]
        key = entry_key(entry)
        from booking_store import fingerprint
        from room_classifier import PARSER_VERSION
        self.manager.data["results"][key] = {"status":"정상", "date":"2026-11-15", "reserved":2,
            "total":2, "fingerprint":fingerprint(entry), "parser_version":PARSER_VERSION,
            "rules_revision":self.manager.rules_revision(entry.company_id)}
        self.assertTrue(self.manager.state()["entries"][0]["business_adjustment"]["assumed_closed"])
        self.manager.data["results"][key]["status"] = "확인 필요"
        self.assertIsNone(self.manager.state()["entries"][0]["business_adjustment"])
        self.manager.data["results"][key]["status"] = "정상"
        self.manager.data["results"][key]["superseded"] = True
        self.assertIsNone(self.manager.state()["entries"][0]["business_adjustment"])


if __name__ == "__main__":
    unittest.main()
