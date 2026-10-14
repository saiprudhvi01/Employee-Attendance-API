# Employee Attendance & Analytics API

A FastAPI + MongoDB attendance tracking system with analytics endpoints.

## Features

- Employee CRUD operations with validation
- Attendance punch-in/punch-out with race-safe concurrency
- Manual attendance regularization with audit trail
- Monthly employee attendance summaries
- Department-level analytics and summaries
- Late-comers leaderboard with ranking
- Department trend analysis with moving averages
- MongoDB query explain endpoint for performance analysis

## Business Rules

All business rules (R1-R10) are implemented per the OpenAPI specification:
- R1: IST timezone handling for attendance dates and overnight shifts
- R2: Late calculation with 10-minute grace period
- R3: Overtime calculation with 30-minute minimum
- R4: Work hours rounded half-up to 2 decimals
- R5: Half-day detection (< 4.50 hours)
- R6-R10: Presence counting, working days, rounding rules, headcount, pagination

## Setup

### Prerequisites

- Python 3.9+
- MongoDB 6.0+ (or MongoDB Atlas free tier)

### Installation

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (Linux/Mac)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Create a `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas:
```env
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

**Important:** Add `.env` to `.gitignore` - never commit credentials.

### Running MongoDB (Local Docker)

```bash
docker run -p 27017:27017 mongo:7
```

### Seed Sample Data

```bash
python sample_seed.py
```

This loads the nine sample documents from `sample_data/` into your database.

### Start the Server

```bash
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`

## API Endpoints

### System
- `GET /health` - Health check (pings MongoDB)

### Employees
- `POST /employees` - Create employee
- `GET /employees` - List employees (with pagination and department filter)

### Attendance
- `POST /attendance/punch-in` - Record punch-in
- `POST /attendance/punch-out` - Record punch-out
- `PATCH /attendance/{emp_code}/{date}` - Regularize/correct attendance record
- `GET /attendance` - List attendance records (with filters and pagination)

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Monthly summary for employee
- `GET /analytics/departments/summary` - Department summary for month
- `GET /analytics/leaderboard/late` - Late-comers leaderboard
- `GET /analytics/departments/{department}/trend` - Daily attendance trend

### Admin
- `GET /admin/explain/{endpoint}` - MongoDB explain plan for endpoint queries

## Testing

The health endpoint should respond within 20 seconds:

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "ok"}
```

## Implementation Details

- **Concurrency**: Uses database unique indexes and conditional updates for race safety
- **Timezones**: All instants stored as UTC in MongoDB, converted to epoch ms in API
- **Validation**: Pydantic models with regex validators for emp_code, email, time formats
- **Indexes**: Created at startup idempotently for query performance
- **Rounding**: Uses Decimal with ROUND_HALF_UP for financial-grade precision

## Files

- `app/main.py` - Complete API implementation (single file as required)
- `app/__init__.py` - Empty package marker
- `requirements.txt` - Python dependencies
- `REVIEW.md` - Bug analysis of starter code
- `DECISIONS.md` - Design decisions and trade-offs
- `sample_seed.py` - Database seeding script
- `sample_data/` - Sample MongoDB documents
