# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for contract v2.0. The application code is in `app/main.py`.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+ (MongoDB 7 is recommended). You can use MongoDB Atlas free tier or a local MongoDB instance.

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` locally and set these values (real environment variables take precedence over `.env`):

- `MONGO_URI` — your MongoDB connection string
- `MONGO_DB` — database name, for example `attendance_db`

For a local instance, use `MONGO_URI=mongodb://localhost:27017`. For Atlas, create a free cluster, create a database user, allow your current IP in Network Access, and copy the Python-driver connection string into `.env`. Keep credentials private; never commit `.env`.

Start the API from the repository root:

```bash
uvicorn app.main:app --port 8000
```

Check `http://127.0.0.1:8000/health` and interactive docs at `http://127.0.0.1:8000/docs`. Indexes are created at startup. The app does not seed or overwrite the grader's database.

## Implementation notes

- Epoch-millisecond instants are converted to/from UTC BSON datetimes at the API boundary.
- Punch-in and employee uniqueness are enforced by MongoDB unique indexes; punch-out uses a conditional atomic update.
- Analytics are aggregation pipelines, including competition ranking and a densified department trend with a seven-row moving average.
- See `REVIEW.md` and `DECISIONS.md` for the starter review and implementation decisions.

## Before submission

Run the endpoints against MongoDB 7 with representative edge cases, including parallel punch-ins/punch-outs, overnight shifts, employees without logs, month joiners, tied leaderboard ranks, and empty trend dates. Check each `/admin/explain/{endpoint}` response on the seeded large dataset to confirm index scans and no collection scans.
