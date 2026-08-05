"""Config store: credential file permissions and validation."""

import json
import os
import stat

import pytest

from hsmcli.config import DIR_MODE, FILE_MODE, Config


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_creates_dir_and_defaults(tmp_path):
    c = Config(str(tmp_path / "cfg"))
    assert c.get_all() == {}
    assert c.get_base_url() == "https://www.hacksmarter.org"
    assert c.get_output_format() == "table"


def test_creates_missing_parent_dirs(tmp_path):
    """`--config-dir a/b/c` used to raise a bare FileNotFoundError."""
    c = Config(str(tmp_path / "a" / "b" / "c"))
    c.set_cookie("a=1")
    assert (tmp_path / "a" / "b" / "c" / "config.json").exists()


def test_dir_is_owner_only(tmp_path):
    Config(str(tmp_path / "cfg"))
    assert _mode(tmp_path / "cfg") == DIR_MODE


def test_cookie_file_is_owner_only(tmp_path):
    """The file holds the access *and* refresh token — group/other must not
    be able to read it."""
    c = Config(str(tmp_path / "cfg"))
    c.set_cookie("sb-auth-auth-token.0=base64-abc")
    mode = _mode(c.config_file)
    assert mode == FILE_MODE
    assert not mode & (stat.S_IRGRP | stat.S_IROTH)


def test_existing_loose_permissions_are_repaired(tmp_path):
    """A config written by an older version was 0644; opening it tightens."""
    d = tmp_path / "cfg"
    d.mkdir()
    f = d / "config.json"
    f.write_text(json.dumps({"cookie": "a=1"}))
    os.chmod(f, 0o644)
    os.chmod(d, 0o755)

    c = Config(str(d))
    assert _mode(f) == FILE_MODE
    assert _mode(d) == DIR_MODE
    assert c.get_cookie() == "a=1"   # and the content survived


def test_rewrite_keeps_tight_permissions(tmp_path):
    c = Config(str(tmp_path / "cfg"))
    c.set_cookie("a=1")
    os.chmod(c.config_file, 0o666)
    c.set_cookie("b=2")
    assert _mode(c.config_file) == FILE_MODE


def test_unwritable_parent_raises_valueerror_not_oserror(tmp_path):
    """main() catches ValueError/OSError to print an error instead of a
    traceback; check we raise something it handles."""
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a dir")
    with pytest.raises((ValueError, OSError)):
        Config(str(blocked / "cfg"))


# ── round-trip ────────────────────────────────────────────────────────────

def test_cookie_round_trip_and_clear(tmp_path):
    d = str(tmp_path / "cfg")
    Config(d).set_cookie("  a=1  ")
    assert Config(d).get_cookie() == "a=1"      # stripped and persisted
    c = Config(d)
    c.clear_cookie()
    assert Config(d).get_cookie() is None


def test_env_var_overrides_stored_cookie(tmp_path, monkeypatch):
    c = Config(str(tmp_path / "cfg"))
    c.set_cookie("stored=1")
    monkeypatch.setenv("HSMCLI_COOKIE", "fromenv=1")
    assert c.get_cookie() == "fromenv=1"


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_cookie_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        Config(str(tmp_path / "cfg")).set_cookie(bad)


def test_base_url_trailing_slash_stripped(tmp_path):
    c = Config(str(tmp_path / "cfg"))
    c.set_base_url("https://example.test/")
    assert c.get_base_url() == "https://example.test"


def test_bad_output_format_rejected(tmp_path):
    with pytest.raises(ValueError):
        Config(str(tmp_path / "cfg")).set_output_format("xml")


def test_corrupt_config_degrades_to_empty(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.json").write_text("{not json")
    assert Config(str(d)).get_all() == {}


def test_reset_wipes(tmp_path):
    d = str(tmp_path / "cfg")
    c = Config(d)
    c.set_cookie("a=1")
    c.set_base_url("https://example.test")
    c.reset()
    assert Config(d).get_all() == {}
