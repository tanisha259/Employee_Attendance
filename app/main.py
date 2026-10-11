"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import json
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB connection configuration (reads from environment; defaults provided)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Initializes all necessary indexes idempotently at application startup."""
    # Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("joined_on", 1)])

    # Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Domain Helpers & Rule Implementations (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Round half-up (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms_to_dt(ms: int) -> datetime:
    """Converts integer epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_utc_truncated() -> datetime:
    """Returns current UTC datetime truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    ist_time_str = punch_in_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start

    if is_overnight and ist_time_str < shift_end:
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    punch_in_ist = punch_in_utc.astimezone(IST)
    delta_sec = int((punch_in_ist - shift_start_dt).total_seconds())

    if delta_sec > 600:
        return delta_sec // 60
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_date = (datetime.fromisoformat(attendance_date) + timedelta(days=1)).date().isoformat()
    else:
        shift_end_date = attendance_date

    eh, em = map(int, shift_end.split(":"))
    shift_end_dt = datetime.fromisoformat(shift_end_date).replace(
        hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST
    )
    punch_out_ist = punch_out_utc.astimezone(IST)
    delta_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    ot_min = delta_sec // 60

    if ot_min >= 30:
        return ot_min
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    work_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(work_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a MongoDB attendance_logs document to the AttendanceRecord response schema."""
    history_out = []
    for h in doc.get("history", []):
        changes_out = {}
        for k, v in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                changes_out[k] = {
                    "from": dt_to_epoch_ms(v.get("from")),
                    "to": dt_to_epoch_ms(v.get("to")),
                }
            else:
                changes_out[k] = {"from": v.get("from"), "to": v.get("to")}
        history_out.append({
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a MongoDB employees document to the Employee response schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start", "09:30")
        if v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


def validate_epoch_millis(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("Must be integer epoch milliseconds; floats and strings rejected")
    if val < 100000000000 or val > 4102444800000:
        raise ValueError("Epoch milliseconds value out of valid range")
    return val


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatus = PresenceStatus.PRESENT

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_punches(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)



# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. Health Probe
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )


# 2. Create Employee
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    created_at = now_utc_truncated()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Employee with this emp_code already exists",
        )

    return format_employee_doc(doc)


# 3. List Employees
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 4. Punch In
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = now_utc_truncated()

    attendance_date = compute_attendance_date(
        punch_in_dt, emp["shift_start"], emp["shift_end"]
    )
    late_minutes = compute_late_minutes(
        punch_in_dt, emp["shift_start"], attendance_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already punched in for this date",
        )

    return format_attendance_doc(doc)


# 5. Punch Out
@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = now_utc_truncated()

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record or not record.get("punch_in"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found",
        )

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in",
        )

    # Validation: punch_out more than 24 hours after punch_in
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    # If already punched out, return 409
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    # Compute derived fields
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = work_hours < 4.50
    overtime_minutes = compute_overtime(
        punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"]
    )

    # Atomic update to avoid punch-out race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    return format_attendance_doc(updated_doc)


# 6. List Attendance
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[Status] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be greater than date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter.value
    if date_from or date_to:
        date_filter: Dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        q["date"] = date_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 7. Regularize Attendance (PATCH)
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record not found for this date",
        )

    # Rule: setting status ABSENT or LEAVE clears both punch times
    if body.status in (Status.ABSENT, Status.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
            )
        target_status = body.status.value
        target_punch_in_dt = None
        target_punch_out_dt = None
    else:
        target_status = body.status.value if body.status is not None else record["status"]
        if target_status in ("ABSENT", "LEAVE"):
            target_punch_in_dt = None
            target_punch_out_dt = None
        else:
            # Presence status requires punch_in
            if body.punch_in is not None:
                target_punch_in_dt = epoch_ms_to_dt(body.punch_in)
            else:
                target_punch_in_dt = record.get("punch_in")
                if target_punch_in_dt and target_punch_in_dt.tzinfo is None:
                    target_punch_in_dt = target_punch_in_dt.replace(tzinfo=timezone.utc)

            if target_punch_in_dt is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A presence status requires a punch_in timestamp",
                )

            # Punch_in must stay on record's attendance date
            computed_date = compute_attendance_date(
                target_punch_in_dt, emp["shift_start"], emp["shift_end"]
            )
            if computed_date != date:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_in must stay on the record's attendance date",
                )

            if body.punch_out is not None:
                target_punch_out_dt = epoch_ms_to_dt(body.punch_out)
            else:
                target_punch_out_dt = record.get("punch_out")
                if target_punch_out_dt and target_punch_out_dt.tzinfo is None:
                    target_punch_out_dt = target_punch_out_dt.replace(tzinfo=timezone.utc)

            if target_punch_out_dt is not None:
                if target_punch_out_dt <= target_punch_in_dt:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="punch_out must be strictly after punch_in",
                    )
                if (target_punch_out_dt - target_punch_in_dt).total_seconds() > 24 * 3600:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="punch_out cannot be more than 24 hours after punch_in",
                    )

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(
            target_punch_in_dt, emp["shift_start"], date
        )
        if target_punch_out_dt is not None:
            new_work_hours = compute_work_hours(target_punch_in_dt, target_punch_out_dt)
            new_half_day = new_work_hours < 4.50
            new_overtime_minutes = compute_overtime(
                target_punch_out_dt, emp["shift_start"], emp["shift_end"], date
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes dictionary comparing old vs new values
    changes: Dict[str, Any] = {}

    def normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    old_punch_in = normalize_dt(record.get("punch_in"))
    old_punch_out = normalize_dt(record.get("punch_out"))

    if record.get("status") != target_status:
        changes["status"] = {"from": record.get("status"), "to": target_status}
    if old_punch_in != target_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in_dt}
    if old_punch_out != target_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out_dt}
    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if bool(record.get("half_day", False)) != new_half_day:
        changes["half_day"] = {"from": bool(record.get("half_day", False)), "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request that changes nothing is rejected",
        )

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match by current history array length
    cur_history_len = len(record.get("history", []))
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": cur_history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in_dt,
                "punch_out": target_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict; please retry",
        )

    return format_attendance_doc(updated_doc)


