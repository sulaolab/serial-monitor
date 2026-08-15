"""Settings resolution: precedence, discovery, and refusing to guess.

The failure this guards against is not "wrong port number". It is an agent that
believes it is on one board while it is driving another, because both
monitors answer on 8080 and only the loopback alias differs.
"""

from __future__ import annotations

import json
import os

import pytest

from .. import config as cfgmod
from ..config import ConfigError, DEFAULTS, find_project_config, resolve


def _write(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


# -- defaults -----------------------------------------------------------------
def test_defaults_when_nothing_declares_anything(tmp_path):
    values, sources = resolve(start_dir=str(tmp_path))
    assert values["http_host"] == DEFAULTS["http_host"] == "127.0.0.1"
    assert values["profile"] is None
    assert sources["http_host"] == "default"


# -- named profiles ----------------------------------------------------------
def test_ck_profile_moves_the_bind_off_the_default_alias(tmp_path):
    values, sources = resolve(start_dir=str(tmp_path), profile="ck")
    assert values["http_host"] == "127.0.0.5"
    assert values["tcp_host"] == "127.0.0.5"
    assert sources["http_host"] == "profile:ck"


def test_profiles_in_the_checkout_do_not_collide(tmp_path):
    """Every profile here must own a distinct (host, port) pair, or they cannot coexist.

    Scoped to the checkout's own ``profiles/`` on purpose: the search path may
    also reach profiles kept outside the repo, and a collision between one of
    those and a file here is its owner's business, not a test failure.
    """
    shipped = sorted(
        f[:-5]
        for f in os.listdir(os.path.join(cfgmod.repo_root(), cfgmod.PROFILE_DIR_NAME))
        if f.endswith(".json")
    )
    binds = {}
    for name in shipped:
        values, _ = resolve(start_dir=str(tmp_path), profile=name)
        key = (values["http_host"], values["http_port"])
        assert key not in binds, f"{name} and {binds[key]} both bind {key}"
        binds[key] = name
    assert len(binds) >= 2


def test_unknown_profile_names_the_available_ones(tmp_path):
    with pytest.raises(ConfigError) as exc:
        resolve(start_dir=str(tmp_path), profile="nope")
    assert "ck" in str(exc.value)


# -- where profiles come from --------------------------------------------------
def test_a_profile_dir_outside_the_checkout_wins(tmp_path, monkeypatch):
    """A profile kept outside the repo must not be shadowed by what a pull brings in."""
    mine = tmp_path / "mine"
    mine.mkdir()
    _write(str(mine / "ck.json"),
           {"name": "my-bench", "http_host": "127.0.0.77", "tcp_host": "127.0.0.77"})
    monkeypatch.setenv(cfgmod.PROFILE_PATH_ENV, str(mine))

    values, sources = resolve(start_dir=str(tmp_path), profile="ck")
    assert values["http_host"] == "127.0.0.77"
    assert values["name"] == "my-bench"
    assert sources["http_host"] == "profile:ck"


def test_profiles_outside_the_checkout_are_listed_too(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    mine.mkdir()
    _write(str(mine / "my-own-board.json"), {"name": "my-own-board"})
    monkeypatch.setenv(cfgmod.PROFILE_PATH_ENV, str(mine))

    names = cfgmod.available_profiles()
    assert "my-own-board" in names, "a listing that omits your bench is a fiction"
    assert "ck" in names, "the profiles in the checkout stay reachable"


def test_an_unset_search_path_still_finds_the_checkout_profiles(monkeypatch):
    monkeypatch.delenv(cfgmod.PROFILE_PATH_ENV, raising=False)
    assert cfgmod.profile_path("ck") is not None
    assert cfgmod.profile_path("no-such-board-here") is None


# -- project config discovery -------------------------------------------------
def test_project_config_is_found_by_walking_up(tmp_path):
    _write(str(tmp_path / ".serial-monitor.json"), {"profile": "ck"})
    deep = tmp_path / "src" / "hal_uart" / "nested"
    deep.mkdir(parents=True)

    values, sources = resolve(start_dir=str(deep))
    assert values["http_host"] == "127.0.0.5"      # inherited via the named profile
    assert values["profile"] == "ck"
    assert sources["http_host"] == "profile:ck"
    assert find_project_config(str(deep)).endswith(".serial-monitor.json")


def test_project_config_can_override_the_profile_it_inherits(tmp_path):
    _write(str(tmp_path / ".serial-monitor.json"),
           {"profile": "ck", "http_port": 8090, "name": "ck-second-board"})
    values, sources = resolve(start_dir=str(tmp_path))
    assert (values["http_host"], values["http_port"]) == ("127.0.0.5", 8090)
    assert values["name"] == "ck-second-board"
    assert sources["http_port"].endswith(".serial-monitor.json")
    assert sources["http_host"] == "profile:ck"


def test_no_project_config_ignores_the_declaration(tmp_path):
    _write(str(tmp_path / ".serial-monitor.json"), {"profile": "ck"})
    values, _ = resolve(start_dir=str(tmp_path), use_project_config=False)
    assert values["http_host"] == "127.0.0.1"


def test_nearest_project_config_wins(tmp_path):
    _write(str(tmp_path / ".serial-monitor.json"), {"http_host": "127.0.0.7"})
    inner = tmp_path / "inner"
    inner.mkdir()
    _write(str(inner / ".serial-monitor.json"), {"http_host": "127.0.0.8"})
    values, _ = resolve(start_dir=str(inner))
    assert values["http_host"] == "127.0.0.8"


# -- explicit values win ------------------------------------------------------
def test_command_line_beats_everything(tmp_path):
    _write(str(tmp_path / ".serial-monitor.json"), {"profile": "ck"})
    values, sources = resolve(
        start_dir=str(tmp_path),
        overrides={"http_host": "127.0.0.9", "baud": None},
    )
    assert values["http_host"] == "127.0.0.9"
    assert sources["http_host"] == "command line"
    # baud=None means "not given" and must not clobber the profile's value.
    assert values["baud"] == 230400
    assert sources["baud"] == "profile:ck"


# -- refusing to guess --------------------------------------------------------
def test_typo_in_a_key_is_an_error_not_a_shrug(tmp_path):
    """A silently ignored 'htp_host' is an afternoon of 'why is it still on 8080'."""
    _write(str(tmp_path / ".serial-monitor.json"), {"htp_host": "127.0.0.5"})
    with pytest.raises(ConfigError) as exc:
        resolve(start_dir=str(tmp_path))
    assert "htp_host" in str(exc.value)


def test_malformed_json_is_reported_with_the_path(tmp_path):
    path = tmp_path / ".serial-monitor.json"
    path.write_text('{ "profile": "ck",, }', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        resolve(start_dir=str(tmp_path))
    assert ".serial-monitor.json" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [{"baud": 0}, {"baud": "230400"}, {"http_port": 99999}, {"vid": "3EB"}, {"vid": 1234}],
)
def test_implausible_values_are_rejected(tmp_path, bad):
    _write(str(tmp_path / ".serial-monitor.json"), bad)
    with pytest.raises(ConfigError):
        resolve(start_dir=str(tmp_path))


def test_a_profile_never_pins_a_com_port(tmp_path):
    """COM numbers move when a board is re-plugged; pinning one bridges you to
    the wrong board. 'port' is not even an accepted key."""
    assert "port" not in cfgmod.ALLOWED_KEYS
    _write(str(tmp_path / ".serial-monitor.json"), {"port": "COM12"})
    with pytest.raises(ConfigError):
        resolve(start_dir=str(tmp_path))


# -- hand-edited files may carry comments -------------------------------------
def test_whole_line_comments_are_allowed(tmp_path):
    """The documented examples show one; strict JSON used to reject them."""
    (tmp_path / ".serial-monitor.json").write_text(
        '{\n  // this repo drives the CK kit\n  "profile": "ck"\n}\n', encoding="utf-8"
    )
    values, _ = resolve(start_dir=str(tmp_path))
    assert values["http_host"] == "127.0.0.5"


def test_a_double_slash_inside_a_value_survives(tmp_path):
    """Stripping trailing // would silently truncate a legitimate value."""
    (tmp_path / ".serial-monitor.json").write_text(
        '{\n  "profile": "ck",\n  "name": "ck // second bench"\n}\n', encoding="utf-8"
    )
    values, _ = resolve(start_dir=str(tmp_path))
    assert values["name"] == "ck // second bench"


def test_comments_do_not_shift_the_reported_error_line(tmp_path):
    (tmp_path / ".serial-monitor.json").write_text(
        '{\n  // a comment\n  "profile": "ck",,\n}\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        resolve(start_dir=str(tmp_path))
    assert "line 3" in str(exc.value)
