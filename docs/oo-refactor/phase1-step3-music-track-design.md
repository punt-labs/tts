# Phase 1 Step 3: MusicTrack Design

Date: 2026-05-15
Status: REVISED — peer review by rej 2026-05-15
Bead: vox-0xa9

## Problem

The track metadata dict `{"name", "size_bytes", "modified", "path"}` is built
in `generator.py:list_tracks()` and then formatted with identical
`int(str(raw_size))` and `float(str(raw_mtime))` casts in two places:

- `server.py` lines 800–806
- `__main__.py` lines 1090–1096

Both callers read the dict with `.get()`, cast through string intermediaries
(defensive against non-int/float JSON types), and format the same display line.
The duplication is exact.

This is Replace Dict with Class (PY-RF-3): same dict shape passed through 3+
functions.

## Proposed Class

In `src/punt_vox/voxd/music/generator.py`:

```python
@dataclass(frozen=True, slots=True)
class MusicTrack:
    """Saved music track with display metadata."""

    name: str
    path: Path
    size_bytes: int
    modified: float  # Unix timestamp (st_mtime)

    @classmethod
    def from_stat(cls, mp3: Path) -> MusicTrack:
        stat = mp3.stat()
        return cls(
            name=mp3.stem,
            path=mp3,
            size_bytes=stat.st_size,
            modified=stat.st_mtime,
        )

    def display_line(self) -> str:
        size_kb = self.size_bytes // 1024
        date_str = datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d %H:%M")
        return f"{self.name} ({size_kb} KB, {date_str})"

    def to_dict(self) -> dict[str, object]:
        """Serialize for WebSocket wire format (backward compat)."""
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
            "path": str(self.path),
        }
```

`list_tracks()` returns `list[MusicTrack]` instead of `list[dict[str, object]]`.

## Updated API

| Before | After |
|--------|-------|
| `list_tracks() -> list[dict]` | `list_tracks() -> list[MusicTrack]` |
| `server.py`: manual `int(str(raw_size)) // 1024` etc. | `track.display_line()` |
| `__main__.py`: same manual cast | `track.display_line()` |
| `client.music_list()` returns `{"tracks": list[dict]}` | unchanged — client returns the wire dict; callers parse with `MusicTrack.from_dict()` or the list handler serializes with `to_dict()` |

**Wire boundary**: The wire format (JSON keys) is unchanged.
`list_tracks()` returns `list[MusicTrack]` internally. `MusicListHandler`
calls `track.to_dict()` for each track before sending the WebSocket response.
Callers (server.py, __main__.py) receive `list[dict]` from the client and
parse each dict into `MusicTrack` via `from_dict()`, then call `display_line()`.

```python
    @classmethod
    def from_dict(cls, d: dict[str, object]) -> MusicTrack:
        return cls(
            name=str(d["name"]),
            path=Path(str(d["path"])),
            size_bytes=int(str(d.get("size_bytes", 0))),  # defensive cast
            modified=float(str(d.get("modified", 0))),    # defensive cast
        )
```

The `int(str(x))` and `float(str(x))` casts move inside the class,
eliminating duplication from server.py and __main__.py.

**stat() race in from_stat**: `mp3.stat()` can raise `FileNotFoundError`
if the file is deleted between `glob()` and `stat()`. The current code has
the same race. Wrap in `try/except OSError` and skip the file if it disappears.

## Files Changed

- `src/punt_vox/voxd/music/generator.py`: add `MusicTrack`, update
  `list_tracks()` return type, use `from_stat()` internally. Update
  `__all__` from `["TrackGenerator"]` to `["TrackGenerator", "MusicTrack"]`.
- `src/punt_vox/voxd/music/list_handler.py`: serialize with
  `track.to_dict()` before sending over WebSocket
- `src/punt_vox/server.py`: parse with `MusicTrack.from_dict(t)`, call
  `track.display_line()`
- `src/punt_vox/__main__.py`: same

## Completion Test

```bash
grep -rn "int(str.*size\|float(str.*mtime\|int(str.*raw" src/punt_vox/ | grep -v "#"
# Must return zero hits — casts absorbed into MusicTrack.from_dict
make check
```
