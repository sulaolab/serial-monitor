"""Where the bind address and baud come from -- one resolver, four sources.

The problem this solves is not typing convenience.  Two boards are debugged on
this PC at once, each wanting its own monitor, and both want port 8080.  They are
kept apart by loopback alias (``127.0.0.1`` for one, ``127.0.0.5`` for another),
which means "which address" is a fact about the *board being worked on*, not about
the person or the tool.  Anything that requires a human to remember it will
eventually put an agent on the wrong board while it believes it is on the right
one -- and both answer on 8080.

So the consuming repo declares it.  A ``.serial-monitor.json`` sitting in the
firmware repo says "monitors for this board live on 127.0.0.5", and the launcher
finds it by walking up from the current directory.  Working in that repo is enough
to get the right bind; nothing has to be remembered or passed.

Precedence, highest first:

1. an explicit command-line value (``--http-host 127.0.0.9``)
2. ``.serial-monitor.json`` found by walking up from the start directory
3. a named profile: ``--profile foo`` -> ``foo.json`` in the profile search path
4. the built-in defaults below

Your boards are not this tool's business
----------------------------------------
The profile search path is deliberately not "this repo" alone.  A profile names
*your* bench -- board names, which alias each one gets -- and that is neither
generic enough to ship nor something a tool update should be able to overwrite.
So a profile is looked up in, first match winning:

1. every directory in ``$SERIAL_MONITOR_PROFILES`` (``os.pathsep``-separated);
2. ``.serial-monitor-profiles/`` NEXT TO this checkout, which needs no setup at
   all and survives deleting and re-cloning the tool;
3. ``profiles/`` inside the checkout -- the shipped examples.

``available_profiles()`` reports the union, so ``-Help`` and ``--show-config``
list your bench, not a documented fiction.

What is deliberately NOT configurable here: the COM port.  Port numbers are
reassigned when a board is re-plugged, so freezing one in a file is a way to
bridge to the wrong board.  Naming the port stays the caller's job every single
time -- nothing is recommended and nothing is auto-picked.  A profile may carry a
``vid``, but it is documentation only: it is shown by ``-Help`` and is **not**
inherited as a port filter by the launcher, because a shared config quietly hiding
candidates misleads in the same way that starring one does.  ``-Vid`` is a filter
the caller types.

``resolve()`` also returns where each value came from, because a wrong bind is
close to undebuggable otherwise: the whole failure mode is a value you did not
know was being applied.
"""

from __future__ import annotations

import json
import os

PROJECT_CONFIG_NAME = ".serial-monitor.json"
PROFILE_DIR_NAME = "profiles"
# Directory of your own profiles, next to the checkout rather than inside it: no
# environment to set, and re-cloning the tool cannot take your bench with it.
LOCAL_PROFILE_DIR_NAME = ".serial-monitor-profiles"
PROFILE_PATH_ENV = "SERIAL_MONITOR_PROFILES"

# Keys a profile or project config may set.  Anything else is an error rather
# than a silently ignored typo -- "why is it still on 8080" is a bad afternoon.
ALLOWED_KEYS = frozenset(
    {
        "name",         # label for the banner and GET /status ("which board am I on?")
        "baud",
        "terminator",
        "http_host",
        "http_port",
        "tcp_host",
        "tcp_port",
        "vid",          # 4 hex digits; documentation only -- see the docstring
        "profile",      # a project config may inherit a named profile
    }
)

DEFAULTS: dict = {
    "name": "default",
    "baud": 230400,
    "terminator": "crlf",
    "http_host": "127.0.0.1",
    "http_port": 8080,
    "tcp_host": "127.0.0.1",
    "tcp_port": 23,
    "vid": None,
}

_MAX_WALK_UP = 40  # a repo is never this deep; stops a pathological loop


class ConfigError(ValueError):
    """A profile or project config that cannot be trusted to be what was meant."""


