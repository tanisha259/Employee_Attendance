import concurrent.futures
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, init_db_indexes

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_indexes():
    init_db_indexes()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_employee():
    # 1. Valid employee create
    emp_code = "EMP9991"
    db.employees.delete_one({"emp_code": emp_code})
    payload = {
        "emp_code": emp_code,
        "name": "Test User",
        "email": "test.user@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
    }
    r = client.post("/employees", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["emp_code"] == emp_code
    assert isinstance(data["created_at"], int)
    assert "_id" not in data
    assert "id" not in data

    # 2. Duplicate create -> 409
    r_dup = client.post("/employees", json=payload)
    assert r_dup.status_code == 409

    # 3. shift_start == shift_end -> 422
    payload_bad_shift = payload.copy()
    payload_bad_shift["emp_code"] = "EMP9992"
    payload_bad_shift["shift_end"] = "09:30"
    r_bad = client.post("/employees", json=payload_bad_shift)
    assert r_bad.status_code == 422

    # 4. Bad emp_code pattern -> 422
    payload_bad_code = payload.copy()
    payload_bad_code["emp_code"] = "INVALID123"
    r_bad2 = client.post("/employees", json=payload_bad_code)
    assert r_bad2.status_code == 422


def test_list_employees():
    r = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert len(data["items"]) <= 10
    # verify sorted by emp_code
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)
    # verify each employee has created_at as int and no _id
    for item in data["items"]:
        assert isinstance(item["created_at"], int)
        assert "_id" not in item
        assert "id" not in item


def test_punch_in_rules():
    emp_code = "EMP0001"  # shift 09:30 - 18:30
    # Clean previous record for test date
    test_date = "2026-08-10"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Grace period test: 09:40:00 -> 0 late_minutes
    # 2026-08-10 09:40:00 IST is 2026-08-10 04:10:00 UTC
    dt_utc = datetime(2026, 8, 10, 4, 10, 0, tzinfo=timezone.utc)
    ts = int(dt_utc.timestamp() * 1000)
    r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts})
    assert r.status_code == 201
    data = r.json()
    assert data["date"] == test_date
    assert data["late_minutes"] == 0
    assert data["history"] == []
    assert data["half_day"] is False
    assert data["overtime_minutes"] == 0
    assert data["work_hours"] is None
    assert data["punch_out"] is None

    # Duplicate punch-in for same date -> 409
    r_dup = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts})
    assert r_dup.status_code == 409

    # Test late: 09:40:01 -> delta 601s -> 10 late_minutes
    test_date_late = "2026-08-11"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date_late})
    dt_utc_late = datetime(2026, 8, 11, 4, 10, 1, tzinfo=timezone.utc)
    ts_late = int(dt_utc_late.timestamp() * 1000)
    r_late = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_late})
    assert r_late.status_code == 201
    assert r_late.json()["late_minutes"] == 10

    # Overnight shift test: EMP0005 has shift 22:00 - 06:00
    # Punch in on 2026-08-13 at 01:30 IST (which is < 06:00) belongs to date 2026-08-12!
    # 2026-08-13 01:30 IST = 2026-08-12 20:00 UTC
    db.attendance_logs.delete_one({"emp_code": "EMP0005", "date": "2026-08-12"})
    dt_overnight = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)
    ts_overnight = int(dt_overnight.timestamp() * 1000)
    r_on = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ts_overnight})
    assert r_on.status_code == 201
    assert r_on.json()["date"] == "2026-08-12"


