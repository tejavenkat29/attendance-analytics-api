"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real
      environment variables always win because load_dotenv does not override).

Contract: openapi.yaml (rules R1-R10). Storage: DATA_MODEL.md.
"""
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
import json

load_dotenv()  # does not override real environment variables

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                      # BSON dates come back as aware UTC datetimes
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent - create_index is a no-op if it already exists)
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    # one record per (emp_code, date); this is what makes punch-in race-safe
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # default list sort + month-range analytics
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_indexes()
    except PyMongoError:
        # Mongo may come up after us; /health keeps reporting 503 until it does.
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Small helpers: time, rounding, serialization
# --------------------------------------------------------------------------- #
def err422(msg: str, loc: Optional[list] = None) -> HTTPException:
    # mirror FastAPI's validation body: detail is a list of objects
    return HTTPException(
        status_code=422,
        detail=[{"loc": loc or ["body"], "msg": msg, "type": "value_error"}],
    )


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp((ms - ms % 1000) / 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: seeded data read without tz_aware
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def valid_date_str(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        date_cls.fromisoformat(s)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: the IST calendar date the punch-in belongs to."""
    ist = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        # an IST time earlier than shift_end belongs to yesterday's shift
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            return ist.date() - timedelta(days=1)
    return ist.date()


def shift_start_at(day: date_cls, shift_start: str) -> datetime:
    h, m = parse_hhmm(shift_start)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def shift_end_at(day: date_cls, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_hhmm(shift_end)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return datetime(end_day.year, end_day.month, end_day.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: date_cls, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 past shift_start; then floor
    minutes since shift_start (not since the end of the grace period)."""
    seconds = (punch_in - shift_start_at(day, shift_start)).total_seconds()
    if seconds > 600:
        return int(seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Python's round() is half-even)."""
    seconds = Decimal(int((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes past shift_end, counted only when >= 30."""
    seconds = (punch_out - shift_end_at(day, shift_start, shift_end)).total_seconds()
    minutes = int(seconds // 60) if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def derive_closed(punch_in: datetime, punch_out: datetime, day: date_cls,
                  shift_start: str, shift_end: str) -> dict:
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "overtime_minutes": compute_overtime(punch_out, day, shift_start, shift_end),
        "half_day": wh < 4.50,  # R5: on the rounded value
    }


# --------------------------------------------------------------------------- #
# Serializers (BSON document -> API shape; instants become epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, ft in entry.get("changes", {}).items():
        f, t = ft.get("from"), ft.get("to")
        if field in ("punch_in", "punch_out"):
            f, t = dt_to_ms(f), dt_to_ms(t)
        changes[field] = {"from": f, "to": t}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,       # legacy docs omit it
        "history": [history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not valid_date_str(v):
            raise ValueError("joined_on must be a real date, YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise err422("shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # uniqueness is enforced by the unique index, not check-then-insert:
        # concurrent duplicates get exactly one 201, the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cur], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out / list / regularize
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # the unique (emp_code, date) index decides the race: one 201, rest 409
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before ts (works across
    # midnight: the record is found by punch_in, not by today's date)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin: datetime = rec["punch_in"]
    if ts <= pin:
        raise err422("punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise err422("punched_at is more than 24 hours after punch_in")

    day = date_cls.fromisoformat(rec["date"])
    fields = derive_closed(pin, ts, day, emp["shift_start"], emp["shift_end"])
    fields["punch_out"] = ts
    # atomic: only wins if punch_out is still null -> one 200, the loser 409
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    if date_from and date_to and date_from > date_to:
        raise err422("date_from must not be after date_to", ["query", "date_from"])
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q)      # sorted and paginated by MongoDB,
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)   # never loaded into memory
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cur], "total": total,
            "page": page, "page_size": page_size}


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


DERIVED_DEFAULTS = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    if not valid_date_str(date):
        raise err422("date must be YYYY-MM-DD", ["path", "date"])
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    sent = body.model_fields_set
    day = date_cls.fromisoformat(date)
    final_status = body.status if "status" in sent else rec["status"]

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            raise err422("punch times cannot be supplied with ABSENT/LEAVE")
        final_pin, final_pout = None, None
        derived = dict(DERIVED_DEFAULTS)
    else:
        final_pin = ms_to_dt(body.punch_in) if "punch_in" in sent else rec.get("punch_in")
        final_pout = ms_to_dt(body.punch_out) if "punch_out" in sent else rec.get("punch_out")
        if final_pin is None:
            raise err422("a presence status requires a punch_in")
        if "punch_in" in sent:
            # R1: the corrected punch_in must still belong to this record's date
            if attendance_date(final_pin, emp["shift_start"], emp["shift_end"]) != day:
                raise err422("punch_in does not fall on the record's attendance date")
        if final_pout is not None:
            if final_pout <= final_pin:
                raise err422("punch_out must be after punch_in")
            if final_pout - final_pin > timedelta(hours=24):
                raise err422("punch_out is more than 24 hours after punch_in")
        derived = {"late_minutes": compute_late_minutes(final_pin, day, emp["shift_start"]),
                   "work_hours": None, "overtime_minutes": 0, "half_day": False}
        if final_pout is not None:
            derived.update(derive_closed(final_pin, final_pout, day,
                                         emp["shift_start"], emp["shift_end"]))

    final = {"status": final_status, "punch_in": final_pin, "punch_out": final_pout, **derived}
    current = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": rec.get("half_day") or False,
    }
    changes = {k: {"from": current[k], "to": v} for k, v in final.items() if current[k] != v}
    if not changes:
        raise err422("the request changes nothing")

    history_len = len(rec.get("history") or [])
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # optimistic concurrency: only apply if history has not grown since we read
    # it, so two simultaneous corrections cannot silently drop an entry
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"],
         "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}},
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "concurrent correction, retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days_between(start: date_cls, end: date_cls) -> int:
    """Mon-Fri count, inclusive (R7). Pure calendar math, no data involved."""
    days = 0
    d = start
    while d <= end:
        if d.isoweekday() <= 5:
            days += 1
        d += timedelta(days=1)
    return days


IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
LM = {"$ifNull": ["$late_minutes", 0]}
OM = {"$ifNull": ["$overtime_minutes", 0]}
DAY_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}  # R5
DOW = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            # Mon-Fri only (R7); a half day counts 0.5 (R5)
            "present_days": {"$sum": {"$cond": [
                {"$and": [IS_PRESENT, {"$lte": [DOW, 5]}]}, DAY_VALUE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [LM, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": LM},     # weekend records included (R7)
            "total_overtime_minutes": {"$sum": OM},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    start, end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0,
                                "total_late_minutes": 0, "total_overtime_minutes": 0}
    # R7: working days counted from joined_on for mid-month joiners
    wd_start = max(date_cls.fromisoformat(start), date_cls.fromisoformat(emp["joined_on"]))
    wd = working_days_between(wd_start, date_cls.fromisoformat(end)) if wd_start <= date_cls.fromisoformat(end) else 0
    pct = round_half_up(agg["present_days"] / wd * 100, 2) if wd > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": agg["present_days"],
        "leave_days": agg["leave_days"],
        "late_count": agg["late_count"],
        "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"],
        "attendance_pct": pct,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}        # R9
    log_emp_match: dict = {"emp.joined_on": {"$lte": end}}
    if department is not None:
        emp_match["department"] = department
        log_emp_match["emp.department"] = department
    return [
        # one row per log in the month, tagged with its employee's department
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                               # drops orphan logs
        {"$match": log_emp_match},
        {"$project": {
            "department": "$emp.department",
            "hc": {"$literal": 0},
            "present_days": {"$cond": [
                {"$and": [IS_PRESENT, {"$lte": [DOW, 5]}]}, DAY_VALUE, 0]},
            "wh": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                              "$work_hours", 0]},
            "wh_n": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]},
            "late_flag": {"$cond": [{"$gt": [LM, 0]}, 1, 0]},
            "lm": LM,
            "leave_flag": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
            "od_flag": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
        }},
        # plus one row per employee in the period: R9 headcount counts people
        # with zero logs too
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": emp_match},
            {"$project": {"department": 1, "hc": {"$literal": 1},
                          "present_days": {"$literal": 0}, "wh": {"$literal": 0},
                          "wh_n": {"$literal": 0}, "late_flag": {"$literal": 0},
                          "lm": {"$literal": 0}, "leave_flag": {"$literal": 0},
                          "od_flag": {"$literal": 0}}},
        ]}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": "$hc"},
            "present_days": {"$sum": "$present_days"},
            "wh_sum": {"$sum": "$wh"},
            "wh_n": {"$sum": "$wh_n"},
            "late_count": {"$sum": "$late_flag"},
            "total_late_minutes": {"$sum": "$lm"},
            "leave_count": {"$sum": "$leave_flag"},
            "on_duty_count": {"$sum": "$od_flag"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = []
    for r in db.attendance_logs.aggregate(department_summary_pipeline(month, department)):
        # mean over records, not an average of per-employee averages
        avg = round_half_up(r["wh_sum"] / r["wh_n"], 2) if r["wh_n"] > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    p: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                 # logs whose employee is gone: ignored
    ]
    if department is not None:
        p.append({"$match": {"emp.department": department}})  # rank inside the dept
    p += [
        # standard competition ranking: ties share a rank, next rank skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit AFTER ranking (ties stay)
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ,
                     limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


HALF_UP_4DP = lambda expr: {"$divide": [  # noqa: E731 - half-up, $round is half-even
    {"$floor": {"$add": [{"$multiply": [expr, 10000]}, 0.5]}}, 10000]}


def trend_pipeline(department: str, d_from: date_cls, d_to: date_cls, emp_codes: list) -> list:
    f_dt = datetime(d_from.year, d_from.month, d_from.day)
    t_dt = datetime(d_to.year, d_to.month, d_to.day) + timedelta(days=1)
    return [
        {"$match": {"emp_code": {"$in": emp_codes},
                    "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {
            "_id": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$sum": {"$cond": [IS_PRESENT, DAY_VALUE, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [LM, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "d": "$_id", "present_count": 1, "late_count": 1}},
        # exactly one row per calendar day: fill the gaps in the database
        {"$densify": {"field": "d",
                      "range": {"step": 1, "unit": "day", "bounds": [f_dt, t_dt]}}},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "dow": {"$isoDayOfWeek": "$d"},
            "dstr": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
        }},
        # headcount on each day: department employees already joined (R9)
        {"$lookup": {"from": "employees", "let": {"day": "$dstr"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]},
                {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "n"},
        ], "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": [{"$lte": ["$dow", 5]}, {"$gt": ["$headcount", 0]}]},
            HALF_UP_4DP({"$divide": ["$present_count", "$headcount"]}),
            None]}}},
        # 7-day moving average over rows inside the range; $avg skips nulls and
        # is null when the window holds no non-null rate
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate",
                              "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"hc": 0}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[date_cls, Query(alias="from")],
                     to: date_cls):
    if to < from_:
        raise err422("'to' must not be before 'from'", ["query", "to"])
    if (to - from_).days + 1 > 92:
        raise err422("the range must not exceed 92 days", ["query", "to"])
    emp_codes = db.employees.distinct("emp_code", {"department": department})
    if not emp_codes:
        raise HTTPException(404, "unknown department")
    rows = db.attendance_logs.aggregate(trend_pipeline(department, from_, to, emp_codes))
    items = [{
        "date": r["dstr"],
        "is_working_day": r["dow"] <= 5,
        "headcount": r["headcount"],
        "present_count": r["present_count"],
        "late_count": r["late_count"],
        "attendance_rate": r["attendance_rate"],
        "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Annotated[Optional[date_cls], Query(alias="from")] = None,
    to: Optional[date_cls] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    def need(**params):
        for name, value in params.items():
            if value is None:
                raise err422(f"'{name}' is required for {endpoint}", ["query", name])

    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code, date_from, date_to, status)
        inner: dict[str, Any] = {"find": "attendance_logs", "filter": q,
                                 "sort": {"date": -1, "emp_code": 1},
                                 "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        inner = {"aggregate": "attendance_logs",
                 "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        inner = {"aggregate": "attendance_logs",
                 "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        inner = {"aggregate": "attendance_logs",
                 "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:  # department_trend
        need(department=department)
        need(**{"from": from_, "to": to})
        emp_codes = db.employees.distinct("emp_code", {"department": department})
        inner = {"aggregate": "attendance_logs",
                 "pipeline": trend_pipeline(department, from_, to, emp_codes), "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(result))}
