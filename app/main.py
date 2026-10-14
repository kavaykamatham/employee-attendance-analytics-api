"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import json
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes():
    """Idempotent indexes for uniqueness, list filters, joins and aggregation ranges."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_code")


@app.on_event("startup")
def startup():
    create_indexes()


def as_utc(dt: datetime) -> datetime:
    # PyMongo returns naive BSON datetimes in UTC by default.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def epoch_ms(dt: Optional[datetime]):
    if dt is None:
        return None
    return int(as_utc(dt).replace(microsecond=0).timestamp() * 1000)


def dt_from_ms(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("timestamp must be an integer epoch-millisecond value in range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def parse_date(value: str, field: str = "date") -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field} must be a valid YYYY-MM-DD date")


def parse_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def rounded(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_minutes(value: str) -> int:
    try:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError
        h, m = map(int, value.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        raise HTTPException(422, "shift times must use HH:MM")


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = as_utc(punch_in).astimezone(IST)
    if shift_minutes(shift_end) <= shift_minutes(shift_start) and local.hour * 60 + local.minute < shift_minutes(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None, attendance_day: Optional[str] = None) -> int:
    local = as_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    base_day = date.fromisoformat(attendance_day) if attendance_day else local.date()
    # A post-midnight punch for an overnight shift belongs to the previous day's shift.
    start_day = base_day
    if shift_end and shift_minutes(shift_end) <= shift_minutes(shift_start):
        if attendance_day:
            start_day = base_day
        elif local.hour * 60 + local.minute < shift_minutes(shift_end):
            start_day = local.date() - timedelta(days=1)
    start = datetime.combine(start_day, time(h, m), tzinfo=IST)
    elapsed_seconds = (local - start).total_seconds()
    elapsed = int(elapsed_seconds // 60)
    return elapsed if elapsed_seconds > 600 else 0


def shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    d = parse_date(date_str)
    h, m = map(int, shift_end.split(":"))
    end_day = d + timedelta(days=1) if shift_minutes(shift_end) <= shift_minutes(shift_start) else d
    return datetime.combine(end_day, time(h, m), tzinfo=IST).astimezone(UTC)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return rounded(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    delta_seconds = (as_utc(punch_out) - shift_end_datetime(date_str, shift_start, shift_end)).total_seconds()
    return int(delta_seconds // 60) if delta_seconds >= 1800 else 0


def record_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        out[key] = epoch_ms(out.get(key))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_ms(change[side])
    return out


def valid_page(page: int, page_size: int):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def date_match(month: str) -> dict:
    start, end = parse_month(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictBody):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_date_valid(cls, v):
        if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v): raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictBody):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def timestamp_valid(cls, v):
        if v is not None: dt_from_ms(v)
        return v


class PunchOutIn(StrictBody):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def timestamp_valid(cls, v):
        if v is not None: dt_from_ms(v)
        return v


class RegularizeIn(StrictBody):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp_valid(cls, v):
        if v is not None: dt_from_ms(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    
    return {**{k: v for k, v in doc.items() if k not in ("_id", "created_at")}, "created_at": epoch_ms(doc["created_at"])}



@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        if isinstance(item.get("created_at"), datetime): item["created_at"] = epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None: raise HTTPException(422, "punched_at must be an integer epoch-millisecond timestamp")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    try: ts = dt_from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    except ValueError as e: raise HTTPException(422, str(e))
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], day), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None: raise HTTPException(422, "punched_at must be an integer epoch-millisecond timestamp")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    # Select latest eligible open record; conditional update makes concurrent calls atomic.
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if doc is None: raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    elapsed = (ts - as_utc(doc["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(doc["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], doc["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}},
        return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                   status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
    if date_from: parse_date(date_from, "date_from")
    if date_to: parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    if not db.employees.find_one({"emp_code": emp_code}): raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    emp = db.employees.find_one({"emp_code": emp_code})
    changes_requested = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    if "status" in changes_requested and changes_requested["status"] is None: raise HTTPException(422, "status cannot be null")
    # Distinguish omitted values from explicitly supplied nulls: null punch times are not valid request values.
    if any(v is None for k, v in changes_requested.items() if k in ("punch_in", "punch_out")):
        raise HTTPException(422, "punch timestamps cannot be null when supplied")
    new_status = changes_requested.get("status", old.get("status"))
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
        new_in = new_out = None
    else:
        raw_in = changes_requested.get("punch_in", epoch_ms(old.get("punch_in")))
        raw_out = changes_requested.get("punch_out", epoch_ms(old.get("punch_out")))
        new_in = dt_from_ms(raw_in) if raw_in is not None else None
        new_out = dt_from_ms(raw_out) if raw_out is not None else None
        if new_in is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    if new_status in ("ABSENT", "LEAVE"):
        new_values.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"], date)
        hours = compute_work_hours(new_in, new_out) if new_out else None
        overtime = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date) if new_out else 0
        half = hours < 4.50 if hours is not None else False
        new_values.update(work_hours=hours, late_minutes=late, overtime_minutes=overtime, half_day=half)
    old_values = {k: old.get(k) for k in new_values}
    old_values["late_minutes"] = old.get("late_minutes", 0)
    old_values["overtime_minutes"] = old.get("overtime_minutes", 0)
    old_values["half_day"] = old.get("half_day", False)
    actual = {}
    for key, val in new_values.items():
        before = old_values.get(key)
        if key in ("punch_in", "punch_out"):
            if before is not None: before = as_utc(before)
            if val is not None: val = as_utc(val)
        if before != val:
            actual[key] = {"from": before, "to": val}
    if not actual: raise HTTPException(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": actual}
    update_set = dict(new_values)
    # Compare-and-swap on old history length and key fields prevents lost updates; retry is deliberately not implicit.
    history = old.get("history", [])
    history_guard = {"$or": [{"history": []}, {"history": {"$exists": False}}]} if not history else {"history": history}
    result = db.attendance_logs.update_one(
        {"_id": old["_id"], "$and": [history_guard, {"status": old.get("status")}, {"punch_in": old.get("punch_in")}, {"punch_out": old.get("punch_out")}]},
        {"$set": update_set, "$push": {"history": entry}})
    if result.modified_count != 1: raise HTTPException(409, "record changed concurrently; retry regularization")
    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return record_out(updated)


def weekday_count(start: date, end: date) -> int:
    if end < start: return 0
    count = 0
    for n in range((end - start).days + 1):
        if (start + timedelta(days=n)).weekday() < 5: count += 1
    return count


def monthly_pipeline(emp_code: str, month: str):
    start, end = parse_month(month)
    start_s, end_s = start.isoformat(), end.isoformat()
    weekdays = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start_s]}}}}, [2, 3, 4, 5, 6]]}
    is_present_weekday = {"$and": [
        {"$in": ["$logs.status", list(PRESENCE)]},
        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
        weekdays,
    ]}
    date_range = {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$_periodStart", "endDate": "$_monthEnd", "unit": "day"}}, 1]}]}
    dates_from_range = {"$map": {"input": date_range, "as": "offset", "in": {"$dateAdd": {"startDate": "$_periodStart", "unit": "day", "amount": "$$offset"}}}}
    weekday_dates = {"$size": {"$filter": {"input": dates_from_range, "as": "day", "cond": {"$and": [
        {"$gte": [{"$dayOfWeek": "$$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$$day"}, 6]}
    ]}}}}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$addFields": {
            "_joinedDate": {"$dateFromString": {"dateString": "$joined_on"}},
            "_monthStart": {"$dateFromString": {"dateString": start_s}},
            "_monthEnd": {"$dateFromString": {"dateString": end_s}},
        }},
        {"$addFields": {"_periodStart": {"$cond": [{"$gt": ["$_joinedDate", "$_monthStart"]}, "$_joinedDate", "$_monthStart"]}}},
        {"$addFields": {"_workingDays": {"$cond": [{"$gt": ["$_joinedDate", "$_monthEnd"]}, 0, weekday_dates]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}
        ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "working_days": {"$first": "$_workingDays"},
            "present_days": {"$sum": {"$cond": [is_present_weekday, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
        {"$addFields": {"_attendancePct": {"$cond": [{"$gt": ["$working_days", 0]}, {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, None]}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "working_days": 1, "present_days": 1, "leave_days": 1,
                       "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
                       "attendance_pct": {"$cond": [{"$ne": ["$_attendancePct", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_attendancePct", 10000]}, 0.5]}}, 10000]}, None]}}},
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "employee not found")
    result = list(db.employees.aggregate(monthly_pipeline(emp_code, month)))
    stats = result[0] if result else {"working_days": 0}
    working = stats.get("working_days", 0)
    present = rounded(stats.get("present_days", 0), 2)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
            "leave_days": stats.get("leave_days", 0), "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0), "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
            "attendance_pct": stats.get("attendance_pct")}


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "_employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$and": [{"$ne": ["$logs.date", None]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start.isoformat()]}}}}, [2,3,4,5,6]]}]}, {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}, 0]}},
            "_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "_hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$_employee_codes"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$_hours_n", 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$_hours_sum", "$_hours_n"]}, 100]}, 0.5]}}, 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department), allowDiskUse=True))
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, start: date, end: date):
    start_s, end_s = start.isoformat(), end.isoformat()
    return [
        {"$match": {"department": department}},
        {"$facet": {
            "seed": [{"$group": {"_id": {"$literal": start_s}, "present_count": {"$sum": 0}, "late_count": {"$sum": 0}}}],
            "days": [
                {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date",
                    "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}}}
            ]
        }},
        {"$project": {"all_days": {"$concatArrays": ["$seed", "$days"]}}},
        {"$unwind": "$all_days"}, {"$replaceRoot": {"newRoot": "$all_days"}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)]}}},
        {"$set": {"date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]},
                   "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_moving": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                       "attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]}, None]},
                       "moving_avg_7d": {"$cond": [{"$ne": ["$_moving", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_moving", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$sort": {"date": 1}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = parse_date(from_, "from"), parse_date(to, "to")
    if end < start: raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, end), allowDiskUse=True))
    return {"department": department, "from": start.isoformat(), "to": end.isoformat(), "items": rows}


def explain_raw(collection, pipeline=None, query=None, sort=None, skip=0, limit=None):
    if pipeline is not None:
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
        result = db.command({"explain": command, "verbosity": "executionStats"})
    else:
        find_cmd = {"find": collection.name, "filter": query or {}, "sort": sort or {}, "skip": skip}
        if limit is not None: find_cmd["limit"] = limit
        result = db.command({"explain": find_cmd, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.CANONICAL_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if status is not None: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        ex = explain_raw(db.attendance_logs, query=q, sort={"date": -1, "emp_code": 1}, skip=(page-1)*page_size, limit=page_size)
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        ex = explain_raw(db.employees, pipeline=monthly_pipeline(emp_code, month)); coll = "employees"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        ex = explain_raw(db.employees, pipeline=department_summary_pipeline(month, department)); coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        ex = explain_raw(db.attendance_logs, pipeline=leaderboard_pipeline(month, limit, department)); coll = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start or (end-start).days+1 > 92: raise HTTPException(422, "invalid trend range")
        ex = explain_raw(db.employees, pipeline=department_trend_pipeline(department, start, end)); coll = "employees"
    else:
        raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": coll, "explain": ex}
