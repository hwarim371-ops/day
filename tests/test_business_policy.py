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
    def test_sunday_through_thursday_require_a_sustained_pattern(self):
        for offset in range(7):
            day = date(2026, 11, 15) + timedelta(days=offset)
            rows = history(start=day-timedelta(days=27), weekday=10)
            a = assess(rows, {"date": day.isoformat(), "reserved": 10, "total": 10, "revenue": 90000})
            self.assertEqual(a["assumed_closed"], offset < 5)
            self.assertEqual(a["revenue"], 0 if offset < 5 else 90000)
            self.assertEqual(a["rate"], None if offset < 5 else 1)
            self.assertEqual(a["total"], 0 if offset < 5 else 10)

    def test_full_weekday_alone_keeps_unknown_revenue(self):
        a = assess([], {"date": "2026-11-15", "reserved": 1, "total": 1, "revenue": None})
        self.assertEqual(a["reason"], "insufficient")
        self.assertFalse(a["sufficient"])
        self.assertFalse(a["assumed_closed"])
        self.assertIsNone(a["revenue"])

    def test_average_reversal_below_full_keeps_original(self):
        rows = history()
        before = copy.deepcopy(rows)
        a = assess(rows, rows[-1])
        self.assertEqual((a["weekday_days"], a["weekend_days"]), (20, 8))
        self.assertEqual((a["weekday_rate"], a["weekend_rate"]), (.8, .4))
        self.assertEqual(a["reason"], "unchanged")
        self.assertEqual(a["revenue"], 80000)
        self.assertEqual(rows, before)

    def test_only_full_weekday_average_and_nonfull_weekends_are_closed(self):
        for weekday, weekend, expected in [(10,8,True),(10,0,True),(10,10,False),(9,8,False),(8,4,False)]:
            rows = history(weekday=weekday, weekend=weekend)
            a = assess(rows, rows[-1])
            self.assertEqual(a["assumed_closed"], expected)
            self.assertEqual(a["revenue"], 0 if expected else rows[-1]["revenue"])

    def test_one_full_day_does_not_override_the_average(self):
        rows = history(weekday=8, weekend=6)
        current = {**rows[-1], "reserved":10, "revenue":100000}
        a = assess(rows,current)
        self.assertFalse(a["assumed_closed"])
        self.assertEqual(a["revenue"],100000)
        self.assertLess(a["weekday_rate"],1)

    def test_rounded_100_percent_is_not_exactly_full(self):
        rows = history(weekday=10)
        for row in rows:
            row.update(total=10000, reserved=10000 if date.fromisoformat(row["date"]).weekday() not in (4,5) else 8000)
        rows[0]["reserved"] -= 1
        a = assess(rows,rows[-1])
        self.assertEqual(round(a["weekday_rate"]*100,1),100)
        self.assertFalse(a["assumed_closed"])

    def test_missing_weekends_never_imply_zero_percent(self):
        rows = [r for r in history(weekday=10) if date.fromisoformat(r["date"]).weekday() not in (4,5)]
        a = assess(rows,rows[-1])
        self.assertIsNone(a["weekend_rate"])
        self.assertFalse(a["assumed_closed"])

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
        rows = history(weekday=10)
        points = annotate_history(rows)
        self.assertEqual(points[0]["business_adjustment"]["reason"], "insufficient")
        self.assertEqual(points[-1]["business_adjustment"]["reason"], "weekday_average_full")
        self.assertEqual(points[0]["reserved"], 10)
        self.assertEqual(points[-1]["revenue"], 100000)

    def test_average_is_room_day_weighted(self):
        rows = history(weekday=4, weekend=4)
        rows[0].update(total=100, reserved=90)
        a = assess(rows, rows[-1])
        self.assertAlmostEqual(a["weekday_rate"], (90+19*4)/(100+19*10))
        self.assertFalse(a["assumed_closed"])


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
        with patch.object(self.manager.store, "histories", return_value={key:history(weekday=10)}):
            self.assertTrue(self.manager.state()["entries"][0]["business_adjustment"]["assumed_closed"])
            self.manager.data["results"][key]["status"] = "확인 필요"
            self.assertIsNone(self.manager.state()["entries"][0]["business_adjustment"])
            self.manager.data["results"][key]["status"] = "정상"
            self.manager.data["results"][key]["superseded"] = True
            self.assertIsNone(self.manager.state()["entries"][0]["business_adjustment"])

    def test_export_recalculates_legacy_adjustments_from_raw_records(self):
        entry = self.manager.store.entries()[0][0]
        wb = load_workbook(self.manager.store.result)
        ws = wb[entry.sheet_title]
        ws.delete_rows(1,ws.max_row)
        ws.append(['일자','예약','전체','비율','금액','추정매출','보정가동률','보정매출','사유'])
        for row in history():
            ws.append([date.fromisoformat(row['date']),row['reserved'],row['total'],row['reserved']/row['total'],
                       10000,row['revenue'],None,0,'평균 역전'])
        wb.save(self.manager.store.result)
        wb.close()
        state = self.manager.state()
        exported = load_workbook(io.BytesIO(workbook_bytes(self.manager.store.result,state,self.manager.store.histories())))
        try:
            last = exported[entry.sheet_title].max_row
            self.assertEqual(exported[entry.sheet_title].cell(last,6).value,80000)
            self.assertEqual(exported[entry.sheet_title].cell(last,8).value,80000)
            self.assertEqual(exported[entry.sheet_title].cell(last,9).value,'보정 없음')
        finally:
            exported.close()


if __name__ == "__main__":
    unittest.main()
