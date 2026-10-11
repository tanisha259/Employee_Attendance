# DECISIONS.md

1. **Indexes.**
   I created a unique index on `employees.emp_code` to enforce identity uniqueness, and `employees` indexes on `department` and `joined_on` to back department filtering and aggregation joins. On `attendance_logs`, I created a unique compound index on `(emp_code, date)` to enforce at most one record per employee per day and eliminate punch-in race conditions; `(date, -1, emp_code, 1)` to back paginated attendance listings; and `(emp_code, punch_in, -1)` for instant punch-out lookups. I considered and rejected an index on `(status, date)` due to low cardinality and write overhead without scan improvements.

2. **Punch-in race.**
   Both requests validate payload timestamps and derive the attendance date concurrently. Both attempt an `insert_one`. The database serializes writes against the unique index on `(emp_code, date)`: the first request commits and returns HTTP 201 Created; the second trips MongoDB's unique constraint with a `DuplicateKeyError`. Our code catches this error and returns HTTP 409 Conflict without relying on flawed check-then-insert patterns.

3. **Ties.**
   Using `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), tied employees share the same rank. Because `limit` filters rows where `rank <= limit`, all employees tied at or before the cutoff rank are returned, even if the total row count exceeds `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.**
   The department summary pipeline originates on `employees`, matching `joined_on <= month_end`, and groups by department with `headcount: {$sum: 1}`. Attendance records are joined via `$lookup`. Employees who have zero attendance records still produce an employee document with an empty log array, guaranteeing they are fully counted.

5. **One thing you would change for 100x the data.**
   I would pre-aggregate attendance into a daily/monthly roll-up collection during punch-out and scheduled end-of-day jobs instead of executing full multi-stage cross-collection `$lookup` aggregations on demand across tens of millions of raw log documents.
