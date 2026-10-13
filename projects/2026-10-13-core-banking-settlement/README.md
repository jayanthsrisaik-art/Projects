# Core Banking Daily Settlement & Member Loan Analytics (local reimplementation)

A runnable, synthetic-data version of a credit-union daily settlement pipeline: flat-file ingestion with checksum validation, staging with error redirection, SCD Type 2 member/loan dimensions, a per-batch trial-balance audit that gates the fact table, and the mart queries behind delinquency, product-penetration and deposit-velocity reporting.

> **Scope note.** The original design targets SQL Server, SSIS, T-SQL, dbt and Power BI. None of those are available here, so this version uses **Python + SQLite** and plain SQL. It reproduces the *logic* (SCD2 by row hash, double-entry audit, gated loads, as-of analytics), not the tools. There is no SSIS package, T-SQL, `MERGE`, columnstore index, partitioning, dbt project, or Power BI file in this repo, and no performance numbers are claimed.

## Problem
Core-system journals arrive as flat files. Before anything reaches reporting you must (1) prove files weren't corrupted, (2) isolate bad rows without failing the load, (3) prove every settlement batch balances to the penny and agrees with the control file, (4) keep history of member/loan attribute changes, and (5) never publish unbalanced batches.

## Architecture
```
samples/input/*.txt + MANIFEST.sha256
   └─ ingest.py   verify SHA-256 → quarantine bad files; parse → stg_* ; bad rows → stg_rejects; file_log (idempotent)
   └─ warehouse.py
        build_dimensions   SCD2 on dim_member / dim_loan_account (row hash, valid_from/valid_to/is_current)
        audit_batches      per batch: SUM(D)-SUM(C)=0 AND totals == control file → audit_batch, exception_log, notification_log
        load_facts         only BALANCED batches → fact_member_transactions (member_key = version valid on txn_date)
   └─ analytics.py  delinquency 30/60/90+, product penetration, MoM deposit velocity, region-scoped portfolio
```
Audit statuses: `BALANCED`, `UNBALANCED`, `CONTROL_MISMATCH`, `NO_CONTROL`.

## Setup & usage
```
pip install -r requirements.txt
python -m pytest -q
python -m settlement.cli generate /tmp/landing         # deterministic synthetic files (seed 42)
python -m settlement.cli run samples/input [--db wh.db] # prints a JSON report
```
`samples/input/` is a small committed landing folder; `samples/expected_report.json` is the report produced from it (the test suite and a diff both check this).

The synthetic data deliberately injects two faults: a malformed amount (`12x.00`) in the 2026-09-02 CORE batch and a one-cent credit error in the 2026-09-03 ACH batch.

## Measured results (this run)
- 16 files loaded, 0 quarantined, 1 row rejected.
- 8 batches audited: 6 `BALANCED`, 2 `UNBALANCED` (`20260902-CORE`, `20260903-ACH`); both held back, one notification logged; 88 fact rows loaded.
- 15 tests passed; the CLI report matched `samples/expected_report.json`.

## Design decisions
- Integer cents everywhere (`Decimal` parsing, no floats), so "to the penny" is exact.
- SCD2 intervals are half-open `[valid_from, valid_to)`; fact rows join the dimension version valid on the transaction date.
- Tracked attributes are branch, region, credit **band** (not raw score), address, share-account count; changes within a band don't create versions. Replaying old snapshots is a no-op.
- Audit checks both internal balance and the external control totals.
- The "RLS" function (`branch_region_portfolio`) only demonstrates region scoping at query level; it is not a security control.

## Limitations
- SQLite, not SQL Server; no SSIS/T-SQL/dbt/Power BI artifacts, no DAX, no incremental-refresh or columnstore work, no timing benchmarks.
- Tiny synthetic data (20 members, 14 loans, 4 days); "delinquency" is balance-weighted on one daily snapshot; "household" is approximated by member.
- Held-back batches have no repair/resubmit workflow; notifications are rows in a table, not email/alerts.
- Corrected files with the same name are not reloaded (file_log marks them loaded); a real system would version files.
