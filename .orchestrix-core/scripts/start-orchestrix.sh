#!/bin/bash
# Orchestrix tmux Multi-Agent Session Starter (MCP Version)
# Pro/Team Feature: This script is only available for Pro and Team subscribers.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Working directory: $WORK_DIR"

CONFIG_FILE="$WORK_DIR/.orchestrix-core/core-config.yaml"
REPO_ID=""

if [ -f "$CONFIG_FILE" ]; then
    REPO_ID=$(grep -E "^\s*repository_id:" "$CONFIG_FILE" 2>/dev/null | head -1 | sed "s/.*repository_id:[[:space:]]*['\"]*//" | sed "s/['\"].*//")
    REPO_ID=$(echo "$REPO_ID" | tr -d "'" | tr -d '"' | tr -d ' ')
fi

if [ -z "$REPO_ID" ]; then
    REPO_ID=$(basename "$WORK_DIR")
    echo "Warning: No repository_id in config, using directory name: $REPO_ID"
fi

REPO_ID=$(echo "$REPO_ID" | tr -cd 'a-zA-Z0-9_-')

if [ -z "$REPO_ID" ]; then
    REPO_ID="default"
    echo "Warning: REPO_ID is empty after sanitization, using fallback: $REPO_ID"
fi

SESSION_NAME="orchestrix-${REPO_ID}"
LOG_FILE="/tmp/${SESSION_NAME}-handoff.log"

echo "Repository ID: $REPO_ID"
echo "tmux Session: $SESSION_NAME"
echo "Log file: $LOG_FILE"

if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Please run: brew install tmux"
    exit 1
fi

CLI_TOOL="${CLI_TOOL:-cc}"

if ! command -v "$CLI_TOOL" &> /dev/null; then
    echo "Error: $CLI_TOOL command not found. Please install or configure the CLI tool."
    exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Warning: Session '$SESSION_NAME' exists, closing..."
    tmux kill-session -t "$SESSION_NAME"
fi

echo "Creating tmux session: $SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" -n "Arch" -c "$WORK_DIR"

# Inject handoff-detector hook into target project
SETTINGS_LOCAL="$WORK_DIR/.claude/settings.local.json"
HANDOFF_HOOK_CMD="bash -c 'cd \"\$(git rev-parse --show-toplevel)\" && .orchestrix-core/scripts/handoff-detector.sh'"
mkdir -p "$WORK_DIR/.claude"
if [ -f "$SETTINGS_LOCAL" ]; then
    if grep -q "handoff-detector" "$SETTINGS_LOCAL" 2>/dev/null; then
        echo "Handoff hook already configured"
    elif command -v jq &>/dev/null; then
        EXISTING=$(cat "$SETTINGS_LOCAL")
        echo "$EXISTING" | jq --arg cmd "$HANDOFF_HOOK_CMD" \
            '.hooks.Stop = (.hooks.Stop // []) + [{"hooks": [{"type": "command", "command": $cmd}]}]' \
            > "$SETTINGS_LOCAL.tmp" && mv "$SETTINGS_LOCAL.tmp" "$SETTINGS_LOCAL"
        echo "Handoff hook injected into settings.local.json"
    fi
else
    cat > "$SETTINGS_LOCAL" << SETTINGS_EOF
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$HANDOFF_HOOK_CMD"
          }
        ]
      }
    ]
  }
}
SETTINGS_EOF
    echo "Created $SETTINGS_LOCAL with handoff hook"
fi

# Ensure handoff-detector.sh exists in target project
HANDOFF_SCRIPT="$WORK_DIR/.orchestrix-core/scripts/handoff-detector.sh"
if [ ! -f "$HANDOFF_SCRIPT" ]; then
    SCRIPT_DIR_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR_SRC/handoff-detector.sh" ]; then
        mkdir -p "$(dirname "$HANDOFF_SCRIPT")"
        cp "$SCRIPT_DIR_SRC/handoff-detector.sh" "$HANDOFF_SCRIPT"
        chmod +x "$HANDOFF_SCRIPT"
        echo "Copied handoff-detector.sh to target project"
    fi
fi

# Create tmux automation marker
RUNTIME_DIR="$WORK_DIR/.orchestrix-core/runtime"
TMUX_MARKER="$RUNTIME_DIR/tmux-automation-active"
mkdir -p "$RUNTIME_DIR"
echo "{\"session\": \"$SESSION_NAME\", \"started_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$TMUX_MARKER"
cleanup() { rm -f "$TMUX_MARKER"; }
trap cleanup EXIT INT TERM

tmux set-option -t "$SESSION_NAME" status-left-length 20
tmux set-option -t "$SESSION_NAME" status-right-length 60
tmux set-option -t "$SESSION_NAME" window-status-format "#I:#W"
tmux set-option -t "$SESSION_NAME" window-status-current-format "#I:#W*"

declare -a AGENTS=("architect" "sm" "dev" "qa")
declare -a WINDOW_NAMES=("Arch" "SM" "Dev" "QA")

for i in 0 1 2 3; do
    if [ "$i" -gt 0 ]; then
        tmux new-window -t "$SESSION_NAME:$i" -n "${WINDOW_NAMES[$i]}" -c "$WORK_DIR"
    fi
    tmux send-keys -t "$SESSION_NAME:$i" "export AGENT_ID=${AGENTS[$i]} ORCHESTRIX_SESSION=$SESSION_NAME ORCHESTRIX_LOG=$LOG_FILE && clear" C-m
done

CC_STARTUP_WAIT=60
AGENT_ACTIVATION_DELAY=2
AGENT_LOAD_WAIT=15

send_command_with_delay() {
    tmux send-keys -t "$SESSION_NAME:$1" "$2"
    sleep 1
    tmux send-keys -t "$SESSION_NAME:$1" "Enter"
}

echo "Starting Claude Code in all windows..."
for i in 0 1 2 3; do
    tmux send-keys -t "$SESSION_NAME:$i" "$CLI_TOOL" C-m
done

echo "Waiting ${CC_STARTUP_WAIT}s for Claude Code to start..."
for i in $(seq "$CC_STARTUP_WAIT" -1 1); do
    printf "\r   %2d seconds remaining..." "$i"
    sleep 1
done
printf "\r   Claude Code should be ready now!      \n"

echo "Auto-activating agents..."
for i in 0 1 2 3; do
    send_command_with_delay "$i" "/o ${AGENTS[$i]}"
    [ "$i" -lt 3 ] && sleep "$AGENT_ACTIVATION_DELAY"
done

echo "Waiting ${AGENT_LOAD_WAIT}s for agents to load..."
sleep "$AGENT_LOAD_WAIT"

echo "Starting workflow in SM window..."
send_command_with_delay "1" "1"

tmux select-window -t "$SESSION_NAME:1"

echo ""
echo "==============================================="
echo "Orchestrix automation started!"
echo "==============================================="
echo "Window Layout: 0-Architect, 1-SM (current), 2-Dev, 3-QA"
echo "Navigation: Ctrl+b -> 0/1/2/3 | n/p | d | ["
echo "Monitor: tail -f $LOG_FILE"
echo "Reconnect: tmux attach -t $SESSION_NAME"
echo ""

tmux attach-session -t "$SESSION_NAME"