# 8. Employee Monthly Summary
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate working_days (R7: Mon-Fri, counted from joined_on)
    joined_on = emp["joined_on"]
    working_days = 0
    cur_d = date(year, m, 1)
    end_d = date(year, m, num_days)
    while cur_d <= end_d:
        if cur_d.weekday() < 5 and cur_d.isoformat() >= joined_on:
            working_days += 1
        cur_d += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "status": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late_minutes = int(stats["total_late_minutes"])
        total_overtime_minutes = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# 9. Department Summary
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "logs": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {
                            "is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "is_present": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "half_day": {"$ifNull": ["$$l.half_day", False]},
                            "work_hours": "$$l.work_hours",
                            "late_minutes": {"$ifNull": ["$$l.late_minutes", 0]},
                            "status": "$$l.status",
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_weekday", "$$l.is_present"]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for item in raw_items:
        items.append({
            "department": item["department"],
            "headcount": int(item["headcount"]),
            "present_days": round_half_up(item["present_days"], 2),
            "avg_work_hours": round_half_up(item.get("avg_work_hours"), 2),
            "late_count": int(item["late_count"]),
            "total_late_minutes": int(item["total_late_minutes"]),
            "leave_count": int(item["leave_count"]),
            "on_duty_count": int(item["on_duty_count"]),
        })

    return {"month": month, "items": items}


# 10. Late Leaderboard
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# 11. Department Trend
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Verify valid dates
    try:
        f_date = date.fromisoformat(from_date)
        t_date = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid date format, expected YYYY-MM-DD",
        )

    if t_date < f_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date cannot be before 'from' date",
        )

    num_days = (t_date - f_date).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    # 404 unknown department
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found",
        )

    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$project": {
                "employees": 1,
                "date": {"$literal": from_dt},
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {"$fill": {"output": {"employees": {"method": "locf"}}}},
        {
            "$project": {
                "date": 1,
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "employees": 1,
                "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_str": 1,
                "employees": 1,
                "is_working_day": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date_str"]},
                        }
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date_str", "dept_emps": "$employees.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date_str": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for item in raw_items:
        items.append({
            "date": item["date"],
            "is_working_day": bool(item["is_working_day"]),
            "headcount": int(item["headcount"]),
            "present_count": round_half_up(item["present_count"], 2),
            "late_count": int(item["late_count"]),
            "attendance_rate": round_half_up(item.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(item.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# 12. Admin Explain Endpoint
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[Status] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown endpoint: {endpoint}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be greater than date_to",
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter.value
        if date_from or date_to:
            date_filter: Dict[str, str] = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            q["date"] = date_filter

        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly",
            )
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$project": {
                    "is_weekday": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "status": 1,
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", "$is_present"]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary",
            )
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        match_stage = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "logs": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "is_weekday": {
                                    "$in": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                                "is_present": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                "half_day": {"$ifNull": ["$$l.half_day", False]},
                                "work_hours": "$$l.work_hours",
                                "late_minutes": {"$ifNull": ["$$l.late_minutes", 0]},
                                "status": "$$l.status",
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_weekday", "$$l.is_present"]},
                                        {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_work_hours_sum": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                        "$$l.work_hours",
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_work_hours_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                        1,
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                            }
                        }
                    },
                    "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                    "emp_leave_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                            }
                        }
                    },
                    "emp_on_duty_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$emp_present_days"},
                    "work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                    "work_hours_count": {"$sum": "$emp_work_hours_count"},
                    "late_count": {"$sum": "$emp_late_count"},
                    "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                    "leave_count": {"$sum": "$emp_leave_count"},
                    "on_duty_count": {"$sum": "$emp_on_duty_count"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$work_hours_count", 0]},
                            {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
        ]
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard",
            )
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to parameters are required",
            )
        try:
            f_date = date.fromisoformat(from_date)
            t_date = date.fromisoformat(to_date)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid date format",
            )
        if t_date < f_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'to' date cannot be before 'from' date",
            )
        num_days = (t_date - f_date).days + 1
        if num_days > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Date range cannot exceed 92 days",
            )

        from_dt = datetime.fromisoformat(from_date)
        to_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

        pipeline = [
            {"$match": {"department": department}},
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
                }
            },
            {
                "$project": {
                    "employees": 1,
                    "date": {"$literal": from_dt},
                }
            },
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt],
                    },
                }
            },
            {"$fill": {"output": {"employees": {"method": "locf"}}}},
            {
                "$project": {
                    "date": 1,
                    "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                    "employees": 1,
                    "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]},
                }
            },
            {
                "$project": {
                    "date": 1,
                    "date_str": 1,
                    "employees": 1,
                    "is_working_day": 1,
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$employees",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date_str"]},
                            }
                        }
                    },
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"cur_date": "$date_str", "dept_emps": "$employees.emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$cur_date"]},
                                        {"$in": ["$emp_code", "$$dept_emps"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "date_str": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
                            }
                        }
                    },
                }
            },
            {
                "$project": {
                    "date": "$date_str",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
        ]
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(exp)),
        }
