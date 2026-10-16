#!/usr/bin/env bash
# Regenerate or verify backend/constraints.txt (the pinned, fully resolved dependency set).
#
#   scripts/constraints.sh regenerate   rebuild it from a clean virtual environment
#   scripts/constraints.sh check        fail if requirements-dev.txt, installed with
#                                       `-c constraints.txt`, resolves to anything other than
#                                       exactly what constraints.txt lists
#
# Run it from anywhere; set PYTHON to pick the interpreter (use the one CI and the image use).
set -euo pipefail

mode="${1:-}"
backend_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
header='# Pinned, fully resolved dependency set (runtime + dev tooling). Generated -- do not edit by hand.
# Regenerate with:  PYTHON=python3.11 scripts/constraints.sh regenerate
# CI runs:          scripts/constraints.sh check   (fails if requirements*.txt resolve differently)
# Produced with Python 3.11 (the version CI and the backend image use).'

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
"$python_bin" -m venv "$work/venv"
pip="$work/venv/bin/pip"
"$pip" install --quiet --upgrade pip

case "$mode" in
  regenerate)
    "$pip" install --quiet -r "$backend_dir/requirements-dev.txt"
    { printf '%s\n' "$header"; "$pip" freeze --exclude pip; } >"$backend_dir/constraints.txt"
    echo "constraints.txt regenerated"
    ;;
  check)
    "$pip" install --quiet -r "$backend_dir/requirements-dev.txt" -c "$backend_dir/constraints.txt"
    "$pip" freeze --exclude pip >"$work/resolved.txt"
    grep -v '^#' "$backend_dir/constraints.txt" >"$work/pinned.txt"
    if ! diff -u "$work/pinned.txt" "$work/resolved.txt"; then
      echo "constraints.txt is out of date: run scripts/constraints.sh regenerate" >&2
      exit 1
    fi
    echo "constraints.txt matches the resolved set"
    ;;
  *)
    echo "usage: $0 regenerate|check" >&2
    exit 2
    ;;
esac
