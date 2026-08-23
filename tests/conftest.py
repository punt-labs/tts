"""Shared test fixtures for punt-vox."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydub import AudioSegment

from punt_vox.core import TTSClient
from punt_vox.dirs import find_config_dir
from punt_vox.providers.elevenlabs import ElevenLabsProvider
from punt_vox.providers.espeak import EspeakProvider, EspeakVoiceConfig
from punt_vox.providers.openai import OpenAIProvider
from punt_vox.providers.polly import PollyProvider, VoiceConfig
from punt_vox.providers.say import SayProvider, SayVoiceConfig

# Test voice configs — constructed directly, no API call needed.
JOANNA = VoiceConfig(voice_id="Joanna", language_code="en-US", engine="neural")
HANS = VoiceConfig(voice_id="Hans", language_code="de-DE", engine="standard")
TATYANA = VoiceConfig(voice_id="Tatyana", language_code="ru-RU", engine="standard")
SEOYEON = VoiceConfig(voice_id="Seoyeon", language_code="ko-KR", engine="neural")


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Provide a temporary output directory."""
    out = tmp_path / "output"
    out.mkdir()
    return out


# ---------------------------------------------------------------------------
# Config hermeticity — belt-and-suspenders isolation from the real vox config
# ---------------------------------------------------------------------------
#
# ``DEFAULT_CONFIG_DIR`` is a *relative* path (``.punt-labs/vox``) and
# ``find_config_dir()`` walks up from the cwd.  Run from the repo root, both
# resolve to the developer's *real* ``.punt-labs/vox/``.  Any test that drives a
# config-writing path without redirecting the dir -- the ``vibe`` MCP tool, the
# ``/vibe`` CLI command -- would clobber the live config (vox-73m5: the session
# vibe "always reverts to sad" because test fixtures wrote into it).  The
# autouse fixture below makes every *ambient* config resolution land in a
# per-test tmp dir, so no test can read or write the real config through the
# default path.


