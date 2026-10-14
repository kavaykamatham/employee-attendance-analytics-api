# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses naive local time, mishandles overnight shifts, and does not make the grace comparison explicit in seconds. | Test 09:40:00 and 09:40:01 for a 09:30 shift; test a 05:00 punch for a 22:00-06:00 shift. | Normalize instants to UTC, compare in IST, use the attendance day for overnight shifts, and floor elapsed minutes only after the strict 10-minute check. |
| 2 | `compute_work_hours` | Python/float `round` can use ties-to-even rather than required half-up rounding. | Test a duration exactly halfway between hundredth-hour values. | Use `Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Always uses the same calendar day and counts any positive overtime, not only overtime of at least 30 minutes. | Test a 15-minute overtime and a 22:00-06:00 overnight shift. | Calculate the shift end on the correct day and apply the 30-minute threshold. |
| 4 | `health` | Always reports healthy without checking MongoDB. | Stop MongoDB and call `/health`; starter still returns 200. | Ping MongoDB and return 503 on failure. |
| 5 | `create_employee` | Check-then-insert is race-prone; duplicate requests can both pass the check. | Send parallel POSTs with the same `emp_code`. | Enforce a unique database index and translate `DuplicateKeyError` to 409. |
| 6 | `create_employee` / `EmployeeIn` | Missing contract validation for employee code, email, date, shift format, and equal shift start/end. | Submit malformed codes, invalid dates, and equal shift times. | Add Pydantic validation and reject equal shift times. |
| 7 | `create_employee` | Returns a BSON datetime for `created_at`, but API instants must be epoch milliseconds. | Inspect the JSON response after employee creation. | Store a BSON datetime and convert it to epoch milliseconds at the API boundary. |
| 8 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the department filter; no deterministic sort. | Request page 1 and compare department-filtered totals. | Use `(page - 1) * page_size`, count the filtered query, and sort by `emp_code`. |
| 9 | `punch_in` | Unknown employee causes a server error; timestamps are interpreted as local seconds, not validated epoch milliseconds; milliseconds are not truncated. | Punch for an unknown code or send seconds/float/string timestamps. | Return 404 for unknown employees; validate epoch milliseconds, normalize to UTC, and truncate to seconds. |
| 10 | `punch_in` | Check-then-insert permits duplicate attendance records under concurrency; status is not restricted to presence statuses. | Send simultaneous punch-ins and try `ABSENT` as a punch-in status. | Use a unique `(emp_code, date)` index and a validated status enum. |
| 11 | `list_attendance` | Loads every matching record into Python and paginates after sorting, which is too slow at 100,000 records. | Run it against a large dataset and inspect memory/query execution. | Apply indexed filters, sort, skip, and limit in MongoDB; count the filtered query separately. |
| 12 | `list_attendance` | Missing validation for date order, date format, status, page and page size; returns `_id` as an `id` field and raw datetime values. | Try an inverted date range and inspect returned fields/types. | Validate query parameters and serialize the natural-key record with epoch-millisecond timestamps and no internal ID. |
| 13 | Application startup | No required indexes are created. | Inspect indexes on a fresh database and run the explain endpoint. | Create idempotent unique and query-supporting indexes at startup. |
| 14 | Missing endpoints | Punch-out, regularization, analytics, and explain endpoints are not implemented. | Request any path listed as a candidate-implemented endpoint in `openapi.yaml`. | Implement all contract paths in `app/main.py`. |

### Reviewed and intentionally retained

- `emp_code` is the business identifier; MongoDB `_id` is never exposed.
- Calendar dates remain `YYYY-MM-DD` strings in storage, while instants are BSON datetimes.
- Analytics use stored derived fields rather than recalculating them from punch times.
- A missing attendance record is not automatically treated as an `ABSENT` record.
