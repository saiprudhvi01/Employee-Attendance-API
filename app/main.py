"""Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This API follows the contract in openapi.yaml and stores data per DATA_MODEL.md.
All code lives in this single file as required.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from enum import Enum

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# IST timezone (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# MongoDB client with tz_aware=True for timezone-aware datetimes
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# --------------------------------------------------------------------------- #
# Validators and Type Definitions
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


def validate_epoch_millis(value: Optional[int]) -> Optional[int]:
    """Validate that value is an integer epoch milliseconds in valid range."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("must be an integer")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("must be between 100000000000 and 4102444800000")
    return value


# --------------------------------------------------------------------------- #
# Time Conversion Helpers
# --------------------------------------------------------------------------- #

def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.replace(microsecond=0)


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (remove microseconds)."""
    return dt.replace(microsecond=0)


def utc_to_ist(dt: datetime) -> datetime:
    """Convert UTC datetime to IST datetime."""
    return dt.astimezone(IST)


def ist_to_utc(dt: datetime) -> datetime:
    """Convert IST datetime to UTC datetime."""
    return dt.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Business Rule Helpers (R1-R5)
# --------------------------------------------------------------------------- #

def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute the attendance date per R1.
    - For normal shifts: IST calendar date of punch_in
    - For overnight shifts (shift_end <= shift_start): if punch_in IST time is before shift_end,
      it belongs to the shift that started the previous day.
    """
    punch_in_ist = utc_to_ist(punch_in_utc)
    punch_in_ist = truncate_to_seconds(punch_in_ist)
    
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    
    # Check if overnight shift
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    
    if is_overnight:
        # For overnight shifts, if punch_in IST is before shift_end, it belongs to previous day's shift
        punch_time = (punch_in_ist.hour, punch_in_ist.minute)
        if punch_time < (eh_h, eh_m):
            # This punch is for the shift that started yesterday
            punch_in_ist = punch_in_ist - timedelta(days=1)
    
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    Compute late minutes per R2.
    - Grace period is 10 minutes
    - Late only if strictly more than 10 minutes after shift_start
    - Late minutes = whole minutes elapsed since shift_start (not since grace end)
    """
    punch_in_ist = utc_to_ist(punch_in_utc)
    punch_in_ist = truncate_to_seconds(punch_in_ist)
    
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_ist = punch_in_ist.replace(hour=sh_h, minute=sh_m, second=0, microsecond=0)
    
    # Calculate seconds difference
    seconds_late = (punch_in_ist - shift_start_ist).total_seconds()
    
    # Only late if strictly more than 600 seconds (10 minutes)
    if seconds_late <= 600:
        return 0
    
    # Floor to whole minutes
    return int(seconds_late // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Compute work hours per R4.
    - (punch_out - punch_in) in seconds / 3600
    - Rounded to 2 decimals half-up (not banker's rounding)
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(seconds) / Decimal(3600)
    # Round half-up to 2 decimals
    rounded = hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, date_str: str) -> int:
    """
    Compute overtime minutes per R3.
    - Whole minutes between shift_end and punch_out
    - Only counted when >= 30 minutes, otherwise 0
    - Handles overnight shifts
    """
    punch_out_ist = utc_to_ist(punch_out_utc)
    punch_out_ist = truncate_to_seconds(punch_out_ist)
    
    eh_h, eh_m = map(int, shift_end.split(":"))
    
    # Build shift_end datetime in IST
    # For overnight shifts, shift_end is on the next calendar day
    date_dt = datetime.fromisoformat(date_str)
    shift_end_ist = datetime.combine(date_dt, datetime.min.time()).replace(
        hour=eh_h, minute=eh_m, second=0, microsecond=0, tzinfo=IST
    )
    
    # Check if overnight shift
    sh_h, sh_m = map(int, shift_end.split(":"))
    # We need shift_start to determine overnight, but we can infer from date_str
    # Actually, we need the employee's shift_start. This function should take it.
    # For now, assume we handle it in the caller.
    
    # Calculate seconds difference
    seconds_overtime = (punch_out_ist - shift_end_ist).total_seconds()
    
    if seconds_overtime < 0:
        return 0
    
    # Floor to whole minutes
    minutes = int(seconds_overtime // 60)
    
    # Only count if >= 30 minutes
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """
    Compute half_day per R5.
    - True when rounded work_hours < 4.50
    """
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Record Serializer
# --------------------------------------------------------------------------- #

def serialize_attendance_record(doc: dict) -> dict:
    """
    Convert MongoDB document to API response format.
    - Convert datetimes to epoch ms
    - Apply defaults for missing fields
    - Never include _id
    """
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": datetime_to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }
    return result


def serialize_history(history: list) -> list:
    """Serialize history entries, converting datetimes to epoch ms."""
    result = []
    for entry in history:
        serialized = {
            "at": datetime_to_epoch_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {}
        }
        for field, change in entry.get("changes", {}).items():
            serialized_change = {}
            if "from" in change:
                if isinstance(change["from"], datetime):
                    serialized_change["from"] = datetime_to_epoch_ms(change["from"])
                else:
                    serialized_change["from"] = change["from"]
            if "to" in change:
                if isinstance(change["to"], datetime):
                    serialized_change["to"] = datetime_to_epoch_ms(change["to"])
                else:
                    serialized_change["to"] = change["to"]
            serialized["changes"][field] = serialized_change
        result.append(serialized)
    return result


def serialize_employee(doc: dict) -> dict:
    """Convert employee document to API response format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    
    @field_validator('emp_code')
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        import re
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError('emp_code must match pattern EMP followed by 4-6 digits')
        return v
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError('invalid email format')
        return v
    
    @field_validator('shift_start', 'shift_end')
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        import re
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError('time must be in HH:MM format (24-hour)')
        return v
    
    @field_validator('joined_on')
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        from datetime import datetime
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError('date must be in YYYY-MM-DD format')
        return v
    
    @field_validator('shift_end')
    @classmethod
    def validate_shift_not_equal(cls, v: str, info) -> str:
        if 'shift_start' in info.data and info.data['shift_start'] == v:
            raise ValueError('shift_end must differ from shift_start')
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatus = PresenceStatus.PRESENT
    
    @field_validator('punched_at')
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    
    @field_validator('punched_at')
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str
    
    @field_validator('punch_in', 'punch_out')
    @classmethod
    def validate_epoch(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)
    
    @field_validator('reason')
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError('reason must be between 5 and 200 characters')
        return v
    
    @field_validator('regularized_by')
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if len(v) < 1 or len(v) > 50:
            raise ValueError('regularized_by must be between 1 and 50 characters')
        return v


