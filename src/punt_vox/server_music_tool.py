"""The single ``music`` ``mic`` tool: one subcommand-dispatched playback+catalog verb.

``vox <group> <subcommand>`` maps to the MCP tool ``<group>`` with its first
argument ``<subcommand>``; ``music`` is the reference. One :class:`MusicTool`
collapses what were seven tools (``music``/``music_play``/``music_list``/
``music_next`` plus the ``music_new``/``get``/``remove`` catalog verbs) behind a
``subcommand`` argument, routed through an explicit method table -- polymorphism
over an ``if``-ladder (PY-OO-6). Playback verbs drive the :class:`ProgramGateway`
seam; catalog verbs drive the :class:`CatalogGateway` seam, the same two seams
the ``vox music`` CLI hits, so both surfaces share one code path.

Held apart from ``server.py`` so that module stays under the module-size and
class-count thresholds, mirroring how ``server_audio_tools.py`` was split out.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.music_args import MusicArgs
from punt_vox.music_phrases import MusicMarquee
from punt_vox.types_programs.control import SelectionRequest, StartRequest
from punt_vox.types_programs.prompts import PromptSet

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.program_gateway import ProgramGateway
    from punt_vox.vibe_command import MusicPreference

__all__ = ["MusicSubcommand", "MusicTool"]

logger = logging.getLogger(__name__)

MusicSubcommand = Literal["on", "off", "play", "next", "new", "list", "get", "remove"]

# The daemon-transport faults every subcommand funnels to a JSON _error; named
# once so the whole tool shares one contract, mirroring server.py/server_audio_tools.
_DAEMON_ERRORS = (VoxdConnectionError, VoxdProtocolError, WebSocketException, OSError)


def _error(message: str) -> str:
    """Return a JSON error string."""
    return json.dumps({"error": message})


class MusicSession(Protocol):
    """The session surface :class:`MusicTool` reads for the ``on`` request.

    A structural view (PY-TS-6) of :class:`~punt_vox.server.SessionConfig`, so
    this module never imports the presentation-layer session -- the dependency
    arrow keeps pointing inward.
    """

    @property
    def vibe(self) -> str | None:
        """Return the session mood tag, or None when it is cleared."""

    def refresh_from_config(self) -> None:
        """Re-read the config files so the yielded mood is current."""


@final
class MusicTool:
    """Dispatch one ``music`` subcommand to its playback or catalog handler.

    Holds the two gateway seams (playback + catalog) and the session/preference
    registers as call-time providers, so a test that patches the server's
    module globals is honoured on the next call. The DJ-booth marquee is owned
    outright. The subcommand selects a handler through :data:`_HANDLERS`, an
    explicit method map -- never ``getattr``-by-name (PY-TS-11).
    """

    __slots__ = (
        "_catalog_factory",
        "_marquee",
        "_pref_provider",
        "_program_factory",
        "_session_provider",
    )
    _program_factory: Callable[[], ProgramGateway]
    _catalog_factory: Callable[[], CatalogGateway]
    _session_provider: Callable[[], MusicSession]
    _pref_provider: Callable[[], MusicPreference]
    _marquee: MusicMarquee

    def __new__(
        cls,
        program_factory: Callable[[], ProgramGateway],
        catalog_factory: Callable[[], CatalogGateway],
        session_provider: Callable[[], MusicSession],
        pref_provider: Callable[[], MusicPreference],
    ) -> Self:
        self = super().__new__(cls)
        self._program_factory = program_factory
        self._catalog_factory = catalog_factory
        self._session_provider = session_provider
        self._pref_provider = pref_provider
        self._marquee = MusicMarquee()
        return self

    def dispatch(
        self,
        subcommand: MusicSubcommand,
        style: str | None = None,
        vibe: str | None = None,
        name: str | None = None,
        title: str | None = None,
        album_id: str | None = None,
        base_prompt: str | None = None,
        variations: list[str] | None = None,
        dest: str | None = None,
    ) -> str:
        """Route one ``music`` call to its subcommand handler.

        vox never interprets a genre -- YOU, the calling agent, author the
        prompts. On ``on`` (and on any style/vibe change) supply ``base_prompt``
        plus exactly 12 literal, genre-accurate ``variations`` (one per pool
        slot); voxd generates track ``i`` from ``base_prompt`` + ``variations[i]``.
        Omit both to fall back to ``"<style> music, <mood>. instrumental,
        loopable."``. See ``/music`` for a worked example.

        Args:
            subcommand: The verb -- ``on``/``off``/``play``/``next`` drive the
                running Program; ``new``/``get``/``remove`` mutate the saved
                catalog; ``list`` shows it.
            style: Style tag; persists across calls for ``on``/``play``.
            vibe: Vibe tag radio for ``play``.
            name: Existing album handle ``play`` replays by name.
            title: Human album title the authoring verbs (``on``/``new``)
                give the album they create; it becomes the album's unique
                ``name`` and rides the ID3 ``TALB``/``TIT2`` frames.
            album_id: Bare album id for ``play``/``get``/``remove``.
            base_prompt: Authored base for ``on`` (with the 12 ``variations``)
                and the verbatim single prompt for ``new``.
            variations: The 12-entry ``on`` pool; requires ``base_prompt``.
            dest: Destination directory ``get`` exports the album into.

        Returns:
            JSON string: ``{"message", "applied"}`` for the control/playback
            verbs, ``{"message", "programs"}`` for ``list``, ``{"album_id"}``/
            ``{"album_id", "path"}``/``{"removed"}`` for the catalog verbs, and
            an ``{"error": ...}`` envelope on a daemon fault or malformed prompt.
            Control actions emit no agent prose; the JSON drives the panel only.
        """
        self._session_provider().refresh_from_config()
        args = MusicArgs(
            subcommand=subcommand,
            style=style,
            vibe=vibe,
            name=name,
            title=title,
            album_id=album_id,
            base_prompt=base_prompt,
            variations=variations,
            dest=dest,
        )
        handler = self._HANDLERS.get(subcommand)
        if handler is None:
            return _error(f"unknown music subcommand: {subcommand!r}")
        return handler(self, args)

    def _on(self, args: MusicArgs) -> str:
        """Start a Program from the authored pool (or the fallback literal)."""
        session = self._session_provider()
        style = args.canonical_style
        try:
            prompts = PromptSet.from_tool_args(args.base_prompt, args.variations)
            outcome = self._program_factory().start(
                StartRequest(
                    style=style,
                    vibe=session.vibe,
                    name=args.canonical_title,
                    prompts=prompts,
                )
            )
            # confirm_started adopts the genre and traces only on an applied
            # outcome, so a rejected/lost-race start leaves the register untouched.
            self._pref_provider().confirm_started(
                outcome, style, session.vibe, authored=args.authored
            )
            message = f"♪ {outcome.display(self._marquee.generating(style))}"
        except (ValueError, *_DAEMON_ERRORS) as exc:  # malformed prompt or fault
            return _error(str(exc))
        return json.dumps({"message": message, "applied": outcome.applied})

    def _off(self, _args: MusicArgs) -> str:
        """Stop the active Program; clear the style register."""
        try:
            outcome = self._program_factory().stop()
            self._pref_provider().confirm_stopped(outcome)
            message = f"♪ {outcome.display(self._marquee.stopped())}"
        except _DAEMON_ERRORS as exc:
            return _error(str(exc))
        return json.dumps({"message": message, "applied": outcome.applied})

    def _play(self, args: MusicArgs) -> str:
        """Replay a Selection resolved by tags or by an exact album id."""
        vibe = args.canonical_vibe
        name = args.canonical_name
        request = SelectionRequest(
            style=args.canonical_style, vibe=vibe, name=name, id=args.album_id
        )
        gateway = self._program_factory()
        try:
            outcome = gateway.select(request)
        except (ValueError, *_DAEMON_ERRORS) as exc:  # bad id / no match, or fault
            return _error(str(exc))
        # Name the re-pool genre from the live catalog on an applied replay; a
        # catalog fault falls back to None, never failing the applied replay.
        resolved_style: str | None = None
        if outcome.applied:
            try:
                resolved_style = request.resolved_style(gateway.catalog())
            except _DAEMON_ERRORS as exc:
                # Best-effort: a fault here never fails the applied replay, but
                # log it so the dropped re-pool style is traceable, not silent.
                logger.warning("music play: re-pool genre lookup failed: %s", exc)
                resolved_style = None
        self._pref_provider().confirm_selected(outcome, resolved_style, vibe, name)
        message = f"♪ {outcome.display(self._marquee.replay(name))}"
        return json.dumps({"message": message, "applied": outcome.applied})

    def _advance(self, _args: MusicArgs) -> str:
        """Advance to another Part -- the one ungated skip/next transition."""
        try:
            outcome = self._program_factory().advance()
        except _DAEMON_ERRORS as exc:
            return _error(str(exc))
        message = f"♪ {outcome.display(self._marquee.skip())}"
        return json.dumps({"message": message, "applied": outcome.applied})

    def _list(self, _args: MusicArgs) -> str:
        """List saved albums with their tags and ready/total part counts."""
        try:
            summaries = self._program_factory().catalog()
        except _DAEMON_ERRORS as exc:
            return _error(str(exc))
        if not summaries:
            message = "♪ No saved albums."
        else:
            lines = [f"♪ {len(summaries)} saved album(s):"]
            lines.extend(f"  ♪ {summary.display_line()}" for summary in summaries)
            message = "\n".join(lines)
        programs = [
            {
                "id": s.id,
                "style": s.style,
                "vibe": s.vibe,
                "name": s.name,
                "format": s.format,
                "ready": s.ready,
                "total": s.total,
            }
            for s in summaries
        ]
        return json.dumps({"message": message, "programs": programs})

    def _new(self, args: MusicArgs) -> str:
        """Author one verbatim-prompt track into a fresh catalog album."""
        if args.base_prompt is None:
            return _error("music new requires base_prompt")
        try:
            prompts = PromptSet.single(args.base_prompt)
            album_id = self._catalog_factory().new(prompts, args.canonical_title)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"album_id": album_id})

    def _get(self, args: MusicArgs) -> str:
        """Export a saved album into *dest*; return the written locator."""
        if args.album_id is None or args.dest is None:
            return _error("music get requires album_id and dest")
        try:
            target = self._catalog_factory().get(args.album_id, args.dest)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"album_id": args.album_id, "path": target})

    def _remove(self, args: MusicArgs) -> str:
        """Delete a saved album by id (a playing album is refused)."""
        if args.album_id is None:
            return _error("music remove requires album_id")
        try:
            self._catalog_factory().remove(args.album_id)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"removed": args.album_id})

    # The subcommand -> handler map: an explicit literal of the class's own
    # methods, never getattr-by-name (PY-TS-11 forbids introspective dispatch).
    _HANDLERS: ClassVar[dict[str, Callable[[MusicTool, MusicArgs], str]]] = {
        "on": _on,
        "off": _off,
        "play": _play,
        "next": _advance,
        "list": _list,
        "new": _new,
        "get": _get,
        "remove": _remove,
    }
