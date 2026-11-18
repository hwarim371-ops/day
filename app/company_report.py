"""Derived company views; category sheets remain the source of historical counts."""
from collections import defaultdict
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

MARK = "camping-insights:company-report:"


def generated(ws):
    comment = ws["A1"].comment
    return bool(comment and comment.author == "Camping Insights" and comment.text.startswith(MARK))


def _sheet(wb, name, columns, widths, protected):
    for ws in list(wb):
        if ws.title not in protected and generated(ws) and ws["A1"].comment.text == MARK + name:
            del wb[ws.title]
    ws = wb.create_sheet(name)
    ws.append(columns)
    ws["A1"].comment = Comment(MARK + name, "Camping Insights")
    for cell, width in zip(ws[1], widths):
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="205548")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "D2"
    return ws


def _append(ws, row, rates, amounts):
    ws.append(row)
    for col, value in enumerate(row, 1):
        cell = ws.cell(ws._current_row, col)
        if isinstance(value, str):
            cell.data_type = "s"
        if col in rates:
            cell.number_format = "0.0%"
        elif col in amounts:
            cell.number_format = '#,##0'


def _sum(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return sum(values) if values else None


def _summary(rows, adjusted):
    values = [(h.get("business_adjustment") or h) if adjusted else h for h in rows]
    sold, total = _sum(values, "reserved"), _sum(values, "total")
    return [sold, total, sold/total if total else None, _sum(values, "revenue"),
            sum(h.get("revenue") is not None for h in values)]


def add_company_views(wb, state, histories):
    protected = {e["sheet_title"] for e in state["entries"]}
    groups = defaultdict(list)
    for e in state["entries"]:
        if not e.get("archived"):
            groups[e.get("company_id") or e["key"]].append(e)
    header = ["체크인", "업체번호", "업체 / 대표 분류", "구분", "객실 분류", "확인 분류", "등록 분류",
              "원본 마감", "원본 객실", "원본 가동률", "원본 예상매출", "원본 금액확인",
              "보정 마감", "영업 객실", "보정 가동률", "보정 예상매출", "보정 금액확인"]
    widths = [15,17,38,14,34,12,12,12,12,15,19,15,12,12,15,19,15]
    current = _sheet(wb, "업체별 현황", header, widths, protected)
    trend = _sheet(wb, "업체별 일별 추이", header, widths, protected)
    detail = _sheet(wb, "동별 단가", ["업체번호","업체 / 대표 분류","객실 분류","실제 동·사이트","기준단가","최근 체크인","예약 상태","보정 예상매출"],
                    [17,38,34,48,17,17,18,20], protected)
    current.sheet_properties.outlinePr.summaryBelow = False
    day = (state.get("job") or {}).get("date") or state.get("today")
    for cid, entries in groups.items():
        first = entries[0]
        name = first.get("major") or first["sheet_title"]
        if not (len(entries)>1 and " " in name and all(e.get("major")==name for e in entries)):
            title = first["sheet_title"] if len(entries)==1 else first["sheet_title"].split("(")[0].split(" ")[0]
            if name != title:
                name += " / " + title
        def record(ws, d, label, title, rows, count):
            _append(ws, [d,str(cid),name,label,title,len(rows),count,*_summary(rows,False),*_summary(rows,True)],
                    (10,15),(11,16))
        days = defaultdict(list)
        sections = []
        for e in entries:
            for d, h in {h["date"]:h for h in histories.get(e["key"], [])}.items():
                days[d].append(h)
            r = e.get("result") or {}
            a = e.get("business_adjustment")
            ok = bool(a and r.get("status") == "정상" and not r.get("stale") and not e.get("changed")
                      and not (e.get("proposal") or {}).get("changed") and a["date"] == r.get("date"))
            price = e.get("price_amount")
            raw = dict(r, revenue=r["reserved"]*price if price is not None else None, business_adjustment=a) if ok else {}
            sections.append((e["sheet_title"], [raw] if ok and r["date"]==day else []))
            booked = set(r.get("reserved_rooms") or [])
            identifiable = ok and (r["reserved"] in (0,len(e["rooms"])) or
                                   (len(booked)==r["reserved"] and booked.issubset(e["rooms"])))
            for room in e["rooms"]:
                sold = identifiable and (r["reserved"]==len(e["rooms"]) or room in booked)
                closed = ok and a.get("assumed_closed")
                status = "미확인" if not identifiable else "미영업 추정" if closed else "예약 마감" if sold else "마감 아님"
                _append(detail, [str(cid), name, e["sheet_title"], room, price, r.get("date"), status,
                                 None if not identifiable else 0 if closed or not sold else price], (), (5,8))
        record(current,day,"업체 합계",None,[r for _,rows in sections for r in rows],len(entries))
        start = current._current_row+1
        for title, rows in sections:
            record(current,day,"분류",title,rows,1)
        current.row_dimensions.group(start,current._current_row,hidden=True)
        for d, rows in sorted(days.items(), reverse=True):
            record(trend,d,"업체 합계",None,rows,len(entries))
    for ws in (current, trend, detail):
        ws.auto_filter.ref = ws.dimensions