class PaginationParams(BaseModel):
    page: int = 1
    page_size: int = 20
    
    @field_validator('page')
    @classmethod
    def validate_page(cls, v: int) -> int:
        if v < 1:
            raise ValueError('page must be at least 1')
        return v
    
    @field_validator('page_size')
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError('page_size must be between 1 and 100')
        return v


# --------------------------------------------------------------------------- #
# Index Creation
# --------------------------------------------------------------------------- #

async def create_indexes():
    """Create all required indexes idempotently."""
    # Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    
    # Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await create_indexes()
    yield
    # Shutdown
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Health check - ping MongoDB and return 503 on failure."""
    try:
        db.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code enforced by database index."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending."""
    q = {}
    if department:
        q["department"] = department
    
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    
    items = list(
        db.employees
        .find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    
    return {
        "items": [serialize_employee(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in with race-safe unique index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    
    # Get punch time
    if body.punched_at:
        punch_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_utc = datetime.now(UTC)
    punch_utc = truncate_to_seconds(punch_utc)
    
    # Compute attendance date per R1
    date_str = attendance_date(punch_utc, emp["shift_start"], emp["shift_end"])
    
    # Compute late minutes per R2
    late_minutes = compute_late_minutes(punch_utc, emp["shift_start"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status.value,
        "punch_in": punch_utc,
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
        raise HTTPException(status_code=409, detail="already punched in for this date")
    
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out with race-safe conditional update."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    
    # Get punch time
    if body.punched_at:
        punch_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_utc = datetime.now(UTC)
    punch_utc = truncate_to_seconds(punch_utc)
    
    # Find the latest open record for this employee
    latest = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_utc},
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )
    
    if not latest:
        raise HTTPException(status_code=404, detail="no open punch-in found")
    
    punch_in_utc = latest["punch_in"]
    
    # Validate punch_out > punch_in
    if punch_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    
    # Validate within 24 hours
    if (punch_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    
    # Compute derived fields
    work_hours = compute_work_hours(punch_in_utc, punch_utc)
    
    # For overtime, we need to handle overnight shifts properly
    # Get the shift end in IST for the attendance date
    date_str = latest["date"]
    shift_end = emp["shift_end"]
    shift_start = emp["shift_start"]
    
    # Determine if overnight shift
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    
    # Build shift_end datetime
    date_dt = datetime.fromisoformat(date_str)
    if is_overnight:
        # Shift end is on the next day
        shift_end_dt = datetime.combine(date_dt + timedelta(days=1), datetime.min.time())
    else:
        shift_end_dt = datetime.combine(date_dt, datetime.min.time())
    
    shift_end_dt = shift_end_dt.replace(hour=eh_h, minute=eh_m, second=0, microsecond=0, tzinfo=IST)
    shift_end_utc = ist_to_utc(shift_end_dt)
    
    # Compute overtime
    seconds_overtime = (punch_utc - shift_end_utc).total_seconds()
    if seconds_overtime < 0:
        overtime_minutes = 0
    else:
        minutes = int(seconds_overtime // 60)
        overtime_minutes = minutes if minutes >= 30 else 0
    
    half_day = compute_half_day(work_hours)
    
    # Race-safe update using conditional update
    result = db.attendance_logs.update_one(
        {
            "_id": latest["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="record already punched out")
    
    # Fetch updated document
    updated = db.attendance_logs.find_one({"_id": latest["_id"]})
    return serialize_attendance_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct an attendance record with optimistic locking."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(status_code=404, detail="attendance record not found")
    
    old_status = doc["status"]
    old_punch_in = doc.get("punch_in")
    old_punch_out = doc.get("punch_out")
    old_work_hours = doc.get("work_hours")
    old_late_minutes = doc.get("late_minutes", 0)
    old_overtime_minutes = doc.get("overtime_minutes", 0)
    old_half_day = doc.get("half_day", False)
    old_history = doc.get("history", [])
    
    # Build new values
    new_status = body.status.value if body.status is not None else old_status
    new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old_punch_in
    new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old_punch_out
    
    # Validation rules
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    elif new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
    
    # Validate punch_in stays on the record's date (R1)
    if new_punch_in:
        computed_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must be on the record's attendance date")
    
    # Validate punch_out after punch_in and within 24 hours
    if new_punch_in and new_punch_out:
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    
    # Recompute derived fields
    if new_punch_in and new_punch_out:
        new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
        
        # Recompute late minutes
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"])
        
        # Recompute overtime
        sh_h, sh_m = map(int, emp["shift_start"].split(":"))
        eh_h, eh_m = map(int, emp["shift_end"].split(":"))
        is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
        
        date_dt = datetime.fromisoformat(date)
        if is_overnight:
            shift_end_dt = datetime.combine(date_dt + timedelta(days=1), datetime.min.time())
        else:
            shift_end_dt = datetime.combine(date_dt, datetime.min.time())
        
        shift_end_dt = shift_end_dt.replace(hour=eh_h, minute=eh_m, second=0, microsecond=0, tzinfo=IST)
        shift_end_utc = ist_to_utc(shift_end_dt)
        
        seconds_overtime = (new_punch_out - shift_end_utc).total_seconds()
        if seconds_overtime < 0:
            new_overtime_minutes = 0
        else:
            minutes = int(seconds_overtime // 60)
            new_overtime_minutes = minutes if minutes >= 30 else 0
        
        new_half_day = compute_half_day(new_work_hours)
    else:
        new_work_hours = None
        new_late_minutes = 0 if new_status in ("ABSENT", "LEAVE") else old_late_minutes
        new_overtime_minutes = 0
        new_half_day = False
    
    # Build changes object (only fields that actually changed)
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {
            "from": datetime_to_epoch_ms(old_punch_in) if old_punch_in else None,
            "to": datetime_to_epoch_ms(new_punch_in) if new_punch_in else None
        }
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {
            "from": datetime_to_epoch_ms(old_punch_out) if old_punch_out else None,
            "to": datetime_to_epoch_ms(new_punch_out) if new_punch_out else None
        }
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}
    
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")
    
    # Build history entry
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    # Optimistic locking: use old history length in filter
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day
        },
        "$push": {"history": history_entry}
    }
    
    result = db.attendance_logs.update_one(
        {
            "_id": doc["_id"],
            "history": old_history
        },
        update_doc
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="concurrent modification")
    
    updated = db.attendance_logs.find_one({"_id": doc["_id"]})
    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    
    items = list(
        db.attendance_logs
        .find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    
    return {
        "items": [serialize_attendance_record(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    """Monthly attendance summary for one employee using aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    
    # Parse month
    year, month_num = map(int, month.split('-'))
    
    # Calculate working days (Mon-Fri) from max(joined_on, month start) to month end
    joined_on = datetime.fromisoformat(emp["joined_on"]).date()
    month_start = datetime(year, month_num, 1).date()
    month_end = (datetime(year, month_num + 1, 1) - timedelta(days=1)).date() if month_num < 12 else datetime(year + 1, 1, 1).date() - timedelta(days=1)
    
    start_date = max(joined_on, month_start)
    
    # Count working days (Mon-Fri)
    working_days = 0
    current = start_date
    while current <= month_end:
        if current.weekday() < 5:  # Mon-Fri
            working_days += 1
        current += timedelta(days=1)
    
    # Aggregation for attendance
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month, "$lt": f"{year}-{month_num + 1:02d}" if month_num < 12 else f"{year + 1}-01"}
        }},
        {"$addFields": {
            "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
        }},
        {"$match": {"weekday": {"$in": [2, 3, 4, 5, 6]}}},  # Mon-Fri in MongoDB (1=Sun, 2=Mon, ..., 6=Fri, 7=Sat)
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]
    
    result = list(db.attendance_logs.aggregate(pipeline))
    
    if result:
        data = result[0]
        present_days = data["present_days"]
        leave_days = data["leave_days"]
        late_count = data["late_count"]
        total_late_minutes = data["total_late_minutes"]
        total_overtime_minutes = data["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0
    
    # Calculate attendance percentage
    if working_days > 0:
        attendance_pct = float((Decimal(present_days) / Decimal(working_days) * Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
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
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    """Department summary for a month using aggregation."""
    year, month_num = map(int, month.split('-'))
    month_start = f"{month}-01"
    month_end = f"{year}-{month_num + 1:02d}-01" if month_num < 12 else f"{year + 1}-01-01"
    
    # Build match for employees
    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department
    
    # Aggregation starting from employees
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp_code"]},
                            {"$gte": ["$date", month_start]},
                            {"$lt": ["$date", month_end]}
                        ]
                    }
                }},
                {"$addFields": {
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "is_leave": {"$eq": ["$status", "LEAVE"]},
                    "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
                    "has_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                    "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }},
                {"$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_present", {"$in": ["$weekday", [2, 3, 4, 5, 6]]}]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                    "on_duty_days": {"$sum": {"$cond": ["$is_on_duty", 1, 0]}},
                    "late_count": {"$sum": {"$cond": ["$has_late", 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_work_hours": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                                "$work_hours",
                                0
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$logs.present_days", 0]}},
            "leave_days": {"$sum": {"$ifNull": ["$logs.leave_days", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$logs.on_duty_days", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$logs.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.total_late_minutes", 0]}},
            "total_work_hours": {"$sum": {"$ifNull": ["$logs.total_work_hours", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$logs.work_hours_count", 0]}}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "leave_days": 1,
            "on_duty_count": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$total_work_hours", "$work_hours_count"]},
                    None
                ]
            },
            "_id": 0
        }},
        {"$sort": {"department": 1}}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    
    # Round avg_work_hours to 2 decimals half-up
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = float(Decimal(item["avg_work_hours"]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    
    # Filter out departments with zero headcount
    items = [item for item in items if item["headcount"] > 0]
    
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers leaderboard using aggregation with standard competition ranking."""
    year, month_num = map(int, month.split('-'))
    month_start = f"{month}-01"
    month_end = f"{year}-{month_num + 1:02d}-01" if month_num < 12 else f"{year + 1}-01-01"
    
    # Build match
    match_stage = {
        "date": {"$gte": month_start, "$lt": month_end},
        "late_minutes": {"$gt": 0}
    }
    
    # Aggregation
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
    ]
    
    if department:
        pipeline.append({"$match": {"department": department}})
    
    # Add ranking with $setWindowFields
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1, "_id": 1},
            "output": {
                "rank": {
                    "$rank": {}
                }
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
            "_id": 0
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    
    items = list(db.attendance_logs.aggregate(pipeline))
    
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias='from'),
    to_date: str = Query(..., alias='to')
):
    """Daily attendance trend for a department using $densify for gap filling."""
    # Validate date range
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    
    # Calculate range length
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date)
    if (to_dt - from_dt).days > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    
    # Check if department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="department not found")
    
    # Aggregation with $densify
    pipeline = [
        # Start with all dates in range
        {"$documents": [{"date": from_date}]},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_date, to_date]
            }
        }},
        # Add weekday and is_working_day
        {"$addFields": {
            "date_obj": {"$dateFromString": {"dateString": "$date"}},
            "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
        }},
        # Lookup employees for headcount
        {"$lookup": {
            "from": "employees",
            "let": {"date": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "joined_on": {"$lte": "$$date"}
                }},
                {"$count": "count"}
            ],
            "as": "emp_count"
        }},
        {"$addFields": {"headcount": {"$arrayElemAt": ["$emp_count.count", 0]}}},
        {"$project": {"emp_count": 0}},
        # Lookup attendance logs
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$date", "$$date"]},
                    "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
                }},
                {"$group": {
                    "_id": None,
                    "present_count": {
                        "$sum": {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    }
                }}
            ],
            "as": "attendance"
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]}
        }},
        {"$project": {"attendance": 0}},
        # Calculate attendance_rate
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": [{"$ifNull": ["$headcount", 0]}, 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        # Calculate moving average with $setWindowFields
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "rates_window": {
                    "$collect": {
                        "input": "$attendance_rate",
                        "into": "rates"
                    }
                }
            }
        }},
        # Note: $setWindowFields with window is MongoDB 5.0+, using simpler approach
        # For moving average, we'll use $setWindowFields with window
    ]
    
    # MongoDB 5.0+ has $setWindowFields with window, but for compatibility we'll use a different approach
    # Let's use $setWindowFields with documents window
    pipeline = [
        {"$documents": [{"date": from_date}]},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_date, to_date]
            }
        }},
        {"$addFields": {
            "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"date": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "joined_on": {"$lte": "$$date"}
                }},
                {"$count": "count"}
            ],
            "as": "emp_count"
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$emp_count.count", 0]}, 0]}}},
        {"$project": {"emp_count": 0}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$date", "$$date"]},
                    "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
                }},
                {"$group": {
                    "_id": None,
                    "present_count": {
                        "$sum": {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    }
                }}
            ],
            "as": "attendance"
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]}
        }},
        {"$project": {"attendance": 0}},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": [{"$ifNull": ["$headcount", 0]}, 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "prev_rates": {
                    "$push": "$attendance_rate"
                }
            }
        }},
        # Calculate moving average (this is complex in pure aggregation, doing in Python for simplicity)
        {"$sort": {"date": 1}}
    ]
    
    raw_items = list(db.employees.aggregate(pipeline))
    
    # Calculate moving average in Python (simpler and more reliable)
    items = []
    for i, item in enumerate(raw_items):
        # Get non-null rates from current and up to 6 previous rows within range
        rates = []
        for j in range(max(0, i - 6), i + 1):
            if raw_items[j].get("attendance_rate") is not None:
                rates.append(raw_items[j]["attendance_rate"])
        
        if rates:
            moving_avg = float((Decimal(sum(rates)) / Decimal(len(rates))).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        else:
            moving_avg = None
        
        # Round attendance_rate to 4 decimals
        attendance_rate = item.get("attendance_rate")
        if attendance_rate is not None:
            attendance_rate = float(Decimal(attendance_rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        
        items.append({
            "date": item["date"],
            "is_working_day": item["is_working_day"],
            "headcount": item["headcount"],
            "present_count": item["present_count"],
            "late_count": item["late_count"],
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg
        })
    
    return {
        "department": department,
        "items": items
    }


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias='from'),
    to_date_param: Optional[str] = Query(None, alias='to'),
    page: Optional[int] = None,
    page_size: Optional[int] = None
):
    """Run explain with executionStats for the named endpoint's main query."""
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"invalid endpoint, must be one of {valid_endpoints}")
    
    collection = None
    pipeline_or_query = None
    
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        pipeline_or_query = {"find": collection, "filter": q}
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection = "attendance_logs"
        year, month_num = map(int, month.split('-'))
        pipeline_or_query = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": month, "$lt": f"{year}-{month_num + 1:02d}" if month_num < 12 else f"{year + 1}-01"}
            }},
            {"$addFields": {
                "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }},
            {"$match": {"weekday": {"$in": [2, 3, 4, 5, 6]}}},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }}
        ]
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "employees"
        year, month_num = map(int, month.split('-'))
        month_start = f"{month}-01"
        month_end = f"{year}-{month_num + 1:02d}-01" if month_num < 12 else f"{year + 1}-01-01"
        
        emp_match = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        
        pipeline_or_query = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$emp_code"]},
                                {"$gte": ["$date", month_start]},
                                {"$lt": ["$date", month_end]}
                            ]
                        }
                    }},
                    {"$addFields": {
                        "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "is_leave": {"$eq": ["$status", "LEAVE"]},
                        "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
                        "has_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                    }},
                    {"$group": {
                        "_id": "$emp_code",
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {"$and": ["$is_present", {"$in": ["$weekday", [2, 3, 4, 5, 6]]}]},
                                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                    0
                                ]
                            }
                        },
                        "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                        "on_duty_days": {"$sum": {"$cond": ["$is_on_duty", 1, 0]}},
                        "late_count": {"$sum": {"$cond": ["$has_late", 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "total_work_hours": {
                            "$sum": {
                                "$cond": [
                                    {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                                    "$work_hours",
                                    0
                                ]
                            }
                        },
                        "work_hours_count": {
                            "$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$logs.present_days", 0]}},
                "leave_days": {"$sum": {"$ifNull": ["$logs.leave_days", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$logs.on_duty_days", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$logs.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.total_late_minutes", 0]}},
                "total_work_hours": {"$sum": {"$ifNull": ["$logs.total_work_hours", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$logs.work_hours_count", 0]}}
            }}
        ]
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        year, month_num = map(int, month.split('-'))
        month_start = f"{month}-01"
        month_end = f"{year}-{month_num + 1:02d}-01" if month_num < 12 else f"{year + 1}-01-01"
        
        match_stage = {
            "date": {"$gte": month_start, "$lt": month_end},
            "late_minutes": {"$gt": 0}
        }
        
        pipeline_or_query = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.joined_on": {"$lte": month_end}}},
            {"$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
        ]
        
        if department:
            pipeline_or_query.append({"$match": {"department": department}})
        
        pipeline_or_query.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1, "_id": 1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }},
            {"$match": {"rank": {"$lte": limit or 10}}}
        ])
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date_param:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        collection = "employees"
        
        pipeline_or_query = [
            {"$documents": [{"date": from_date}]},
            {"$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_date, to_date_param]
                }
            }},
            {"$addFields": {
                "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"date": "$date"},
                "pipeline": [
                    {"$match": {
                        "department": department,
                        "joined_on": {"$lte": "$$date"}
                    }},
                    {"$count": "count"}
                ],
                "as": "emp_count"
            }},
            {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$emp_count.count", 0]}, 0]}}},
            {"$project": {"emp_count": 0}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"date": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$date", "$$date"]},
                        "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
                    }},
                    {"$group": {
                        "_id": None,
                        "present_count": {
                            "$sum": {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}
                        },
                        "late_count": {
                            "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                        }
                    }}
                ],
                "as": "attendance"
            }},
            {"$addFields": {
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]}
            }},
            {"$project": {"attendance": 0}},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": [{"$ifNull": ["$headcount", 0]}, 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }}
        ]
    
    # Run explain
    if isinstance(pipeline_or_query, dict):
        # It's a find query
        explain_result = db[collection].database.command(
            'explain',
            pipeline_or_query,
            verbosity='executionStats'
        )
    else:
        # It's an aggregation
        explain_result = db[collection].database.command(
            'explain',
            {
                'aggregate': collection,
                'pipeline': pipeline_or_query,
                'cursor': {}
            },
            verbosity='executionStats'
        )
    
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_result
    }
