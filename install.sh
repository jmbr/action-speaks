#!/usr/bin/env bash
# Install the action-speaks skill, CLI and MCP server into the agent harnesses on this machine.
#
#   ./install.sh              install skill + CLI symlink + MCP config
#   ./install.sh --skill      skill only
#   ./install.sh --mcp        MCP config only
#   ./install.sh --cli        `action-speaks` on PATH only
#   ./install.sh --uninstall  remove what this script installed
#   ./install.sh --dry-run    show what would change
#
# Everything is wired with absolute paths, so no virtualenv ever has to be activated: the
# MCP server names the interpreter the package was installed into, the skill wrapper finds
# it through its own symlink, and the CLI symlink points at a wrapper that hard-codes it.
#
# The skill is symlinked rather than copied, so editing it in the repository takes effect
# immediately and there is only ever one copy to maintain.

set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="$ROOT/skills/action-speaks"
SKILL_DST="$HOME/.agents/skills/action-speaks"
MCP_TEMPLATE="$ROOT/mcp/copilot-mcp-config.json"
MCP_DST="$HOME/.copilot/mcp-config.json"
CLI_SRC="$ROOT/.venv/bin/action-speaks"
CLI_DST="${ACTION_SPEAKS_BIN_DIR:-$HOME/.local/bin}/action-speaks"

do_skill=1
do_mcp=1
do_cli=1
uninstall=0
dry=0

for arg in "$@"; do
  case "$arg" in
    --skill)     do_mcp=0; do_cli=0 ;;
    --mcp)       do_skill=0; do_cli=0 ;;
    --cli)       do_skill=0; do_mcp=0 ;;
    --uninstall) uninstall=1 ;;
    --dry-run|-n) dry=1 ;;
    -h|--help)   sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
  act "chmod +x '$SKILL_SRC/scripts/action-speaks'"
  say "  ok (pi and Copilot both read ~/.agents/skills)"
  remove_legacy_skill
}

# The skill used to be called `lean-proof-check`. A symlink under the old name still points
# into this repository, so leaving it behind registers the same skill twice under two names.
remove_legacy_skill() {
  # Names this project has been installed under before. Removed only when the link points
  # at this checkout, so an unrelated skill of the same name is left alone.
  local legacy
  for legacy in "$HOME/.agents/skills/lean-proof-check" "$HOME/.agents/skills/nullius"; do
  if [ -L "$legacy" ] && case "$(readlink "$legacy")" in "$ROOT"/*) true ;; *) false ;; esac; then
    say "  removing superseded symlink $legacy (skill renamed to action-speaks)"
    act "rm '$legacy'"
  fi
  done
  local old_cli="${ACTION_SPEAKS_BIN_DIR:-$HOME/.local/bin}/nullius"
  if [ -L "$old_cli" ] && case "$(readlink "$old_cli")" in "$ROOT"/*) true ;; *) false ;; esac; then
    say "  removing superseded command $old_cli"
    act "rm '$old_cli'"
  fi
}

uninstall_skill() {
  remove_legacy_skill
  if [ -L "$SKILL_DST" ]; then
    say "removing skill symlink $SKILL_DST"
    act "rm '$SKILL_DST'"
  else
    say "skill: nothing to remove at $SKILL_DST"
  fi
}

# --- CLI on PATH ---------------------------------------------------------

# The console script lives inside the virtualenv, which would otherwise have to be activated
# before every `action-speaks` call. A symlink from a directory already on PATH removes that step;
# the script is a generated wrapper that hard-codes the venv's interpreter, so it works
# through the symlink without activation.
install_cli() {
  if [ ! -x "$CLI_SRC" ]; then
    say "cli: no console script at $CLI_SRC"
    say "     (create it with: python3 -m venv .venv && .venv/bin/pip install -e .)"
    return 0
  fi
  say "cli: $CLI_DST -> $CLI_SRC"
  if [ -e "$CLI_DST" ] && [ ! -L "$CLI_DST" ]; then
    say "  ERROR: $CLI_DST exists and is not a symlink; leaving it alone."
    return 1
  fi
  act "mkdir -p '$(dirname "$CLI_DST")'"
  act "ln -sfn '$CLI_SRC' '$CLI_DST'"
  case ":$PATH:" in
    *":$(dirname "$CLI_DST"):"*) say "  ok ('action-speaks' is on PATH)" ;;
    *) say "  ok, but $(dirname "$CLI_DST") is not on PATH; add it to use 'action-speaks' directly" ;;
  esac
}

uninstall_cli() {
  if [ -L "$CLI_DST" ]; then
    say "removing cli symlink $CLI_DST"
    act "rm '$CLI_DST'"
  else
    say "cli: nothing to remove at $CLI_DST"
  fi
}

# --- MCP -----------------------------------------------------------------

# Merge our server entry into any existing config rather than overwriting the file, since
# other servers may already be configured there.
merge_mcp() {
  python3 - "$MCP_TEMPLATE" "$MCP_DST" "$ROOT" "$dry" <<'PY'
import json, os, shutil, sys

template_path, dst_path, root, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"

entry = json.loads(open(template_path).read().replace("__ACTION_SPEAKS_ROOT__", root))["mcpServers"]["action-speaks"]

# Point the server at the interpreter the package was installed into, when there is one.
# A venv's python needs no PYTHONPATH and cannot be shadowed by whatever `python3` happens
# to mean in the agent's environment, which is the failure this avoids.
venv_python = os.path.join(root, ".venv", "bin", "python3")
if os.path.exists(venv_python):
    entry["command"] = venv_python
    entry.get("env", {}).pop("PYTHONPATH", None)

existing = {}
if os.path.exists(dst_path):
    try:
        existing = json.load(open(dst_path))
    except json.JSONDecodeError:
        print(f"  ERROR: {dst_path} is not valid JSON; fix or move it aside")
        sys.exit(1)

servers = existing.setdefault("mcpServers", {})
superseded = servers.pop("nullius", None) is not None
if servers.get("action-speaks") == entry and not superseded:
    print("  ok (already configured)")
    sys.exit(0)
if superseded:
    print("  removing superseded server `nullius`")

action = "updating" if "action-speaks" in servers else "adding"
servers["action-speaks"] = entry
others = [k for k in servers if k != "action-speaks"]
print(f"  {action} `action-speaks`" + (f", preserving: {', '.join(others)}" if others else ""))

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
if cfg.get("mcpServers", {}).pop("action-speaks", None) is None:
    print("  mcp: `action-speaks` not present")
    sys.exit(0)
print("  removing `action-speaks` from", dst)
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
  [ "$do_cli" -eq 1 ] && uninstall_cli
  [ "$do_mcp" -eq 1 ] && remove_mcp
  exit 0
fi

[ "$do_skill" -eq 1 ] && install_skill
[ "$do_cli" -eq 1 ] && install_cli
if [ "$do_mcp" -eq 1 ]; then
  say "mcp: $MCP_DST"
  merge_mcp
fi

if [ "$dry" -eq 0 ]; then
  say ""
  say "Next: ./skills/action-speaks/scripts/action-speaks doctor"
  say "      (checks Lean, Mathlib and that the verifier discriminates correctly)"
fi
