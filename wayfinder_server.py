#!/usr/bin/env python3
"""Small live backend for the Sandboxer Wayfinder tree.

It polls GitHub in a background thread and pushes changed issue snapshots to
connected browsers over Server-Sent Events (SSE). No third-party packages are
required. Set GITHUB_TOKEN for a higher GitHub API rate limit when needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO = os.environ.get("WAYFINDER_REPO", "andreamicheli/sandboxer")
GITHUB_URL = f"https://api.github.com/repos/{REPO}/issues?state=all&per_page=100&sort=created&direction=asc"
ROOT = Path(__file__).resolve().parent
HTML = ROOT / "docs" / "wayfinder-tree.html"


class Store:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.payload: dict[str, Any] = {"repo": REPO, "issues": [], "updated_at": None, "error": None}
        self.version = 0
        self.etag: str | None = None
        self.github_headers: dict[str, str] = {}

    def set(self, payload: dict[str, Any], etag: str | None = None) -> None:
        with self.condition:
            # Avoid waking SSE clients when GitHub returned an unchanged result.
            if payload["issues"] == self.payload["issues"] and payload.get("error") == self.payload.get("error"):
                self.etag = etag or self.etag
                return
            self.payload = payload
            self.etag = etag or self.etag
            self.version += 1
            self.condition.notify_all()

    def wait_for_change(self, version: int, timeout: float = 25) -> tuple[int, dict[str, Any]]:
        with self.condition:
            self.condition.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version, self.payload


STORE = Store()
ANALYSIS_LOCK = threading.Lock()
ANALYSIS: dict[str, Any] = {"id": None, "status": "idle", "progress": 0, "message": "Pronto", "result": None, "error": None}


def labels(issue: dict[str, Any]) -> list[str]:
    return [label if isinstance(label, str) else label.get("name", "") for label in issue.get("labels", [])]


def parent_of(issue: dict[str, Any]) -> int | None:
    number = issue["number"]
    if number == 1:
        return None
    body = str(issue.get("body") or "")
    match = re.search(r"(?:Part of|Parent)[\s\S]{0,220}?(?:issues/|#)(\d+)", body, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def normalize(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for issue in raw:
        if issue.get("pull_request"):
            continue
        number = issue["number"]
        issue_labels = labels(issue)
        body = str(issue.get("body") or "")
        linked = re.search(r"(?:Part of|Parent)[\s\S]{0,220}?(?:issues/|#)(\d+)", body, re.IGNORECASE)
        if not (1 <= number <= 41 or linked or any(label.startswith("wayfinder:") for label in issue_labels)):
            continue
        selected.append({
            "number": number,
            "title": issue.get("title", ""),
            "state": issue.get("state", "closed"),
            "labels": issue_labels,
            "body": issue.get("body") or "",
            "html_url": issue.get("html_url", f"https://github.com/{REPO}/issues/{number}"),
            "parent": parent_of(issue),
            "kind": "map" if "wayfinder:map" in issue_labels else "operation" if number >= 26 else "ticket",
        })
    return sorted(selected, key=lambda item: item["number"])


def poll() -> None:
    while True:
        request = Request(GITHUB_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sandboxer-wayfinder/1.0",
            **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}),
            **STORE.github_headers,
        })
        try:
            with urlopen(request, timeout=20) as response:
                raw = json.load(response)
                etag = response.headers.get("ETag")
                STORE.github_headers = {"If-None-Match": etag} if etag else {}
                STORE.set({"repo": REPO, "issues": normalize(raw), "updated_at": time.time(), "error": None}, etag)
        except HTTPError as error:
            if error.code != HTTPStatus.NOT_MODIFIED:
                STORE.set({**STORE.payload, "error": f"GitHub HTTP {error.code}"})
        except (URLError, TimeoutError, OSError) as error:
            STORE.set({**STORE.payload, "error": f"GitHub non raggiungibile: {error.reason if isinstance(error, URLError) else error}"})
        time.sleep(POLL_INTERVAL)


def analysis_prompt() -> str:
    issues = STORE.payload["issues"]
    open_numbers = [str(issue["number"]) for issue in issues if issue["state"] == "open"]
    closed_numbers = [str(issue["number"]) for issue in issues if issue["state"] == "closed"]
    rows = "\n".join(
        f"- #{issue['number']} [{issue['state']}] {issue['title']} (parent: {issue['parent']})"
        for issue in issues
    )
    return f"""Analizza lo stato del progetto Sandboxer sulla base del seguente snapshot aggiornato delle GitHub issues.

