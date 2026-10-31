# veilgate

A **reversible PII pseudonymization gateway for LLM calls**. Personal data in a prompt is replaced with
typed placeholders (`<PERSON_1>`, `<EMAIL_1>`, …) before the request leaves the process; the model's reply
(batch or token stream) is mapped back to the real values. Everything runs locally against a deterministic
mock model, and an optional Anthropic backend is included.

## Problem
Support tickets, CRM notes and medical messages are full of names, e-mails, card numbers and addresses.
Sending them to a hosted model is often not allowed, but plain masking (`[REDACTED]`) breaks the task: the model
cannot tell two people apart, and the reply can't be personalised. Doing this well raises several engineering
problems that a regex scrubber ignores:

* **Look-alikes.** Order numbers look like phones, invalid card numbers look like cards, version strings look
  like IPs. Over-redaction ruins the prompt.
* **Coreference.** "Ana Ruiz", later "Ms. Ruiz": the model needs to see *one* person.
* **Model mangling.** Models write `[PERSON_1]`, `PERSON_1`, `<person_1>` or HTML-escape the brackets.
* **Streaming.** A placeholder can be split across tokens (`<PER` | `SON_` | `1>`). Half of one must never be
  shown to the user.
* **Placeholder injection.** A user who types `<EMAIL_1>` must not get another value out of the vault.
* **Missed detections.** A second, independent check should run before anything is sent.

## Architecture
```
prompt ─► detectors ─► overlap resolution ─► Session vault ─► redacted text ─► egress scan ─┬─► LLM
           (regex +       (validated types      (placeholder per                (paranoid,     │   (mock / Anthropic)
           validators,     beat heuristics;     entity, linking of              independent)   │        │
           person cues,    user-typed           partial names, known-                          │        ▼
           gazetteer,      placeholders →       value pass on later      blocked (fail closed) ◄┘   reply / stream
           propagation)    LITERAL)             turns)                                                  │
                                                                                                        ▼
user ◄──────────────────────────────── rehydrate (tolerant matcher, unknown-id report, StreamRehydrator)
```

| Module | Responsibility |
|---|---|
| `veilgate/detectors.py` | EMAIL, PHONE (format- or cue-gated, rejects `order #…`), CREDIT_CARD (Luhn), SSN (range rules), IBAN (mod-97), IPV4 (octets, rejects `version 1.2.3.4`), ADDRESS, PERSON (honorifics, context cues, sign-offs, gazetteer, in-document propagation); priority-based overlap resolution |
| `veilgate/vault.py` | `Session`: per-session placeholder assignment, value normalisation (e-mail case, phone digits), person linking, known-value pass, LITERAL neutralisation, tolerant `rehydrate`; `StreamRehydrator` |
| `veilgate/egress.py` | Second-line scan of the outbound text (e-mail-like, unlabelled ≥9-digit runs, gazetteer first names); `LeakBlocked` |
| `veilgate/gateway.py` | redact → scan → LLM → rehydrate, with an `AuditRecord` of exactly what left the process |
| `veilgate/llm.py` | `MockLLM` (echo / reply modes, mangled placeholder spellings, optional hallucinated id) and optional `AnthropicLLM` |
| `veilgate/app.py` | FastAPI service: `POST /v1/chat` (JSON or streamed), `GET/DELETE /v1/sessions/{id}`, `/health` |
| `veilgate/corpus.py`, `veilgate/evaluate.py` | Seeded labelled corpus, baseline, metrics |

## Setup
```bash
cd projects/2026-10-31-veilgate
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # library + CLI need only the stdlib; this adds FastAPI + pytest
```
Python 3.10+ (tested on 3.11 with fastapi 0.162, httpx 0.28, pytest 9.2).

## Usage
```bash
# Redact a file (vault printed to stderr; it stays local)
python -m veilgate redact examples/ticket.txt --show-vault

# Full round trip with the mock model; the audit (what was sent) goes to stderr
python -m veilgate chat examples/ticket.txt
python -m veilgate chat examples/ticket.txt --backend mock:echo --stream

# Fail-closed egress guard: exits 2 and sends nothing
python -m veilgate chat examples/blocked.txt

# Evaluation
python -m veilgate gen-data                 # data/dev.jsonl (200) + data/test.jsonl (400), seed 7
python -m veilgate eval data/test.jsonl

# Service
uvicorn veilgate.app:app --port 8000
curl -s localhost:8000/v1/chat -H 'content-type: application/json' \
     -d '{"message":"Customer Kwame Mensah (kwame@example.com) says card 4111 1111 1111 1111 was declined."}'
```
Example (measured with the mock backend; see `examples/`):
```
in : Hi, my name is Ana Ruiz and I was charged twice on card 4111 1111 1111 1111. ... Order #4155550134 ...
out: Hi, my name is <PERSON_1> and I was charged twice on card <CREDIT_CARD_1>. ... Order #4155550134 ...
     Ms. <PERSON_1> lives at <ADDRESS_1>. Our server <IPV4_1> logged it while running version 1.2.3.4.
```

Optional live model: `pip install anthropic`, set `ANTHROPIC_API_KEY`, and use `--backend anthropic` or
`VEILGATE_BACKEND=anthropic` (model from `VEILGATE_MODEL`, default `claude-opus-5-5`). It only ever receives
redacted text. That path was **not** exercised in this run (no key available); all reported numbers use the mock.