def test_punch_out_rules():
    emp_code = "EMP0001"  # shift 09:30 - 18:30
    test_date = "2026-08-14"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Punch in 09:30 IST (04:00 UTC)
    in_dt = datetime(2026, 8, 14, 4, 0, 0, tzinfo=timezone.utc)
    in_ts = int(in_dt.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ts})

    # Punch out after 4 hours: 13:30 IST (08:00 UTC) -> work_hours = 4.0, half_day = True (<4.50)
    out_dt = datetime(2026, 8, 14, 8, 0, 0, tzinfo=timezone.utc)
    out_ts = int(out_dt.timestamp() * 1000)
    r_out = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ts})
    assert r_out.status_code == 200
    data = r_out.json()
    assert data["work_hours"] == 4.0
    assert data["half_day"] is True
    assert data["overtime_minutes"] == 0

    # Second punch out for already punched out record -> 409
    r_out_again = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ts})
    assert r_out_again.status_code == 409

    # Test overtime: shift end is 18:30 IST. Punch out 19:10 IST (+40 min >= 30 min) -> overtime_minutes = 40
    test_date_ot = "2026-08-15"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date_ot})
    in_ot_dt = datetime(2026, 8, 15, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(in_ot_dt.timestamp() * 1000)})
    out_ot_dt = datetime(2026, 8, 15, 13, 40, 0, tzinfo=timezone.utc)  # 19:10 IST
    r_ot = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(out_ot_dt.timestamp() * 1000)})
    assert r_ot.status_code == 200
    assert r_ot.json()["overtime_minutes"] == 40
    assert r_ot.json()["half_day"] is False


def test_list_attendance():
    r = client.get("/attendance?page=1&page_size=5")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) <= 5

    # Test date range validation: date_from > date_to -> 422
    r_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert r_bad.status_code == 422


def test_regularize_attendance():
    emp_code = "EMP0001"
    test_date = "2026-08-16"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Create record via punch in
    in_dt = datetime(2026, 8, 16, 4, 35, 0, tzinfo=timezone.utc)  # 10:05 IST (35 min late)
    in_ts = int(in_dt.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ts})

    # Regularize punch_in to 09:28 IST (03:58 UTC)
    new_in_dt = datetime(2026, 8, 16, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ts = int(new_in_dt.timestamp() * 1000)

    patch_payload = {
        "punch_in": new_in_ts,
        "reason": "Biometric device sensor error",
        "regularized_by": "hr_manager",
    }
    r = client.patch(f"/attendance/{emp_code}/{test_date}", json=patch_payload)
    assert r.status_code == 200
    data = r.json()
    assert data["late_minutes"] == 0  # Recomputed to 0!
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr_manager"
    assert h["reason"] == "Biometric device sensor error"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # Changing nothing -> 422
    r_no_change = client.patch(f"/attendance/{emp_code}/{test_date}", json=patch_payload)
    assert r_no_change.status_code == 422


def test_analytics_employee_monthly():
    # EMP0001 July 2026
    r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200
    data = r.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # Mon-Fri in July 2026
    assert isinstance(data["present_days"], (int, float))
    assert isinstance(data["attendance_pct"], (int, float))


def test_analytics_department_summary():
    r = client.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200
    data = r.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) > 0
    dept_names = [item["department"] for item in data["items"]]
    assert dept_names == sorted(dept_names)
    for item in data["items"]:
        assert item["headcount"] > 0
        assert "present_days" in item
        assert "avg_work_hours" in item


def test_analytics_leaderboard():
    r = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert r.status_code == 200
    data = r.json()
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)
    if data["items"]:
        assert data["items"][0]["rank"] == 1
        assert data["items"][0]["total_late_minutes"] > 0


def test_analytics_department_trend():
    r = client.get("/analytics/departments/Sales/trend?from=2026-07-05&to=2026-07-09")
    assert r.status_code == 200
    data = r.json()
    assert data["department"] == "Sales"
    assert len(data["items"]) == 5  # Exactly 5 calendar days!
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]
    for item in data["items"]:
        assert "is_working_day" in item
        assert "headcount" in item
        assert "attendance_rate" in item
        assert "moving_avg_7d" in item

    # 404 unknown department
    r_404 = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-05&to=2026-07-09")
    assert r_404.status_code == 404

    # 422 date range > 92 days
    r_bad = client.get("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-05-01")
    assert r_bad.status_code == 422


def test_admin_explain():
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-07-05"}),
    ]
    for ep, params in endpoints:
        r = client.get(f"/admin/explain/{ep}", params=params)
        assert r.status_code == 200, f"Explain failed for {ep}: {r.text}"
        data = r.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        # Check plan has IXSCAN and no COLLSCAN in the explain document string
        explain_str = str(data["explain"])
        assert "IXSCAN" in explain_str, f"Missing IXSCAN in explain for {ep}"
        assert "COLLSCAN" not in explain_str, f"Found COLLSCAN in explain for {ep}"


