import sys
import unittest
from pathlib import Path
from openpyxl import Workbook

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
from company_report import add_company_views


class CompanyReport(unittest.TestCase):
    def test_idempotent_grouping_and_unknowns(self):
        wb = Workbook()
        wb.active.title = "업체별 현황"
        wb.active["A1"] = "user owned"
        day = "2026-11-19"
        entries, history = [], {}
        for key, rooms, reserved, price in [("w",["여울"],1,480000),("o",["오름","채움"],0,None)]:
            result = dict(date=day,status="정상",reserved=reserved,total=len(rooms))
            revenue = price*reserved if price is not None else None
            a = dict(date=day,reserved=reserved,total=len(rooms),revenue=revenue,assumed_closed=False)
            entries.append(dict(key=key,company_id="01",major="업체",sheet_title=key,rooms=rooms,result=result,
                                price_amount=price,business_adjustment=a))
            history[key] = [dict(result, revenue=revenue,business_adjustment=a)]
        state = dict(entries=entries,today=day)
        for _ in range(2):
            add_company_views(wb,state,history)
            self.assertEqual(len(wb.sheetnames),4)
            self.assertEqual(wb.active["A1"].value,"user owned")
            row = list(wb["업체별 일별 추이"].values)[1]
            self.assertEqual(row[5:12],(2,2,1,3,1/3,480000,1))
            self.assertEqual(wb["동별 단가"].max_row,4)
        wb.close()
