from __future__ import annotations

import os
import webbrowser
from unittest import mock

from scripts.setup_credentials import _mask, _run_oauth_flow, _run_oauth_flow_manual, _write_env


class _FakeFlow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_local_server(self, **kwargs: object) -> object:
        self.calls.append(kwargs)

        class _Creds:
            refresh_token = "tok"

        return _Creds()


def test_env_writer_updates_keys_preserves_others_and_chmods(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=existing\nGEMINI_API_KEY=old\n", encoding="utf-8")
    _write_env(env, {"GEMINI_API_KEY": "new", "YOUTUBE_REFRESH_TOKEN": "secret"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "GEMINI_API_KEY=new" in lines
    assert "YOUTUBE_REFRESH_TOKEN=secret" in lines
    assert "GROQ_API_KEY=existing" in lines
    assert env.stat().st_mode & 0o777 == 0o600
    # Re-running does not duplicate keys.
    _write_env(env, {"GEMINI_API_KEY": "newer"})
    assert env.read_text(encoding="utf-8").splitlines().count("GEMINI_API_KEY=newer") == 1


def test_orca_writes_key_to_target_env_without_exposing_it(tmp_path, capsys):
    from scripts.setup_credentials import _cmd_orca

    class _Args:
        key = "sk-orca-test1234567890abcdef"
        probe = False
        env = tmp_path / "hermes.env"

    assert _cmd_orca(_Args()) == 0
    lines = (tmp_path / "hermes.env").read_text(encoding="utf-8").splitlines()
    assert "ORCAROUTER_API_KEY=sk-orca-test1234567890abcdef" in lines
    out = capsys.readouterr().out
    assert "sk-orca-test1234567890abcdef" not in out
    assert "sk-o...cdef" in out


def test_orca_rejects_missing_key():
    from scripts.setup_credentials import _cmd_orca

    class _Args:
        key = None
        probe = False
        env = None

    with mock.patch("builtins.input", return_value="  "):
        assert _cmd_orca(_Args()) == 2


def test_mask_never_exposes_full_secret():
    assert _mask("AIza0123456789") == "AIza...6789"
    assert _mask("short") == "*****"
    assert "0123456789" not in _mask("AIza0123456789")


def test_oauth_flow_headless_uses_tunnel_prompt_without_browser():
    flow = _FakeFlow()
    with mock.patch.object(webbrowser, "get", side_effect=webbrowser.Error("no browser")):
        creds = _run_oauth_flow(flow, port=8080)
    assert creds.refresh_token == "tok"
    (call,) = flow.calls
    assert call["port"] == 8080
    assert call["open_browser"] is False
    prompt = call["authorization_prompt_message"]
    assert "ssh -L 8080:localhost:8080" in prompt
    assert "{url}" in prompt


def test_oauth_flow_with_browser_auto_opens():
    flow = _FakeFlow()
    with mock.patch.object(webbrowser, "get", return_value=object()):
        creds = _run_oauth_flow(flow, port=9090)
    assert creds.refresh_token == "tok"
    (call,) = flow.calls
    assert call["port"] == 9090
    assert call["open_browser"] is True


def test_oauth_flow_manual_pastes_redirect_url():
    class _ManualFlow:
        def __init__(self) -> None:
            self.redirect_uri = None
            self.pasted = None

        def authorization_url(self, **kwargs: object) -> tuple[str, str]:
            self.kwargs = kwargs
            assert self.redirect_uri == "http://localhost:8080/"
            return "https://accounts.google.com/o/oauth2/auth?state=x", "x"

        def fetch_token(self, **kwargs: object) -> object:
            self.pasted = kwargs["authorization_response"]

        @property
        def credentials(self) -> object:
            class _Creds:
                refresh_token = "tok"

            return _Creds()

    flow = _ManualFlow()
    with mock.patch("builtins.input", return_value="http://localhost:8080/?code=abc&state=x"):
        creds = _run_oauth_flow_manual(flow, port=8080)
    assert creds.refresh_token == "tok"
    # oauthlib requires https; the loopback http scheme is normalized before exchange.
    assert flow.pasted == "https://localhost:8080/?code=abc&state=x"


def test_oauth_flow_manual_keeps_https_url_untouched():
    class _ManualFlow:
        redirect_uri = None

        def authorization_url(self, **kwargs: object) -> tuple[str, str]:
            return "https://accounts.google.com/o/oauth2/auth?state=x", "x"

        def fetch_token(self, **kwargs: object) -> object:
            self.pasted = kwargs["authorization_response"]

        @property
        def credentials(self) -> object:
            class _Creds:
                refresh_token = "tok"

            return _Creds()

    flow = _ManualFlow()
    with mock.patch("builtins.input", return_value="https://localhost:8080/?code=abc&state=x"):
        creds = _run_oauth_flow_manual(flow, port=8080)
    assert creds.refresh_token == "tok"
    assert flow.pasted == "https://localhost:8080/?code=abc&state=x"
