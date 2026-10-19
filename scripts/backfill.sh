#!/bin/bash
# 月ごとに過去データを取り込む（新しい月から順に）
PY=/c/Users/okita/.venvs/clinic-reports/Scripts/python
export PYTHONIOENCODING=utf-8
for r in "2026-08-01 2026-08-31" "2026-07-01 2026-07-31" "2026-06-01 2026-06-30" "2026-05-01 2026-05-31" "2026-04-01 2026-04-30" "2026-03-01 2026-03-31" "2026-02-01 2026-02-28" "2026-01-01 2026-01-31"; do
  set -- $r
  echo "=== $1 .. $2 start $(date +%T)"
  $PY -m app.cli run manual --start $1 --end $2 > var/backfill_$1.log 2>&1
  grep -m1 '"status"' var/backfill_$1.log
  echo "=== done $(date +%T)"
done
