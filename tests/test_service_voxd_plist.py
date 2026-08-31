"""Tests for punt_vox.service.voxd_plist -- the voxd LaunchAgent plist."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING
from unittest.mock import patch

from punt_vox.service.voxd_plist import VoxdPlist

if TYPE_CHECKING:
    from pathlib import Path

_ARGS = ["/usr/local/bin/voxd", "--port", "8421"]


def _plist(tmp_path: Path) -> VoxdPlist:
    """Build a plist under tmp_path so no test touches ~/Library."""
    return VoxdPlist(
        "com.punt-labs.voxd",
        tmp_path / "LaunchAgents" / "com.punt-labs.voxd.plist",
        lambda: list(_ARGS),
    )


# ---------------------------------------------------------------------------
# captured_env -- which install-time vars ride into the plist
# ---------------------------------------------------------------------------


@patch.dict("os.environ", {}, clear=True)
def test_captured_env_is_empty_when_nothing_is_set() -> None:
    assert VoxdPlist.captured_env() == {}


@patch.dict("os.environ", {"VOXD_BIND": "127.0.0.1"}, clear=True)
def test_captured_env_carries_the_bind_address() -> None:
    assert VoxdPlist.captured_env() == {"VOXD_BIND": "127.0.0.1"}


@patch.dict(
    "os.environ",
    {
        "SSL_CERT_FILE": "/etc/ssl/certs/proxy-ca.pem",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/proxy-ca.pem",
    },
    clear=True,
)
def test_captured_env_carries_both_ca_vars() -> None:
    assert VoxdPlist.captured_env() == {
        "SSL_CERT_FILE": "/etc/ssl/certs/proxy-ca.pem",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/proxy-ca.pem",
    }


@patch.dict("os.environ", {"SSL_CERT_FILE": ""}, clear=True)
def test_an_empty_ca_var_is_omitted_not_written_through() -> None:
    """An empty value must not reach the plist.

    Writing ``SSL_CERT_FILE=""`` is worse than omitting it: OpenSSL reads an
    empty path as a bundle containing no certificates rather than falling back
    to the system trust store, so a var the user left blank would break every
    HTTPS call instead of being ignored.
    """
    assert VoxdPlist.captured_env() == {}


# ---------------------------------------------------------------------------
# content -- the XML
# ---------------------------------------------------------------------------


@patch.dict(
    "os.environ",
    {
        "SSL_CERT_FILE": "/etc/ssl/certs/proxy-ca.pem",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/proxy-ca.pem",
    },
    clear=True,
)
def test_content_bakes_the_ca_vars_into_the_environment_block(
    tmp_path: Path,
) -> None:
    """Both CA vars are forwarded so a launchd-started voxd trusts the proxy CA.

    Behind a TLS-inspecting proxy the daemon must carry the same CA the
    installing shell had; without it, HTTPS calls that worked at install time
    fail once launchd starts the job.
    """
    content = _plist(tmp_path).content()
    assert "<key>SSL_CERT_FILE</key>" in content
    assert "<key>REQUESTS_CA_BUNDLE</key>" in content
    assert content.count("/etc/ssl/certs/proxy-ca.pem") == 2


@patch.dict("os.environ", {}, clear=True)
def test_content_omits_the_ca_vars_when_they_are_not_set(tmp_path: Path) -> None:
    content = _plist(tmp_path).content()
    assert "<key>SSL_CERT_FILE</key>" not in content
    assert "<key>REQUESTS_CA_BUNDLE</key>" not in content


@patch.dict("os.environ", {}, clear=True)
def test_content_falls_back_to_a_usable_path_when_path_is_unset(
    tmp_path: Path,
) -> None:
    """A plist with no PATH leaves voxd on launchd's bare default."""
    content = _plist(tmp_path).content()
    assert "<string>/usr/bin:/bin:/usr/sbin:/sbin</string>" in content


