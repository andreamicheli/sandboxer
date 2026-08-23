"""Two-phase manual OAuth: phase 1 prints an auth URL and saves the flow state;
phase 2 takes the pasted redirect URL as argv[1] and completes the exchange.

State file holds the PKCE code_verifier so the two phases can be separate
processes. Writes YOUTUBE_* into the pilot .env on success.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.youtube import YoutubeCredentials  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

STATE = Path("/tmp/yt-oauth-state.json")
PORT = 8080
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def client_config() -> dict:
    creds = YoutubeCredentials.from_env()
    return {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def main() -> int:
    mode = sys.argv[1]
    cfg = client_config()
    if mode == "start":
        flow = InstalledAppFlow.from_client_config({"installed": cfg}, scopes=SCOPES)
        flow.redirect_uri = f"http://localhost:{PORT}/"
        url, _state = flow.authorization_url(prompt="consent")
        STATE.write_text(json.dumps({
            "code_verifier": flow.code_verifier,
        }))
        print(url)
        return 0
    if mode == "finish":
        r = sys.argv[2]
        response = r if r.startswith("http") else "http://" + r
        response = response.replace("http://", "https://", 1)
        saved = json.loads(STATE.read_text())
        flow = InstalledAppFlow.from_client_config({"installed": cfg}, scopes=SCOPES)
        flow.redirect_uri = f"http://localhost:{PORT}/"
        flow.code_verifier = saved["code_verifier"]
        flow.fetch_token(authorization_response=response)
        rt = flow.credentials.refresh_token
        if not rt:
            print("YOUTUBE_REFRESH_TOKEN_MISSING", file=sys.stderr)
            return 2
        env_path = Path(__file__).resolve().parent.parent / ".env"
        lines = env_path.read_text().splitlines()
        out, seen = [], set()
        for line in lines:
            key = line.split("=", 1)[0] if "=" in line else None
            if key in {"YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"}:
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"{key}={cfg['client_id'] if key == 'YOUTUBE_CLIENT_ID' else cfg['client_secret'] if key == 'YOUTUBE_CLIENT_SECRET' else rt}")
            else:
                out.append(line)
        for key, val in (("YOUTUBE_CLIENT_ID", cfg["client_id"]),
                         ("YOUTUBE_CLIENT_SECRET", cfg["client_secret"]),
                         ("YOUTUBE_REFRESH_TOKEN", rt)):
            if key not in seen:
                out.append(f"{key}={val}")
        env_path.write_text("\n".join(out) + "\n")
        print(f"OK refresh token written to {env_path}")
        # verify channel access immediately
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=flow.credentials, cache_discovery=False)
        ch = yt.channels().list(part="snippet", mine=True).execute().get("items") or []
        if ch:
            print("channel:", ch[0]["id"], (ch[0].get("snippet") or {}).get("title"))
        return 0
    print(f"unknown mode {mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
