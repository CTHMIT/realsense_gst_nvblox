#!/bin/bash

echo "=== checkD435i =="
for i in {0..5}; do
    dev="/dev/video$i"
    echo -e "\n--- $dev ---"
    v4l2-ctl -d "$dev" --all 2>/dev/null | grep -E "Card type|Driver name|Format|fourcc" | head -5
    echo "Formats:"
    v4l2-ctl -d "$dev" --list-formats-ext 2>/dev/null | grep -E "YUYV|Z16|GREY|Y8|MJPG" | head -3
done