def repo_root() -> str:
    """The serial-monitor repo root (the parent of this package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def profile_dirs() -> list[str]:
    """Where profiles are looked for, highest precedence first.

    See the module docstring: your bench comes before the shipped examples, and
    lives outside the checkout so updating the tool cannot touch it.
    """
    dirs: list[str] = []
    for entry in os.environ.get(PROFILE_PATH_ENV, "").split(os.pathsep):
        if entry.strip():
            dirs.append(os.path.abspath(entry.strip()))
    root = repo_root()
    dirs.append(os.path.join(os.path.dirname(root), LOCAL_PROFILE_DIR_NAME))
    dirs.append(os.path.join(root, PROFILE_DIR_NAME))
    return dirs


def profile_path(name: str) -> str | None:
    """Path of profile ``name``, or None if no search directory has it."""
    for d in profile_dirs():
        candidate = os.path.join(d, f"{name}.json")
        if os.path.isfile(candidate):
            return candidate
    return None


def available_profiles() -> list[str]:
    """Every profile name reachable through the search path, deduplicated."""
    names: set[str] = set()
    for d in profile_dirs():
        if not os.path.isdir(d):
            continue
        names.update(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    return sorted(names)


def _strip_line_comments(text: str) -> str:
    """Drop whole-line ``//`` comments, keeping line numbers intact.

    These files are hand-edited and their entire job is to record *why* a repo
    binds where it does, so a comment has to be allowed -- the documented examples
    have always shown one, and strict JSON rejected them.

    Only a line whose first non-space characters are ``//`` is removed, never a
    trailing ``//`` inside a line: a value may legitimately contain one (a path, a
    URL), and silently truncating a value is worse than refusing the file. The line
    is blanked rather than deleted so JSON's error line numbers still match the
    file you are looking at.
    """
    return "\n".join(
        "" if line.lstrip().startswith("//") else line for line in text.split("\n")
    )


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc})") from None
    try:
        data = json.loads(_strip_line_comments(raw))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(ALLOWED_KEYS))}"
        )
    return data


def find_project_config(start_dir: str | None = None) -> str | None:
    """Walk up from ``start_dir`` looking for ``.serial-monitor.json``."""
    d = os.path.abspath(start_dir or os.getcwd())
    for _ in range(_MAX_WALK_UP):
        candidate = os.path.join(d, PROJECT_CONFIG_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:  # filesystem root
            return None
        d = parent
    return None


def load_profile(name: str) -> dict:
    path = profile_path(name)
    if path is None:
        known = ", ".join(available_profiles()) or "(none)"
        looked = os.pathsep.join(profile_dirs())
        raise ConfigError(
            f"no profile '{name}'. Available: {known}. Looked in: {looked}"
        )
    data = _load_json(path)
    data.pop("profile", None)  # a profile inheriting a profile is not supported
    return data


def resolve(
    *,
    start_dir: str | None = None,
    profile: str | None = None,
    overrides: dict | None = None,
    use_project_config: bool = True,
) -> tuple[dict, dict]:
    """Return ``(values, sources)`` -- the effective settings and their origin.

    ``overrides`` are explicit command-line values; keys whose value is ``None``
    count as "not given" and do not override anything.
    """
    values = dict(DEFAULTS)
    sources = {k: "default" for k in values}

    project_path = find_project_config(start_dir) if use_project_config else None
    project_cfg = _load_json(project_path) if project_path else {}

    # A project config may name the profile it builds on, so a repo says "I am a
    # board of that family" once instead of restating every field.
    effective_profile = profile or project_cfg.get("profile")
    if effective_profile:
        for key, val in load_profile(effective_profile).items():
            values[key] = val
            sources[key] = f"profile:{effective_profile}"

    for key, val in project_cfg.items():
        if key == "profile":
            continue
        values[key] = val
        sources[key] = project_path

    for key, val in (overrides or {}).items():
        if val is None or key not in ALLOWED_KEYS:
            continue
        values[key] = val
        sources[key] = "command line"

    values["profile"] = effective_profile
    sources["profile"] = (
        "command line" if profile
        else (project_path if project_cfg.get("profile") else "default")
    )
    _validate(values)
    return values, sources


def _validate(values: dict) -> None:
    if not isinstance(values["baud"], int) or values["baud"] <= 0:
        raise ConfigError(f"baud must be a positive integer, got {values['baud']!r}")
    for key in ("http_port", "tcp_port"):
        port = values[key]
        if not isinstance(port, int) or not (0 <= port <= 65535):
            raise ConfigError(f"{key} must be 0..65535, got {port!r}")
    vid = values.get("vid")
    if vid is not None:
        if not isinstance(vid, str) or len(vid) != 4 or not all(
            c in "0123456789abcdefABCDEF" for c in vid
        ):
            raise ConfigError(f"vid must be 4 hex digits, got {vid!r}")


def main(argv: list[str] | None = None) -> int:
    """``python -m serial_monitor.config`` -- print the resolved settings as JSON.

    This exists so the PowerShell launchers do not reimplement the precedence
    rules.  Two implementations of "where does the bind come from" is exactly how
    the answer starts depending on which one you asked.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="serial_monitor.config",
        description="Resolve serial-monitor settings for a directory.",
    )
    p.add_argument("--start-dir", default=None, help="Directory to search upward from (default cwd)")
    p.add_argument("--profile", default=None, help=f"Named profile: {', '.join(available_profiles()) or '(none)'}")
    p.add_argument("--no-project-config", action="store_true", help=f"Ignore any {PROJECT_CONFIG_NAME}")
    p.add_argument("--with-sources", action="store_true", help="Also report where each value came from")
    args = p.parse_args(argv)

    try:
        values, sources = resolve(
            start_dir=args.start_dir,
            profile=args.profile,
            use_project_config=not args.no_project_config,
        )
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    out = dict(values)
    if args.with_sources:
        out["_sources"] = sources
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
