#!/usr/bin/env bash
# Run every project's test-suite in its own directory and virtualenv.
#
# Projects are independent: they pin different dependency versions and reuse
# test/helper module names (test_cli.py, helpers.py), so one repo-wide pytest
# run cannot collect them together.
#
#   .github/scripts/test_projects.sh [project-dir ...]   (default: projects/*/)
set -uo pipefail

PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_ROOT="${VENV_ROOT:-${RUNNER_TEMP:-/tmp}/project-venvs}"
mkdir -p "$VENV_ROOT"

if [ "$#" -eq 0 ]; then
  set -- "$ROOT"/projects/*/
fi

failed=()
passed=()
for dir in "$@"; do
  dir="${dir%/}"
  name="$(basename "$dir")"
  if [ ! -d "$dir/tests" ]; then
    echo "::notice::skipping $name (no tests/ directory)"
    continue
  fi
  echo "::group::$name"
  venv="$VENV_ROOT/$name"
  "$PYTHON" -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet --upgrade pip
  if [ -f "$dir/requirements.txt" ]; then
    "$venv/bin/python" -m pip install --quiet -r "$dir/requirements.txt"
  else
    "$venv/bin/python" -m pip install --quiet pytest
  fi
  (cd "$dir" && "$venv/bin/python" -m pytest -q -p no:cacheprovider)
  status=$?
  echo "::endgroup::"
  if [ "$status" -eq 0 ]; then
    passed+=("$name")
  else
    echo "::error::$name: tests failed (exit $status)"
    failed+=("$name")
  fi
done

echo "passed: ${#passed[@]}  failed: ${#failed[@]}"
if [ "${#failed[@]}" -gt 0 ]; then
  printf '  FAILED %s\n' "${failed[@]}"
  exit 1
fi
