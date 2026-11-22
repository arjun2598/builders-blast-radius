#!/bin/sh
# Run the selector natively (no Docker) on the sample set and score it.
set -e
cd "$(dirname "$0")"
S=${SCRATCH:-/tmp/blast-local}; rm -rf "$S"; mkdir -p "$S"
SET=${SET:-blast-radius-sample/sample}
BLAST_DATA=$SET BLAST_REPO=work/sqlglot BLAST_MAP=data/map.json.gz \
BLAST_PYC=work/pyc BLAST_CPU=data/cpu.json BLAST_DUCK=data/duck.pkl.gz BLAST_CMPMAP=data/cmp BLAST_KEYMAP=data/keys BLAST_BOOLMAP=data/bool BLAST_REQMAP=data/req.json.gz BLAST_NEEDS=data/imports.json.gz BLAST_PREBUILT=work/prebuilt.pkl BLAST_DISP=data/disp.json.gz BLAST_POPEN=data/popen.json BLAST_TMP="$S" PYTHONPATH=src \
  /usr/bin/time -p work/venv/bin/python -m blast.main > "$S/out.jsonl" 2> "$S/err.log" || { tail -30 "$S/err.log"; exit 1; }
tail -4 "$S/err.log"
python3 blast-radius-sample/score.py $SET $SET/answers.json "$S/out.jsonl"
