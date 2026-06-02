#!/bin/zsh

cd "$(dirname "$0")"

echo "Mac -> Windows control demo"
echo
echo "Windows host is configured in config.json."
echo "Make sure Windows is already running: python win_server.py"
echo "If macOS says permissions are missing, enable Accessibility and Input Monitoring for Terminal."
echo
echo "Press F8 to control Windows. Press F8 again to stop."
echo "If F8 does not work, use Control + Option + Command + 8."
echo "Mac local mouse/keyboard is suppressed only while control mode is ON."
echo "Press Ctrl+C in this window to quit."
echo

python3 mac_client.py

echo
echo "Program exited. You can close this window."
read -r "?Press Enter to close..."