Conteggio autorevole dello snapshot: {len(open_numbers)} aperte ({', '.join('#'+n for n in open_numbers) or 'nessuna'}); {len(closed_numbers)} chiuse ({', '.join('#'+n for n in closed_numbers) or 'nessuna'}).
Usa questi stati esatti. Un ticket aperto non è completato anche se il suo testo descrive criteri o lavoro già esistente.

{rows}

Scrivi una risposta in italiano, naturale e concreta, per il team. Spiega:
1. a che punto siamo separando decisioni chiuse, implementazione chiusa e implementazione ancora aperta;
2. cosa è realmente pronto oggi e cosa è solo pianificato;
3. quali sono i blocchi e le dipendenze più importanti;
4. una stima qualitativa di quanto manca per un Sandboxer v0 pubblicabile;
5. i prossimi tre passi consigliati.

Non inventare dati non presenti nello snapshot. Considera la conclusione solo quando il ticket di release finale e le sue prove di accettazione sono completati. Usa titoli brevi e linguaggio comprensibile.

IMPORTANTE: non usare strumenti, shell, filesystem, rete o comandi. Non leggere altri file. Rispondi esclusivamente usando lo snapshot qui sopra."""


def set_analysis(**values: Any) -> None:
    with ANALYSIS_LOCK:
        ANALYSIS.update(values)


def run_analysis(job_id: str) -> None:
    set_analysis(status="running", progress=8, message="Avvio agy / Gemini 3.6…", result=None, error=None)
    command = [
        "agy", "--model", "gemini-3.6-flash-medium",
        "--output-format", "text", "--print-timeout", "5m",
        "--mode", "plan", "--disable-slash-commands", "--sandbox", "--prompt", analysis_prompt(),
    ]
    try:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            if line.lower().startswith("warning:"):
                continue
            output.append(line)
            match = re.search(r"(?<!\d)(\d{1,3})\s*%", line)
            progress = min(94, max(10, int(match.group(1)))) if match else min(90, 10 + len(output))
            set_analysis(progress=progress, message="Gemini sta analizzando lo snapshot…")
        return_code = process.wait()
        result = "".join(output).strip()
        if return_code:
            raise RuntimeError(result[-1200:] or f"agy terminato con codice {return_code}")
        set_analysis(status="complete", progress=100, message="Analisi completata", result=result, error=None)
    except Exception as error:  # surface the command failure in the UI
        set_analysis(status="error", progress=100, message="Analisi non riuscita", error=str(error), result=None)


def start_analysis() -> dict[str, Any]:
    with ANALYSIS_LOCK:
        if ANALYSIS["status"] == "running":
            return dict(ANALYSIS)
        job_id = uuid.uuid4().hex
        ANALYSIS.update({"id": job_id, "status": "queued", "progress": 2, "message": "In coda…", "result": None, "error": None})
    threading.Thread(target=run_analysis, args=(job_id,), daemon=True, name="agy-analysis").start()
    return dict(ANALYSIS)


class Handler(BaseHTTPRequestHandler):
    server_version = "SandboxerWayfinder/1.0"

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/api/wayfinder":
            self.send_bytes(json.dumps(STORE.payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path == "/events":
            self.sse()
            return
        if self.path == "/api/analyze/status":
            with ANALYSIS_LOCK:
                payload = dict(ANALYSIS)
            self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path in ("/", "/wayfinder-tree.html"):
            self.send_bytes(HTML.read_bytes(), "text/html; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/api/analyze":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_bytes(json.dumps(start_analysis(), ensure_ascii=False).encode(), "application/json; charset=utf-8", HTTPStatus.ACCEPTED)

    def sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        version = -1
        try:
            while True:
                version, payload = STORE.wait_for_change(version)
                message = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                self.wfile.write(message)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    global POLL_INTERVAL
    parser = argparse.ArgumentParser(description="Live GitHub backend for the Sandboxer Wayfinder tree")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--poll-interval", type=int, default=30, help="secondi tra le richieste GitHub")
    args = parser.parse_args()
    POLL_INTERVAL = max(10, args.poll_interval)
    threading.Thread(target=poll, daemon=True, name="github-poller").start()
    print(f"Wayfinder live at http://{args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


POLL_INTERVAL = 30

if __name__ == "__main__":
    main()