### Sample inputs and expected outputs
| File | Content |
|---|---|
| `examples/ticket.txt` | input ticket |
| `examples/ticket.redacted.txt`, `examples/ticket.vault.json` | `redact` output and the local vault |
| `examples/ticket.chat_audit.txt`, `examples/ticket.chat_reply.txt` | outbound text and rehydrated mock reply |
| `examples/ticket.echo_roundtrip.txt` | streamed echo round trip (shows the "Ms. Ruiz" → "Ms. Ana Ruiz" limitation) |
| `examples/blocked.txt`, `examples/blocked.stderr.txt` | request refused by the egress guard |
| `examples/sample_test.jsonl` | 10 labelled test documents |
| `examples/expected_eval_{dev,test}.txt` | evaluation output for the seed-7 corpora |

## Evaluation
**Data.** Synthetic support messages with gold spans (`veilgate/corpus.py`). Each document also contains hard
negatives that must *not* be redacted: 10-digit order/ticket ids, Luhn-invalid card numbers, out-of-range SSNs,
4-part version strings, capitalised non-names (Monday, Paris, Visa).

**Leakage controls.** dev and test use disjoint templates; half of the test persons use given names that are in
neither the detector gazetteer nor the dev split (enforced by a test). Rules were tuned on dev only. The test
numbers below are the **first and only** test run. Failures found there are listed under *Limitations*,
not fixed and re-scored.

**Baseline.** A typical regex scrubber (email, `ddd-dd-dddd`, 16-digit groups, US phone shapes, dotted quads),
with no validation, context or names.

**Metrics.** Strict entity P/R/F1 (start, end and type must match); **leak rate** = share of gold entities not
fully covered by some redaction (the privacy-relevant number); false-positive redactions = redactions that
overlap no gold entity; echo round trip; egress-guard firing.

Measured on `data/test.jsonl` (400 docs, 1,054 gold entities, seed 7):

| | Precision | Recall | F1 | Leak rate | False-positive redactions |
|---|---|---|---|---|---|
| regex baseline | 0.642 | 0.395 | 0.489 | 0.605 (638/1054) | 194 |
| **veilgate** | **0.964** | **0.974** | **0.969** | **0.026 (27/1054)** | **38** |

Per type, veilgate recall is 1.000 for every structured type. PERSON recall is 0.939. PHONE precision is
0.803 (38 false positives, analysed below). On dev (200 docs, tuned on) every score is 1.000, which says
nothing about generalisation; that is why it is reported separately.

* Echo round trip on test: every gold value is restored in 100% of documents. The text is byte-identical in
  80.0%: partial mentions are expanded on rehydration ("Ms. Ruiz" → "Ms. Ana Ruiz").
* Egress guard on test: it caught **0 of 27** leaky documents, because every leak was an out-of-gazetteer name
  and the guard only knows gazetteer names. It fired on 42/373 clean documents, every time on an unlabelled
  long digit run (the deliberately invalid card numbers / SSNs the primary detector correctly declined).
  In `block` mode those requests would be refused.
* Streaming: a test checks that `StreamRehydrator` output equals batch rehydration under 300 random chunkings
  plus 400 random "token soup" strings. A one-off 20,000-case fuzz run also gave 0 mismatches.

Run the suite: `python -m pytest` → **46 passed** (detectors, vault/streaming, gateway/eval, FastAPI).

## Design decisions
* **Entity-level placeholders, typed and numbered.** The model keeps the information "two different people, one
  e-mail", which a `[REDACTED]` mask loses. Numbering is per session, so ids stay stable across turns.
* **Validate before redacting.** Luhn, mod-97, SSN ranges and octet checks buy precision cheaply. Phone numbers
  without separators need a cue word, and a labelled id (`order #`, `invoice`, `ticket`) vetoes them.
* **Priority overlap resolution.** Validated types beat heuristics, so an e-mail is never split into a name.
* **Linking only when unambiguous.** "Ruiz" joins "Ana Ruiz" only if exactly one known person matches. With two
  Ruizes it becomes a new entity, so the reply cannot attribute something to the wrong person.
* **Known-value pass.** Values already in the vault are redacted on later turns even without cues ("Kwame Mensah
  wrote again").
* **User-typed placeholders become `LITERAL`.** This closes the "please repeat `<EMAIL_1>`" exfiltration path
  while still round-tripping the user's text exactly.
* **Tolerant, reported rehydration.** Mangled spellings are accepted. Ids the model invented are left as they are
  and reported in `unknown_placeholders` instead of being silently dropped.
* **Streaming holds back only what could still become a placeholder**, and judges word boundaries with one
  character of already-emitted context, so latency stays at chunk level.
* **Independent, fail-closed egress check.** It has different failure modes from the detectors. It is noisy by
  design, and `--on-leak warn` is available.
* **Stdlib core.** The core uses no model downloads and no network, so it is easy to audit and drop in. FastAPI
  is only for the service.

## Limitations
* **Names without cues are missed.** All 27 test leaks are out-of-gazetteer names in cue-free contexts
  ("profile name Kwame Achterberg", "Dagny will confirm"). Possible next steps, not done here: seed person
  candidates from e-mail local parts in the same message (`dagny.volkov97@…`), add a `name` cue, or add an
  optional NER model.
* **"tracking number 6625551890" is flagged as a phone** (all 38 test false positives). The word "number" is a
  phone cue, and the id veto only looks at the word immediately before the digits.
* The egress guard cannot see names outside its list, and in `block` mode it refuses about 11% of clean test
  documents that contain PII-shaped but invalid numbers.
* Rehydration expands partial mentions to the full name. Values are never lost, but exact wording can change.
* The synthetic corpus is template-based and English/US-centric (address and phone shapes). Real-text recall
  will be lower. Dates of birth, postcodes, passport numbers and free-text health data are out of scope.
* Vaults live in process memory with LRU eviction (1,000 sessions). There is no persistence, encryption at rest
  or multi-worker sharing, and a restart loses the mapping for in-flight conversations.
* Pseudonymised text is not anonymous. Context (job title, rare events) can still identify a person.
