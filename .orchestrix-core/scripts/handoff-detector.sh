#!/bin/bash
# Orchestrix HANDOFF Detector - tmux Automation Hook (MCP Version)
# Triggers on Claude Code Stop event, detects HANDOFF and routes to target agent
#
# Pro/Team Feature: This script is only available for Pro and Team subscribers.

set +e

# Fix PATH for homebrew (Apple Silicon & Intel Macs)
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

SESSION_NAME="${ORCHESTRIX_SESSION:-}"

get_agent_name() {
    case "$1" in
        0) echo "architect" ;;
        1) echo "sm" ;;
        2) echo "dev" ;;
        3) echo "qa" ;;
        *) echo "" ;;
    esac
}

get_window_num() {
    case "$1" in
        architect) echo "0" ;;
        sm) echo "1" ;;
        dev) echo "2" ;;
        qa) echo "3" ;;
        *) echo "" ;;
    esac
}

get_agent_command() {
    case "$1" in
        architect) echo "/o architect" ;;
        sm) echo "/o sm" ;;
        dev) echo "/o dev" ;;
        qa) echo "/o qa" ;;
        *) echo "" ;;
    esac
}

get_target_from_command() {
    local cmd="$1"
    case "$cmd" in
        develop-story|apply-qa-fixes|quick-develop)
            echo "dev" ;;
        review|quick-verify|test-design|finalize-commit)
            echo "qa" ;;
        draft|revise-story|revise|apply-proposal|create-next-story)
            echo "sm" ;;
        review-escalation|resolve-change)
            echo "architect" ;;
        *)
            echo "" ;;
    esac
}

if [[ -z "$SESSION_NAME" && -n "$TMUX" ]]; then
    CURRENT_SESSION=$(tmux display-message -p '#{session_name}' 2>/dev/null)
    if [[ "$CURRENT_SESSION" == orchestrix* || "$CURRENT_SESSION" =~ ^u[a-f0-9-]+-bp- ]]; then
        SESSION_NAME="$CURRENT_SESSION"
    fi
fi

if [[ -z "$SESSION_NAME" ]]; then
    SESSION_NAME=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E '^(u[a-f0-9-]+-bp-|orchestrix-)' | head -1)
fi

[[ -z "$SESSION_NAME" ]] && exit 0
! tmux has-session -t "$SESSION_NAME" 2>/dev/null && exit 0

LOG_FILE="/tmp/${SESSION_NAME}-handoff.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

log "========== Hook triggered =========="
log "Session: $SESSION_NAME"

PROCESSED_FILE="/tmp/${SESSION_NAME}-processed.txt"
touch "$PROCESSED_FILE"

SOURCE_WIN=""
TARGET=""
CMD=""
HANDOFF_HASH=""

