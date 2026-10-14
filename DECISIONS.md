# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` prevents duplicate identities. Unique `(emp_code, date)` guarantees one attendance record per employee/day and makes punch-in race-safe. `(date, emp_code)` supports date-range lists and sorting; `(status, date, emp_code)` supports status filters; `(department, joined_on, emp_code)` supports department headcount/trend; `joined_on` supports month headcount without a department filter. I rejected indexes on every derived field because they add write cost without replacing the date-range scans.

2. **Punch-in race.** Parallel requests may both reach the insert, but MongoDB's unique compound index decides uniqueness atomically. One succeeds with 201; the other raises `DuplicateKeyError`, translated to 409. A preliminary read is not the guarantee.

3. **Ties.** The leaderboard uses `$rank` on total late minutes and filters `rank <= limit`. If several employees tie at rank 10, all are returned. Rows are then ordered by `emp_code` for stable output.

4. **Headcount.** Department summary starts from employees who joined by month-end and looks up their logs. Employees without logs remain included; later joiners are filtered out.

5. **100x data.** I would benchmark realistic traffic and query plans, tune indexes from measured workloads, and consider pre-aggregated daily summaries to reduce repeated analytics work.
