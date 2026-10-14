# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | compute_late_minutes (33-37) | Ignores IST timezone, uses datetime's own timezone instead of IST | Punch-in at 09:40:01 UTC would be incorrectly calculated against IST shift | Convert punch_in to IST before calculation |
| 2 | compute_late_minutes (33-37) | Ignores overnight shifts (shift_end <= shift_start) | Overnight shift punch-in at 21:55 for 22:00-06:00 shift gives wrong date | Check if shift is overnight and adjust date calculation per R1 |
| 3 | compute_late_minutes (36) | Truncates minutes before comparing: int(10.01)=10, not >10, returns 0 | Punch-in at 09:40:01 for 09:30 shift returns 0 instead of 10 late | Compare seconds first, then floor minutes after comparison |
| 4 | compute_work_hours (40-41) | Uses Python round() which is banker's rounding, needs half-up per R4 | 8.125 hours rounds to 8.12 instead of 8.13 | Use Decimal with ROUND_HALF_UP |
| 5 | compute_late_minutes, compute_work_hours, compute_overtime | None truncate to whole seconds per R1 | 09:40:00.900 stored as 09:40:00.900 instead of 09:40:00 | Truncate microseconds to 0 before any calculation |
| 6 | compute_overtime (44-47) | No 30-minute minimum per R3 | 25 minutes overtime returns 25 instead of 0 | Return 0 if overtime < 30 |
| 7 | compute_overtime (44-47) | No overnight handling for punch_out on next day | Overnight shift punch-out at 06:40 next day gives wrong overtime | Handle overnight shift end on next calendar day |
| 8 | compute_overtime (46) | Mixes aware (with tzinfo) and naive datetimes | Comparison between aware punch_out and naive end causes errors | Make both datetimes timezone-aware |
| 9 | compute_overtime, compute_late_minutes, compute_work_hours | Don't set half_day at all per R5 | half_day always False even when work_hours < 4.50 | Add half_day computation in punch-out |
| 10 | health (72-74) | Always returns ok, must ping Mongo and return 503 on failure | Returns 200 even when MongoDB is down | Add db.command('ping') and return 503 on exception |
| 11 | create_employee (78-85) | Check-then-insert causes race condition on concurrent creates | Two simultaneous creates with same emp_code both succeed | Use unique index on emp_code and catch DuplicateKeyError |
| 12 | create_employee (78) | No validation: emp_code pattern, email format, shift_start ≠ shift_end, date format | Accepts "INVALID" as emp_code, invalid email, shift_start == shift_end | Add Pydantic validators with regex patterns |
| 13 | create_employee (82) | Stores naive local datetime.now() instead of UTC | created_at stored as local time, not UTC | Use datetime.now(timezone.utc) |
| 14 | create_employee (84) | Returns raw datetime instead of epoch milliseconds | Response contains datetime object, not integer ms | Convert datetime to epoch ms |
| 15 | list_employees (93) | skip = page * page_size skips page 1 (should be page-1) | page=1 skips first 20 records | Use skip = (page - 1) * page_size |
| 16 | list_employees (94) | total counts all employees instead of filtered set | department filter applied but total shows all employees | Use count_documents with the same filter |
| 17 | list_employees (95) | No sort by emp_code | Results returned in insertion order | Add .sort("emp_code", 1) |
| 18 | list_employees (89) | page and page_size have no bounds validation | page=0 or page_size=1000 accepted | Add Pydantic validators with min/max |
| 19 | punch_in (101) | Crashes with 500 when employee doesn't exist (emp["shift_start"] on None) | POST with non-existent emp_code returns 500 | Check if emp is None and return 404 |
| 20 | punch_in (102) | Converts timestamps with local time via fromtimestamp instead of UTC | punched_at=1783312500000 interpreted as local time | Use datetime.fromtimestamp(ts, tz=timezone.utc) |
| 21 | punch_in (103) | Date isn't computed by R1 (IST calendar date, overnight shift handling) | Overnight shift punch-in at 21:55 gets wrong date | Implement attendance_date helper per R1 |
| 22 | punch_in (102) | Doesn't truncate seconds per R1 | 09:40:00.900 stored with milliseconds | Truncate microseconds before storage |
| 23 | punch_in (102) | Doesn't reject seconds-looking values (<100000000000) or floats with 422 | punched_at=1783312500 (seconds) accepted | Add EpochMillis validator |
| 24 | punch_in (106) | Doesn't validate status, accepts ABSENT and LEAVE | POST with status="ABSENT" creates record | Validate status is PRESENT/WFH/ON_DUTY only |
| 25 | punch_in (104) | Check-then-insert causes race condition | Two simultaneous punch-ins for same date both succeed | Use unique index on (emp_code, date) and catch DuplicateKeyError |
| 26 | punch_in (119) | Response leaks id field | Response contains "id": "..." which is not in schema | Remove id field, never expose _id |
| 27 | punch_in (110) | Response returns raw datetimes instead of epoch ms | punch_in field is datetime object | Convert all datetimes to epoch ms in serializer |
| 28 | list_attendance (144) | Loads whole collection into memory and sorts in Python | Slow on 100k records, high memory usage | Use MongoDB sort with index |
| 29 | list_attendance (145) | Sorts by date only, no emp_code tiebreak | Records with same date have unpredictable order | Sort by date descending, then emp_code ascending |
| 30 | list_attendance (133-141) | Never returns 422 for date_from > date_to | date_from=2026-07-10, date_to=2026-07-01 accepted | Add validation and return 422 |
| 31 | list_attendance (89) | No pagination bounds checking (page, page_size) | page=0 or page_size=1000 accepted | Add Pydantic validators |
| 32 | list_attendance (149) | Response leaks id field | Response contains "id" which is not in schema | Remove id field |
| 33 | list_attendance (149) | Response returns raw datetimes instead of epoch ms | punch_in/punch_out are datetime objects | Convert all datetimes to epoch ms in serializer |
| 34 | list_attendance (144) | Doesn't default missing history or half_day | Legacy records without these fields break serialization | Apply defaults in serializer |
| 35 | MongoClient (22) | Not tz_aware=True, returns naive datetimes | All datetimes from DB are naive (no timezone) | Add tz_aware=True to MongoClient |
| 36 | Startup | No indexes created at startup | Queries will COLLSCAN on large datasets | Create all required indexes in lifespan hook |

Also note anything you looked at and decided was **not** a defect, and why:
- load_dotenv() (20): Not a defect - by default it doesn't override existing environment variables, which is the correct behavior
- Optional defaults in models (e.g., shift_start="09:30"): Not a defect - matches OpenAPI spec defaults
- /health route shape returning {"status": "ok"}: Not a defect - matches OpenAPI spec