@pytest.fixture(autouse=True)
def hermetic_config(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect every default vox-config resolution to a per-test tmp dir.

    Covers all three ways production code reaches the config with no explicit
    directory:

    * ``ConfigStore(None)`` and the module-level ``read_config`` /
      ``read_field`` / ``write_field`` / ``write_fields`` wrappers, which fall
      back to ``config.DEFAULT_CONFIG_DIR``;
    * ``server._find_config_dir()``, which re-imports ``find_config_dir`` from
      ``dirs`` at call time;
    * the ``/vibe`` / ``notify`` / ``voice`` CLI commands, which read
      ``__main__.find_config_dir``.

    ``hooks.find_config_dir`` is *not* redirected: hooks always pass an explicit
    ``cwd``, so they resolve relative to isolated payload directories and never
    touch the real config.  Tests that exercise config-resolution walking
    (``test_dirs``, ``test_config``) call ``find_config_dir`` through their own
    module-level import, a binding captured before this patch, so their explicit
    ``start=`` behaviour is preserved.

    The redirect dir is minted from ``tmp_path_factory`` rather than the test's
    own ``tmp_path`` so it never appears when a test enumerates ``tmp_path`` or
    rebuilds ``tmp_path/.punt-labs/vox`` itself.
    """
    config_dir = tmp_path_factory.mktemp("vox-config") / ".punt-labs" / "vox"
    config_dir.mkdir(parents=True)

    # Seed a real ``provider:`` value so surfaces that fill from state through
    # SessionSpec get a configured provider by default. Without this every CLI
    # ``vox say``, hook synthesis, panel preview, and mic:unmute test would hit
    # the F1 refusal introduced by vox-w3f8 and exit before the assertion it
    # was actually written for. Tests that mean to exercise F1 overwrite this
    # file explicitly; the seed keeps the rest of the suite unchanged.
    (config_dir / "vox.md").write_text("---\nprovider: elevenlabs\n---\n")

    def _resolve(_start: Path | None = None) -> Path:
        """Stand-in for ``find_config_dir`` that never escapes the tmp dir."""
        return config_dir

    monkeypatch.setattr("punt_vox.config.DEFAULT_CONFIG_DIR", config_dir)
    monkeypatch.setattr("punt_vox.dirs.DEFAULT_CONFIG_DIR", config_dir)
    monkeypatch.setattr("punt_vox.dirs.find_config_dir", _resolve)
    monkeypatch.setattr("punt_vox.__main__.find_config_dir", _resolve)
    # session_spec.py owns SessionSpec.for_repo(), the one config-lookup
    # path vox say/vox rec new/vox call all share -- ``vox say``'s
    # __main__._fill_from_state and vox rec new's cli_rec._fill_from_state
    # both route through it now, so neither module imports find_config_dir
    # directly any more; only session_spec's own "from X import Y" binding
    # (captured at import time, same reason __main__'s needs its own entry
    # above) needs patching.
    monkeypatch.setattr("punt_vox.session_spec.find_config_dir", _resolve)
    return config_dir


@pytest.fixture(autouse=True)
def hermetic_vibe_trace(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the durable vibe-trace log into a per-test tmp dir.

    ``VibeTraceLog.default()`` resolves ``log_dir() / "vibe-trace.log"`` to the
    real ``~/.punt-labs/vox/logs/vibe-trace.log``.  Every test that drives
    ``music`` / ``VibeCommand.apply`` / the nudge hook builds a *default* sink,
    so without this each such test would append into that live proof trail --
    contaminating the exact record the subsystem exists to make trustworthy
    (the recurring test-config-pollution burn).  Patch the sink's bound
    ``log_dir`` so every default resolution lands under a tmp ``logs`` dir; no
    test can touch the real file.  The tmp dir keeps the ``logs`` basename so
    the default-path assertions still see the production layout.
    """
    logs = tmp_path_factory.mktemp("vibe-trace-state") / "logs"
    logs.mkdir()
    monkeypatch.setattr("punt_vox.vibe_trace.log_dir", lambda: logs)
    return logs


@pytest.fixture(autouse=True)
def hermetic_client_log(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the unified ``vox.log`` into a per-test tmp ``logs`` dir.

    Every process appends its records to ``log_dir() / "vox.log"`` -- resolving to
    the real ``~/.punt-labs/vox/logs``. A CLI/hook test that configures logging and
    emits a record would otherwise write the developer's live log. Redirecting the
    import-time ``logging_config`` paths keeps every test off the real logs dir.
    """
    logs = tmp_path_factory.mktemp("client-log-state") / "logs"
    logs.mkdir()
    monkeypatch.setattr("punt_vox.logging_config._LOG_DIR", logs)
    monkeypatch.setattr("punt_vox.logging_config._LOG_FILE", logs / "vox.log")
    return logs


def _repo_free_base() -> Path:
    """Return a temp base directory with no ``.punt-labs/vox`` ancestor.

    On dev boxes ``TMPDIR`` points at the repo's ``.tmp/`` (see .envrc),
    which sits inside a real ``.punt-labs/vox`` config.  ``find_config_dir``
    walks up into that ambient config, so tests asserting "no config
    resolves" break.  Climb above any config found from the platform temp
    root so the search starts clean — reproducing CI, where ``TMPDIR`` is
    outside the repo.
    """
    base = Path(tempfile.gettempdir()).resolve()
    while (found := find_config_dir(base)) is not None:
        # found is ``<repo>/.punt-labs/vox``; step above <repo>.
        base = found.parent.parent.parent
    if not os.access(base, os.W_OK):
        # A global ``~/.punt-labs/vox`` (present on a real dev box) forces the
        # climb all the way up to an unwritable root like ``/Users``. Fall back
        # to the system temp root, which is writable and has no vox-config
        # ancestor -- so ``mkdtemp`` succeeds and the search still resolves none.
        base = Path("/tmp").resolve()  # config-free writable base
    return base


@pytest.fixture
def no_config_dir() -> Iterator[Path]:
    """Yield a temp dir guaranteed to have no ``.punt-labs/vox`` ancestor.

    ``tmp_path`` is unusable for config-resolution tests when ``TMPDIR``
    lives inside the repo, because the upward walk reaches the real
    config.  This fixture roots the tree above any such config and cleans
    it up afterward.
    """
    path = Path(tempfile.mkdtemp(dir=_repo_free_base()))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _generate_valid_mp3_bytes() -> bytes:
    """Generate minimal valid MP3 bytes using pydub."""
    silence = AudioSegment.silent(duration=50)
    buf = io.BytesIO()
    silence.export(buf, format="mp3")  # pyright: ignore[reportUnknownMemberType]
    return buf.getvalue()


# Cache to avoid regenerating on every call.
_VALID_MP3_BYTES: bytes | None = None


def _get_valid_mp3_bytes() -> bytes:
    global _VALID_MP3_BYTES
    if _VALID_MP3_BYTES is None:
        _VALID_MP3_BYTES = _generate_valid_mp3_bytes()  # pyright: ignore[reportConstantRedefinition]
    return _VALID_MP3_BYTES


def _make_polly_response() -> dict[str, Any]:
    """Create a mock Polly synthesize_speech response with valid MP3."""
    stream = MagicMock()
    stream.read.return_value = _get_valid_mp3_bytes()
    return {
        "AudioStream": stream,
        "ContentType": "audio/mpeg",
        "RequestCharacters": 10,
    }


@pytest.fixture
def mock_boto_client() -> MagicMock:
    """Create a mock boto3 Polly client that returns valid MP3 bytes."""
    client = MagicMock()
    client.synthesize_speech.side_effect = lambda **kwargs: _make_polly_response()  # pyright: ignore[reportUnknownLambdaType]
    return client


@pytest.fixture
def polly_provider(mock_boto_client: MagicMock) -> PollyProvider:
    """Create a PollyProvider with a mocked boto3 backend."""
    provider = PollyProvider(boto_client=mock_boto_client)
    # Pre-populate the VoiceResolver cache for tests.
    provider._voices._cache.update(  # pyright: ignore[reportPrivateUsage]
        {
            "joanna": JOANNA,
            "hans": HANS,
            "tatyana": TATYANA,
            "seoyeon": SEOYEON,
        }
    )
    provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
    return provider


@pytest.fixture
def tts_client(polly_provider: PollyProvider) -> TTSClient:
    """Create a TTSClient backed by a mock PollyProvider."""
    return TTSClient(polly_provider)


def _make_openai_speech_response() -> MagicMock:
    """Create a mock OpenAI audio.speech.create() response with valid MP3."""
    response = MagicMock()
    response.content = _get_valid_mp3_bytes()
    return response


@pytest.fixture
def mock_openai_client() -> MagicMock:
    """Create a mock openai.OpenAI client that returns valid MP3 bytes."""
    client = MagicMock()
    client.audio.speech.create.side_effect = (
        lambda **kwargs: _make_openai_speech_response()  # pyright: ignore[reportUnknownLambdaType]
    )
    return client


@pytest.fixture
def openai_provider(mock_openai_client: MagicMock) -> OpenAIProvider:
    """Create an OpenAIProvider with a mocked OpenAI client."""
    return OpenAIProvider(client=mock_openai_client)


def _make_elevenlabs_stream_response() -> list[bytes]:
    """Create a mock ElevenLabs text_to_speech.stream() byte iterator."""
    return [_get_valid_mp3_bytes()]


@pytest.fixture
def mock_elevenlabs_client() -> MagicMock:
    """Create a mock ElevenLabs client that returns valid MP3 bytes."""
    client = MagicMock()
    client.text_to_speech.stream.side_effect = (
        lambda **kwargs: _make_elevenlabs_stream_response()  # pyright: ignore[reportUnknownLambdaType]
    )

    # Mock voices.get_all for voice resolution.
    voice_matilda = MagicMock()
    voice_matilda.name = "Matilda - Knowledgable, Professional"
    voice_matilda.voice_id = "XrExE9yKIg1WjnnlVkGX"

    voice_drew = MagicMock()
    voice_drew.name = "Drew - eloquent, calm"
    voice_drew.voice_id = "29vD33N1CtxCmqQRPOHJ"

    voices_response = MagicMock()
    voices_response.voices = [voice_matilda, voice_drew]
    client.voices.get_all.return_value = voices_response

    # Mock subscription for health checks.
    subscription = MagicMock()
    subscription.tier = "free"
    subscription.character_count = 500
    subscription.character_limit = 10000
    client.user.subscription.get.return_value = subscription

    return client


@pytest.fixture
def elevenlabs_provider(mock_elevenlabs_client: MagicMock) -> ElevenLabsProvider:
    """Create an ElevenLabsProvider with a mocked client."""
    provider = ElevenLabsProvider(client=mock_elevenlabs_client)
    # Pre-populate the VoiceResolver cache for tests.
    provider._voices._cache.update(  # pyright: ignore[reportPrivateUsage]
        {
            "matilda": "XrExE9yKIg1WjnnlVkGX",
            "drew": "29vD33N1CtxCmqQRPOHJ",
        }
    )
    # Mark cache as loaded so TTL checks work.
    import time

    provider._voices._loaded_at = time.monotonic()  # pyright: ignore[reportPrivateUsage]
    return provider


# ---------------------------------------------------------------------------
# macOS Say provider fixtures
# ---------------------------------------------------------------------------

FRED = SayVoiceConfig(name="Fred", locale="en_US")
SAMANTHA = SayVoiceConfig(name="Samantha", locale="en_US")
ANNA_SAY = SayVoiceConfig(name="Anna", locale="de_DE")


@pytest.fixture
def say_provider() -> SayProvider:
    """Create a SayProvider with platform and say command mocked."""
    with (
        patch("punt_vox.providers.say.platform") as mock_platform,
        patch("punt_vox.providers.say.shutil") as mock_shutil,
    ):
        mock_platform.system.return_value = "Darwin"
        mock_shutil.which.return_value = "/usr/bin/say"
        provider = SayProvider()

    # Pre-populate the VoiceResolver cache for tests.
    provider._voices._cache.update(  # pyright: ignore[reportPrivateUsage]
        {
            "fred": FRED,
            "samantha": SAMANTHA,
            "anna": ANNA_SAY,
        }
    )
    provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
    return provider


# ---------------------------------------------------------------------------
# espeak-ng provider fixtures
# ---------------------------------------------------------------------------

ENGLISH_ESPEAK = EspeakVoiceConfig(name="en", language="en")
GERMAN_ESPEAK = EspeakVoiceConfig(name="de", language="de")
FRENCH_ESPEAK = EspeakVoiceConfig(name="fr", language="fr")


@pytest.fixture
def espeak_provider() -> EspeakProvider:
    """Create an EspeakProvider with espeak-ng binary mocked."""
    with patch(
        "punt_vox.providers.espeak._find_espeak_binary",
        return_value="/usr/bin/espeak-ng",
    ):
        provider = EspeakProvider()

    # Pre-populate the VoiceResolver cache for tests.
    provider._voices._cache.update(  # pyright: ignore[reportPrivateUsage]
        {
            "english": ENGLISH_ESPEAK,
            "en": ENGLISH_ESPEAK,
            "german": GERMAN_ESPEAK,
            "de": GERMAN_ESPEAK,
            "french": FRENCH_ESPEAK,
            "fr": FRENCH_ESPEAK,
        }
    )
    provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
    return provider
