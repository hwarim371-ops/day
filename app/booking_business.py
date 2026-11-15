"""Owner-requested operating-day assumptions, separate from observed closures."""
from __future__ import annotations

import io
import math
from datetime import date, timedelta


POLICY = {"version": "weekday-off-v2", "window_days": 28, "min_weekdays": 10,
          "min_weekends": 4, "weekday_average_full_only": True,
          "weekend_days": ["Friday", "Saturday"]}


def valid_day(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def valid_counts(row):
    total, reserved = row.get("total"), row.get("reserved")
    return (type(total) is int and total > 0 and type(reserved) is int
            and 0 <= reserved <= total)


def is_weekend(day):
    return day.weekday() in (4, 5)


def normalize_history(rows):
    days = {}
    for row in rows:
        day = valid_day(row.get("date"))
        if day and valid_counts(row):
            days[day] = {**row, "date": day.isoformat()}
    return [days[day] for day in sorted(days)]


def assess(history, current):
    day = valid_day(current.get("date"))
    if not day or not valid_counts(current):
        return None
    # Current observations replace the same day's last successful record. Future
    # observations must not affect an earlier check-in date's calculation.
    start = day - timedelta(days=POLICY["window_days"] - 1)
    sample = [h for h in normalize_history([*history, current])
              if start <= valid_day(h["date"]) <= day]
    weekdays = [h for h in sample if not is_weekend(valid_day(h["date"]))]
    weekends = [h for h in sample if is_weekend(valid_day(h["date"]))]
    weekday_total = sum(h["total"] for h in weekdays)
    weekend_total = sum(h["total"] for h in weekends)
    weekday_reserved = sum(h["reserved"] for h in weekdays)
    weekend_reserved = sum(h["reserved"] for h in weekends)
    sufficient = (len(weekdays) >= POLICY["min_weekdays"]
                  and len(weekends) >= POLICY["min_weekends"])
    # Compare integer room-day totals, not rounded display percentages. A single
    # full weekday or an average merely higher than weekends is not enough.
    pattern = (sufficient and weekday_reserved == weekday_total
               and weekend_reserved < weekend_total)
    weekend = is_weekend(day)
    reason = ("weekend" if weekend else "insufficient" if not sufficient else
              "weekday_average_full" if pattern else "unchanged")
    closed = reason == "weekday_average_full"
    revenue = current.get("revenue")
    if isinstance(revenue, bool) or not isinstance(revenue, (int, float)) or not math.isfinite(revenue) or revenue < 0:
        revenue = None
    return {"policy_version": POLICY["version"], "date": day.isoformat(), "reason": reason,
            "assumed_closed": closed, "day_type": "weekend" if weekend else "weekday",
            "sample_start": start.isoformat(), "sample_end": day.isoformat(),
            "weekday_days": len(weekdays), "weekend_days": len(weekends), "sufficient": sufficient,
            "weekday_rate": weekday_reserved / weekday_total if weekday_total else None,
            "weekend_rate": weekend_reserved / weekend_total if weekend_total else None,
            "reserved": 0 if closed else current["reserved"],
            "total": 0 if closed else current["total"],
            "rate": None if closed else current["reserved"] / current["total"],
            "revenue": 0 if closed else revenue}


def annotate_history(rows):
    normalized = normalize_history(rows)
    return [{**row, "business_adjustment": assess(normalized, row)} for row in normalized]


def reason_label(adjustment):
    if not adjustment:
        return "집계 제외"
    return {"weekend": "주말 · 원본 유지", "weekday_average_full": "평일 미영업 추정 · 평일 평균 100%",
            "insufficient": "비교 자료 부족 · 원본 유지", "unchanged": "보정 없음"}[adjustment["reason"]]


def workbook_bytes(path, state, histories):
    """Append derived columns to an export without rewriting stored observations."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from naver_room_booking_upgraded import STATUS_SHEET_NAME

    wb = load_workbook(path)
    headers = ["보정 가동률", "보정 예상매출", "영업 보정 사유", "평일 평균(28일)",
               "주말 평균(28일)", "평일 표본일", "주말 표본일", "보정 기준"]
    def write(ws, row, start, adjustment):
        a = adjustment or {}
        values = [a.get("rate"), a.get("revenue"), reason_label(adjustment),
                  a.get("weekday_rate"), a.get("weekend_rate"),
                  a.get("weekday_days"), a.get("weekend_days"), POLICY["version"]]
        for offset, value in enumerate(values):
            cell = ws.cell(row, start + offset, value)
            if value is None:
                cell.value = None
            if isinstance(value, str):
                cell.data_type = "s"
            cell.number_format = "0.0%" if offset in (0, 3, 4) else "#,##0" if offset == 1 else "General"
    def heading(ws, start):
        for offset, title in enumerate(headers):
            cell = ws.cell(1, start + offset, title)
            cell.font = Font(name="맑은 고딕", color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="2563A6")
            ws.column_dimensions[get_column_letter(start + offset)].width = 38 if offset == 2 else 20
        ws.auto_filter.ref = ws.dimensions

    try:
        entries = {e["sheet_title"]: e for e in state["entries"]}
        if STATUS_SHEET_NAME in wb.sheetnames:
            ws = wb[STATUS_SHEET_NAME]
            for row in range(2, ws.max_row + 1):
                entry = entries.get(str(ws.cell(row, 3).value))
                a = entry.get("business_adjustment") if entry and not entry.get("archived") else None
                raw = ws.cell(row, 4).value
                row_date = raw.date().isoformat() if hasattr(raw, "hour") else raw.isoformat() if isinstance(raw, date) else str(raw)
                write(ws, row, 13, a if a and a["date"] == row_date else None)
            heading(ws, 13)
        for title, entry in entries.items():
            if title not in wb.sheetnames or title == STATUS_SHEET_NAME:
                continue
            ws = wb[title]
            records = {h["date"]: h.get("business_adjustment") for h in histories.get(entry["key"], [])}
            for row in range(2, ws.max_row + 1):
                raw = ws.cell(row, 1).value
                row_date = raw.date().isoformat() if hasattr(raw, "hour") else raw.isoformat() if isinstance(raw, date) else str(raw)
                write(ws, row, 7, records.get(row_date))
            heading(ws, 7)
        output = io.BytesIO()
        wb.save(output)
        return output.getvalue()
    finally:
        wb.close()
