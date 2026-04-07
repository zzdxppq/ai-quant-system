#!/bin/bash
# ACP Stop Hook - Sends agent output to Gateway for LLM-based orchestration
# VERSION=3 (2026-02-03): BUG-10 fix - Check transcript for thinking state before sending webhook
# VERSION=2 (2026-01-27): Fixed settings.json to use git rev-parse for reliable path resolution
set +e

# ============================================
# Read JSON input from Claude Code (contains transcript_path)
# ============================================
HOOK_INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)

# ============================================
# Resolve PROJECT_ROOT from script path (critical fix)
# Claude Code may execute hooks from non-project directories
# ============================================
SCRIPT_PATH="$0"
if [[ "$SCRIPT_PATH" != /* ]]; then
    SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
fi
PROJECT_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"

if [[ ! "$PWD" =~ /users/[a-f0-9-]+/blueprints/ ]]; then
    PWD="$PROJECT_ROOT"
fi

GATEWAY_URL="${ACP_GATEWAY_URL:-https://ws.youlidao.ai}"
# BUG-11 Fix: Use $HOME for multi-platform support (macOS: /Users/youlidao, Ubuntu: /home/ubuntu)
WEBHOOK_SECRET_FILE="$HOME/o/webhook-secret.txt"
[[ -f "$WEBHOOK_SECRET_FILE" ]] && WEBHOOK_SECRET=$(cat "$WEBHOOK_SECRET_FILE" | tr -d '\n') || WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
LOG_FILE="/tmp/acp-stop-hook.log"
CAPTURE_LINES=500

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }
log "========== ACP Stop Hook triggered =========="
log "PROJECT_ROOT: $PROJECT_ROOT"
log "Effective PWD: $PWD"

[[ -z "$WEBHOOK_SECRET" ]] && { log "ERROR: WEBHOOK_SECRET not configured"; exit 0; }
command -v curl &>/dev/null || { log "ERROR: curl not found"; exit 0; }

# ============================================
# Session Detection (multi-method, priority-based)
# ============================================

SESSION_NAME="${ORCHESTRIX_SESSION:-}"
BLUEPRINT_ID=""

# Method A (PREFERRED): Use ORCHESTRIX_SESSION env var if set by Gateway
if [[ -n "$SESSION_NAME" ]]; then
    log "Session from env ORCHESTRIX_SESSION: $SESSION_NAME"
fi

# Method A.5 (NEW - SECOND PRIORITY): Read blueprint-id.txt from project directory
# This file is written by Gateway on ACP session start for reliable session matching
BLUEPRINT_ID_FILE="$PROJECT_ROOT/.orchestrix-core/blueprint-id.txt"
if [[ -z "$SESSION_NAME" && -f "$BLUEPRINT_ID_FILE" ]]; then
    BLUEPRINT_ID=$(cat "$BLUEPRINT_ID_FILE" | tr -d '\n')
    if [[ -n "$BLUEPRINT_ID" ]]; then
        log "Blueprint ID from file: $BLUEPRINT_ID"
        if [[ "$PWD" =~ /users/([a-f0-9-]+)/blueprints/ ]]; then
            INFERRED_USER_ID="${BASH_REMATCH[1]}"
            SESSION_NAME="u${INFERRED_USER_ID}-bp-${BLUEPRINT_ID}"
            log "Session constructed from blueprint-id.txt: $SESSION_NAME"
            if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
                log "WARNING: Constructed session does not exist, falling back"
                SESSION_NAME=""
            fi
        fi
    fi
fi

# Method B: Infer from current working directory (PWD)
if [[ -z "$SESSION_NAME" && -n "$PWD" ]]; then
    log "Attempting to infer session from PWD: $PWD"

    if [[ "$PWD" =~ /users/([a-f0-9-]+)/blueprints/ ]]; then
        INFERRED_USER_ID="${BASH_REMATCH[1]}"
        log "Inferred userId from PWD: $INFERRED_USER_ID"

        MATCHING_SESSIONS=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^u${INFERRED_USER_ID}-bp-")
        SESSION_COUNT=$(echo "$MATCHING_SESSIONS" | grep -c '^' 2>/dev/null || echo 0)

        if [[ "$SESSION_COUNT" -eq 1 ]]; then
            SESSION_NAME="$MATCHING_SESSIONS"
            log "Session inferred from PWD (single match): $SESSION_NAME"
        elif [[ "$SESSION_COUNT" -gt 1 ]]; then
            # Multiple sessions - cannot reliably determine which one without blueprint-id.txt
            log "ERROR: Multiple sessions found for user, cannot determine correct session"
            log "ERROR: Please ensure Gateway writes blueprint-id.txt on initialization"
            log "EXIT: Ambiguous session matching"
            exit 0
        fi
    fi
fi

# Method C: Fallback - first matching session (least reliable)
if [[ -z "$SESSION_NAME" ]]; then
    SESSION_NAME=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E '^(u[a-f0-9-]+-bp-|orchestrix-)' | head -1)
    [[ -n "$SESSION_NAME" ]] && log "Session from fallback (head -1): $SESSION_NAME"
fi

[[ -z "$SESSION_NAME" ]] && { log "EXIT: No session found"; exit 0; }
log "Final session: $SESSION_NAME"

# Extract blueprint_id from session name (if not already set from file)
if [[ -z "$BLUEPRINT_ID" ]]; then
    [[ "$SESSION_NAME" =~ ^u[a-f0-9-]+-bp-([a-f0-9-]+)$ ]] && BLUEPRINT_ID="${BASH_REMATCH[1]}"
fi
[[ -z "$BLUEPRINT_ID" ]] && { log "ERROR: Cannot determine blueprint_id from session: $SESSION_NAME"; exit 0; }
log "Blueprint ID: $BLUEPRINT_ID"

# Agent window names (Story 15.3: unified architect)
AGENT_WINDOWS="sm dev architect qa"

normalize_agent_id() { echo "$1"; }

find_window_for_agent() {
    local agent_id="$1"
    local session="$2"
    if tmux list-windows -t "$session" -F '#{window_name}' 2>/dev/null | grep -qx "$agent_id"; then
        echo "$agent_id"; return 0
    fi
    local match=$(tmux list-windows -t "$session" -F '#{window_name}' 2>/dev/null | grep "^${agent_id}" | head -1)
    if [[ -n "$match" ]]; then echo "$match"; return 0; fi
    echo ""; return 1
}

AGENT_ID="${AGENT_ID:-}"
WINDOW_NAME=""

# Method 0 (PREFERRED): Read from temp file written by Gateway before dispatch
AGENT_FILE="/tmp/orchestrix-$SESSION_NAME-current-agent.txt"
if [[ -z "$AGENT_ID" && -f "$AGENT_FILE" ]]; then
    AGENT_ID=$(cat "$AGENT_FILE" | tr -d '\n')
    if [[ -n "$AGENT_ID" ]]; then
        log "Agent ID (from temp file): $AGENT_ID"
        WINDOW_NAME=$(find_window_for_agent "$AGENT_ID" "$SESSION_NAME")
        if [[ -n "$WINDOW_NAME" && "$WINDOW_NAME" != "$AGENT_ID" ]]; then
            log "Mapped agent '$AGENT_ID' to window '$WINDOW_NAME'"
        elif [[ -z "$WINDOW_NAME" ]]; then
            WINDOW_NAME="$AGENT_ID"
        fi
    fi
fi

# Method 1: Try to determine from current tmux window by process
if [[ -z "$AGENT_ID" ]]; then
    for agent in $AGENT_WINDOWS; do
        PANE_PID=$(tmux display-message -t "$SESSION_NAME:$agent" -p '#{pane_pid}' 2>/dev/null)
        if [[ -n "$PANE_PID" ]] && ps -p "$PANE_PID" -o args= 2>/dev/null | grep -qE "(claude|opencode)"; then
            WINDOW_NAME="$agent"
            AGENT_ID=$(normalize_agent_id "$agent")
            log "Detected agent from window '$agent': $AGENT_ID"
            break
        fi
    done
fi

# Method 2: Fallback - check all windows for HANDOFF message
if [[ -z "$AGENT_ID" ]]; then
    for agent in $AGENT_WINDOWS; do
        OUTPUT=$(tmux capture-pane -t "$SESSION_NAME:$agent" -p -S -50 2>/dev/null)
        if echo "$OUTPUT" | grep -qE "(HANDOFF|handoff)"; then
            WINDOW_NAME="$agent"
            AGENT_ID=$(normalize_agent_id "$agent")
            log "Found HANDOFF in window '$agent': $AGENT_ID"
            break
        fi
    done
fi

[[ -z "$AGENT_ID" ]] && { log "ERROR: Cannot determine agent_id"; exit 0; }
log "Agent ID: $AGENT_ID, Window: ${WINDOW_NAME:-$AGENT_ID}"

# ============================================
# BUG-10: Check transcript for "working" state (Dev/QA only)
# Stop hook fires during Claude's "Pondering...", "Reading...", etc. pauses
# For heavy-task agents (dev, qa), check if still working before sending webhook
# Simple rule: if transcript ends with "*ing..." pattern, agent is still working
# ============================================
if [[ "$AGENT_ID" == "dev" || "$AGENT_ID" == "qa" ]]; then
    if [[ -n "$TRANSCRIPT_PATH" && -f "$TRANSCRIPT_PATH" ]]; then
        # Read last 500 chars of transcript (where status would appear)
        TRANSCRIPT_TAIL=$(tail -c 500 "$TRANSCRIPT_PATH" 2>/dev/null)

        # Simple pattern: any word ending in "ing" followed by "..." or "…"
        # Examples: "Pondering...", "Reading…", "Writing...", "Ideating…"
        if echo "$TRANSCRIPT_TAIL" | grep -qE '[A-Za-z]+ing(\.\.\.|…)'; then
            log "BUG-10: $AGENT_ID agent still working (*ing... pattern found), skipping webhook"
            exit 0
        fi

        # Also check for "thinking)" pattern (API call indicator)
        if echo "$TRANSCRIPT_TAIL" | grep -qE 'thinking\)'; then
            log "BUG-10: $AGENT_ID agent in API call (thinking pattern), skipping webhook"
            exit 0
        fi

        log "BUG-10: No working state found in transcript, proceeding"
    else
        log "BUG-10: transcript_path not available or file not found, proceeding anyway"
    fi
fi

CAPTURE_WINDOW="${WINDOW_NAME:-$AGENT_ID}"
OUTPUT=$(tmux capture-pane -t "$SESSION_NAME:$CAPTURE_WINDOW" -p -S -"$CAPTURE_LINES" 2>/dev/null)
log "Captured output: ${#OUTPUT} characters from window $CAPTURE_WINDOW"

ESCAPED_OUTPUT=$(echo "$OUTPUT" | jq -Rs '.' 2>/dev/null || echo '""')
PAYLOAD="{\"event_type\":\"agent_output\",\"blueprint_id\":\"$BLUEPRINT_ID\",\"agent_id\":\"$AGENT_ID\",\"content\":$ESCAPED_OUTPUT,\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

log "Sending webhook to $GATEWAY_URL/api/hook"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST -H "Content-Type: application/json" -H "X-Webhook-Secret: $WEBHOOK_SECRET" -d "$PAYLOAD" --max-time 10 "$GATEWAY_URL/api/hook" 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
[[ "$HTTP_CODE" == "200" ]] && log "SUCCESS: Webhook sent" || log "ERROR: Webhook failed (HTTP $HTTP_CODE)"
log "========== ACP Stop Hook complete =========="
exit 0