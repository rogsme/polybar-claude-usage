#!/usr/bin/env python3
"""Your Claude plan limits in polybar.

Asks the installed Claude Code for its `/usage` report and prints one line for a polybar
`custom/script` module. Claude Code fetches the numbers with its own sign-in and renews that
sign-in itself, so Claude Code doesn't need to be open and this script never handles a token.
`/usage` runs locally inside Claude Code: no message is sent to a model.

Modes:
  (default)   Keep running and print a new line whenever the display changes (`tail = true`).
              SIGUSR1 refreshes now.
  --once      Fetch once and print one line.
  --notify    Show every limit and its reset time with notify-send. Never runs Claude Code.
  --details   Print the same details to the terminal.
"""

from __future__ import annotations

import argparse
import configparser
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable

APP_NAME = "polybar-claude-usage"

# MARK: - Model

SESSION = "five_hour"
WEEKLY = "seven_day"
WEEKLY_OPUS = "seven_day_opus"
WEEKLY_SONNET = "seven_day_sonnet"
EXTRA_USAGE = "extra_usage"

# The two windows every plan has; always shown even when idle.
PRIMARY = (SESSION, WEEKLY)


class Level(IntEnum):
    """How close a meter is to its limit."""

    NORMAL = 0
    ELEVATED = 1
    CRITICAL = 2

    @classmethod
    def from_percent(cls, percent: float) -> "Level":
        if percent >= 90:
            return cls.CRITICAL
        if percent >= 70:
            return cls.ELEVATED
        return cls.NORMAL


@dataclass(frozen=True)
class Meter:
    """One usage-limit window, e.g. the rolling 5-hour session."""

    # Stable key, e.g. `five_hour`, `seven_day`, `seven_day_fable`.
    id: str
    # Short name, e.g. "Session" or "Weekly · Sonnet".
    title: str
    # One-line explanation of the window, e.g. "Rolling 5-hour window".
    detail: str
    # Short label shown in the bar, e.g. "5" or "W".
    glyph: str
    # Percent of the limit used, clamped to 0...100.
    percent: float
    # When the window resets (Unix seconds), if Claude reported it.
    resets_at: float | None
    # Position relative to other meters (lower comes first).
    sort_order: int
    # Claude's own reading of how close this limit is, when it gives one.
    severity: Level | None = None

    def __post_init__(self) -> None:
        percent = self.percent if math.isfinite(self.percent) else 0
        object.__setattr__(self, "percent", min(max(percent, 0.0), 100.0))

    @property
    def fraction(self) -> float:
        return self.percent / 100

    @property
    def level(self) -> Level:
        """Claude's severity when reported, otherwise derived from the percentage."""
        return self.severity if self.severity is not None else Level.from_percent(self.percent)

    @property
    def is_primary(self) -> bool:
        return self.id in PRIMARY

    @property
    def relevant(self) -> bool:
        """Secondary windows (per-model caps, extra usage) are only worth showing once in use."""
        return self.is_primary or self.percent > 0 or self.resets_at is not None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.name.lower() if self.severity is not None else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Meter":
        severity = data.get("severity")
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            detail=str(data.get("detail", "")),
            glyph=str(data["glyph"]),
            percent=float(data["percent"]),
            resets_at=None if data.get("resets_at") is None else float(data["resets_at"]),
            sort_order=int(data.get("sort_order", 0)),
            severity=Level[severity.upper()] if isinstance(severity, str) else None,
        )


