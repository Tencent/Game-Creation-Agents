#!/bin/bash
# PreToolUse hook: prevent agents from hot-restarting the gca MCP server
# during an active game pipeline. Restarting the server severs the harness'
# stdio client connection and strands verification/generation phases.
#
# Set GCA_ALLOW_MCP_RESTART=1 only for explicit framework maintenance.

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT=$(cat)

if [ "${GCA_ALLOW_MCP_RESTART:-0}" = "1" ]; then
  exit 0
fi

TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null)

# True only when this close cannot strand a pipeline, so the deadlock below has
# an in-band exit. The MCP server leases one workspace for its whole lifetime,
# and init_workspace on a second path fails with WORKSPACE_ALREADY_OPEN. If a
# stray bind (say a listing subagent) claims the lease, blocking close_workspace
# unconditionally leaves the session no way to reach the real game directory:
# the Bash rules below also refuse to restart the server, so the only remedy is
# an external kill. Recovery is safe exactly when no pipeline is bound, or when
# the bound workspace never persisted Startup state -- the same condition the
# Stop hook reports as "workspace is bound but .playtest/state.json has not been
# created". Anything unresolved still blocks.
close_is_recoverable() {
  # A subagent has no business closing a workspace, live pipeline or not.
  local agent_id
  agent_id=$(printf '%s' "$INPUT" | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('agent_id') or '')" 2>/dev/null)
  [ -n "$agent_id" ] && return 1

  local meta status game_dir
  meta=$(printf '%s' "$INPUT" | python3 "$HOOK_DIR/guard-agent-boundary.py" \
    --resolve-hook-session 2>/dev/null)
  status=$?

  # 3: no binding, or already closed -- nothing is tracked, nothing to strand.
  [ "$status" -eq 3 ] && return 0
  # Any other non-zero is an unreadable binding; keep failing closed.
  [ "$status" -ne 0 ] && return 1

  game_dir=$(python3 -c \
    "import json,sys; print(json.loads(sys.argv[1]).get('game_dir',''))" \
    "$meta" 2>/dev/null)
  [ -n "$game_dir" ] || return 1
  [ -f "$game_dir/.playtest/state.json" ] && return 1
  return 0
}

if [ "$TOOL_NAME" = "mcp__gca__close_workspace" ]; then
  if close_is_recoverable; then
    exit 0
  fi
  echo "Blocked: do not close/restart the gca MCP workspace mid-pipeline. Pause and ask the user to restart/reconnect via /mcp if required." >&2
  exit 2
fi

if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)
LOWER=$(printf '%s' "$COMMAND" | tr '[:upper:]' '[:lower:]')

case "$LOWER" in
  *"pkill"*"gca-mcp"*|*"killall"*"gca-mcp"*|*"kill "*"gca-mcp"*|*"uv "*"run gca-mcp"*|*" gca-mcp "*|*"restart"*"gca-mcp"*)
    echo "Blocked: hot-restarting or killing gca-mcp during a game pipeline breaks the MCP stdio connection." >&2
    echo "If a server code/config change is required, stop the pipeline, ask the user to reconnect via /mcp, then resume from .playtest/state.json." >&2
    exit 2
    ;;
esac

exit 0
