from __future__ import annotations

import pytest

from sandboxer_v0.reasoning import (
    GeminiReasoningSanitizer,
    SanitizedThought,
    validate_sanitized_thought,
)


def _secret() -> str:
    return "AIza" + "a" * 20  # a realistic-length AI Studio key shape


def test_validate_sanitized_thought_accepts_clean_line():
    assert validate_sanitized_thought("It examined the login and moved on.") == ()


def test_validate_sanitized_thought_flags_leaks_and_bounds():
    assert "LEAK_REMAINS" in validate_sanitized_thought(f"key {_secret()}")
    assert "LEAK_REMAINS" in validate_sanitized_thought("it read /home/ubuntu/.env")
    assert "EMPTY" in validate_sanitized_thought("   ")
    assert "TOO_LONG" in validate_sanitized_thought("word " * 200)


def test_sanitizer_without_model_redacts_and_stays_unapproved():
    sanitizer = GeminiReasoningSanitizer()  # no client, no key
    secret = _secret()
    thought = sanitizer.sanitize(f"login used the api key {secret} and flag{{seekrit}}")
    assert isinstance(thought, SanitizedThought)
    assert thought.approved is False
    assert thought.redacted_any is True
    # The secret-shaped value and the flag are gone from the output.
    assert secret not in thought.text and "seekrit" not in thought.text


def test_sanitizer_without_model_handles_empty_reasoning():
    sanitizer = GeminiReasoningSanitizer()
    thought = sanitizer.sanitize("   ")
    assert thought.text == "[thinking redacted]"


class _Response:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, text: str | None) -> None:
        self._text = text
        self.last_prompt: str | None = None

    def generate_content(self, *, model: str, contents: str):
        self.last_prompt = contents
        return _Response(self._text)


class _FakeClient:
    def __init__(self, text: str | None) -> None:
        self.models = _FakeModels(text)


def test_sanitizer_rewrites_with_model_and_stays_unapproved():
    client = _FakeClient("It studied the login page carefully.")
    sanitizer = GeminiReasoningSanitizer(client=client, model="gemini-2.5-flash")
    thought = sanitizer.sanitize("It enumerated the login route for bypasses.")
    assert thought.method == "model"
    assert thought.text == "It studied the login page carefully."
    assert thought.approved is False


def test_sanitizer_falls_back_when_model_output_leaks():
    client = _FakeClient("the api key was hardcoded in /home/ubuntu/.env")
    sanitizer = GeminiReasoningSanitizer(client=client)
    thought = sanitizer.sanitize("It found a way in.")
    # A failing model rewrite never reaches the public path.
    assert thought.method == "deterministic"
    assert validate_sanitized_thought(thought.text) == ()


def test_secret_never_reaches_the_model():
    client = _FakeClient("It looked around.")
    sanitizer = GeminiReasoningSanitizer(client=client)
    secret = _secret()
    sanitizer.sanitize(f"the api key is {secret} and it read /home/ubuntu/.env")
    # The deterministic pre-pass runs before the model call, so the prompt the
    # model received contains neither the secret nor the internal path.
    prompt = client.models.last_prompt or ""
    assert secret not in prompt
    assert "/home/ubuntu/.env" not in prompt


def test_rewrite_empty_response_falls_back():
    sanitizer = GeminiReasoningSanitizer(client=_FakeClient(None))
    thought = sanitizer.sanitize("It probed the endpoint.")
    assert thought.method == "deterministic"
    assert validate_sanitized_thought(thought.text) == ()
