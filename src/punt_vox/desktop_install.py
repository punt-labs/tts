"""Register the vox MCP server with Claude Desktop -- without leaking secrets.

Threat (PL-PP-4): the earlier ``install-desktop`` path resolved the
ElevenLabs/OpenAI API key and wrote it in plaintext into
``claude_desktop_config.json`` as ``"env": {"ELEVENLABS_API_KEY": "sk_..."}``.
That file is world-readable in practice, cloud-synced, and lands in
backups -- a long-lived secret sprayed into an unprotected config. Anyone
with read access to the user's home, sync, or backup gets the key.

The MCP server (``vox mcp``) is a thin WebSocket client of ``voxd``. It
never synthesizes audio and never needs a provider key. ``voxd`` -- the
daemon that actually calls the provider -- reads its key from ``keys.env``
at startup (see :meth:`punt_vox.voxd.config.DaemonConfig.load_keys`).
``vox daemon install`` writes ``keys.env`` (mode 0600, in the 0700 state
dir) from the installing user's environment.

Therefore the Claude Desktop config must carry only non-secret routing
config -- the provider name and the output directory. The API key never
appears there. This module builds that secret-free entry and, when the
selected provider needs a key the daemon cannot yet reach, emits guidance
that names the missing variable and where to set it -- never the value.

Design (§3.8): the provider-name choice runs against
:class:`ProviderCredentials`' own dispatch rather than a second local
probe, and the ``keys.env`` readiness check uses the same key-variable
map the daemon's gate reads. Two code paths asking the same question
were the shape of the state-authority defect this bead closes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from punt_vox.keys import parse_keys_env
from punt_vox.paths import keys_env_file
from punt_vox.providers.credentials import ProviderCredentials

__all__ = ["DesktopInstaller"]


# provider -> env-var the daemon reads for authentication. Derived from
# :class:`ProviderCredentials` so this module and the readiness gate cannot
# list different variables for the same provider (§3.4). Providers absent
# from the map (say, espeak, polly) need no bearer key: say/espeak
# authenticate against a binary on ``PATH``, polly against the AWS chain,
# neither of which lives in ``keys.env`` under a single name.
_PROVIDER_KEY_VARS: dict[str, str] = ProviderCredentials().api_key_env_vars()


@final
class DesktopInstaller:
    """Build the secret-free Claude Desktop entry for one provider.

    Owns the provider name and output directory; produces the ``env`` map
    for the MCP server registration and reports whether the daemon can
    obtain the provider credential without ever embedding or echoing it.
    """

    __slots__ = ("_audio_dir", "_provider")

    _provider: str
    _audio_dir: Path

    def __new__(cls, provider: str, audio_dir: Path) -> Self:
        self = super().__new__(cls)
        self._provider = provider
        self._audio_dir = audio_dir
        return self

    @classmethod
    def detect(cls, provider_name: str | None, audio_dir: Path) -> Self:
        """Build from an explicit provider or the credential dispatch's proposal.

        A ``None`` provider means "pick a provider whose credentials the
        installer's environment can already satisfy" -- the same question
        :meth:`ProviderCredentials.preferred` answers for the enable path.
        A given name is lowercased so ``ElevenLabs`` and ``elevenlabs``
        resolve identically. Raises :class:`ValueError` when no provider
        is ready: a fresh install with no credentials in view of the
        installer has nothing sensible to write as the routing default.
        """
        if provider_name is not None:
            return cls(provider_name.lower(), audio_dir)
        provider = ProviderCredentials().preferred()
        if provider is None:
            msg = (
                "No TTS provider credentials found in the current environment. "
                "Export ELEVENLABS_API_KEY / OPENAI_API_KEY / AWS credentials, "
                "or pass --provider explicitly."
            )
            raise ValueError(msg)
        return cls(provider, audio_dir)

    @property
    def provider(self) -> str:
        """Return the resolved provider name."""
        return self._provider

    def server_env(self) -> dict[str, str]:
        """Return the non-secret ``env`` for the MCP server registration.

        Only routing config -- provider name and output directory. A
        resolved API key is deliberately never included: it would land in
        plaintext in ``claude_desktop_config.json`` (PL-PP-4). The daemon
        reads the key from ``keys.env`` instead, so the server still
        authenticates without the secret ever touching this file.
        """
        return {
            "TTS_PROVIDER": self._provider,
            "VOX_OUTPUT_DIR": str(self._audio_dir),
        }

    def requires_credential(self) -> bool:
        """True if the provider authenticates with an API key."""
        return self._provider in _PROVIDER_KEY_VARS

    def daemon_can_authenticate(self) -> bool:
        """True if the daemon can reach the provider key (or needs none).

        Reachability is determined *solely* from ``keys.env``. The daemon
        (``voxd``) is a detached launchd/systemd service that never inherits
        the installer's interactive shell, so a key merely exported in this
        process is invisible to it -- only ``keys.env`` (written by
        ``vox daemon install``, read by ``DaemonConfig.load_keys``) counts.
        """
        if not self.requires_credential():
            return True
        return self._keys_env_has(_PROVIDER_KEY_VARS[self._provider])

    def credential_guidance(self) -> str:
        """Return operator guidance for a key the daemon cannot yet reach.

        Precondition: the provider is key-based (:meth:`requires_credential`).
        Names the missing variable and where the daemon reads it -- never the
        secret value.
        """
        if not self.requires_credential():
            msg = (
                "credential_guidance requires a key-based provider, "
                f"got {self._provider!r}"
            )
            raise ValueError(msg)
        key_var = _PROVIDER_KEY_VARS[self._provider]
        return (
            f"Warning: {key_var} is not available to the vox daemon. "
            "The Claude Desktop config stores no API key by design. voxd "
            f"reads its key from {keys_env_file()} at startup -- export "
            f"{key_var} and run 'vox daemon install' to store it there, "
            f"or choose --provider say/espeak/polly."
        )

    @staticmethod
    def _keys_env_has(key_var: str) -> bool:
        """True if ``keys.env`` holds a non-empty value for *key_var*.

        Reading the daemon's credential file is a system boundary: a
        missing, unreadable, or non-UTF-8 file means "the daemon has no
        usable key", not a crash.
        """
        path = keys_env_file()
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return bool(parse_keys_env(text).get(key_var))
