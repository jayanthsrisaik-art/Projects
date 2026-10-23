# apidiff

Breaking-change detector for OpenAPI 3.x documents (JSON). Compares an old and a new spec and classifies every difference as **breaking**, **warning** or **safe**, using the direction of data flow. Python stdlib only; pytest for tests.

## Problem
A naive spec diff treats "enum value added" or "property removed" the same everywhere. They are not: adding an enum value to a *request* is safe, but adding one to a *response* breaks clients with exhaustive switches. `apidiff` encodes these asymmetric rules so it can gate CI.

## Architecture
- `apidiff/differ.py` – `Differ` walks paths -> operations -> parameters / request body / responses -> schemas. Local `$ref`s are resolved on each side independently, with a cycle guard for recursive schemas. Every schema comparison carries a `direction` (`request` or `response`).
- `apidiff/cli.py` – text/JSON rendering and exit codes.

## Rules (summary)
| Change | Request | Response |
|---|---|---|
| Path/operation/response status removed | breaking | breaking |
| New required param / required property | breaking | safe |
| Property removed | warning | breaking |
| Property became required | breaking | safe |
| Property no longer required | safe | breaking |
| Enum value removed | breaking | safe |
| Enum value added | safe | breaking |
| Type changed | breaking | breaking |
| integer -> number | safe | breaking |
| min/max/length tightened | breaking | safe |
| min/max/length loosened | safe | breaking |

## Setup and usage
```
python -m pip install -r requirements.txt
python -m apidiff samples/petstore_v1.json samples/petstore_v2.json
python -m apidiff old.json new.json --format json --fail-on warning
python -m pytest -q
```
Exit codes: 0 = under threshold, 1 = changes at/above `--fail-on` (default `breaking`), 2 = unreadable input.
`samples/expected_output.txt` is the checked output for the sample pair (asserted by a test).

## Design decisions
- Direction-aware rules instead of one symmetric diff.
- Severity `warning` for ambiguous cases (removed request param/property, new response status) so teams can choose strictness.
- Deterministic ordering (severity, location, code) so output is diff-friendly.

## Limitations
- JSON only (no YAML); only local `#/...` `$ref`s.
- `allOf` / `oneOf` / `anyOf`, `additionalProperties`, `pattern`, discriminators, security schemes and servers are not compared.
- Parameters are keyed by (location, name); `style`/`explode` changes are ignored.
- A removed 4xx/5xx response is reported as breaking, which may be stricter than you want.
- Tested on small hand-written specs only, not real-world large APIs.
