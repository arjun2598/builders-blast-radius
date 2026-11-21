#!/bin/sh
# Run the selector natively (no Docker) on the sample set and score it.
set -e
cd "$(dirname "$0")"
S=${SCRATCH:-/tmp/blast-local}; rm -rf "$S"; mkdir -p "$S"
BLAST_DATA=blast-radius-sample/sample BLAST_REPO=work/sqlglot BLAST_MAP=data/map.json.gz \
BLAST_PYC=work/pyc BLAST_CPU=data/cpu.json BLAST_TMP="$S" PYTHONPATH=src \
  /usr/bin/time -p work/venv/bin/python -m blast.main > "$S/out.jsonl" 2> "$S/err.log" || { tail -30 "$S/err.log"; exit 1; }
tail -4 "$S/err.log"
python3 blast-radius-sample/score.py blast-radius-sample/sample blast-radius-sample/sample/answers.json "$S/out.jsonl"