@dataclass(frozen=True)
class Snapshot:
    """Everything fetched in one refresh."""

    # All meters Claude reported, sorted for display.
    meters: tuple[Meter, ...]
    fetched_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "meters", tuple(sorted(self.meters, key=lambda m: (m.sort_order, m.title))))

    def meter(self, meter_id: str) -> Meter | None:
        return next((m for m in self.meters if m.id == meter_id), None)

    @property
    def relevant_meters(self) -> list[Meter]:
        """The primary windows plus any secondary window in use."""
        return [m for m in self.meters if m.relevant]

    def next_reset(self, after: float) -> float | None:
        """The soonest reset that is still in the future."""
        resets = [m.resets_at for m in self.meters if m.resets_at is not None and m.resets_at > after]
        return min(resets) if resets else None

    def to_json(self) -> dict[str, Any]:
        return {"fetched_at": self.fetched_at, "meters": [m.to_json() for m in self.meters]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(meters=tuple(Meter.from_json(m) for m in data["meters"]), fetched_at=float(data["fetched_at"]))


class UsageError(Exception):
    """Why a refresh produced no numbers."""

    # Claude Code isn't installed, or isn't at the configured path.
    CLI_NOT_FOUND = "cli_not_found"
    # Claude Code is installed but not signed in to a Claude account.
    NOT_SIGNED_IN = "not_signed_in"
    # Claude Code ran but couldn't fetch the plan's usage this time.
    USAGE_UNAVAILABLE = "usage_unavailable"
    # Claude Code failed to run or didn't finish.
    CLI_FAILED = "cli_failed"
    INVALID_RESPONSE = "invalid_response"

    def __init__(self, kind: str, detail: str | None = None) -> None:
        super().__init__(kind, detail)
        self.kind = kind
        self.detail = detail

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UsageError) and (self.kind, self.detail) == (other.kind, other.detail)

    def __hash__(self) -> int:
        return hash((self.kind, self.detail))

    def __repr__(self) -> str:
        return f"UsageError({self.kind!r}, {self.detail!r})"

    @property
    def is_local(self) -> bool:
        """Problems fixed on this machine (installing or signing in to Claude Code). These are
        re-checked every minute so the module recovers soon after."""
        return self.kind in (self.CLI_NOT_FOUND, self.NOT_SIGNED_IN)

    @property
    def short(self) -> str:
        """A few words for the bar."""
        if self.kind == self.CLI_NOT_FOUND:
            return "not found"
        if self.kind == self.NOT_SIGNED_IN:
            return "sign in"
        return "error"

    def __str__(self) -> str:
        if self.kind == self.CLI_NOT_FOUND:
            return f"Claude Code isn't at {self.detail}." if self.detail else "Couldn't find Claude Code."
        if self.kind == self.NOT_SIGNED_IN:
            return "Claude Code isn't signed in. Run `claude` once and sign in."
        if self.kind == self.USAGE_UNAVAILABLE:
            return "Claude Code couldn't fetch your usage" + (f": {self.detail}" if self.detail else ".")
        if self.kind == self.INVALID_RESPONSE:
            return f"Couldn't read Claude Code's usage report. {self.detail or ''}".strip()
        return self.detail or "Claude Code failed."

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UsageError":
        return cls(str(data["kind"]), data.get("detail"))


# MARK: - Lenient JSON helpers
#
# The usage report is experimental and its numbers arrive as either integers or floats, so values
# are read leniently.


def _object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_iso8601(text: str) -> float | None:
    """Lenient ISO-8601 parsing for timestamps such as `2026-02-20T14:00:00.364238+00:00`.
    Timestamps without a zone are UTC."""
    text = text.strip()
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?", text
    )
    if not match:
        return None
    day, clock, digits, zone = match.groups()
    if zone in (None, "Z", "z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    try:
        parsed = datetime.fromisoformat(f"{day}T{clock}{zone}")
    except ValueError:
        return None
    return parsed.timestamp() + (float("0." + digits) if digits else 0.0)


def parse_timestamp(value: Any) -> float | None:
    """Accepts ISO-8601 strings as well as Unix timestamps in seconds or milliseconds."""
    if isinstance(value, str):
        return parse_iso8601(value)
    number = _number(value)
    if number is None:
        return None
    return number / 1000 if number > 100_000_000_000 else number


# MARK: - Reading the usage report


def slugify(text: str) -> str:
    slug = ""
    last_was_separator = False
    for char in text.lower():
        if char.isascii() and char.isalnum():
            slug += char
            last_was_separator = False
        elif not last_was_separator and slug:
            slug += "_"
            last_was_separator = True
    return slug.rstrip("_")


def short_model_name(name: str) -> str:
    """"Claude Fable" → "Fable": the brand prefix adds nothing in a status bar."""
    trimmed = name.strip()
    if trimmed.startswith("Claude ") and len(trimmed) > 7:
        return trimmed[7:]
    return trimmed


_KNOWN_NAMES = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "fable": "Fable",
    "mythos": "Mythos",
    "oauth_apps": "OAuth apps",
    "cowork": "Cowork",
}


def _prettify(suffix: str) -> str:
    words = [w for w in suffix.split("_") if w]
    if not words:
        return suffix
    return " ".join([words[0][:1].upper() + words[0][1:]] + words[1:])


