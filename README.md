# Projects
Python, Data, Software AI projects with tests and documentation.

## Index
- [2026-10-13 Webhook Outbox](projects/2026-10-13-webhook-outbox/) – reliable webhook delivery (FastAPI, SQLite outbox, HMAC signing, backoff, dead-letter).
- [2026-10-13 taskgraph](projects/2026-10-13-taskgraph/) – make-like task runner with content-hash caching, parallel DAG execution and failure propagation (Python stdlib).
- [2026-10-14 demandcast](projects/2026-10-14-demand-forecast/) – leakage-safe day-ahead hourly demand forecasting with seasonal-naive baselines, Ridge and gradient boosting (scikit-learn, synthetic data).
- [2026-10-15 scd2-warehouse](projects/2026-10-15-scd2-warehouse/) – Type-2 slowly changing dimension built from daily snapshots with one set-based SQL rebuild; idempotent, handles deletes/late arrivals, invariant checker (SQLite, Python stdlib).
- [2026-10-16 lsmkv](projects/2026-10-16-lsm-kv/) – LSM-tree key-value store: WAL, memtable, SSTables with Bloom filters and checksums, tombstones, compaction, crash recovery (Python stdlib).
- [2026-10-19 eventlake](projects/2026-10-19-eventlake/) – contract-validated idempotent JSONL ingestion into a partitioned lake: dedupe, quarantine, schema-drift report, late-data watermark, crash-safe checkpoints, compaction (Python stdlib, SQLite).
- [2026-10-17 groundcheck](projects/2026-10-17-groundcheck/) – claim-level faithfulness checker for AI answers: supported / unsupported / contradicted vs. sources, offline lexical verifier with dev/test evaluation plus optional Claude judge (Python stdlib).
- [2026-10-18 logpulse](projects/2026-10-18-logpulse/) – log template mining + SQL minute-window features + IsolationForest incident detection vs z-score baseline, with a FastAPI service (SQLite, scikit-learn, synthetic data).
- [2026-10-20 ratelimit-gateway](projects/2026-10-20-ratelimit-gateway/) – FastAPI rate-limit middleware with fixed-window, sliding-log, sliding-counter and token-bucket algorithms, route policies, RateLimit headers and a measured comparison (Python, FastAPI).
- [2026-10-21 etaband](projects/2026-10-21-etaband/) – delivery-time prediction intervals: split-conformal Ridge baseline vs quantile GBM vs conformalized quantile regression, chronological split, coverage/interval-score/segment evaluation (scikit-learn, synthetic data).
- [2026-10-22 minidbt](projects/2026-10-22-minidbt/) – dbt-style SQL model runner: ref() DAG, incremental merge models, selectors, failure propagation and data tests (Python stdlib, SQLite).
- [2026-10-23 apidiff](projects/2026-10-23-apidiff/) – direction-aware OpenAPI breaking-change detector: request vs response rules, $ref/recursion handling, text/JSON output, CI exit codes (Python stdlib).
- [2026-10-26 cdcsync](projects/2026-10-26-cdcsync/) – CDC change-stream applier that converges under reordering, duplicates and crashes: LSN last-writer-wins, tombstones, atomic transaction staging, offset+data in one commit, bucketed-checksum reconciliation vs a naive baseline (Python stdlib, SQLite).
- [2026-10-26 goldenrec](projects/2026-10-26-goldenrec/) – customer entity resolution / MDM: normalisation, multi-key blocking, weighted Jaro-Winkler scoring with DOB veto, constrained clustering against household over-merges, consensus survivorship with lineage, stable golden IDs across merges/splits, review queue; measured vs exact-match baseline (Python stdlib, SQLite).