def test_punch_in_concurrency():
    emp_code = "EMP0002"
    test_date = "2026-08-20"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    dt_utc = datetime(2026, 8, 20, 4, 0, 0, tzinfo=timezone.utc)
    ts = int(dt_utc.timestamp() * 1000)

    def do_punch():
        # New client instance for each thread
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        results = [f.result() for f in futures]

    status_codes = [r.status_code for r in results]
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 4


def test_punch_out_concurrency():
    emp_code = "EMP0003"
    test_date = "2026-08-21"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Punch in first
    in_dt = datetime(2026, 8, 21, 4, 0, 0, tzinfo=timezone.utc)
    in_ts = int(in_dt.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ts})

    out_dt = datetime(2026, 8, 21, 13, 0, 0, tzinfo=timezone.utc)
    out_ts = int(out_dt.timestamp() * 1000)

    def do_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ts})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result() for f in futures]

    status_codes = [r.status_code for r in results]
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 4


def test_leaderboard_ties_and_cutoff():
    # Seed specific late logs for month 2026-09 to test standard competition ranking ties
    month = "2026-09"
    db.attendance_logs.delete_many({"date": {"$regex": f"^{month}"}})

    # EMP0001: 50 late minutes
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": f"{month}-01",
        "status": "PRESENT",
        "late_minutes": 50,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
    # EMP0002: 50 late minutes (tied with EMP0001 at rank 1)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": f"{month}-01",
        "status": "PRESENT",
        "late_minutes": 50,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
    # EMP0003: 30 late minutes (should be rank 3, since rank 2 is skipped!)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0003",
        "date": f"{month}-01",
        "status": "PRESENT",
        "late_minutes": 30,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Query with limit=1: both EMP0001 and EMP0002 must be returned (rank 1 <= limit 1)
    r1 = client.get(f"/analytics/leaderboard/late?month={month}&limit=1")
    assert r1.status_code == 200
    data1 = r1.json()
    assert len(data1["items"]) == 2  # More than limit because of tie at cutoff!
    assert data1["items"][0]["rank"] == 1
    assert data1["items"][1]["rank"] == 1
    assert data1["items"][0]["emp_code"] == "EMP0001"
    assert data1["items"][1]["emp_code"] == "EMP0002"

    # Query with limit=3: EMP0003 should have rank 3
    r3 = client.get(f"/analytics/leaderboard/late?month={month}&limit=3")
    assert r3.status_code == 200
    data3 = r3.json()
    assert len(data3["items"]) == 3
    assert data3["items"][2]["rank"] == 3
    assert data3["items"][2]["emp_code"] == "EMP0003"


def test_weekend_records_and_midmonth_joiner():
    # EMP0002 joined on 2026-07-13 (Monday).
    # July 2026 working days (Mon-Fri) from 2026-07-13 to 2026-07-31:
    # 2026-07-13 to 2026-07-17 (5)
    # 2026-07-20 to 2026-07-24 (5)
    # 2026-07-27 to 2026-07-31 (5)
    # Total = 15 working days!
    r = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert r.status_code == 200
    data = r.json()
    assert data["working_days"] == 15

    # Test weekend records: add a Saturday record for EMP0002 (e.g. 2026-07-18)
    # R7: "present_days counts only Mon - Fri records; weekend records still add to late and overtime totals."
    db.attendance_logs.delete_one({"emp_code": "EMP0002", "date": "2026-07-18"})
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-18",  # Saturday
        "status": "PRESENT",
        "late_minutes": 20,
        "overtime_minutes": 60,
        "half_day": False,
        "history": [],
    })
    r_weekend = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    data_weekend = r_weekend.json()
    # present_days should still be 1.0 (from 2026-07-14 in sample data), not 2.0!
    assert data_weekend["present_days"] == 1.0
    # but late_count and overtime should include Saturday!
    assert data_weekend["total_late_minutes"] == 20
    assert data_weekend["total_overtime_minutes"] == 60


def test_epoch_millis_validation():
    # Seconds-looking value < 100000000000 -> 422
    r_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310280})
    assert r_sec.status_code == 422

    # String value -> 422
    r_str = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783310280000"})
    assert r_str.status_code == 422

    # Float value -> 422
    r_float = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310280000.5})
    assert r_float.status_code == 422

