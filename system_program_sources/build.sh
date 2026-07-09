#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$ROOT_DIR/data/SYS1/LOADLIB"

mkdir -p "$OUT_DIR"

cc "$SCRIPT_DIR/iefbr14.c" -O2 -Wall -Wextra -o "$OUT_DIR/IEFBR14"

cp "$SCRIPT_DIR/cobcomp.py" "$OUT_DIR/COBCOMP"
chmod +x "$OUT_DIR/COBCOMP"

echo "Built $OUT_DIR/IEFBR14"
echo "Installed $OUT_DIR/COBCOMP"
