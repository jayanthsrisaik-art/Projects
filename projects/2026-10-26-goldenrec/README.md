# goldenrec: customer entity resolution with stable golden IDs

The same customer shows up in the CRM, in billing and in support tickets, written slightly
differently each time: "Bob Smiht" with `ROB.SMITH+shop@gmail.com`, then "Robert Smith" with
`(555) 123-4567` and a US-format birthday. Analytics, marketing and churn models need **one
row per real person**, and downstream tables need an **ID for that person that stays the same**
when the matching is re-run tomorrow.

`goldenrec` is a small master-data pipeline. It normalises records from several sources, finds
duplicates with blocking and weighted field comparison, groups them without chaining household
members together, and builds a golden record with per-field lineage. Golden IDs are kept stable
across runs: merges retire an ID with a forwarding pointer, and splits keep the ID for the larger
part.

Python 3.10+ standard library only (pytest for tests). All data is synthetic.

## Results (measured)

`python benchmark.py` (1,000 synthetic people, about 2,300 source records per split). Weights and
thresholds were set by hand and only inspected on the **dev** seed. **Test** is a fresh draw from
the same generator, reported as it came out.

| split | method | precision | recall | F1 | clusters (true: 1000) |
|---|---|---|---|---|---|
| dev (seed 7) | baseline: exact email OR phone | 0.712 | 0.695 | 0.703 | 1237 |
| dev | goldenrec | **1.000** | **0.960** | **0.980** | 1047 |
| test (seed 2026) | baseline: exact email OR phone | 0.681 | 0.690 | 0.686 | 1218 |
| test | goldenrec without DOB constraint | 0.987 | 0.962 | 0.974 | 1041 |
| test | goldenrec | **1.000** | **0.962** | **0.981** | 1044 |

Metrics are **pairwise**: a pair of records counts as predicted-same if they share a cluster.

* Blocking keeps 99.2% / 99.2% of true pairs (dev / test) while discarding 99.0% of all pairs.
  That puts a ceiling of about 0.99 on recall.
* The DOB constraint never fired on dev. On test it blocked 4 merges, which raised precision from
  0.987 to 1.000 at no recall cost.
* The pairs the pipeline misses mostly score 4–5 (for example name + full address but no email,
  phone or DOB in common). These go to the **review queue** (159 / 172 pairs) instead of being
  auto-merged.
* Timing: 0.45 s for about 2,300 records. 10.7 s for 11,445 records / 376k candidate pairs (5,000
  people). The synthetic data has only 6 ZIP codes, so the surname+ZIP blocks are much denser than
  they would be on real data.
* Precision of 1.000 reflects how clean the synthetic data is. Real data has twins, Jr./Sr. at
  the same address, and shared family emails, so don't expect these numbers on it.

## Architecture

```
 crm.csv ─┐
 billing ─┼─► upsert source_records ──► normalise ──► blocking keys ──► candidate pairs
 support ─┘   (newer updated_at wins)   (names,       e:email p:phone       │
                                         nicknames,   nd:soundex(names)+dob  ▼
                                         gmail, phone, lz:soundex(last)+zip  score_pair
                                         dates, street) fz:soundex(first)+zip (weights, DOB veto)
                                                                            │
                         review_queue ◄── 4 ≤ score < 6        score ≥ 6 ──►│
                                                                            ▼
          golden_records ◄── survivorship ◄── stable ID assignment ◄── constrained union-find
          (+ lineage JSON)    per-field rules  crosswalk / golden_ids /     (no cluster may hold
                                               id_events                     two different DOBs)
```

| module | role |
|---|---|
| `goldenrec/normalize.py` | Canonical forms: accent folding, nickname table, Gmail dot/+tag rules, NANP phones, 4 date formats, street abbreviations, ZIP5 |
| `goldenrec/similarity.py` | Jaro-Winkler, American Soundex, token Jaccard (pure Python) |
| `goldenrec/matcher.py` | Blocking, `score_pair`, `ConstrainedUnionFind`, `resolve`, plus the naive `baseline_clusters` |
| `goldenrec/survivorship.py` | Golden record per cluster with per-field strategy and lineage |
| `goldenrec/store.py` | SQLite pipeline run, stable-ID assignment, merge/split events, `lookup` |
| `goldenrec/synth.py` | Deterministic synthetic sources plus separate ground truth |
| `goldenrec/evaluate.py` | Pairwise P/R/F1, blocking pair completeness and reduction ratio |
| `goldenrec/cli.py` | `generate`, `run`, `evaluate`, `lookup`, `export`, `review` |
| `benchmark.py` | Dev/test comparison, DOB-constraint ablation, timing |

### Scoring

Each field adds or subtracts a fixed weight. A missing field adds nothing, so "unknown" is not
treated as "different".

| evidence | weight |
|---|---|
| same normalised email / different email | +4 / −1 |
| same phone / different phone | +3 / −1 |
| same DOB | +3 |
| **different DOB** | **veto** (never auto-merge) |
| name similarity ≥ 0.92 / ≥ 0.85 / < 0.75 (Jaro-Winkler, nickname-folded, swap-aware) | +3 / +1.5 / −3 |
| same ZIP and street token overlap ≥ 0.75 / same ZIP only | +2 / +0.5 |

A pair with score ≥ 6 is a match. A pair with 4 ≤ score < 6 goes to the review queue. So "same
name + same DOB" matches on its own, while "same surname, address and landline" (household
members) doesn't.

