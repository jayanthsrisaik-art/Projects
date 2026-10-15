#!/usr/bin/env sh
# Loads the sample snapshots (day 4 before day 3 to show late arrival) and prints results.
set -e
cd "$(dirname "$0")/.."
DB=$(mktemp -d)/demo.db
for d in 01 02 04 03; do
  python -m scd2 --db "$DB" load samples/snapshots/customers_2026-01-$d.csv --as-of 2026-01-$d --key customer_id
done
echo "--- check"; python -m scd2 --db "$DB" check
echo "--- history 1"; python -m scd2 --db "$DB" history 1
echo "--- history 4"; python -m scd2 --db "$DB" history 4
echo "--- as-of 2026-01-02"; python -m scd2 --db "$DB" as-of 2026-01-02
