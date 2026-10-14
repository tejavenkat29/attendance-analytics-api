# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)` — enforces one record per day and makes punch-in race-safe; `(date desc, emp_code asc)` — the list endpoint's exact sort plus every month-range analytics match. On `employees`: unique `emp_code` (identity, 409 guarantee), `(department, emp_code)` (filtered listing), `(department, joined_on)` and `(joined_on)` (headcount lookups). I considered `(status, date)` for the status filter and rejected it: five possible values is too unselective to pay for on every write, and the date index already avoids a collection scan.

2. **Punch-in race.** Both requests build their document and call `insert_one` — there is no check-then-insert. The unique `(emp_code, date)` index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which I translate to 409.

3. **Ties.** Both employees get the same rank from `$rank` (competition ranking), and the next rank is skipped. `limit` filters `rank <= limit` *after* ranking, so both tied rows are returned — more rows than `limit` — ordered by `emp_code`.

4. **Headcount.** The pipeline `$unionWith`s one row per employee whose `joined_on` is on or before month end, alongside the log rows. Grouping by department sums those rows, so a zero-log employee still counts.

5. **At 100x.** Stop aggregating raw logs at read time: maintain pre-computed daily/monthly summary collections updated on write, and shard `attendance_logs` on `(emp_code, date)`.
