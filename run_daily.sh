#!/bin/sh
# Run the digest once (e.g. from cron: 0 7 * * * /path/to/run_daily.sh)
cd "$(dirname "$0")" && python3 -m jobsearch
