"""Per-provider "is this credential present here" checks.

The three concrete :class:`~punt_vox.providers.credentials.CredentialRequirement`
implementations live here so the interface and its facade (in
:mod:`~punt_vox.providers.credentials`) stay under the PY-OO-2 module-class
limit. Each requirement answers cheaply, never raises, and returns a
prefix-free message when unmet -- the message crosses the wire, and an
absolute path in the message would defeat what :class:`SafeFault` exists
to prevent.
"""

from __future__ import annotations

import os
import shutil
from typing import Self, final

__all__ = [
    "ApiKeyRequirement",
    "AwsRequirement",
    "BinaryRequirement",
]


@final
class ApiKeyRequirement:
    """A single API-key env var must be present and non-empty.

    Used by ElevenLabs (``ELEVENLABS_API_KEY``) and OpenAI
    (``OPENAI_API_KEY``). The check is cheap and answers the same
    question the provider SDK will ask at construction: is there a key
    for it to use.
    """

    __slots__ = ("_env_var",)
    _env_var: str

    def __new__(cls, env_var: str) -> Self:
        self = super().__new__(cls)
        self._env_var = env_var
        return self

    @property
    def env_var(self) -> str:
        """Return the env var name this requirement reads."""
        return self._env_var

    def satisfied(self) -> bool:
        """Return True when *env_var* is set and non-empty."""
        return bool(os.environ.get(self._env_var, ""))

    def unmet_message(self, provider: str) -> str:
        """Return the F2 message for a missing API key.

        Points at ``vox doctor`` (which runs host-local and may print
        the ``keys.env`` path) rather than embedding the path itself,
        so no absolute prefix crosses to a client.
        """
        return (
            f"provider {provider!r} is configured but voxd has no "
            f"{self._env_var}; run `vox doctor`"
        )


@final
class AwsRequirement:
    """Polly needs some AWS credential chain botocore can resolve.

    Uses ``boto3.Session().get_credentials() is not None``, which
    consults the same chain botocore will use at synthesis --
    environment, profile, config file, instance role -- with no
    network call. This is the deliberate replacement for
    ``aws sts get-caller-identity``: that subprocess costs up to five
    seconds, needs the ``aws`` CLI on ``PATH``, and answers a
    different question (are these credentials *valid* right now) than
    the gate asks (are there credentials for boto3 to use). Presence
    is a cheap local fact; validity is a network fact -- the live
    probe belongs to ``vox doctor``.

    The check is defensive against an environment without boto3
    installed: an ``ImportError`` here is "no credentials to boto3",
    not a crash, because the same install shape means boto3 could not
    have been constructed at synthesis either.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def satisfied(self) -> bool:
        """Return True when boto3 can resolve some credential chain.

        The boto3 import is inline because it is an optional heavy
        dep at the daemon boundary -- a base-only install (no boto3)
        answers "no credentials to boto3" without an ImportError at
        module scope, matching the pattern the rest of the codebase
        uses for provider-SDK imports.
        """
        try:
            import boto3  # noqa: PLC0415 -- optional heavy dep, see docstring
        except ImportError:
            return False
        try:
            credentials = boto3.Session().get_credentials()
        except Exception:  # noqa: BLE001 -- botocore raises many concrete types
            # ProfileNotFound, ClientError, and a family of botocore
            # exceptions all mean "no usable chain". A broad guard here is
            # the boundary between the readiness check (returns bool) and
            # the boto3 SDK (raises), and keeps the check from becoming
            # its own fault path.
            return False
        return credentials is not None

    def unmet_message(self, provider: str) -> str:
        """Return the F2 message for an unresolvable AWS chain."""
        return (
            f"provider {provider!r} is configured but voxd has no AWS "
            "credentials (AWS_PROFILE, or AWS_ACCESS_KEY_ID + "
            "AWS_SECRET_ACCESS_KEY); run `vox doctor`"
        )


@final
class BinaryRequirement:
    """A native speech binary must be reachable on ``PATH``.

    Used by ``say`` (macOS, one binary) and ``espeak`` (Linux, two
    accepted binaries: ``espeak-ng`` and ``espeak``). Presence on
    ``PATH`` mirrors what the provider will try at synthesis --
    :func:`shutil.which` is the same lookup :mod:`subprocess` runs
    with a bare name.
    """

    __slots__ = ("_binaries",)
    _binaries: tuple[str, ...]

    def __new__(cls, *binaries: str) -> Self:
        if not binaries:
            msg = "BinaryRequirement needs at least one binary name"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._binaries = tuple(binaries)
        return self

    def satisfied(self) -> bool:
        """Return True when any candidate binary is on ``PATH``."""
        return any(shutil.which(name) is not None for name in self._binaries)

    def unmet_message(self, provider: str) -> str:
        """Return the F2 message for an absent binary.

        Single-binary providers get ``"X is not on voxd's PATH"``;
        alternative-binary providers (espeak accepts either
        ``espeak-ng`` or ``espeak``) get ``"neither X nor Y is on
        voxd's PATH"`` -- the design's F2 wording, which reads as
        English rather than a mechanical join.
        """
        if len(self._binaries) == 1:
            phrase = f"{self._binaries[0]} is not on voxd's PATH"
        else:
            phrase = f"neither {' nor '.join(self._binaries)} is on voxd's PATH"
        return f"provider {provider!r} is configured but {phrase}; run `vox doctor`"