def describe_window(
    key: str,
    percent: float,
    resets_at: float | None,
    severity: Level | None,
    display_name: str | None = None,
) -> Meter | None:
    """Builds a meter from a window key such as `seven_day_sonnet`."""
    if key == SESSION:
        return Meter(key, "Session", "Rolling 5-hour window", "5", percent, resets_at, 0, severity)
    if key == WEEKLY:
        return Meter(key, "Weekly", "All models · 7-day window", "W", percent, resets_at, 1, severity)

    if key.startswith(WEEKLY + "_"):
        is_session, suffix = False, key[len(WEEKLY) + 1 :]
    elif key.startswith(SESSION + "_"):
        is_session, suffix = True, key[len(SESSION) + 1 :]
    else:
        return None
    if not suffix:
        return None

    name = short_model_name(display_name or _KNOWN_NAMES.get(suffix) or _prettify(suffix))
    title = f"{'Session' if is_session else 'Weekly'} · {name}"
    detail = f"{name} only · {'5-hour' if is_session else '7-day'} window"
    sort_order = {"opus": 10, "sonnet": 11}.get(suffix, 5 if is_session else 20)
    return Meter(key, title, detail, name[:1].upper(), percent, resets_at, sort_order, severity)


def _severity(value: str | None) -> Level | None:
    """Claude grades each row itself; follow its reading where there is one."""
    value = (value or "").lower()
    if value in ("critical", "exceeded", "error"):
        return Level.CRITICAL
    if value in ("warning", "elevated", "high"):
        return Level.ELEVATED
    if value in ("normal", "ok", "low"):
        return Level.NORMAL
    return None


def meter_from_row(row: dict[str, Any]) -> Meter | None:
    percent = _number(row.get("percent"))
    if percent is None:
        percent = _number(row.get("utilization"))
    if percent is None:
        return None
    kind = (_string(row.get("kind")) or "").lower()
    group = (_string(row.get("group")) or "").lower()
    scope = _object(row.get("scope")) or {}
    scope_name = _string((_object(scope.get("model")) or {}).get("display_name")) or _string(
        (_object(scope.get("surface")) or {}).get("display_name")
    )

    if kind == "session" and scope_name is None:
        key = SESSION
    elif kind == "weekly_all":
        key = WEEKLY
    else:
        is_session = group == "session" or kind.startswith("session")
        # Scoped rows are named after their model or surface; otherwise after the kind itself.
        name = scope_name or kind
        if scope_name is None:
            for prefix in ("weekly_", "session_"):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
        slug = slugify(short_model_name(name))
        if not slug:
            return None
        key = f"{SESSION if is_session else WEEKLY}_{slug}"

    return describe_window(
        key,
        percent,
        parse_timestamp(row.get("resets_at")),
        _severity(_string(row.get("severity"))),
        display_name=scope_name,
    )


def extra_usage_meter(extra: dict[str, Any]) -> Meter | None:
    if extra.get("is_enabled") is not True:
        return None
    used = _number(extra.get("used_credits"))
    limit = _number(extra.get("monthly_limit"))
    if used is not None and limit is not None and limit > 0:
        percent = used / limit * 100
    else:
        percent = _number(extra.get("utilization"))
        if percent is None:
            return None
    return Meter(
        EXTRA_USAGE,
        "Extra usage",
        "Paid usage beyond your plan",
        "+",
        percent,
        parse_timestamp(extra.get("resets_at")),
        90,
    )


def meters_from_rows(rows: Iterable[Any], extra_usage: dict[str, Any] | None) -> list[Meter]:
    meters: list[Meter] = []
    seen: set[str] = set()
    for row in rows:
        meter = meter_from_row(row) if isinstance(row, dict) else None
        # The first row of a kind wins.
        if meter is None or meter.id in seen:
            continue
        seen.add(meter.id)
        meters.append(meter)
    extra = extra_usage_meter(extra_usage) if extra_usage else None
    if extra is not None:
        meters.append(extra)
    return meters


def meters_from_text(text: str) -> list[Meter]:
    """Parses lines such as "Current week (Sonnet only): 94% used · resets Wed 9am"."""
    meters: list[Meter] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Current ") or ":" not in line:
            continue
        colon = line.index(":")
        percent_sign = line.find("%", colon)
        if percent_sign < 0:
            continue
        try:
            percent = float(line[colon + 1 : percent_sign].strip())
        except ValueError:
            continue

        title = line[:colon]
        display_name = None
        if title == "Current session":
            key = SESSION
        elif title == "Current week (all models)":
            key = WEEKLY
        elif title.startswith("Current week (") and title.endswith(")"):
            name = title[len("Current week (") : -1]
            if name.endswith(" only"):
                name = name[: -len(" only")]
            display_name = short_model_name(name)
            key = f"{WEEKLY}_{slugify(display_name or name)}"
        else:
            continue
        meter = describe_window(key, percent, None, None, display_name=display_name)
        if meter is None or key in seen:
            continue
        seen.add(key)
        meters.append(meter)
    return meters


