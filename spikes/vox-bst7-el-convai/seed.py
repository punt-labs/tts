"""Deterministic generator of realistic session-context seed text."""

from __future__ import annotations

from itertools import islice
from typing import Self, final

_COMMITS: tuple[str, ...] = (
    "feat(voxd): route recordings through the daemon store",
    "fix(hooks): classify permission_prompt before idle_prompt",
    "refactor(core): extract split_text sentence chunker",
    "feat(music): re-pool program on vibe change with 12 variations",
    "fix(providers): raise ProviderAuthError on 401 instead of laundering",
    "docs: record DES-068..071 E+ voice-agent architecture",
    "test(cache): cover content-addressed dedup by (text, voice, provider)",
)

_FILES: tuple[str, ...] = (
    "src/punt_vox/voxd/daemon.py — WebSocket dispatch, playback queue",
    "src/punt_vox/providers/elevenlabs.py — TTS provider, voice resolver",
    "src/punt_vox/hooks.py — per-event Claude Code hook dispatch",
    "src/punt_vox/config.py — durable vs ephemeral key routing",
    "src/punt_vox/cache.py — MP3 quip cache, MD5 content addressing",
)

_DECISIONS: tuple[str, ...] = (
    "The daemon is the audio host; clients are thin controllers.",
    "No migration or compat shims — delete superseded paths outright.",
    "Client tools ride the Conv AI WebSocket; no HTTP callback surface.",
    "The primary session authors the call seed; voxd holds rolling context.",
    "Barge-in authority belongs to the voice-native turn loop, not the CLI.",
)

_QUESTIONS: tuple[str, ...] = (
    "Does PostToolUse carry actionable state or only metadata?",
    "Where does the rolling context store truncate under WAN drops?",
    "Should launch_session default to a read-only permissions profile?",
    "Is the seed alone rich enough to make Layer 2 decorative?",
)

_TRANSCRIPT_TOPICS: tuple[str, ...] = (
    "the playback queue backpressure fix",
    "why the music pool caps at twelve variations",
    "splitting speech_handlers out of the daemon module",
    "the OO ratchet paydown in the provider registry",
    "wiring the lux control panel into session start",
)


@final
class SeedGenerator:
    """Produce session-context text of a target byte size, deterministically.

    The text imitates what a real ``/vox:talk`` seed would carry: recent
    commits, working files, standing decisions, open questions, and
    transcript excerpts. Pools rotate round-robin — no randomness — so
    latency comparisons across seed sizes measure size, not content drift.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def generate(self, target_bytes: int) -> str:
        """Return seed text whose UTF-8 size is close to ``target_bytes``."""
        if target_bytes <= 0:
            msg = f"target_bytes must be positive, got {target_bytes}"
            raise ValueError(msg)
        blocks: list[str] = [self._header()]
        size = len(blocks[0].encode())
        n = 0
        while size < target_bytes:
            block = self._block(n)
            blocks.append(block)
            size += len(block.encode()) + 2
            n += 1
        return self._trim("\n\n".join(blocks), target_bytes)

    def _header(self) -> str:
        return (
            "SESSION CONTEXT SNAPSHOT (seed)\n"
            "Repo: punt-labs/vox — text-to-speech CLI, MCP server, plugin.\n"
            "You are the voice agent for this coding session."
        )

    def _block(self, n: int) -> str:
        kind = n % 5
        if kind == 0:
            return "Recent commits:\n" + self._bullets(_COMMITS, n, 4)
        if kind == 1:
            return "Working files:\n" + self._bullets(_FILES, n, 3)
        if kind == 2:
            return "Standing decisions:\n" + self._bullets(_DECISIONS, n, 3)
        if kind == 3:
            return "Open questions:\n" + self._bullets(_QUESTIONS, n, 2)
        topic = _TRANSCRIPT_TOPICS[n % len(_TRANSCRIPT_TOPICS)]
        return (
            f"Transcript excerpt #{n}:\n"
            f"operator: walk me through {topic}.\n"
            f"assistant: the change isolates {topic} behind its own module "
            "boundary so the daemon keeps one code path per capability; "
            "tests cover the happy, invalid, and boundary cases."
        )

    @staticmethod
    def _bullets(pool: tuple[str, ...], offset: int, k: int) -> str:
        rotated = (pool[(offset + i) % len(pool)] for i in range(len(pool)))
        return "\n".join(f"- {item}" for item in islice(rotated, k))

    @staticmethod
    def _trim(text: str, target_bytes: int) -> str:
        raw = text.encode()
        if len(raw) <= target_bytes:
            return text
        return raw[:target_bytes].decode(errors="ignore")
