#!/usr/bin/env bash
# Put the fleet tooling where its hosts expect it. Idempotent, and safe to
# re-run after every change because everything is a symlink back to this repo.
#
#   ./install.sh            install or repair
#   ./install.sh --check    report what is wrong, change nothing
#
# The three destinations are not arbitrary:
#   ~/.local/bin/codex-fleet          on PATH, and agents call it too
#   ~/.codex/hooks/                   Codex reads hooks from here
#   ~/.claude/hooks/                  Claude Code reads hooks from here
#
# Symlinks rather than copies, so editing this repo takes effect at once and
# nobody has to remember a deploy step. That mattered: for one afternoon the
# only "deploy" was a cp, and a fix could sit in a scratchpad unused.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

link() {
  local src="$1" dest="$2" name="$3"
  mkdir -p "$(dirname "$dest")"
  if [[ -L "$dest" && "$(readlink -f "$dest")" == "$(readlink -f "$src")" ]]; then
    echo "  ok       $name"
    return 0
  fi
  if (( CHECK )); then
    if [[ -e "$dest" ]]; then echo "  STALE    $name -> $(readlink -f "$dest" 2>/dev/null || echo 'a real file')"
    else echo "  MISSING  $name"; fi
    return 1
  fi
  # A real file here is somebody's un-committed work. Keep it.
  if [[ -e "$dest" && ! -L "$dest" ]]; then
    mv "$dest" "$dest.replaced-$(date +%Y%m%d-%H%M%S)"
    echo "  kept the previous real file as $dest.replaced-*"
  fi
  ln -sfn "$src" "$dest"
  echo "  linked   $name"
}

rc=0
echo "codex-fleet from $ROOT"
link "$ROOT/codex-fleet"                  "$HOME/.local/bin/codex-fleet"              "cli" || rc=1
link "$ROOT/hooks/codex-fleet-inbox.py"   "$HOME/.codex/hooks/codex-fleet-inbox.py"   "codex inbox hook" || rc=1
link "$ROOT/hooks/codex-fleet-status.py"  "$HOME/.claude/hooks/codex-fleet-status.py" "claude status hook" || rc=1
link "$ROOT/hooks/codex-fleet-stop.py"    "$HOME/.claude/hooks/codex-fleet-stop.py"   "claude stop hook" || rc=1

# Housekeeping runs after every spawn, which covers any week the fleet is in
# use. Cron covers the weeks it is not: run state used to be collapsed only on
# spawn, so a fleet that went quiet kept every run forever.
CRON_LINE="30 6 * * * $HOME/.local/bin/codex-fleet gc --quiet >> $HOME/.codex-fleet/gc.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "codex-fleet gc"; then
  echo "  ok       daily gc cron"
elif (( CHECK )); then
  echo "  MISSING  daily gc cron"
  rc=1
else
  { crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab -
  echo "  added    daily gc cron (06:30)"
fi

echo
echo "Hook registration is NOT done here, because both hosts gate it:"
echo "  Codex  ~/.codex/hooks.json   PostToolUse -> codex-fleet-inbox.py"
echo "         Codex asks the owner to trust a new hook once. Until they do,"
echo "         'codex-fleet tell' is queued but never delivered."
echo "  Claude ~/.claude/settings.json  PostToolUse + UserPromptSubmit -> codex-fleet-status.py, Stop -> codex-fleet-stop.py"
echo "         -> codex-fleet-status.py"
echo "See MAINTAINING.md, 'Where the pieces live'."
exit $rc