### Stable golden IDs

Each run re-clusters **all** stored records, which keeps the result independent of arrival
order. The new clusters are then mapped onto the previous IDs:

1. Clusters claim IDs largest first. Each cluster keeps the previous ID held by most of its
   members (ties go to the lowest ID).
2. A previous ID that no cluster keeps is **retired** with `merged_into` set to the cluster that
   absorbed most of its members. `lookup` follows that chain, so an ID stored last month in a
   downstream table still resolves.
3. A cluster that can't claim any previous ID gets a new one. It is logged as `created` with
   `split_from` if its members came from an ID that a bigger piece kept.
4. Unchanged clusters produce no events, so re-running the same input is a no-op
   (tested).

Everything for a run (source upserts, crosswalk, golden records, events, review queue, run stats)
happens in **one SQLite transaction**. A bad row in a batch rolls the whole batch back.

### Survivorship

| field | rule | why |
|---|---|---|
| first / last name | **consensus**: value with the highest summed Jaro-Winkler to all candidates (first names compared after nickname folding); formal spelling preferred; then source priority crm > billing > support | Typos and nicknames are per-record noise. Agreement across records is better evidence than which system the value came from. |
| email, phone, street, city, ZIP | most recent `updated_at` | People move and change contact details |
| DOB | most frequent | Clustering already guarantees there is at most one |

Every golden row stores `lineage`, a JSON map of field → `source:source_id` that supplied the
value.

## Setup

```bash
cd projects/2026-10-26-goldenrec
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # only pytest; the package itself is stdlib
```

## Usage

```bash
# initial load of the 29-record sample (12 people, 3 sources)
python -m goldenrec run --db demo.db samples/customers.csv
python -m goldenrec export --db demo.db --out golden.csv     # == samples/expected_golden.csv
python -m goldenrec review --db demo.db                      # possible matches for a data steward

# incremental batch: one record gains an email (merge), one new ticket,
# one stale replay (ignored), one new customer
python -m goldenrec run --db demo.db samples/batch2.csv      # == samples/expected_batch2_summary.json
python -m goldenrec lookup G000011 --db demo.db              # retired id -> G000007 + golden record

# accuracy vs ground truth (truth is never read by the pipeline)
python -m goldenrec evaluate samples/customers.csv samples/truth.csv   # == samples/expected_eval.json

# bigger synthetic run + benchmark
python -m goldenrec generate --entities 1000 --seed 7 --out customers.csv --truth truth.csv
python benchmark.py

python -m pytest -q
```

Input CSV columns: `source, source_id, first_name, last_name, email, phone, dob, street, city, zip,
updated_at` (ISO date). Several files can be passed to one `run`.

### Sample walk-through

`samples/customers.csv` contains two households, which are the tricky part:

* the **Clarks** (Michael and Omar) share a landline and `8358 Hill Ave Apt 4`;
* the **Kims** (Hiro and Thomas) share `1837 Elm Rd`.

Both stay separate people. Michael's records use three spellings (`Clark`, `Clak`, `Clrak`), and
all three are matched through email, DOB and fuzzy name. `samples/expected_eval.json` shows
precision 1.0 / recall 0.93 on the sample. The two missed pairs come from one Olivia Miller record
that has only name + address. It sits in the review queue at score 5.

After `batch2.csv`, that record gains an email: `G000011` is retired into `G000007`, and
`lookup G000011` returns `G000007`. A replay of `BIL-000004` with an older `updated_at` doesn't
overwrite the newer row.

## Design decisions

* **Rules + weights instead of a trained model.** The weights are a hand-set Fellegi-Sunter-style
  score. Every decision is explainable (`reasons` like `email:+4,name:+3`), and the pipeline needs
  no labels to run. Labels are used only for evaluation.
* **Several blocking keys, all combined.** No single key catches everything. Email misses
  people who changed address; Soundex+DOB misses records with no DOB. The order-independent
  name key catches swapped first/last fields. Blocks over 60 records (for example a shared
  `info@` address) are skipped and counted instead of going quadratic.
* **A hard DOB veto plus a constrained union-find.** Plain union-find chains A~B~C even when A and C
  conflict. Merging strongest-first and refusing any merge that would put two DOBs in one cluster
  stops parent/child over-merges through a DOB-less record. The ablation above measures it.
* **Full recompute with ID reconciliation, not incremental matching.** This is simpler and
  order-independent. The cost is O(candidate pairs) per run, which is fine at this scale.
* **The review band is a first-class output.** Low-evidence pairs are a data-steward task, not
  something to auto-merge.

## Limitations

* Synthetic data only. The noise model (nicknames, one-character typos, format variants,
  households) is simple next to real CRM data, so the measured precision is optimistic.
* Weights and thresholds are fixed by hand. There is no EM/Fellegi-Sunter parameter estimation
  and no learned model.
* DOB is the only cannot-link constraint. Twins share it; "Jr./Sr." aren't parsed.
* Consensus survivorship can't recover a name when every record has a different typo. On the
  sample, Michael's golden last name is `Clak` because `Clark`, `Clak` and `Clrak` each appear once
  and `Clak` is the most central.
* Phone/address normalisation assumes the US (NANP, ZIP5, English street suffixes).
* There is no deletion handling: records removed at the source stay in `source_records`.
* Every run re-clusters all records. For millions of records you would block and score only
  the new or changed records against the store.
* Review-queue decisions (must-link / cannot-link from a steward) aren't fed back yet.
