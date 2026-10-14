# Employee Attendance & Analytics API

FastAPI + MongoDB solution for the HROne engineering assignment.

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp .env.example .env                                # or export real env vars
                                                    # (real env vars always win)
uvicorn app.main:app --port 8000
```

All indexes are created automatically at startup (idempotent). `GET /health`
returns 200 once MongoDB answers a ping.

## What's here

* All endpoints from `openapi.yaml`, including the four analytics endpoints as
  MongoDB aggregation pipelines (`$unionWith`, `$setWindowFields`/`$rank`,
  `$densify`) and `GET /admin/explain/{endpoint}`.
* The starter's defects are fixed and documented in `REVIEW.md`; design answers
  are in `DECISIONS.md`.
* Concurrency is handled at the database: unique indexes decide punch-in races,
  an atomic conditional update decides punch-out races, and regularization uses
  optimistic locking on the history length.

## Notes

* Instants are stored as BSON UTC datetimes and exposed as epoch milliseconds;
  calendar dates/times are IST strings per the data model.
* Rounding is half-up via `Decimal` (Python's `round()` is half-even).
* Nothing is missing from the contract as far as I know.
