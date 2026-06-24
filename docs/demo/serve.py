#!/usr/bin/env python3
"""Serve the demo fixtures for ha-airspace's README screenshots.

Stdlib only. Serves the three JSON files in this directory on
``http://127.0.0.1:8765`` and, on every request, stamps ``now`` with the wall
clock and advances ``messages`` — so ha-airspace's message-rate entities show
realistic msg/s instead of a frozen 0 (the positions stay put, which is what
you want for a stable screenshot).

    cd docs/demo && python3 serve.py
    # then, in another shell:
    ha-airspace --config docs/demo/config.yaml

No dependencies, no network, no personal data.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).parent
_HOST, _PORT = "127.0.0.1", 8765

# Per-poll message increments — with ~1 Hz polling these read as ~msg/s.
_RATE = {"aircraft.json": 212, "aircraft978.json": 38, "remoteid.json": 47}
_counters = {"aircraft.json": 4_821_000, "aircraft978.json": 96_000, "remoteid.json": 18_400}


def _payload(name: str) -> bytes:
    doc = json.loads((_HERE / name).read_text(encoding="utf-8"))
    doc["now"] = time.time()
    if name in _counters:
        _counters[name] += _RATE[name]
        doc["messages"] = _counters[name]
    return json.dumps(doc).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        name = self.path.lstrip("/").split("?", 1)[0]
        if name not in {"aircraft.json", "aircraft978.json", "remoteid.json", "receiver.json"}:
            self.send_error(404)
            return
        body = _payload(name)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:  # quiet
        pass


def main() -> None:
    server = ThreadingHTTPServer((_HOST, _PORT), Handler)
    print(f"demo feeds on http://{_HOST}:{_PORT}/  (aircraft.json, remoteid.json)  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
