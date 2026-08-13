from __future__ import annotations

from scripts.rehearse_command_code_pair import _reason_codes
from sandboxer_v0.command_code import CommandCodeError


def test_pair_rehearsal_flattens_only_stable_provider_reason_codes():
    error = ExceptionGroup("private provider detail", [
        CommandCodeError("COMMAND_CODE_PROVIDER_FAILURE"),
        RuntimeError("secret path /private/key"),
    ])
    assert _reason_codes(error) == ("COMMAND_CODE_PROVIDER_FAILURE", "RuntimeError")
