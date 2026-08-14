"""Control-plane credential setup for the broadcast layer.

Writes secrets into the pilot ``.env`` (chmod 600) — credentials stay out of
the repository and out of every Runner.

Gemini TTS API key (from Google AI Studio):

    python scripts/setup_credentials.py gemini --key AIza... --probe

YouTube OAuth 2.0 login (Desktop-app client, captures a refresh token and
verifies the authorized channel):

    python scripts/setup_credentials.py youtube \\
        --client-id <id>.apps.googleusercontent.com --client-secret <secret>

On a headless host there is no browser to open.  Two options:

- ``--flow local`` (default): starts a loopback authorization server on
  ``--port`` (8080) and prints the URL; run an SSH tunnel from a machine that
  has a browser, then visit the URL there:

      ssh -L 8080:localhost:8080 <user>@<this-host>

- ``--flow manual``: prints the authorization URL, then asks you to paste back
  the full redirect URL after approving.  Works from any device with a
  browser (e.g. a phone) and needs no tunnel:

      python scripts/setup_credentials.py youtube --flow manual \\
          --client-id <id> --client-secret <secret>

Both commands accept ``--env PATH`` to target a different env file and print
only masked values.  The Gemini probe performs one tiny real TTS synthesis
("Say ok.") to prove the key and the pinned preview model work.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.tts import DEFAULT_SETTINGS_VERSION, DEFAULT_TTS_MODEL, DEFAULT_VOICES, GeminiTtsAdapter, TtsError
from sandboxer_v0.video import TtsPreflight

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _env_path(arg: Path | None) -> Path:
    return arg or Path(__file__).resolve().parent.parent / ".env"


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Update or append KEY=VALUE lines in a dotenv file, preserving other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = set(updates)
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                remaining.discard(key)
                continue
        output.append(line)
    output.extend(f"{key}={updates[key]}" for key in sorted(remaining))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _cmd_gemini(args: argparse.Namespace) -> int:
    path = _env_path(args.env)
    key = args.key or os.environ.get("GEMINI_API_KEY")
    if not key:
        key = input("Paste your Google AI Studio API key (https://aistudio.google.com/apikey): ").strip()
    if not key:
        print("GEMINI_KEY_MISSING: no API key provided", file=sys.stderr)
        return 2
    if not key.startswith("AIza"):
        print(f"WARNING: key {_mask(key)} does not look like a Gemini API key (expected AIza...)", file=sys.stderr)
    _write_env(path, {"GEMINI_API_KEY": key})
    print(f"Wrote GEMINI_API_KEY ({_mask(key)}) to {path}")
    if args.probe:
        adapter = GeminiTtsAdapter(api_key=key)
        expected = TtsPreflight(DEFAULT_TTS_MODEL, DEFAULT_VOICES, DEFAULT_SETTINGS_VERSION)
        try:
            result = adapter.preflight(expected)
            result.verify()
        except (TtsError, ValueError) as error:
            print(f"GEMINI_PROBE_FAILED: {error}", file=sys.stderr)
            return 1
        print(f"Gemini TTS probe ok: model={result.observed.model} voices={result.observed.voices} "
              f"probe={result.probe_duration_ms}ms audio_sha256={result.probe_sha256}")
    return 0


def _load_oauth_config(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    if args.client_secrets_file:
        import json

        config = json.loads(Path(args.client_secrets_file).read_text(encoding="utf-8"))
        kind = "installed" if "installed" in config else "web"
        section = config[kind]
        return {
            "client_id": section["client_id"],
            "client_secret": section["client_secret"],
            "auth_uri": section.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": section.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": section.get("redirect_uris", ["http://localhost"]),
        }, kind
    client_id = args.client_id or os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "YOUTUBE_CLIENT_MISSING: pass --client-id/--client-secret or --client-secrets-file "
            "(Google Cloud > APIs & Services > OAuth client ID, type Desktop app)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }, "installed"


def _run_oauth_flow(flow: Any, *, port: int) -> Any:
    """Complete the loopback OAuth dance, headless-host tolerant.

    If a browser is available the URL opens automatically; otherwise the flow
    runs with ``open_browser=False`` and the operator is told to SSH-tunnel the
    port from a machine that has a browser.
    """
    try:
        webbrowser.get()
        open_browser = True
    except webbrowser.Error:
        open_browser = False
    if open_browser:
        return flow.run_local_server(port=port, prompt="consent", open_browser=True)
    print(
        "No browser detected on this host. From a machine that has a browser,",
        "open a second terminal and tunnel the port, then visit the URL the",
        "flow prints below.",
        file=sys.stderr,
    )
    prompt = (
        "Please visit this URL to authorize this application.\n"
        "If this host has no browser, tunnel the port from your own machine:\n"
        f"  ssh -L {port}:localhost:{port} <user>@<this-host>\n"
        "then open:\n"
        "  {url}\n"
    )
    return flow.run_local_server(
        port=port,
        prompt="consent",
        open_browser=False,
        authorization_prompt_message=prompt,
    )


def _run_oauth_flow_manual(flow: Any, *, port: int) -> Any:
    """Copy-paste OAuth for hosts with no browser and no port forwarding.

    Prints the authorization URL; the operator opens it in any browser (e.g.
    a phone), approves, and pastes back the full redirect URL that the browser
    lands on (``http://localhost:{port}/?code=...``).  Works without a tunnel:
    the redirect target is loopback, but the code is in the pasted URL, not in
    anything that must be reachable.
    """
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(prompt="consent")
    print("Open this URL in any browser (e.g. your phone) and approve:")
    print()
    print(f"  {auth_url}")
    print()
    print(
        "After approving, the browser will try to open a page that cannot load "
        f"(http://localhost:{port}/). Copy the FULL address-bar URL (it starts "
        f"with http://localhost:{port}/?code=...) and paste it below."
    )
    response = input("Paste the full redirect URL: ").strip()
    if not response:
        raise SystemExit("OAUTH_RESPONSE_MISSING")
    # oauthlib rejects loopback http URIs ("OAuth 2 MUST utilize https");
    # google_auth_oauthlib applies the same normalization internally in its
    # local-server flow, so mirror it here before the token exchange.
    response = response.replace("http://", "https://", 1)
    flow.fetch_token(authorization_response=response)
    # fetch_token returns the raw token mapping; the Credentials object (with
    # .refresh_token) is exposed via the flow, same as the local-server flow.
    return flow.credentials


def _cmd_youtube(args: argparse.Namespace) -> int:
    path = _env_path(args.env)
    client_config, _kind = _load_oauth_config(args)
    scopes = args.scope.split(",") if args.scope else YOUTUBE_SCOPES
    print(f"OAuth client: {_mask(client_config['client_id'])} scopes={scopes}")
    print("Before continuing, confirm in Google Cloud Console:")
    print("  1. YouTube Data API v3 is enabled for the project.")
    print("  2. The OAuth consent screen lists your Google account as a test user.")
    print("  3. The client is type 'Desktop app' (loopback redirect is automatic).")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config({"installed": client_config}, scopes=scopes)
    if args.flow == "manual":
        credentials = _run_oauth_flow_manual(flow, port=args.port)
    else:
        credentials = _run_oauth_flow(flow, port=args.port)
    refresh_token = credentials.refresh_token
    if not refresh_token:
        print(
            "YOUTUBE_REFRESH_TOKEN_MISSING: Google did not issue a refresh token. "
            "Re-run with --flow local (prompt=consent forces a fresh token); "
            "if you already authorized this client 100 times, create a new OAuth client.",
            file=sys.stderr,
        )
        return 2
    _write_env(
        path,
        {
            "YOUTUBE_CLIENT_ID": client_config["client_id"],
            "YOUTUBE_CLIENT_SECRET": client_config["client_secret"],
            "YOUTUBE_REFRESH_TOKEN": refresh_token,
        },
    )
    print(f"Wrote YouTube OAuth credentials (refresh token {_mask(refresh_token)}) to {path}")
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        channels = youtube.channels().list(part="snippet", mine=True).execute()
    except Exception as error:
        print(f"YOUTUBE_VERIFY_FAILED: {error}", file=sys.stderr)
        return 1
    items = channels.get("items") or []
    if not items:
        print("YOUTUBE_CHANNEL_UNREACHABLE: the authorized account owns no YouTube channel", file=sys.stderr)
        return 1
    channel = items[0]
    print(f"YouTube login ok: channel={channel['id']} title={(channel.get('snippet') or {}).get('title')}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    gemini = sub.add_parser("gemini", help="Store and optionally probe the Gemini TTS API key")
    gemini.add_argument("--key", default=None)
    gemini.add_argument("--probe", action="store_true", help="Run one tiny real TTS synthesis to validate the key")
    gemini.add_argument("--env", type=Path, default=None)
    youtube = sub.add_parser("youtube", help="OAuth login to YouTube and store a refresh token")
    youtube.add_argument("--client-id", default=None)
    youtube.add_argument("--client-secret", default=None)
    youtube.add_argument("--client-secrets-file", type=Path, default=None)
    youtube.add_argument("--flow", choices=("local", "manual"), default="local", help="local: loopback server (needs browser or SSH tunnel); manual: copy-paste the redirect URL (works from a phone)")
    youtube.add_argument("--port", type=int, default=8080, help="Loopback port for the OAuth callback; tunnel it with SSH on headless hosts")
    youtube.add_argument("--scope", default=None, help="Comma-separated extra OAuth scopes")
    youtube.add_argument("--env", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command is None:
        print(__doc__)
        print("Missing command. Run one of:")
        print("  python scripts/setup_credentials.py gemini --key <AIza...> --probe")
        print("  python scripts/setup_credentials.py youtube --client-id <id> --client-secret <secret>")
        return 2
    if args.command == "gemini":
        return _cmd_gemini(args)
    return _cmd_youtube(args)


if __name__ == "__main__":
    raise SystemExit(main())
