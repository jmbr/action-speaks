#!/usr/bin/env bash
# Build Loogle against this project's toolchain, so shape search can run locally.
#
#   scripts/build-loogle.sh            clone (if needed), build, report the binary path
#   scripts/build-loogle.sh --index    additionally build the search index up front
#
# Loogle has no dependencies of its own and does not need to be a dependency of this
# project: the binary reads the `.olean` files of any Lake project built with the same
# toolchain. So we clone it beside the Lean project, copy our `lean-toolchain` over its own,
# and build. That takes seconds, because nothing of Mathlib is rebuilt.
#
# The commit is pinned for the same reason every other component is: a verdict, and the
# lemma search that produced it, should be reproducible.

set -euo pipefail

# Upstream pins an older toolchain than we use; overwriting it is the documented way to
# build against the project you intend to search.
LOOGLE_REPO="${LOOGLE_REPO:-https://github.com/nomeata/loogle}"
LOOGLE_REV="${LOOGLE_REV:-9f11169aaebf1ed1e7dcc4077f2aafe0fcf66fd0}"

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEAN_DIR="$ROOT/lean"
CHECKOUT="${ACTION_SPEAKS_LOOGLE_DIR:-$ROOT/vendor/loogle}"
BIN="$CHECKOUT/.lake/build/bin/loogle"
MODULE="${ACTION_SPEAKS_LOOGLE_MODULE:-ActionSpeaksAll}"

do_index=0
[ "${1:-}" = "--index" ] && do_index=1

if [ ! -f "$LEAN_DIR/lean-toolchain" ]; then
  echo "no lean-toolchain at $LEAN_DIR; run this from the action-speaks repository" >&2
  exit 2
fi

if [ ! -d "$CHECKOUT/.git" ]; then
  echo "cloning loogle into $CHECKOUT"
  mkdir -p "$(dirname "$CHECKOUT")"
  git clone --quiet "$LOOGLE_REPO" "$CHECKOUT"
fi

echo "pinning loogle at $LOOGLE_REV"
git -C "$CHECKOUT" fetch --quiet origin
git -C "$CHECKOUT" checkout --quiet "$LOOGLE_REV"

# Build it with our toolchain rather than its own, so it can read our oleans.
cp "$LEAN_DIR/lean-toolchain" "$CHECKOUT/lean-toolchain"
echo "building loogle with $(cat "$LEAN_DIR/lean-toolchain")"
(cd "$CHECKOUT" && lake build)

if [ ! -x "$BIN" ]; then
  echo "build finished but no binary at $BIN" >&2
  exit 1
fi
echo "ok: $BIN"

# The index lives next to the module's .olean and is rebuilt automatically whenever the
# oleans change, so this step is optional — it only moves the first query's cost up front.
if [ "$do_index" -eq 1 ]; then
  echo "building the search index for $MODULE (a few minutes, once)"
  (cd "$LEAN_DIR" && lake build "$MODULE" && \
     lake env "$BIN" --module "$MODULE" --index-mode write "Nat.succ_le_succ" >/dev/null)
  echo "ok: index built"
fi

cat <<EOF

Local shape search is enabled for this checkout. Nothing further is needed: the verifier
looks for the binary at vendor/loogle by default. To use a checkout elsewhere, set
ACTION_SPEAKS_LOOGLE_BIN to the binary path.
EOF
