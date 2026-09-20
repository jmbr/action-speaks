#!/usr/bin/env sh
# Compatibility wrapper. The installer is install.py; this only locates the checkout so
# existing `./install.sh` callers keep working. Prefer `python3 install.py`.
set -eu
ROOT="$(cd -P "$(dirname "$0")" && pwd)"
exec python3 "$ROOT/install.py" "$@"
