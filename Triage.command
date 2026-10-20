#!/bin/sh
# Double-click this file in Finder: sets up on first use, scans your inboxes, opens the report.
cd "$(dirname "$0")" || exit 1
./triage start "$@"
printf '\nPress Enter to close this window.'
read -r _
