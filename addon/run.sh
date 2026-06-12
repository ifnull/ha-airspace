#!/usr/bin/env sh
# Add-on entrypoint: render the service config from the Supervisor-provided
# add-on options, then hand off to the service. `exec` so ha-airspace becomes
# PID 1 and receives SIGTERM directly for graceful shutdown.
set -eu

OPTIONS="/data/options.json"
CONFIG="/data/config.yaml"

echo "[ha-airspace] rendering config from add-on options"
/app/.venv/bin/python /app/render_config.py "${OPTIONS}" "${CONFIG}"

echo "[ha-airspace] starting service"
exec ha-airspace -c "${CONFIG}"
