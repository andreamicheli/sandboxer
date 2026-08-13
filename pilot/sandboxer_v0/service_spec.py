"""Restricted, versioned service configuration for experimental Arena Matches.

The spec is intentionally declarative: a Competitor can choose an access
policy and endpoints, but cannot supply code, shell fragments, hosts, or
arbitrary process configuration to a Runner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from .blue_briefs import BlueBrief


SERVICE_SPEC_VERSION = "sandboxer.service-spec.v1"
_PATH = re.compile(r"/cgi-bin/service\.cgi\?route=[a-z0-9-]{1,48}\Z")
_HEADER = re.compile(r"X-[A-Za-z0-9-]{1,48}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
_PUBLIC_NOTE = re.compile(r"[a-z0-9_-]{1,64}\Z")
_POLICIES = frozenset({"deny", "header", "public"})
_RECOVERY_POSTURES = frozenset({"isolated", "header", "public"})
BRIEF_FAMILIES = frozenset({"portable_notes", "shared_notes", "service_diagnostics"})


class ServiceSpecError(ValueError):
    """A stable, non-sensitive reason why a supplied specification is invalid."""


@dataclass(frozen=True)
class ServiceSpec:
    schema_version: str
    health_path: str
    public_path: str
    protected_path: str
    protected_policy: str
    recovery_path: str
    recovery_posture: str
    access_header: str | None = None
    access_token: str | None = None
    brief_family: str | None = None
    public_note: str | None = None

    def normalized(self) -> dict[str, str | None]:
        return {
            "schema_version": self.schema_version,
            "health_path": self.health_path,
            "public_path": self.public_path,
            "protected_path": self.protected_path,
            "protected_policy": self.protected_policy,
            "access_header": self.access_header,
            "access_token": self.access_token,
            "recovery_path": self.recovery_path,
            "recovery_posture": self.recovery_posture,
            "brief_family": self.brief_family,
            "public_note": self.public_note,
        }

    @property
    def graph_hash(self) -> str:
        encoded = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def render_runtime_config(self) -> str:
        """Render a fixed-key shell config after validation, never raw model text."""
        values = self.normalized()
        return "".join(f"{key}={value or ''}\n" for key, value in values.items())

    def red_target_contract(self) -> dict[str, str | None]:
        """Return the declared attack surface without a protected credential.

        The contract represents endpoint discovery, not a secret disclosure: a
        Red Competitor may learn route names, policy shape, recovery posture,
        and the header name when a header policy is selected, but never the access token.
        """
        return {
            "health_path": self.health_path,
            "public_path": self.public_path,
            "protected_path": self.protected_path,
            "protected_policy": self.protected_policy,
            "access_header": self.access_header,
            "recovery_path": self.recovery_path,
            "recovery_posture": self.recovery_posture,
        }

    def calibration_metadata(self) -> dict[str, str]:
        """Return the minimal non-secret evidence needed to interpret a Match."""
        metadata = {
            "graph_hash": self.graph_hash,
            "protected_policy": self.protected_policy,
            "recovery_posture": self.recovery_posture,
        }
        if self.brief_family is not None:
            metadata["brief_family"] = self.brief_family
        return metadata


def parse_service_spec(
    raw: str,
    *,
    brief: BlueBrief | None = None,
    brief_family: str | None = None,
    public_note: str | None = None,
) -> ServiceSpec:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ServiceSpecError("SERVICE_SPEC_JSON_INVALID") from error
    if not isinstance(value, Mapping):
        raise ServiceSpecError("SERVICE_SPEC_JSON_INVALID")
    expected = {
        "schema_version", "health_path", "public_path", "protected_path", "protected_policy",
        "access_header", "access_token", "recovery_path", "recovery_posture",
    }
    if set(value) != expected or not all(isinstance(item, str) or item is None for item in value.values()):
        raise ServiceSpecError("SERVICE_SPEC_FIELDS_INVALID")

    if brief is not None:
        if brief_family is not None or public_note is not None:
            raise ServiceSpecError("SERVICE_SPEC_BRIEF_INVALID")
        brief_family = brief.family
        public_note = brief.parameters.get("public_note")

    if brief_family is not None:
        if not isinstance(brief_family, str) or brief_family not in BRIEF_FAMILIES:
            raise ServiceSpecError("SERVICE_SPEC_BRIEF_FAMILY_INVALID")
        if public_note is not None and (not isinstance(public_note, str) or not _PUBLIC_NOTE.fullmatch(public_note)):
            raise ServiceSpecError("SERVICE_SPEC_PUBLIC_NOTE_INVALID")
    elif public_note is not None:
        raise ServiceSpecError("SERVICE_SPEC_BRIEF_INVALID")

    spec = ServiceSpec(**value, brief_family=brief_family, public_note=public_note)
    if spec.schema_version != SERVICE_SPEC_VERSION:
        raise ServiceSpecError("SERVICE_SPEC_VERSION_UNSUPPORTED")
    paths = (spec.health_path, spec.public_path, spec.protected_path, spec.recovery_path)
    if len(set(paths)) != len(paths) or not all(isinstance(p, str) and _PATH.fullmatch(p) for p in paths):
        raise ServiceSpecError("SERVICE_SPEC_PATH_INVALID")
    if spec.protected_policy not in _POLICIES:
        raise ServiceSpecError("SERVICE_SPEC_POLICY_INVALID")
    if spec.recovery_posture not in _RECOVERY_POSTURES:
        raise ServiceSpecError("SERVICE_SPEC_POSTURE_INVALID")
    if spec.brief_family is not None and spec.recovery_posture == "isolated":
        raise ServiceSpecError("SERVICE_SPEC_POSTURE_INVALID")
    if spec.protected_policy == "header":
        if not isinstance(spec.access_header, str) or not _HEADER.fullmatch(spec.access_header):
            raise ServiceSpecError("SERVICE_SPEC_HEADER_INVALID")
        if not isinstance(spec.access_token, str) or not _TOKEN.fullmatch(spec.access_token):
            raise ServiceSpecError("SERVICE_SPEC_TOKEN_INVALID")
    elif spec.access_header is not None or spec.access_token is not None:
        raise ServiceSpecError("SERVICE_SPEC_POLICY_FIELDS_INVALID")
    if spec.recovery_posture == "header" and spec.protected_policy != "header":
        raise ServiceSpecError("SERVICE_SPEC_POLICY_FIELDS_INVALID")
    return spec
