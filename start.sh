#!/bin/sh
# Starts the web screen and the weekly scheduler side by side.
set -e
python -m wijnkoop init
python -m wijnkoop scheduler &
exec datasette serve "${WIJNKOOP_DATA:-/data}/wijnkoop.db" \
  --host 0.0.0.0 --port 8001 \
  --metadata /app/datasette/metadata.yml \
  --setting default_page_size 50 \
  --setting sql_time_limit_ms 5000