def _failure(text: str, stderr: str) -> UsageError:
    """Explains a run that produced no usage rows, using what Claude Code printed."""
    combined = (text + "\n" + stderr).lower()
    markers = ("not logged in", "please run /login", "run claude auth login", "login expired")
    if any(marker in combined for marker in markers):
        return UsageError(UsageError.NOT_SIGNED_IN)
    lines = (line.strip() for line in (text + "\n" + stderr).splitlines())
    # Skip the banner /usage prints before its rows.
    detail = next((line for line in lines if line and not line.startswith("You are currently using")), None)
    return UsageError(UsageError.USAGE_UNAVAILABLE, detail[:200] if detail else None)


def parse_stream_json(output: str, stderr: str = "", fetched_at: float | None = None) -> Snapshot:
    """Reads the output of `claude -p /usage --output-format stream-json --verbose`.

    Claude Code attaches a structured `usage_report` to the message that carries the `/usage`
    text. Its `rate_limits.limits[]` rows are the usage endpoint's, passed through as Claude sent
    them. Older Claude Code versions print only text ("Current session: 23% used · resets …"),
    which is used as a fallback without reset times.
    """
    fetched_at = time.time() if fetched_at is None else fetched_at
    report: dict[str, Any] | None = None
    texts: list[str] = []

    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        usage_report = _object(obj.get("usage_report"))
        if usage_report is not None:
            report = usage_report
        if obj.get("type") == "result" and _string(obj.get("result")):
            texts.append(_string(obj.get("result")) or "")
        if obj.get("type") == "assistant":
            content = (_object(obj.get("message")) or {}).get("content")
            if isinstance(content, list):
                texts += [t for t in (_string(c.get("text")) for c in content if isinstance(c, dict)) if t]
    text = "\n".join(texts)

    if report is not None:
        rate_limits = _object(report.get("rate_limits"))
        rows = rate_limits.get("limits") if rate_limits else None
        if not isinstance(rows, list):
            # Claude Code couldn't fetch the plan's usage this time.
            raise _failure(text, stderr)
        meters = meters_from_rows(rows, _object(rate_limits.get("extra_usage")) if rate_limits else None)
    else:
        meters = meters_from_text(text)
    if not meters:
        raise _failure(text, stderr)
    return Snapshot(tuple(meters), fetched_at)


# MARK: - Running Claude Code

# `--safe-mode` keeps the user's hooks, plugins and MCP servers from starting on every check, and
# `--no-session-persistence` keeps these runs out of the session history. Both are dropped if the
# installed Claude Code is too old to know them.
USAGE_ARGUMENTS = (
    "-p", "/usage",
    "--output-format", "stream-json", "--verbose",
    "--no-session-persistence",
    "--safe-mode",
)  # fmt: skip
OPTIONAL_ARGUMENTS = frozenset({"--no-session-persistence", "--safe-mode"})


def common_locations(home: Path) -> list[Path]:
    """Where `claude` is usually installed, checked after PATH."""
    return [
        home / ".local/bin/claude",
        home / ".claude/local/claude",
        Path("/usr/local/bin/claude"),
        home / ".npm-global/bin/claude",
        home / ".bun/bin/claude",
        home / ".volta/bin/claude",
        Path("/usr/bin/claude"),
    ]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def locate_claude(custom_path: str | None, search_path: str | None, home: Path | None = None) -> Path | None:
    """The configured path, then PATH, then common locations. Returns None if a configured path
    doesn't point at an executable."""
    home = Path.home() if home is None else home
    custom_path = (custom_path or "").strip()
    if custom_path:
        path = Path(os.path.expanduser(custom_path))
        return path if _is_executable(path) else None
    candidates = [Path(d) / "claude" for d in (search_path or "").split(":") if d]
    return next((p for p in candidates + common_locations(home) if _is_executable(p)), None)


def claude_environment(base: dict[str, str], executable: Path) -> dict[str, str]:
    """The environment Claude Code runs with: ours, with a PATH that also covers Claude Code's own
    directory (for Node, with npm installs) and the usual system directories."""
    directories = [str(executable.parent)] + base.get("PATH", "").split(":")
    directories += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin"]
    unique = list(dict.fromkeys(d for d in directories if d))
    return {**base, "PATH": ":".join(unique), "NO_COLOR": "1"}


def unknown_option(output: str) -> str | None:
    """The option named in an "unknown option '--x'" error from an older Claude Code."""
    match = re.search(r"unknown option '([^']*)'", output)
    return match.group(1) if match else None


@dataclass
class ProcessResult:
    status: int
    stdout: str
    stderr: str


