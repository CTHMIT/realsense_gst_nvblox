#!/bin/bash
# RealSense Tmux Session Manager

list_sessions() {
    echo "Active RealSense sessions:"
    tmux list-sessions 2>/dev/null | grep "realsense_" || echo "  No active sessions"
}

attach_latest() {
    SESSION=$(tmux list-sessions 2>/dev/null | grep "realsense_" | tail -1 | cut -d: -f1)
    if [ -z "$SESSION" ]; then
        echo "No RealSense sessions found"
        exit 1
    fi
    echo "Attaching to: $SESSION"
    tmux attach -t "$SESSION"
}

kill_all() {
    SESSIONS=$(tmux list-sessions 2>/dev/null | grep "realsense_" | cut -d: -f1)
    if [ -z "$SESSIONS" ]; then
        echo "No RealSense sessions to kill"
        exit 0
    fi

    echo "Killing sessions:"
    echo "$SESSIONS"
    echo "$SESSIONS" | while read session; do
        tmux kill-session -t "$session"
        echo "  ✓ Killed $session"
    done
}

show_help() {
    cat << EOF
RealSense Tmux Manager

Usage: $0 [command]

Commands:
    list      - List all RealSense tmux sessions
    attach    - Attach to latest RealSense session
    kill-all  - Kill all RealSense sessions
    help      - Show this help

Examples:
    $0 list
    $0 attach
    $0 kill-all
EOF
}

case "${1:-help}" in
    list)
        list_sessions
        ;;
    attach)
        attach_latest
        ;;
    kill-all)
        kill_all
        ;;
    help|*)
        show_help
        ;;
esac
