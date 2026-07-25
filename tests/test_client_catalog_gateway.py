"""Tests for :class:`ClientCatalogGateway` -- the client-backed catalog adapter.

A thin verb-adapter: each catalog-authoring call delegates to the matching
``music_*`` method on a ``VoxClientSync`` and returns what the client parsed.
Tested against a mocked client so no daemon is needed; wire parsing itself
lives on the client (``test_client.py``), mirroring ``test_client_gateway.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from punt_vox.client_catalog_gateway import ClientCatalogGateway


def test_new_delegates_to_music_new_and_returns_the_id() -> None:
    """new() forwards the prompt and name and returns the client's album id."""
    client = MagicMock()
    client.music_new.return_value = "7f3a91"

    album_id = ClientCatalogGateway(client).new("warm pads", "mix")

    client.music_new.assert_called_once_with("warm pads", "mix")
    assert album_id == "7f3a91"


def test_get_delegates_to_music_get_and_stringifies_the_path() -> None:
    """get() forwards a Path dest and returns the written directory as a str."""
    client = MagicMock()
    client.music_get.return_value = Path("/tmp/out/warm-pads-7f3a91")

    target = ClientCatalogGateway(client).get("7f3a91", "/tmp/out")

    client.music_get.assert_called_once_with("7f3a91", Path("/tmp/out"))
    assert target == "/tmp/out/warm-pads-7f3a91"


def test_remove_delegates_to_music_remove() -> None:
    """remove() forwards the album id to the ``music_remove`` wire op."""
    client = MagicMock()

    ClientCatalogGateway(client).remove("7f3a91")

    client.music_remove.assert_called_once_with("7f3a91")