def run_process(
    arguments: list[str], env: dict[str, str] | None, cwd: Path | None, timeout: float
) -> ProcessResult:
    """Runs a command to completion. Raises UsageError if it can't start or doesn't finish."""
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            # Its own process group, so helpers it starts can be stopped with it.
            start_new_session=True,
        )
    except OSError as error:
        raise UsageError(UsageError.CLI_FAILED, f"Couldn't start Claude Code at {arguments[0]} ({error.strerror}).")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.communicate(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        raise UsageError(UsageError.CLI_FAILED, f"Claude Code didn't answer within {int(timeout)} seconds.")
    return ProcessResult(
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def fetch_usage(executable: Path, env: dict[str, str] | None = None, cwd: Path | None = None, timeout: float = 90) -> Snapshot:
    """Blocking: runs Claude Code's `/usage` and reads its report."""
    arguments = list(USAGE_ARGUMENTS)
    while True:
        result = run_process([str(executable)] + arguments, env, cwd, timeout)
        option = unknown_option(result.stderr + result.stdout) if result.status != 0 else None
        if option in OPTIONAL_ARGUMENTS and option in arguments:
            arguments.remove(option)
            continue
        return parse_stream_json(result.stdout, result.stderr)


# MARK: - Formatting and scheduling


def fmt_percent(value: float) -> str:
    """"42%". Values between 0 and 1 show as "<1%" so light use is still visible."""
    if 0 < value < 1:
        return "<1%"
    return f"{int(math.floor(value + 0.5))}%"


def fmt_duration(seconds: float) -> str:
    """Compact duration such as "45m", "2h 14m" or "3d 4h"."""
    minutes = math.ceil(max(seconds, 0) / 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def reset_description(
    resets_at: float | None, now: float, time_format: str = "%H:%M", tz: timezone | None = None
) -> str:
    """"Resets in 2h 14m · 15:14" in local time (or `tz`)."""
    if resets_at is None:
        return "No active window"
    remaining = resets_at - now
    if remaining <= 0:
        return "Resetting now…"
    when = datetime.fromtimestamp(resets_at, tz).astimezone(tz)
    today = datetime.fromtimestamp(now, tz).astimezone(tz)
    if when.date() == today.date():
        day = ""
    elif remaining < 6 * 86400:
        day = when.strftime("%a ")
    else:
        day = f"{when.strftime('%b')} {when.day} "
    return f"Resets in {fmt_duration(remaining)} · {day}{when.strftime(time_format)}"


def fmt_age(fetched_at: float, now: float) -> str:
    """"just now", "5m ago", "2h ago"."""
    elapsed = now - fetched_at
    if elapsed < 45:
        return "just now"
    minutes = int(elapsed / 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


MINIMUM_DELAY = 15.0
MAXIMUM_BACKOFF = 3600.0
# How soon to re-check problems fixed on this machine (installing or signing in to Claude Code).
LOCAL_RETRY_DELAY = 60.0


def next_delay(
    interval: float, error: UsageError | None, consecutive_failures: int, next_reset: float | None, now: float
) -> float:
    """Seconds until the next refresh."""
    interval = max(interval, MINIMUM_DELAY)
    if error is None:
        # Refresh shortly after a window resets so the meter drops back without waiting.
        if next_reset is not None:
            until_reset = next_reset - now + 20
            if until_reset > 0:
                return max(min(interval, until_reset), MINIMUM_DELAY)
        return interval
    if error.is_local:
        return min(interval, LOCAL_RETRY_DELAY)
    # Exponential backoff when Claude Code can't get the numbers, which is usually Claude limiting
    # how often usage is checked.
    exponent = min(max(consecutive_failures - 1, 0), 8)
    return max(min(interval * 2**exponent, MAXIMUM_BACKOFF), interval)


def is_stale(snapshot: Snapshot, interval: float, now: float) -> bool:
    """Whether a snapshot is old enough to be shown dimmed."""
    return now - snapshot.fetched_at > max(interval * 3, 15 * 60)


# MARK: - Settings

STYLES = ("pie", "text", "bar", "icon")
_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


@dataclass(frozen=True)
class Settings:
    # [display]
    style: str = "pie"
    # "auto", or a comma-separated list of labels/ids such as "5,W,S".
    meters: tuple[str, ...] = ()
    separator: str = "  "
    prefix: str = ""
    show_label: bool = True
    show_percent: bool = True
    pie_glyphs: str = "○◔◑◕●"
    # The icon style's glyph. Empty shows the pie glyph of the limit closest to running out.
    icon: str = ""
    bar_width: int = 10
    bar_chars: str = "▰▱"
    # Empty means the bar's own foreground colour.
    color_normal: str = ""
    color_elevated: str = "#ff9500"
    color_critical: str = "#ff3b30"
    color_stale: str = "#888888"
    time_format: str = "%H:%M"
    # [claude]
    claude_path: str = ""
    interval: float = 300.0
    timeout: float = 90.0


_SECTIONS = {
    "display": (
        "style", "meters", "separator", "prefix", "show_label", "show_percent", "pie_glyphs", "icon",
        "bar_width", "bar_chars", "color_normal", "color_elevated", "color_critical", "color_stale",
        "time_format",
    ),
    "claude": ("path", "interval", "timeout"),
}  # fmt: skip


def _unquote(value: str) -> str:
    """Lets values keep leading or trailing spaces: `separator = " | "`."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_setting(name: str, raw: str) -> Any:
    """Raises ValueError with a reason when the value doesn't fit."""
    value = _unquote(raw.strip())
    if name == "style":
        value = value.lower()
        if value not in STYLES:
            raise ValueError(f"must be one of {', '.join(STYLES)}")
        return value
    if name == "meters":
        items = tuple(item.strip() for item in value.split(",") if item.strip())
        return () if [i.lower() for i in items] in ([], ["auto"]) else items
    if name in ("show_label", "show_percent"):
        lowered = value.lower()
        if lowered in ("1", "yes", "true", "on"):
            return True
        if lowered in ("0", "no", "false", "off"):
            return False
        raise ValueError("must be true or false")
    if name == "pie_glyphs":
        if len(value) < 2:
            raise ValueError("needs at least 2 characters, empty to full")
        return value
    if name == "bar_chars":
        if len(value) != 2:
            raise ValueError("needs exactly 2 characters: filled, then empty")
        return value
    if name == "bar_width":
        width = int(value)
        if not 1 <= width <= 50:
            raise ValueError("must be between 1 and 50")
        return width
    if name.startswith("color_"):
        if value and not _COLOR.fullmatch(value):
            raise ValueError("must be empty or a colour such as #ff9500")
        return value
    if name == "time_format":
        datetime.now().strftime(value)
        return value
    if name in ("interval", "timeout"):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("must be a positive number of seconds")
        return max(number, MINIMUM_DELAY) if name == "interval" else number
    return value


def load_settings(path: Path) -> tuple[Settings, list[str]]:
    """Reads the config file. A missing file means defaults; bad values fall back to their default
    with a warning."""
    defaults = Settings()
    warnings: list[str] = []
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as file:
            parser.read_file(file)
    except FileNotFoundError:
        return defaults, warnings
    except (OSError, configparser.Error) as error:
        return defaults, [f"{path}: {error}"]

    values: dict[str, Any] = {}
    for section in parser.sections():
        if section not in _SECTIONS:
            warnings.append(f"{path}: unknown section [{section}]")
            continue
        for key, raw in parser.items(section):
            if key not in _SECTIONS[section]:
                warnings.append(f"{path}: unknown setting {key} in [{section}]")
                continue
            name = "claude_path" if (section, key) == ("claude", "path") else key
            try:
                values[name] = _parse_setting(name, raw)
            except ValueError as error:
                warnings.append(f"{path}: {key} = {raw!r} {error}; using the default")
    return replace(defaults, **values), warnings


# MARK: - Rendering


def colorize(text: str, color: str) -> str:
    return f"%{{F{color}}}{text}%{{F-}}" if color else text


def _level_color(level: Level, settings: Settings) -> str:
    return {
        Level.NORMAL: settings.color_normal,
        Level.ELEVATED: settings.color_elevated,
        Level.CRITICAL: settings.color_critical,
    }[level]


def selected_meters(snapshot: Snapshot, settings: Settings) -> list[Meter]:
    """The meters to show: those in use, or the ones the `meters` setting names."""
    if settings.meters:
        wanted = {m.upper() for m in settings.meters}
        chosen = [m for m in snapshot.meters if m.glyph.upper() in wanted or m.id.upper() in wanted]
        if chosen:
            return chosen
    return snapshot.relevant_meters


def pie_glyph(fraction: float, settings: Settings) -> str:
    glyphs = settings.pie_glyphs
    return glyphs[int(fraction * (len(glyphs) - 1) + 0.5)]


def most_constrained(meters: list[Meter]) -> Meter | None:
    """The meter closest to its limit, going by Claude's reading first."""
    return max(meters, key=lambda m: (m.level, m.percent), default=None)


def render_icon(meters: list[Meter], settings: Settings) -> tuple[str, Level]:
    """The icon style: one glyph for every limit, at the level of the closest one."""
    worst = most_constrained(meters)
    if worst is None:
        return settings.icon or settings.pie_glyphs[0], Level.NORMAL
    return settings.icon or pie_glyph(worst.fraction, settings), worst.level


def render_meter(meter: Meter, settings: Settings) -> str:
    label = meter.glyph if settings.show_label else ""
    percent = fmt_percent(meter.percent) if settings.show_percent else ""
    if settings.style == "pie":
        parts = [pie_glyph(meter.fraction, settings), label, percent]
    elif settings.style == "bar":
        filled = int(meter.fraction * settings.bar_width + 0.5)
        bar = settings.bar_chars[0] * filled + settings.bar_chars[1] * (settings.bar_width - filled)
        parts = [label, bar, percent]
    else:
        # Text with neither label nor percent would be empty; keep the percent.
        parts = [label, percent] if label or percent else [fmt_percent(meter.percent)]
    return " ".join(p for p in parts if p)


def render(snapshot: Snapshot | None, error: UsageError | None, settings: Settings, now: float) -> str:
    """The line polybar shows."""
    if snapshot is None:
        if settings.style == "icon":
            # Too small for a message; the notification explains.
            return colorize(settings.prefix + render_icon([], settings)[0], settings.color_stale)
        message = error.short if error is not None else "…"
        return colorize(f"{settings.prefix}Claude: {message}", settings.color_stale)
    meters = selected_meters(snapshot, settings)
    if settings.style == "icon":
        icon, level = render_icon(meters, settings)
        stale = is_stale(snapshot, settings.interval, now)
        return colorize(settings.prefix + icon, settings.color_stale if stale else _level_color(level, settings))
    if is_stale(snapshot, settings.interval, now):
        return colorize(settings.prefix + settings.separator.join(render_meter(m, settings) for m in meters), settings.color_stale)
    segments = [colorize(render_meter(m, settings), _level_color(m.level, settings)) for m in meters]
    return settings.prefix + settings.separator.join(segments)


def details(state: "State", settings: Settings, now: float) -> str:
    """Every limit with its reset time, for --notify and --details."""
    snapshot = state.snapshot
    lines: list[str] = []
    if snapshot is not None:
        for meter in snapshot.relevant_meters:
            reset = reset_description(meter.resets_at, now, settings.time_format)
            lines.append(f"{meter.title}: {fmt_percent(meter.percent)} · {reset}")
        updated = f"Updated {fmt_age(snapshot.fetched_at, now)}"
        if is_stale(snapshot, settings.interval, now):
            updated += " (out of date)"
        lines.append(updated)
    else:
        lines.append("No usage fetched yet.")
    if state.error is not None:
        lines.append(f"Last check failed: {state.error}")
    return "\n".join(lines)


# MARK: - Shared state
#
# Polybar starts one module per bar (often one bar per monitor). They share the last result through
# a cache file and take turns fetching under a lock, so extra bars don't mean extra checks.


@dataclass
class State:
    snapshot: Snapshot | None = None
    error: UsageError | None = None
    # Failed attempts in a row, including the last one.
    failures: int = 0
    # When Claude Code was last asked, successfully or not.
    checked_at: float | None = None

    def due_at(self, interval: float) -> float:
        """When the next refresh should happen."""
        if self.checked_at is None:
            return 0.0
        next_reset = self.snapshot.next_reset(self.checked_at) if self.snapshot else None
        return self.checked_at + next_delay(interval, self.error, self.failures, next_reset, self.checked_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_json() if self.snapshot else None,
            "error": self.error.to_json() if self.error else None,
            "failures": self.failures,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "State":
        return cls(
            snapshot=Snapshot.from_json(data["snapshot"]) if data.get("snapshot") else None,
            error=UsageError.from_json(data["error"]) if data.get("error") else None,
            failures=int(data.get("failures", 0)),
            checked_at=None if data.get("checked_at") is None else float(data["checked_at"]),
        )


@dataclass
class Paths:
    config: Path
    cache: Path

    @classmethod
    def default(cls, env: dict[str, str] | None = None) -> "Paths":
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or Path.home())
        config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        cache_home = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
        return cls(config=config_home / APP_NAME / "config.ini", cache=cache_home / APP_NAME)

    @property
    def state_file(self) -> Path:
        return self.cache / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.cache / "fetch.lock"

    @property
    def workdir(self) -> Path:
        """An empty folder to run Claude Code in, so no project's settings or CLAUDE.md apply."""
        return self.cache / "workdir"


def read_state(paths: Paths) -> State:
    try:
        return State.from_json(json.loads(paths.state_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return State()


def write_state(paths: Paths, state: State) -> None:
    paths.cache.mkdir(parents=True, exist_ok=True)
    temporary = paths.state_file.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state.to_json()), encoding="utf-8")
    os.replace(temporary, paths.state_file)


def check(settings: Settings, env: dict[str, str] | None = None, cwd: Path | None = None) -> Snapshot:
    """Finds Claude Code and fetches usage once."""
    env = dict(os.environ) if env is None else env
    executable = locate_claude(settings.claude_path, env.get("PATH"))
    if executable is None:
        raise UsageError(UsageError.CLI_NOT_FOUND, settings.claude_path.strip() or None)
    return fetch_usage(executable, claude_environment(env, executable), cwd, settings.timeout)


def refresh(paths: Paths, settings: Settings, force: bool, now: float | None = None, fetch=None) -> State:
    """Fetches when due (or when forced) and records the result. Another bar that fetched while
    this one waited for the lock counts, unless this refresh was forced."""
    fetch = fetch or (lambda: check(settings, cwd=paths.workdir))
    paths.cache.mkdir(parents=True, exist_ok=True)
    paths.workdir.mkdir(parents=True, exist_ok=True)
    with open(paths.lock_file, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(paths)
        started = time.time() if now is None else now
        if not force and state.due_at(settings.interval) > started:
            return state
        try:
            state = State(fetch(), None, 0, started)
        except UsageError as error:
            state = State(state.snapshot, error, state.failures + 1, started)
        except Exception as error:  # Never let one bad run stop the module.
            state = State(state.snapshot, UsageError(UsageError.CLI_FAILED, str(error)), state.failures + 1, started)
        write_state(paths, state)
        return state


# MARK: - Entry points


class Output:
    """Prints lines for polybar, skipping repeats."""

    def __init__(self) -> None:
        self.last: str | None = None

    def show(self, line: str) -> None:
        if line != self.last:
            print(line, flush=True)
            self.last = line


class Warnings:
    """Prints config warnings to stderr (polybar's log) once until they change."""

    def __init__(self) -> None:
        self.last: list[str] = []

    def report(self, warnings: list[str]) -> None:
        if warnings != self.last:
            for warning in warnings:
                print(f"{APP_NAME}: {warning}", file=sys.stderr, flush=True)
            self.last = warnings


# Longest wait between re-renders, so stale dimming and other bars' fetches show up on time.
TICK = 60.0


def run_tail(paths: Paths, style: str | None) -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    output = Output()
    warnings = Warnings()
    force = False
    while True:
        settings, problems = load_settings(paths.config)
        warnings.report(problems)
        if style:
            settings = replace(settings, style=style)
        state = refresh(paths, settings, force)
        now = time.time()
        output.show(render(state.snapshot, state.error, settings, now))

        timeout = min(TICK, max(state.due_at(settings.interval) - now, 1.0))
        force = signal.sigtimedwait([signal.SIGUSR1], timeout) is not None


def notify(text: str) -> None:
    notify_send = shutil.which("notify-send")
    if notify_send is None:
        print(text)
        print(f"{APP_NAME}: notify-send isn't installed.", file=sys.stderr)
        return
    subprocess.run([notify_send, "--app-name=Claude Usage", "Claude usage", text], check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Your Claude plan limits in polybar.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="fetch once, print one line and exit")
    mode.add_argument("--notify", action="store_true", help="show every limit with notify-send (no fetch)")
    mode.add_argument("--details", action="store_true", help="print every limit (no fetch)")
    parser.add_argument("--style", choices=STYLES, help="override the style setting")
    parser.add_argument("--config", type=Path, help="config file (default: ~/.config/polybar-claude-usage/config.ini)")
    args = parser.parse_args(argv)

    paths = Paths.default()
    if args.config:
        paths.config = args.config
    try:
        if args.once:
            settings, problems = load_settings(paths.config)
            Warnings().report(problems)
            if args.style:
                settings = replace(settings, style=args.style)
            state = refresh(paths, settings, force=True)
            if state.error is not None:
                print(f"{APP_NAME}: {state.error}", file=sys.stderr)
            print(render(state.snapshot, state.error, settings, time.time()), flush=True)
        elif args.notify or args.details:
            settings, _ = load_settings(paths.config)
            text = details(read_state(paths), settings, time.time())
            notify(text) if args.notify else print(text)
        else:
            run_tail(paths, args.style)
    except BrokenPipeError:
        # Polybar closed the pipe. Point stdout at /dev/null so the exit flush doesn't complain.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
