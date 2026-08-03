"""Minimal local stubs for mutagen -- only the ID3 surface vox writes.

mutagen ships ``py.typed`` but its call signatures are unannotated, so
``mypy --strict`` rejects every call as untyped. These stubs type just the
handful of ID3 frames and the ``ID3`` container vox uses to tag generated
Parts (mirrors the ``typings/pydub`` precedent).
"""

class MutagenError(Exception): ...
