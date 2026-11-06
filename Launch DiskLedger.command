#!/bin/zsh
set -eu

app_dir="${0:A:h}"
if [[ ! -x /usr/bin/trash ]] || ! command -v python3 >/dev/null; then
    print -u2 'DiskLedger requires macOS 15 or later and Python 3.9 or later.'
    read -r '?Press Return to exit.'
    exit 1
fi

exec python3 -B "$app_dir/server.py"
