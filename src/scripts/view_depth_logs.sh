#!/bin/bash
# Capture logs from the depth_5020 tmux window

echo "============================================"
echo "DEPTH RECEIVER LOGS"
echo "============================================"
echo ""

if ! tmux has-session -t realsense_receiver 2>/dev/null; then
    echo "ERROR: Tmux session 'realsense_receiver' not found"
    echo "Please start the receiver first"
    exit 1
fi

if ! tmux list-windows -t realsense_receiver | grep -q "depth_5020"; then
    echo "ERROR: Window 'depth_5020' not found"
    echo "Available windows:"
    tmux list-windows -t realsense_receiver
    exit 1
fi

echo "Capturing logs from depth_5020 window..."
echo "============================================"
echo ""

# Capture the pane content (scrollback + visible)
tmux capture-pane -t realsense_receiver:depth_5020 -p -S -100

echo ""
echo "============================================"
echo "End of logs"
echo "============================================"
echo ""
echo "To view in real-time:"
echo "  tmux attach -t realsense_receiver"
echo "  Then navigate to window 'depth_5020' (Ctrl+b w)"
