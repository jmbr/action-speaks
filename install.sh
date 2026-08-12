#!/usr/bin/env bash
# Install the nullius skill and MCP server into the agent harnesses on this machine.
#
#   ./install.sh              install skill + MCP config
#   ./install.sh --skill      skill only
#   ./install.sh --mcp        MCP config only
#   ./install.sh --uninstall  remove what this script installed
#   ./install.sh --dry-run    show what would change
#
# The skill is symlinked rather than copied, so editing it in the repository takes effect
# immediately and there is only ever one copy to maintain.

set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="$ROOT/skills/lean-proof-check"
SKILL_DST="$HOME/.agents/skills/lean-proof-check"
MCP_TEMPLATE="$ROOT/mcp/copilot-mcp-config.json"
MCP_DST="$HOME/.copilot/mcp-config.json"

do_skill=1
do_mcp=1
uninstall=0
dry=0

for arg in "$@"; do
  case "$arg" in
    --skill)     do_mcp=0 ;;
    --mcp)       do_skill=0 ;;
    --uninstall) uninstall=1 ;;
    --dry-run|-n) dry=1 ;;
    -h|--help)   sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
act() { if [ "$dry" -eq 1 ]; then say "  would: $*"; else eval "$@"; fi; }

# --- skill ---------------------------------------------------------------

install_skill() {
  say "skill: $SKILL_DST -> $SKILL_SRC"
  if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    say "  ERROR: $SKILL_SRC/SKILL.md is missing"; return 1
  fi
  act "mkdir -p '$HOME/.agents/skills'"
  if [ -e "$SKILL_DST" ] && [ ! -L "$SKILL_DST" ]; then
    # Never silently delete a real directory someone may have edited in place.
    say "  ERROR: $SKILL_DST exists and is not a symlink."
    say "         Move it aside, then re-run. (It may be an older copy of this skill.)"
    return 1
  fi
  act "ln -sfn '$SKILL_SRC' '$SKILL_DST'"
  act "chmod +x '$SKILL_SRC/scripts/nullius'"
  say "  ok (pi and Copilot both read ~/.agents/skills)"
}

uninstall_skill() {
  if [ -L "$SKILL_DST" ]; then
    say "removing skill symlink $SKILL_DST"
    act "rm '$SKILL_DST'"
  else
    say "skill: nothing to remove at $SKILL_DST"
  fi
}

# --- MCP -----------------------------------------------------------------

# Merge our server entry into any existing config rather than overwriting the file, since
# other servers may already be configured there.
merge_mcp() {
  python3 - "$MCP_TEMPLATE" "$MCP_DST" "$ROOT" "$dry" <<'PY'
import json, os, shutil, sys

template_path, dst_path, root, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"

entry = json.loads(open(template_path).read().replace("__NULLIUS_ROOT__", root))["mcpServers"]["nullius"]

existing = {}
if os.path.exists(dst_path):
    try:
        existing = json.load(open(dst_path))
    except json.JSONDecodeError:
        print(f"  ERROR: {dst_path} is not valid JSON; fix or move it aside")
        sys.exit(1)

servers = existing.setdefault("mcpServers", {})
if servers.get("nullius") == entry:
    print("  ok (already configured)")
    sys.exit(0)

action = "updating" if "nullius" in servers else "adding"
servers["nullius"] = entry
others = [k for k in servers if k != "nullius"]
print(f"  {action} `nullius`" + (f", preserving: {', '.join(others)}" if others else ""))

if dry:
    print("  would write:", dst_path)
    sys.exit(0)

os.makedirs(os.path.dirname(dst_path), exist_ok=True)
if os.path.exists(dst_path):
    shutil.copy2(dst_path, dst_path + ".bak")
    print(f"  backed up existing config to {dst_path}.bak")
with open(dst_path, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
print("  ok (restart Copilot to pick it up)")
PY
}

remove_mcp() {
  if [ ! -f "$MCP_DST" ]; then
    say "mcp: nothing to remove ($MCP_DST does not exist)"; return 0
  fi
  python3 - "$MCP_DST" "$dry" <<'PY'
import json, sys
dst, dry = sys.argv[1], sys.argv[2] == "1"
cfg = json.load(open(dst))
if cfg.get("mcpServers", {}).pop("nullius", None) is None:
    print("  mcp: `nullius` not present")
    sys.exit(0)
print("  removing `nullius` from", dst)
if not dry:
    with open(dst, "w") as f:
        json.dump(cfg, f, indent=2); f.write("\n")
PY
}

# --- run -----------------------------------------------------------------

[ "$dry" -eq 1 ] && say "(dry run - no changes will be made)"
say "verifier root: $ROOT"

if [ "$uninstall" -eq 1 ]; then
  [ "$do_skill" -eq 1 ] && uninstall_skill
  [ "$do_mcp" -eq 1 ] && remove_mcp
  exit 0
fi

[ "$do_skill" -eq 1 ] && install_skill
if [ "$do_mcp" -eq 1 ]; then
  say "mcp: $MCP_DST"
  merge_mcp
fi

if [ "$dry" -eq 0 ]; then
  say ""
  say "Next: ./skills/lean-proof-check/scripts/nullius doctor"
  say "      (checks Lean, Mathlib and that the verifier discriminates correctly)"
fi
