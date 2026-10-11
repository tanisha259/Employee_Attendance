# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing employee attendance lifecycle tracking, business rules (R1–R10), audit regularization, and real-time aggregation analytics.

## Tech Stack
- **Python**: 3.11+ (tested on Python 3.12)
- **Framework**: FastAPI, Uvicorn, Pydantic v2
- **Database**: MongoDB 6.0+ (tested on MongoDB 7+/9+) via PyMongo
- **Testing**: PyTest, HTTPX

---

## Getting Started

### 1. Environment Setup
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Set `MONGO_URI` and `MONGO_DB` in your environment (defaults to `mongodb://localhost:27017` and `attendance_db` if omitted):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive OpenAPI documentation will be accessible at [http://localhost:8000/docs](http://localhost:8000/docs).

---

## Running Automated Tests
Run the test suite with:
```bash
python -m pytest tests/test_api.py -v
```

---

## Key Features & Business Rules
- **R1 Attendance Date & Overnight Shifts**: Timestamps truncated to whole seconds; overnight shifts (`shift_end <= shift_start`) punched prior to shift end belong to the preceding day.
- **R2 Late**: 10-minute grace period; late minutes calculated from `shift_start` when elapsed seconds > 600.
- **R3 Overtime**: Minimum 30-minute threshold; accurately accounts for overnight shifts ending the next day.
- **R4 Work Hours**: Exact decimal arithmetic with round-half-up (not banker's rounding).
- **R5 Half Day**: Flagged when work hours < 4.50; counted as 0.5 present days on weekdays.
- **R7 Working Days & Joiners**: Mon–Fri calendar days; mid-month joiners count from `joined_on`.
- **R9 Headcount**: Includes all active employees with `joined_on <= period_end`, including those with zero logs.
- **Audit Regularization (`PATCH`)**: Concurrency-safe audit trail with granular field change tracking (`from`/`to`).
- **Analytics Aggregations**:
  - `employee_monthly`: Single-employee monthly attendance statistics.
  - `departments/summary`: Department-level summary via `$lookup` preserving 0-log headcount.
  - `leaderboard/late`: Standard competition ranking (1, 2, 2, 4) using `$setWindowFields` with tie-handling.
  - `departments/{department}/trend`: Calendar day gap filling via `$densify`, forward-fill via `$fill`, and 7-day moving averages.
- **Admin Explain Probe**: `/admin/explain/{endpoint}` reports execution plans backed by `IXSCAN` without `COLLSCAN`.

---

## Submission Deliverables
- `app/main.py`: Complete application code with self-contained models, routes, rules, and aggregations.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect review of the original starter code with root causes and fixes.
- `DECISIONS.md`: Design rationale for indexes, concurrency guarantees, tie breaking, headcount, and scale.
- `README.md`: Quickstart and execution guide.
