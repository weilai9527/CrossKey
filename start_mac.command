#!/bin/zsh

cd "$(dirname "$0")" || exit 1

if [[ -x .venv/bin/python3 ]]; then
  crosskey_python="$PWD/.venv/bin/python3"
elif [[ -x venv/bin/python3 ]]; then
  crosskey_python="$PWD/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  crosskey_python="$(command -v python3)"
else
  echo "CrossKey needs Python 3.10 or newer with Tcl/Tk."
  echo "Install Python from python.org, then run: python3 -m pip install -r requirements.txt"
  read -r "?Press Enter to close..."
  exit 1
fi

if ! "$crosskey_python" -c 'import sys, tkinter; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'; then
  echo "This Python needs Python 3.10+ and Tcl/Tk support to open the CrossKey window."
  echo "You can still use the command-line client: python3 mac_client.py"
  read -r "?Press Enter to close..."
  exit 1
fi

echo "Opening CrossKey. Connection and screen layout are configured in the small window."
"$crosskey_python" "$PWD/launcher.py"
crosskey_exit=$?
if (( crosskey_exit != 0 )); then
  echo "CrossKey could not start. Review the error above."
  echo "If a dependency is missing, run: python3 -m pip install -r requirements.txt"
  read -r "?Press Enter to close..."
fi
exit "$crosskey_exit"
