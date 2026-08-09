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
from typing import TYPE_CHECKING, ClassVar, Literal, Self, final

from punt_vox.music_args import MusicArgs
from punt_vox.music_faults import DAEMON_ERRORS, MusicFault
from punt_vox.music_phrases import MusicMarquee
from punt_vox.music_state_view import MusicStateView
from punt_vox.server_music_catalog import CatalogVerbs, SavedAlbums
from punt_vox.server_music_transport import TransportVerbs
from punt_vox.types_programs.control import SelectionRequest, StartRequest
from punt_vox.types_programs.prompts import PromptSet

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.music_session import MusicSession
    from punt_vox.program_gateway import ProgramGateway
    from punt_vox.vibe_command import MusicPreference

__all__ = ["MusicSubcommand", "MusicTool"]

logger = logging.getLogger(__name__)

MusicSubcommand = Literal[
    "on",
    "stop",
    "play",
    "next",
    "prev",
    "pause",
    "resume",
    "new",
    "list",
    "status",
    "get",
    "remove",
]


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
        "_catalog",
        "_marquee",
        "_pref_provider",
        "_program_factory",
        "_session_provider",
        "_transport",
    )
    _program_factory: Callable[[], ProgramGateway]
    _catalog: CatalogVerbs
    _transport: TransportVerbs
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
        self._catalog = CatalogVerbs(catalog_factory)
        self._session_provider = session_provider
        self._pref_provider = pref_provider
        self._marquee = MusicMarquee()
        self._transport = TransportVerbs(program_factory, self._marquee)
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
            subcommand: The verb -- ``on``/``stop``/``play``/``next`` drive the
                running Program; ``new``/``get``/``remove`` mutate the saved
                catalog; ``list`` shows it and ``status`` reports what is playing.
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
            verbs, ``{"message", "programs"}`` for ``list``,
            ``{"message", "program", "music_mode"}`` for ``status``, ``{"album_id"}``/
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
            return MusicFault.rejecting(f"unknown music subcommand: {subcommand!r}")
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
        except (ValueError, *DAEMON_ERRORS) as exc:  # malformed prompt or fault
            return MusicFault.of(exc)
        return json.dumps({"message": message, "applied": outcome.applied})

    def _stop(self, _args: MusicArgs) -> str:
        """Halt the active Program; clear the style register."""
        try:
            outcome = self._program_factory().stop()
            self._pref_provider().confirm_stopped(outcome)
            message = f"♪ {outcome.display(self._marquee.stopped())}"
        except DAEMON_ERRORS as exc:
            return MusicFault.of(exc)
        return json.dumps({"message": message, "applied": outcome.applied})

    def _play(self, args: MusicArgs) -> str:
        """Replay a Selection resolved by tags, an exact album id, or last-played.

        A call carrying no id and no tags is the bare ``play``: the daemon repeats
        the last-played album, and with none played yet the reject is rendered with
        the saved-album list (:meth:`_no_history`) rather than an arbitrary album.
        """
        vibe = args.canonical_vibe
        name = args.canonical_name
        request = SelectionRequest(
            style=args.canonical_style, vibe=vibe, name=name, id=args.album_id
        )
        gateway = self._program_factory()
        try:
            outcome = gateway.select(request)
        except (ValueError, *DAEMON_ERRORS) as exc:  # bad id / no match, or fault
            if request.is_empty:
                return self._no_history(str(exc), gateway)
            return MusicFault.of(exc)
        # Name the re-pool genre from the live catalog on an applied replay; a
        # catalog fault falls back to None, never failing the applied replay.
        resolved_style: str | None = None
        if outcome.applied:
            try:
                resolved_style = request.resolved_style(gateway.catalog())
            except DAEMON_ERRORS as exc:
                # Best-effort: a fault here never fails the applied replay, but
                # log it so the dropped re-pool style is traceable, not silent.
                logger.warning("music play: re-pool genre lookup failed: %s", exc)
                resolved_style = None
        self._pref_provider().confirm_selected(outcome, resolved_style, vibe, name)
        message = f"♪ {outcome.display(self._marquee.replay(name))}"
        return json.dumps({"message": message, "applied": outcome.applied})

    def _no_history(self, message: str, gateway: ProgramGateway) -> str:
        """Return the no-history reject with the saved-album list appended.

        A bare ``play`` over a fresh daemon has no album to repeat: the daemon's
        *message* says so, and the saved-album list gives the caller something to
        pick. The list is best-effort -- a second daemon fault (unreachable, not a
        missing history) drops it, so the caller still sees the original reject.
        """
        try:
            summaries = gateway.catalog()
        except DAEMON_ERRORS:
            return MusicFault.rejecting(message)
        return MusicFault.rejecting(SavedAlbums(summaries).appended_to(message))

    def _advance(self, _args: MusicArgs) -> str:
        """User transport next -- step the replay cursor forward, or skip a Program."""
        return self._transport.advance()

    def _prev(self, _args: MusicArgs) -> str:
        """User transport prev -- step the replay cursor back one part."""
        return self._transport.prev()

    def _pause(self, _args: MusicArgs) -> str:
        """Suspend the active source in place (transport pause)."""
        return self._transport.pause()

    def _resume(self, _args: MusicArgs) -> str:
        """Continue a suspended source (transport resume)."""
        return self._transport.resume()

    def _list(self, _args: MusicArgs) -> str:
        """List saved albums with their tags and ready/total part counts."""
        try:
            summaries = self._program_factory().catalog()
        except DAEMON_ERRORS as exc:
            return MusicFault.of(exc)
        albums = SavedAlbums(summaries)
        return json.dumps({"message": albums.announced(), "programs": albums.to_wire()})

    def _status(self, _args: MusicArgs) -> str:
        """Report what the daemon is playing, read fresh on every call.

        A query verb, not a control action: the caller gets the same
        :class:`MusicStateView` fields the ``status`` tool reports, plus the
        human summary :class:`ProgramStatus` renders for every surface -- a
        headline line that grows an indented ``error:`` line on a generation
        failure and one line per permanently failed part. The panel formatter
        shows only the first line, so the failure lines reach the agent through
        the result rather than the panel. A daemon fault returns the
        ``{"error": ...}`` envelope instead, carrying no ``message``.
        """
        try:
            report = self._program_factory().status()
        except DAEMON_ERRORS as exc:
            return MusicFault.of(exc)
        state = MusicStateView.of(report).to_dict()
        return json.dumps({"message": f"♪ {report.summary()}", **state})

    def _new(self, args: MusicArgs) -> str:
        """Author one verbatim-prompt track into a fresh catalog album."""
        return self._catalog.new(args)

    def _get(self, args: MusicArgs) -> str:
        """Export a saved album into *dest*; return the written locator."""
        return self._catalog.get(args)

    def _remove(self, args: MusicArgs) -> str:
        """Delete a saved album by id (a playing album is refused)."""
        return self._catalog.remove(args)

    # The subcommand -> handler map: an explicit literal of the class's own
    # methods, never getattr-by-name (PY-TS-11 forbids introspective dispatch).
    _HANDLERS: ClassVar[dict[str, Callable[[MusicTool, MusicArgs], str]]] = {
        "on": _on,
        "stop": _stop,
        "play": _play,
        "next": _advance,
        "prev": _prev,
        "pause": _pause,
        "resume": _resume,
        "list": _list,
        "status": _status,
        "new": _new,
        "get": _get,
        "remove": _remove,
    }
