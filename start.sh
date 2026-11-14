#!/bin/sh
# Starts the web screen and the weekly scheduler side by side.
set -e
# The web screens import the wijnkoop package from here.
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"
python -m wijnkoop init
# Put the build date and code version on every page.
sed -e "s|__BUILD_DATE__|${BUILD_DATE:-unknown}|g" \
    -e "s|__GIT_SHA__|${GIT_SHA:-main}|g" \
    -e "s|__GIT_SHORT__|$(echo "${GIT_SHA:-unknown}" | cut -c1-7)|g" \
    /app/datasette/metadata.yml > /tmp/metadata.yml
# Signed cookies (the form tokens) stay valid after a restart or update: a page that was open
# before it can still save. Without a fixed secret Datasette makes a new one at every start.
SECRET_FILE="${WIJNKOOP_DATA:-/data}/datasette-secret"
if [ ! -s "$SECRET_FILE" ]; then
  (umask 077; python -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE")
fi
export DATASETTE_SECRET="$(cat "$SECRET_FILE")"
python -m wijnkoop scheduler &
exec datasette serve "${WIJNKOOP_DATA:-/data}/wijnkoop.db" \
  --host 0.0.0.0 --port 8001 \
  --metadata /tmp/metadata.yml \
  --plugins-dir /app/datasette/plugins \
  --template-dir /app/datasette/templates \
  --setting default_page_size 50 \
  --setting sql_time_limit_ms 5000
