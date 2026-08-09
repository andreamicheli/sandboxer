#!/usr/bin/env python3
"""Reference hardening used only by the credential-free pipeline dry-run."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


TEAM = os.environ.get("TEAM_NAME", "unknown")
PORT = int(os.environ.get("SERVICE_PORT", "8080"))
DATA_ROOT = Path("/arena/service/data").resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
(DATA_ROOT / "welcome.txt").write_text(f"Welcome to {TEAM}\n", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CyberRumbleToy/0.1-hardened"

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request = urlparse(self.path)
        if request.path == "/health":
            self._json(200, {"ok": True, "team": TEAM})
            return
        if request.path == "/api/status":
            self._json(200, {"service": "notes", "export": True, "hardened": True})
            return
        if request.path == "/api/export":
            relative = parse_qs(request.query).get("path", ["welcome.txt"])[0]
            candidate = (DATA_ROOT / relative).resolve()
            if candidate != DATA_ROOT and DATA_ROOT not in candidate.parents:
                self._json(403, {"error": "outside_data_root"})
                return
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                self._json(404, {"error": "not_found"})
                return
            self._json(200, {"name": relative, "content": content})
            return
        self._json(404, {"error": "unknown_route"})

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {message % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
