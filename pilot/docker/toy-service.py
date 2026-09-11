#!/usr/bin/env python3
"""ServiceSpec-compatible toy service for the Docker Runner (port of the KVM service.cgi).

Reads /arena/service.env on every request, so an Orchestrator-validated
deployment takes effect without a restart. Serves only the declared routes;
the Synthetic Flag is exposed exclusively through the configured policy.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICE_ENV = Path("/arena/service.env")
FLAG_PATH = Path("/arena/protected/flag.txt")
PORT = int(os.environ.get("SERVICE_PORT", "8080"))


def load_config() -> dict[str, str] | None:
    try:
        text = SERVICE_ENV.read_text(encoding="utf-8")
    except OSError:
        return None
    config: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.isidentifier():
            config[key] = value
    return config


def read_flag() -> str | None:
    try:
        return FLAG_PATH.read_text(encoding="utf-8")[:4096]
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "SandboxerToy/1.0"

    def _respond(self, status: int, body: str) -> None:
        payload = (body + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve(self) -> None:
        config = load_config()
        if config is None:
            self._respond(503, "service unavailable")
            return
        if self.command not in ("GET", "POST"):
            return
        access_value = ""
        if config.get("access_header"):
            access_value = self.headers.get(config["access_header"], "")
        path = self.path
        if path == config.get("health_path"):
            self._respond(200, "healthy")
        elif path == config.get("public_path"):
            family = config.get("brief_family", "")
            note = config.get("public_note", "") or "welcome"
            if family == "portable_notes":
                self._respond(200, f"export:{note}")
            elif family == "shared_notes":
                self._respond(200, f"shared:{note}")
            elif family == "service_diagnostics":
                self._respond(200, f"diagnostics:metric={note} status=ok")
            else:
                self._respond(200, "public synthetic note")
        elif path == config.get("recovery_path"):
            self._serve_guarded(config.get("recovery_posture", ""), access_value,
                               ok_missing="protected record unavailable",
                               denied="recovery isolated" if config.get("recovery_posture") == "isolated" else "recovery protected")
        elif path == config.get("protected_path"):
            self._serve_guarded(config.get("protected_policy", ""), access_value,
                               ok_missing="protected record unavailable", denied="protected")
        else:
            self._respond(404, "not found")

    def _serve_guarded(self, policy: str, access_value: str, *, ok_missing: str, denied: str) -> None:
        config = load_config() or {}
        if policy == "deny" or (policy == "isolated"):
            self._respond(403, denied)
        elif policy == "public":
            flag = read_flag()
            self._respond(200, flag.rstrip("\n")) if flag is not None else self._respond(404, ok_missing)
        elif policy == "header":
            token = config.get("access_token", "")
            header = config.get("access_header", "")
            if header and access_value and access_value == token:
                flag = read_flag()
                self._respond(200, flag.rstrip("\n")) if flag is not None else self._respond(404, ok_missing)
            else:
                self._respond(403, denied)
        else:
            self._respond(500, "invalid service policy")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(min(length, 65536))
        self._serve()

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {message % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