@patch.dict("os.environ", {"PATH": "/opt/homebrew/bin:/usr/bin"}, clear=True)
def test_content_carries_the_install_time_path(tmp_path: Path) -> None:
    assert "/opt/homebrew/bin:/usr/bin" in _plist(tmp_path).content()


@patch.dict("os.environ", {"PATH": ""}, clear=True)
def test_an_empty_path_takes_the_fallback_too(tmp_path: Path) -> None:
    """PATH set to the empty string must fall back, not be written through.

    An empty ``<string>`` leaves voxd with no PATH at all, so every subprocess
    lookup it makes fails -- the same empty-is-worse-than-absent trap the CA
    vars have, and the reason the fallback tests unset rather than blank.
    """
    content = _plist(tmp_path).content()
    assert "<string>/usr/bin:/bin:/usr/sbin:/sbin</string>" in content
    assert "<key>PATH</key>\n            <string></string>" not in content


def test_content_xml_escapes_the_label(tmp_path: Path) -> None:
    """The label is caller-supplied, so it is escaped like every other value.

    An unescaped metacharacter here makes the whole plist malformed XML and
    launchd rejects the job outright.
    """
    plist = VoxdPlist(
        "com.punt-labs.voxd<&>",
        tmp_path / "x.plist",
        lambda: list(_ARGS),
    )
    content = plist.content()
    assert "<string>com.punt-labs.voxd&lt;&amp;&gt;</string>" in content
    assert "voxd<&>" not in content


@patch.dict("os.environ", {"VOXD_BIND": "0.0.0.0 & <friends>"}, clear=True)
def test_content_xml_escapes_captured_values(tmp_path: Path) -> None:
    """launchd reads <string> content literally, so values are entity-encoded.

    An unescaped ``&`` or ``<`` makes the plist malformed XML, and launchd
    rejects the whole job rather than the one key.
    """
    content = _plist(tmp_path).content()
    assert "0.0.0.0 &amp; &lt;friends&gt;" in content
    assert "<friends>" not in content


def test_content_xml_escapes_program_arguments(tmp_path: Path) -> None:
    plist = VoxdPlist(
        "com.punt-labs.voxd",
        tmp_path / "x.plist",
        lambda: ["/opt/a&b/voxd", "--flag=<x>"],
    )
    content = plist.content()
    assert "<string>/opt/a&amp;b/voxd</string>" in content
    assert "<string>--flag=&lt;x&gt;</string>" in content


def test_content_names_the_label_and_pins_the_aqua_session(tmp_path: Path) -> None:
    content = _plist(tmp_path).content()
    assert "<string>com.punt-labs.voxd</string>" in content
    assert "<key>LimitLoadToSessionType</key>" in content
    assert "<string>Aqua</string>" in content


# ---------------------------------------------------------------------------
# write / exists / remove -- the file
# ---------------------------------------------------------------------------


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    plist = _plist(tmp_path)
    assert not plist.path.parent.exists()
    plist.write()
    assert plist.path.is_file()


def test_write_sets_0644(tmp_path: Path) -> None:
    plist = _plist(tmp_path)
    plist.write()
    assert stat.S_IMODE(plist.path.stat().st_mode) == 0o644


def test_write_is_idempotent(tmp_path: Path) -> None:
    plist = _plist(tmp_path)
    plist.write()
    first = plist.path.read_text()
    plist.write()
    assert plist.path.read_text() == first


def test_exists_tracks_the_file(tmp_path: Path) -> None:
    plist = _plist(tmp_path)
    assert not plist.exists()
    plist.write()
    assert plist.exists()


def test_remove_deletes_the_file(tmp_path: Path) -> None:
    plist = _plist(tmp_path)
    plist.write()
    plist.remove()
    assert not plist.exists()


def test_remove_tolerates_an_absent_file(tmp_path: Path) -> None:
    """uninstall calls remove on a path that may already be gone."""
    _plist(tmp_path).remove()


def test_path_reports_where_the_plist_lives(tmp_path: Path) -> None:
    expected = tmp_path / "LaunchAgents" / "com.punt-labs.voxd.plist"
    assert _plist(tmp_path).path == expected