for win in 0 1 2 3; do
    OUTPUT=$(tmux capture-pane -t "$SESSION_NAME:$win" -p -S -100 2>/dev/null)
    [[ -z "$OUTPUT" ]] && continue

    LINE=$(echo "$OUTPUT" | grep -E '🎯.*HANDOFF.*TO' | tail -1)
    if [[ -n "$LINE" ]]; then
        HASH=$(echo "$LINE" | md5 2>/dev/null || echo "$LINE" | md5sum 2>/dev/null | cut -d' ' -f1)
        if grep -q "$HASH" "$PROCESSED_FILE" 2>/dev/null; then continue; fi
        if [[ "$LINE" =~ HANDOFF[[:space:]]+TO[[:space:]]+([a-zA-Z0-9_-]+):[[:space:]]*\*?([a-z0-9-]+)([[:space:]]+([0-9]+\.[0-9]+))? ]]; then
            SOURCE_WIN=$win
            TARGET=$(echo "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]')
            CMD="*${BASH_REMATCH[2]}${BASH_REMATCH[4]:+ ${BASH_REMATCH[4]}}"
            HANDOFF_HASH=$HASH
            log "Found HANDOFF in window $win: $LINE"
            break
        fi
    fi

    if [[ -z "$TARGET" ]]; then
        LAST_LINES=$(echo "$OUTPUT" | tail -30)
        SIMPLE_LINE=$(echo "$LAST_LINES" | grep -E '\*[a-z]+-?[a-z-]*\s+[0-9]+\.[0-9]+' | tail -1)
        if [[ -z "$SIMPLE_LINE" ]]; then
            SIMPLE_LINE=$(echo "$LAST_LINES" | grep -E '^\s*\*[a-z]+-?[a-z-]*\s*$' | tail -1)
        fi
        if [[ -n "$SIMPLE_LINE" ]]; then
            if [[ "$SIMPLE_LINE" =~ \*[[:space:]]*([a-z][a-z-]*)[[:space:]]*([0-9]+\.[0-9]+)? ]]; then
                simple_cmd="${BASH_REMATCH[1]}"
                story_id="${BASH_REMATCH[2]}"
                inferred_target=$(get_target_from_command "$simple_cmd")
                if [[ -n "$inferred_target" ]]; then
                    HASH=$(echo "$SIMPLE_LINE" | md5 2>/dev/null || echo "$SIMPLE_LINE" | md5sum 2>/dev/null | cut -d' ' -f1)
                    if grep -q "$HASH" "$PROCESSED_FILE" 2>/dev/null; then continue; fi
                    SOURCE_WIN=$win
                    TARGET=$inferred_target
                    CMD=$([ -n "$story_id" ] && echo "*${simple_cmd} ${story_id}" || echo "*${simple_cmd}")
                    HANDOFF_HASH=$HASH
                    log "[SIMPLE] Found in window $win: '$SIMPLE_LINE' -> $TARGET: $CMD"
                    break
                fi
            fi
        fi
    fi
done

if [[ -z "$TARGET" || -z "$CMD" ]]; then
    log "No HANDOFF in terminal output, checking fallback file..."
    FALLBACK_FILE=""
    for win in 0 1 2 3; do
        PANE_DIR=$(tmux display-message -t "$SESSION_NAME:$win" -p '#{pane_current_path}' 2>/dev/null)
        if [[ -n "$PANE_DIR" && -f "$PANE_DIR/.orchestrix-core/runtime/pending-handoff.json" ]]; then
            FALLBACK_FILE="$PANE_DIR/.orchestrix-core/runtime/pending-handoff.json"
            SOURCE_WIN=$win
            break
        fi
    done
    if [[ -z "$FALLBACK_FILE" ]]; then log "No pending-handoff.json found"; exit 0; fi
    log "Found fallback file: $FALLBACK_FILE"
    if command -v jq &>/dev/null; then
        STATUS=$(jq -r '.status // "unknown"' "$FALLBACK_FILE" 2>/dev/null)
        [[ "$STATUS" != "pending" ]] && { log "Fallback status '$STATUS', not pending"; exit 0; }
        TARGET=$(jq -r '.target_agent // ""' "$FALLBACK_FILE" 2>/dev/null)
        CMD=$(jq -r '.command // ""' "$FALLBACK_FILE" 2>/dev/null)
        STORY_ID=$(jq -r '.story_id // ""' "$FALLBACK_FILE" 2>/dev/null)
        SOURCE_AGENT=$(jq -r '.source_agent // ""' "$FALLBACK_FILE" 2>/dev/null)
    else
        STATUS=$(grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
        [[ "$STATUS" != "pending" ]] && { log "Fallback status '$STATUS', not pending"; exit 0; }
        TARGET=$(grep -o '"target_agent"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
        CMD=$(grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
        STORY_ID=$(grep -o '"story_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
        SOURCE_AGENT=$(grep -o '"source_agent"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
    fi
    [[ -z "$TARGET" || -z "$CMD" ]] && { log "Invalid fallback file content"; exit 0; }
    HANDOFF_HASH=$(echo "fallback-$SOURCE_AGENT-$TARGET-$CMD-$STORY_ID" | md5 2>/dev/null || echo "fallback-$SOURCE_AGENT-$TARGET-$CMD-$STORY_ID" | md5sum 2>/dev/null | cut -d' ' -f1)
    if grep -q "$HANDOFF_HASH" "$PROCESSED_FILE" 2>/dev/null; then log "Fallback already processed"; exit 0; fi
    SOURCE_WIN=$(get_window_num "$SOURCE_AGENT")
    log "[FALLBACK] Recovered: $SOURCE_AGENT (win $SOURCE_WIN) -> $TARGET: $CMD"
    if command -v jq &>/dev/null; then
        jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.status = "completed_by_fallback" | .completed_at = $ts' "$FALLBACK_FILE" > "$FALLBACK_FILE.tmp.$$" 2>/dev/null && mv "$FALLBACK_FILE.tmp.$$" "$FALLBACK_FILE"
    else
        sed -i.bak 's/"status"[[:space:]]*:[[:space:]]*"pending"/"status": "completed_by_fallback"/' "$FALLBACK_FILE" 2>/dev/null
        rm -f "$FALLBACK_FILE.bak" 2>/dev/null
    fi
fi

echo "$HANDOFF_HASH" >> "$PROCESSED_FILE"
tail -100 "$PROCESSED_FILE" > "$PROCESSED_FILE.tmp.$$" 2>/dev/null && mv "$PROCESSED_FILE.tmp.$$" "$PROCESSED_FILE"

[[ -z "$SOURCE_AGENT" ]] && SOURCE_AGENT=$(get_agent_name "$SOURCE_WIN")
TARGET_WIN=$(get_window_num "$TARGET")
[[ -z "$TARGET_WIN" ]] && { log "ERROR: Unknown target '$TARGET'"; exit 0; }

# Define lock early (needed by both normal path and self-retry)
LOCK="/tmp/${SESSION_NAME}-${SOURCE_WIN}.lock"
LOCK_TIMEOUT=60

if [[ "$TARGET_WIN" == "$SOURCE_WIN" ]]; then
    # Allow self-retry ONLY from fallback (pre-registered pending-handoff with PENDING gate_result)
    GATE_RESULT=""
    if [[ -n "$FALLBACK_FILE" ]]; then
        if command -v jq &>/dev/null; then
            GATE_RESULT=$(jq -r '.gate_result // ""' "$FALLBACK_FILE" 2>/dev/null)
        else
            GATE_RESULT=$(grep -o '"gate_result"[[:space:]]*:[[:space:]]*"[^"]*"' "$FALLBACK_FILE" | sed 's/.*"\([^"]*\)"$/\1/')
        fi
    fi

    if [[ "$GATE_RESULT" == "PENDING" ]]; then
        log "[SELF-RETRY] Fallback self-retry detected ($SOURCE_AGENT -> $TARGET). gate_result=PENDING, allowing."

        if ! mkdir "$LOCK" 2>/dev/null; then
            log "SKIP: Window $SOURCE_WIN locked (self-retry)"
            exit 0
        fi
        date +%s > "$LOCK/ts"

        RELOAD_CMD=$(get_agent_command "$SOURCE_AGENT")
        (
            log "[SELF-RETRY] Clearing and reloading $SOURCE_AGENT (window $SOURCE_WIN)"
            tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" "/clear" 2>/dev/null
            sleep 0.5
            tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" Enter
            sleep 5

            if [[ -n "$RELOAD_CMD" ]]; then
                tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" "$RELOAD_CMD" 2>/dev/null
                sleep 0.5
                tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" Enter
                sleep 15
            fi

            tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" "$CMD" 2>/dev/null
            sleep 0.5
            tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" Enter
            log "[SELF-RETRY] Command '$CMD' sent to $SOURCE_AGENT"

            rm -rf "$LOCK"
            log "[SELF-RETRY] Complete, lock released"
        ) >> "$LOG_FILE" 2>&1 &
        log "Self-retry background process started (PID $!)"
        log "========== Hook complete (self-retry) =========="
        exit 0
    else
        log "ERROR: Source and target are same window (not a fallback self-retry)"
        exit 0
    fi
fi

log "HANDOFF: $SOURCE_AGENT (win $SOURCE_WIN) -> $TARGET (win $TARGET_WIN)"
log "Command: $CMD"

if ! mkdir "$LOCK" 2>/dev/null; then
    if [[ -f "$LOCK/ts" ]]; then
        ts=$(cat "$LOCK/ts" 2>/dev/null || echo 0)
        now=$(date +%s)
        age=$((now - ts))
        [[ $age -lt $LOCK_TIMEOUT ]] && { log "SKIP: Locked (${age}s ago)"; exit 0; }
        log "Stale lock (${age}s), cleaning"
    fi
    rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { log "SKIP: lock race"; exit 0; }
fi
date +%s > "$LOCK/ts"

log "Sending '$CMD' to $TARGET (window $TARGET_WIN)..."
if tmux send-keys -t "$SESSION_NAME:$TARGET_WIN" "$CMD" 2>/dev/null; then
    sleep 0.5
    tmux send-keys -t "$SESSION_NAME:$TARGET_WIN" Enter
    log "SUCCESS: Command sent to $TARGET"
else
    log "ERROR: Failed to send command"
    rm -rf "$LOCK"
    exit 0
fi

RELOAD_CMD=$(get_agent_command "$SOURCE_AGENT")
(
    log "[BG] Starting cleanup for $SOURCE_AGENT (window $SOURCE_WIN)"
    sleep 2
    tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" "/clear" 2>/dev/null
    sleep 0.5
    tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" Enter
    log "[BG] /clear sent to $SOURCE_AGENT"
    sleep 5
    if [[ -n "$RELOAD_CMD" ]]; then
        tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" "$RELOAD_CMD" 2>/dev/null
        sleep 0.5
        tmux send-keys -t "$SESSION_NAME:$SOURCE_WIN" Enter
        log "[BG] Reload sent: $RELOAD_CMD"
        sleep 15
    fi
    if [[ -n "$HANDOFF_HASH" && -f "$PROCESSED_FILE" ]]; then
        grep -v "^${HANDOFF_HASH}$" "$PROCESSED_FILE" > "$PROCESSED_FILE.tmp.$$" 2>/dev/null && mv -f "$PROCESSED_FILE.tmp.$$" "$PROCESSED_FILE"
        log "[BG] Hash removed from processed file: $HANDOFF_HASH"
    fi
    rm -rf "$LOCK"
    log "[BG] Cleanup complete, lock released"
) >> "$LOG_FILE" 2>&1 &

log "Background process started (PID $!)"
log "========== Hook complete =========="
exit 0
