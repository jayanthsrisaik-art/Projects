# groundcheck

Checks whether each sentence of an AI-written answer is **supported**, **unsupported** or **contradicted** by the source documents it was supposed to rely on, and reports a per-answer faithfulness score. It works fully offline; an optional Claude-backed judge can be switched on with an API key.

## Problem
RAG and summarisation outputs often contain a fluent sentence with one wrong number, a flipped negation or an invented detail. A cheap, explainable first-pass guard is useful before spending LLM calls or human review. The most dangerous failures are *contradictions* (wrong figure/date/name), so those are distinguished from merely unsupported claims.

## Architecture
```
answer.txt ─► split_sentences ─► claims
sources/*.txt ─► sentences ─┐
                            ▼
   retrieve(): source sentence covering most claim content tokens
                            ▼
   LexicalVerifier (offline)          AnthropicVerifier (optional)
   coverage ≥ conflict_thr → rules:   top-3 sentences → Messages API (temp 0)
     number mismatch / unmatched      → strict JSON label; bad output → unsupported
     proper noun / negation parity /
     antonym pair → contradicted
   coverage ≥ support_thr → supported
   else unsupported
                            ▼
   report: label, reason, evidence sentence + source, faithfulness = supported/claims
```
Code: `groundcheck/text.py` (tokenising), `lexical.py`, `llm.py`, `report.py`, `evaluate.py`, `cli.py`.

## Setup
```
cd projects/2026-10-17-groundcheck
pip install -r requirements.txt     # only pytest; the library is stdlib-only (Python 3.10+)
```

## Usage
```
python -m groundcheck check --answer examples/answer.txt --sources examples/sources/mount_alder.txt
python -m groundcheck check --json ...            # machine-readable (see examples/expected_report.json)
python -m groundcheck check --fail-on-contradiction ...   # exit 1 if any claim is contradicted (CI gate)
python -m groundcheck eval                        # labelled evaluation (see examples/expected_eval.json)
ANTHROPIC_API_KEY=... python -m groundcheck check --backend anthropic [--model ID] ...
python -m pytest
```
Sample output for the example answer:
```
verifier=lexical faithfulness=0.5 {'supported': 3, 'unsupported': 1, 'contradicted': 2}
OK supported    Mount Alder is 2,140 metres tall.  (coverage 1.00)
XX contradicted The first recorded ascent happened in 1873.  (number 1873 not in evidence)
XX contradicted The visitor centre opens daily at 10 a.m.  (number 10 not in evidence)
?? unsupported  Guided night hikes are offered at the summit every Friday.  (coverage 0.14 below threshold)
...
```

## Evaluation (measured, `python -m groundcheck eval`)
Data: 58 hand-written claims over 6 short synthetic passages (`data/`), labelled supported / unsupported / contradicted. Split deterministically by SHA-1 of the claim id into dev (35) and test (23). The two thresholds were chosen by grid search on **dev only**; the test numbers below are from the untouched split.

| Test split (n=23) | accuracy | macro-F1 |
|---|---|---|
| Majority class (from dev) | 0.391 | 0.187 |
| Overlap baseline (no conflict rules) | 0.522 | 0.493 |
| **groundcheck lexical** | **0.870** | **0.866** |

Lexical per-class test F1: supported 0.900, unsupported 0.857, contradicted 0.842 (full confusion matrices in `examples/expected_eval.json`). The Anthropic backend was **not** evaluated: no API access was used while building this, only its parsing and request logic is unit-tested with a fake transport.

## Design decisions
- Three labels instead of true/false: a contradiction is a different severity from "not mentioned".
- Conflict rules only fire when a related source sentence exists (coverage ≥ conflict threshold), which keeps unrelated claims "unsupported" rather than falsely "contradicted".
- The LLM judge sees only retrieved candidate sentences, treats them as untrusted data, runs at temperature 0, and any unparseable reply degrades to `unsupported` (never silently `supported`).
- No dependencies and an injectable transport keep the offline mode and tests hermetic.

## Limitations
- The test set is small (23 claims), so the numbers have wide uncertainty; the passages are synthetic and short. The rules and the dataset were written by the same author, so real-world accuracy will be lower than reported. Test claims were not hidden from the author while writing the rules (only the thresholds were tuned dev-only).
- Lexical matching has no semantics: synonyms ("tall"/"high"), multi-sentence reasoning, coreference and unit conversion are missed. Known test errors: two contradictions predicted as supported and one unsupported claim predicted contradicted.
- Antonym list is tiny; conflicts involving unlisted wording (e.g. "free" vs "costs 12 dollars") are missed.
- English only; sentence splitting is regex-based.
- Live Anthropic mode is untested against the real API and the default model id may need updating.
