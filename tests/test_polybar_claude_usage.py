import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import polybar_claude_usage as pcu  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A `claude -p /usage --output-format stream-json --verbose` run, trimmed to the lines that matter.
STREAM_JSON = "\n".join(
    [
        '{"type":"system","subtype":"init","session_id":"abc","tools":[],"model":"claude-opus-5-5"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"You are currently using your subscription to power your Claude Code usage\\n\\nCurrent session: 23% used · resets 3:14pm"}]},"usage_report":{"session":{"total_cost_usd":0},"rate_limits":{"limits":['
        '{"kind":"session","group":"session","percent":23,"resets_at":"2026-10-04T15:14:00.364238+00:00","severity":"normal","is_active":true},'
        '{"kind":"weekly_all","group":"weekly","percent":71.4,"resets_at":"2026-10-07T09:00:00Z","severity":"warning","is_active":false},'
        '{"kind":"weekly_scoped","group":"weekly","percent":94,"resets_at":"2026-10-07T09:00:00Z","scope":{"model":{"display_name":"Sonnet"}},"severity":"critical","is_active":false},'
        '{"kind":"weekly_scoped","group":"weekly","percent":8,"resets_at":"2026-10-07T09:00:00Z","scope":{"model":{"display_name":"Claude Fable"}},"severity":"normal","is_active":false},'
        '{"kind":"weekly_scoped","group":"weekly","percent":0,"resets_at":null,"scope":{"model":{"display_name":"Opus"}},"severity":"normal","is_active":false}],'
        '"extra_usage":{"is_enabled":true,"monthly_limit":5000,"used_credits":1250,"utilization":null,"currency":"USD"}}}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"You are currently using your subscription to power your Claude Code usage\\n\\nCurrent session: 23% used · resets 3:14pm","session_id":"abc"}',
    ]
)

ENVIRONMENT = {"PATH": "/usr/bin:/bin"}


def meter(glyph="5", percent=0.0, severity=None, meter_id=None, resets_at=None, sort_order=0):
    return pcu.Meter(meter_id or glyph, glyph, "", glyph, percent, resets_at, sort_order, severity)


class ReportTests(unittest.TestCase):
    def test_parses_structured_report(self):
        snapshot = pcu.parse_stream_json(STREAM_JSON, fetched_at=0)
        self.assertEqual(
            [m.id for m in snapshot.meters],
            ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_fable", "extra_usage"],
        )

        session = snapshot.meter(pcu.SESSION)
        self.assertEqual((session.title, session.glyph, session.percent), ("Session", "5", 23))
        self.assertAlmostEqual(session.resets_at, 1_791_126_840.364238, places=3)

        weekly = snapshot.meter(pcu.WEEKLY)
        self.assertEqual((weekly.glyph, weekly.percent), ("W", 71.4))
        # Claude's own severity wins over the local thresholds.
        self.assertEqual(weekly.level, pcu.Level.ELEVATED)

        sonnet = snapshot.meter(pcu.WEEKLY_SONNET)
        self.assertEqual(sonnet.title, "Weekly · Sonnet")
        self.assertEqual(sonnet.level, pcu.Level.CRITICAL)

        fable = snapshot.meter("seven_day_fable")
        self.assertEqual((fable.title, fable.glyph), ("Weekly · Fable", "F"))

        # Idle per-model limits are reported but not shown.
        self.assertFalse(snapshot.meter(pcu.WEEKLY_OPUS).relevant)
        self.assertEqual(snapshot.meter(pcu.EXTRA_USAGE).percent, 25)

    def test_parses_real_claude_code_output(self):
        snapshot = pcu.parse_stream_json((FIXTURES / "usage-2.1.292.jsonl").read_text(encoding="utf-8"))
        self.assertEqual([m.glyph for m in snapshot.relevant_meters], ["5", "W", "F"])
        self.assertEqual(snapshot.meter(pcu.SESSION).percent, 32)
        self.assertIsNone(snapshot.meter(pcu.EXTRA_USAGE))

    def test_severity_overrides_thresholds(self):
        line = '{"usage_report":{"rate_limits":{"limits":[{"kind":"session","group":"session","percent":40,"resets_at":null,"severity":"critical"}]}}}'
        self.assertEqual(pcu.parse_stream_json(line).meter(pcu.SESSION).level, pcu.Level.CRITICAL)
        unknown = '{"usage_report":{"rate_limits":{"limits":[{"kind":"session","group":"session","percent":95,"resets_at":null,"severity":"mystery"}]}}}'
        self.assertEqual(pcu.parse_stream_json(unknown).meter(pcu.SESSION).level, pcu.Level.CRITICAL)

    def test_unknown_kinds_and_surfaces(self):
        line = (
            '{"usage_report":{"rate_limits":{"limits":['
            '{"kind":"weekly_scoped","group":"weekly","percent":12,"resets_at":null,"scope":{"surface":{"display_name":"Cowork"}},"severity":"normal"},'
            '{"kind":"session_burst","group":"session","percent":3,"resets_at":null,"severity":"normal"},'
            '{"kind":"weekly_all","group":"weekly","percent":50,"resets_at":null,"severity":"normal"},'
            '{"kind":"weekly_all","group":"weekly","percent":99,"resets_at":null,"severity":"critical"}'
            "]}}}"
        )
        snapshot = pcu.parse_stream_json(line)
        self.assertEqual(snapshot.meter("seven_day_cowork").title, "Weekly · Cowork")
        self.assertEqual(snapshot.meter("five_hour_burst").title, "Session · Burst")
        # The first row of a kind wins.
        self.assertEqual(snapshot.meter(pcu.WEEKLY).percent, 50)

    def test_falls_back_to_text(self):
        line = (
            '{"type":"result","is_error":false,"result":"You are currently using your subscription to power your Claude Code usage'
            "\\n\\nCurrent session: 23% used · resets 3:14pm (Europe/London)\\nCurrent week (all models): 49% used · resets Oct 7, 9am"
            '\\nCurrent week (Sonnet only): 3% used\\nCurrent week (Claude Fable): 12% used"}'
        )
        snapshot = pcu.parse_stream_json(line)
        self.assertEqual([m.id for m in snapshot.meters], ["five_hour", "seven_day", "seven_day_sonnet", "seven_day_fable"])
        self.assertEqual(snapshot.meter(pcu.WEEKLY).percent, 49)
        self.assertEqual(snapshot.meter("seven_day_fable").title, "Weekly · Fable")
        self.assertIsNone(snapshot.meter(pcu.SESSION).resets_at)

    def test_missing_rows_explain_why(self):
        unavailable = (
            '{"usage_report":{"rate_limits":null}}\n'
            '{"type":"result","result":"You are currently using your subscription to power your Claude Code usage\\n\\nUsage data is temporarily unavailable"}'
        )
        with self.assertRaises(pcu.UsageError) as raised:
            pcu.parse_stream_json(unavailable)
        self.assertEqual(raised.exception, pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE, "Usage data is temporarily unavailable"))

        with self.assertRaises(pcu.UsageError) as raised:
            pcu.parse_stream_json('{"type":"result","is_error":true,"result":"Not logged in · Please run /login"}')
        self.assertEqual(raised.exception.kind, pcu.UsageError.NOT_SIGNED_IN)

        with self.assertRaises(pcu.UsageError) as raised:
            pcu.parse_stream_json("", stderr="boom")
        self.assertEqual(raised.exception, pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE, "boom"))

    def test_slugify(self):
        self.assertEqual(pcu.slugify("Fable"), "fable")
        self.assertEqual(pcu.slugify("Claude Opus 5.5"), "claude_opus_5_5")
        self.assertEqual(pcu.slugify("  --  "), "")

    def test_parses_timestamps(self):
        base = 1_771_596_000.0  # 2026-02-20T14:00:00Z
        self.assertEqual(pcu.parse_iso8601("2026-02-20T14:00:00Z"), base)
        self.assertAlmostEqual(pcu.parse_iso8601("2026-02-20T14:00:00.5Z"), base + 0.5)
        self.assertAlmostEqual(pcu.parse_iso8601("2026-02-20T14:00:00.943648+00:00"), base + 0.943648)
        self.assertEqual(pcu.parse_iso8601("2026-02-20T16:00:00+02:00"), base)
        self.assertEqual(pcu.parse_iso8601("2026-02-20 14:00:00"), base)
        self.assertEqual(pcu.parse_iso8601("2026-02-20T14:00:00"), base)
        self.assertIsNone(pcu.parse_iso8601("not a date"))
        self.assertIsNone(pcu.parse_iso8601(""))
        self.assertEqual(pcu.parse_timestamp(1_771_596_000), base)
        self.assertEqual(pcu.parse_timestamp(1_771_596_000_000.0), base)
        self.assertIsNone(pcu.parse_timestamp(None))


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pcu-tests-"))

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.directory)], check=False)

    def fake_claude(self, body: str, directory: Path | None = None) -> Path:
        """Writes an executable shell script standing in for `claude`."""
        path = (directory or self.directory) / "claude"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_output(self) -> Path:
        output = self.directory / "output.jsonl"
        output.write_text(STREAM_JSON, encoding="utf-8")
        return output

    def test_runs_claude_and_parses_report(self):
        output = self.write_output()
        arguments = self.directory / "arguments.txt"
        claude = self.fake_claude(f"echo \"$@\" >> '{arguments}'\ncat '{output}'")

        snapshot = pcu.fetch_usage(claude, ENVIRONMENT, timeout=20)
        self.assertEqual(snapshot.meter(pcu.SESSION).percent, 23)
        self.assertEqual(
            arguments.read_text().strip(),
            "-p /usage --output-format stream-json --verbose --no-session-persistence --safe-mode",
        )

    def test_drops_options_an_older_claude_does_not_know(self):
        output = self.write_output()
        claude = self.fake_claude(
            'for argument in "$@"; do\n'
            '  if [ "$argument" = "--safe-mode" ]; then\n'
            "    echo \"error: unknown option '--safe-mode'\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
            f"cat '{output}'"
        )
        self.assertEqual(pcu.fetch_usage(claude, ENVIRONMENT, timeout=20).meter(pcu.WEEKLY).percent, 71.4)

    def test_times_out(self):
        claude = self.fake_claude("sleep 30")
        started = time.monotonic()
        with self.assertRaises(pcu.UsageError) as raised:
            pcu.fetch_usage(claude, ENVIRONMENT, timeout=1)
        self.assertEqual(raised.exception.kind, pcu.UsageError.CLI_FAILED)
        self.assertLess(time.monotonic() - started, 10)

    def test_captures_large_output(self):
        # More than a pipe buffer's worth of output must not deadlock the runner.
        claude = self.fake_claude(
            "i=0\n"
            "while [ $i -lt 3000 ]; do\n"
            '  echo \'{"type":"system","subtype":"padding","text":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}\'\n'
            "  i=$((i + 1))\n"
            "done\n"
            'echo \'{"usage_report":{"rate_limits":{"limits":[{"kind":"session","group":"session","percent":5,"resets_at":null,"severity":"normal"}]}}}\''
        )
        self.assertEqual(pcu.fetch_usage(claude, ENVIRONMENT, timeout=30).meter(pcu.SESSION).percent, 5)

    def test_locates_claude(self):
        claude = self.fake_claude("exit 0")
        home = self.directory / "home"

        # A configured path wins, and a wrong one is reported rather than replaced.
        self.assertEqual(pcu.locate_claude(str(claude), None, home), claude)
        self.assertIsNone(pcu.locate_claude(str(self.directory / "nope"), None, home))

        # Otherwise PATH is searched.
        self.assertEqual(pcu.locate_claude("", f"/nonexistent:{self.directory}", home), claude)

        # Then the usual install locations, such as ~/.local/bin.
        local = self.fake_claude("exit 0", home / ".local/bin")
        self.assertEqual(pcu.locate_claude(None, "/nonexistent", home), local)

    def test_check_reports_missing_claude(self):
        settings = pcu.Settings(claude_path="/no/such/claude")
        with self.assertRaises(pcu.UsageError) as raised:
            pcu.check(settings, env={})
        self.assertEqual(raised.exception, pcu.UsageError(pcu.UsageError.CLI_NOT_FOUND, "/no/such/claude"))

    def test_environment_includes_install_directories(self):
        env = pcu.claude_environment({"PATH": "/usr/bin:/bin", "HOME": "/home/me"}, Path("/home/me/.local/bin/claude"))
        path = env["PATH"].split(":")
        self.assertEqual(path[0], "/home/me/.local/bin")
        self.assertIn("/usr/local/bin", path)
        self.assertEqual(path.count("/usr/bin"), 1)
        self.assertEqual((env["HOME"], env["NO_COLOR"]), ("/home/me", "1"))

    def test_unknown_option_parsing(self):
        self.assertEqual(pcu.unknown_option("error: unknown option '--safe-mode'\n"), "--safe-mode")
        self.assertIsNone(pcu.unknown_option("error: something else"))


class FormattingTests(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(pcu.fmt_percent(0), "0%")
        self.assertEqual(pcu.fmt_percent(0.4), "<1%")
        self.assertEqual(pcu.fmt_percent(41.6), "42%")
        self.assertEqual(pcu.fmt_percent(42.5), "43%")
        self.assertEqual(pcu.fmt_percent(100), "100%")

    def test_duration(self):
        self.assertEqual(pcu.fmt_duration(0), "<1m")
        self.assertEqual(pcu.fmt_duration(59), "1m")
        self.assertEqual(pcu.fmt_duration(45 * 60), "45m")
        self.assertEqual(pcu.fmt_duration(2 * 3600 + 14 * 60), "2h 14m")
        self.assertEqual(pcu.fmt_duration(3 * 3600), "3h")
        self.assertEqual(pcu.fmt_duration(3 * 86400 + 4 * 3600), "3d 4h")

    def test_reset_description(self):
        now = pcu.parse_iso8601("2026-10-04T13:00:00Z")  # a Sunday
        utc = timezone.utc
        self.assertEqual(pcu.reset_description(now + 2 * 3600 + 14 * 60, now, tz=utc), "Resets in 2h 14m · 15:14")
        self.assertEqual(pcu.reset_description(now + 2 * 86400, now, tz=utc), "Resets in 2d · Tue 13:00")
        self.assertEqual(pcu.reset_description(now + 8 * 86400, now, tz=utc), "Resets in 8d · Oct 12 13:00")
        self.assertEqual(pcu.reset_description(None, now), "No active window")
        self.assertEqual(pcu.reset_description(now - 5, now), "Resetting now…")

    def test_age(self):
        now = 1_000_000.0
        self.assertEqual(pcu.fmt_age(now, now), "just now")
        self.assertEqual(pcu.fmt_age(now - 300, now), "5m ago")
        self.assertEqual(pcu.fmt_age(now - 7200, now), "2h ago")


class ScheduleTests(unittest.TestCase):
    now = 1_000_000.0

    def test_success_uses_interval_or_next_reset(self):
        self.assertEqual(pcu.next_delay(300, None, 0, None, self.now), 300)
        self.assertEqual(pcu.next_delay(300, None, 0, self.now + 100, self.now), 120)
        self.assertEqual(pcu.next_delay(300, None, 0, self.now + 3600, self.now), 300)

    def test_local_errors_retry_quickly(self):
        self.assertEqual(pcu.next_delay(600, pcu.UsageError(pcu.UsageError.NOT_SIGNED_IN), 5, None, self.now), 60)
        self.assertEqual(pcu.next_delay(600, pcu.UsageError(pcu.UsageError.CLI_NOT_FOUND), 1, None, self.now), 60)

    def test_server_errors_back_off(self):
        error = pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE)
        self.assertEqual(pcu.next_delay(300, error, 1, None, self.now), 300)
        self.assertEqual(pcu.next_delay(300, error, 2, None, self.now), 600)
        self.assertEqual(pcu.next_delay(300, error, 3, None, self.now), 1200)
        self.assertEqual(pcu.next_delay(300, error, 30, None, self.now), 3600)

    def test_staleness(self):
        snapshot = pcu.Snapshot((), self.now)
        self.assertFalse(pcu.is_stale(snapshot, 300, self.now + 600))
        self.assertTrue(pcu.is_stale(snapshot, 300, self.now + 1000))


class RenderTests(unittest.TestCase):
    now = 1_000_000.0

    def setUp(self):
        self.snapshot = pcu.Snapshot(
            (
                pcu.describe_window(pcu.SESSION, 29, None, None),
                pcu.describe_window(pcu.WEEKLY, 75, None, None),
                pcu.describe_window("seven_day_sonnet", 95, None, None),
                pcu.describe_window(pcu.WEEKLY_OPUS, 0, None, None),
            ),
            self.now,
        )

    def render(self, **settings):
        return pcu.render(self.snapshot, None, pcu.Settings(**settings), self.now)

    def test_pie(self):
        self.assertEqual(self.render(), "◔ 5 29%  %{F#ff9500}◕ W 75%%{F-}  %{F#ff3b30}● S 95%%{F-}")

    def test_text(self):
        self.assertEqual(self.render(style="text", separator=" · "), "5 29% · %{F#ff9500}W 75%%{F-} · %{F#ff3b30}S 95%%{F-}")
        self.assertEqual(self.render(style="text", show_label=False, show_percent=False, meters=("5",)), "29%")

    def test_bar(self):
        self.assertEqual(
            self.render(style="bar", bar_width=4, color_elevated="", color_critical=""),
            "5 ▰▱▱▱ 29%  W ▰▰▰▱ 75%  S ▰▰▰▰ 95%",
        )

    def test_icon(self):
        # The Sonnet limit is closest, so the icon is full and red.
        self.assertEqual(self.render(style="icon"), "%{F#ff3b30}●%{F-}")
        self.assertEqual(self.render(style="icon", meters=("5",)), "◔")
        self.assertEqual(self.render(style="icon", icon="C", prefix="x"), "%{F#ff3b30}xC%{F-}")
        # Claude's reading beats a higher percentage.
        snapshot = pcu.Snapshot(
            (
                pcu.describe_window(pcu.SESSION, 60, None, pcu.Level.CRITICAL),
                pcu.describe_window(pcu.WEEKLY, 80, None, pcu.Level.NORMAL),
            ),
            self.now,
        )
        self.assertEqual(pcu.render(snapshot, None, pcu.Settings(style="icon"), self.now), "%{F#ff3b30}◑%{F-}")
        # Stale and missing numbers are grey; the notification explains.
        self.assertEqual(pcu.render(self.snapshot, None, pcu.Settings(style="icon"), self.now + 3600), "%{F#888888}●%{F-}")
        error = pcu.UsageError(pcu.UsageError.NOT_SIGNED_IN)
        self.assertEqual(pcu.render(None, error, pcu.Settings(style="icon"), self.now), "%{F#888888}○%{F-}")

    def test_normal_color_and_options(self):
        line = self.render(style="text", color_normal="#75d85a", show_label=False, meters=("5",), prefix="C ")
        self.assertEqual(line, "C %{F#75d85a}29%%{F-}")

    def test_meter_filter(self):
        self.assertEqual(self.render(style="text", meters=("w", "seven_day_sonnet"), color_elevated="", color_critical=""), "W 75%  S 95%")
        # Opus is idle so auto hides it, but naming it shows it.
        self.assertEqual(self.render(style="text", meters=("O",)), "O 0%")
        # Names that match nothing fall back to the usual meters.
        self.assertEqual(self.render(style="text", meters=("X",), color_elevated="", color_critical=""), "5 29%  W 75%  S 95%")

    def test_stale_is_dimmed(self):
        line = pcu.render(self.snapshot, None, pcu.Settings(style="text"), self.now + 3600)
        self.assertEqual(line, "%{F#888888}5 29%  W 75%  S 95%%{F-}")

    def test_errors_without_numbers(self):
        settings = pcu.Settings()
        self.assertEqual(pcu.render(None, None, settings, self.now), "%{F#888888}Claude: …%{F-}")
        self.assertEqual(
            pcu.render(None, pcu.UsageError(pcu.UsageError.NOT_SIGNED_IN), settings, self.now), "%{F#888888}Claude: sign in%{F-}"
        )
        self.assertEqual(
            pcu.render(None, pcu.UsageError(pcu.UsageError.CLI_NOT_FOUND), settings, self.now), "%{F#888888}Claude: not found%{F-}"
        )
        # Known numbers keep showing when a later check fails.
        error = pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE)
        self.assertTrue(pcu.render(self.snapshot, error, settings, self.now).startswith("◔ 5 29%"))

    def test_details(self):
        state = pcu.State(self.snapshot, pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE, "slow down"), 1, self.now)
        text = pcu.details(state, pcu.Settings(), self.now + 300)
        self.assertEqual(
            text.splitlines(),
            [
                "Session: 29% · No active window",
                "Weekly: 75% · No active window",
                "Weekly · Sonnet: 95% · No active window",
                "Updated 5m ago",
                "Last check failed: Claude Code couldn't fetch your usage: slow down",
            ],
        )
        self.assertEqual(pcu.details(pcu.State(), pcu.Settings(), self.now), "No usage fetched yet.")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pcu-settings-"))
        self.path = self.directory / "config.ini"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.directory)], check=False)

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return pcu.load_settings(self.path)

    def test_missing_file_means_defaults(self):
        self.assertEqual(pcu.load_settings(self.directory / "none.ini"), (pcu.Settings(), []))

    def test_reads_values(self):
        settings, warnings = self.load(
            "[display]\nstyle = Bar\nmeters = 5, W\nseparator = \" | \"\nshow_label = no\n"
            "bar_width = 6\ncolor_normal = #75d85a\n[claude]\npath = ~/bin/claude\ninterval = 5\n"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(settings.style, "bar")
        self.assertEqual(settings.meters, ("5", "W"))
        self.assertEqual(settings.separator, " | ")
        self.assertFalse(settings.show_label)
        self.assertEqual(settings.bar_width, 6)
        self.assertEqual(settings.color_normal, "#75d85a")
        self.assertEqual(settings.claude_path, "~/bin/claude")
        # Never more often than every 15 seconds.
        self.assertEqual(settings.interval, 15)

    def test_auto_meters(self):
        settings, _ = self.load("[display]\nmeters = auto\n")
        self.assertEqual(settings.meters, ())

    def test_bad_values_fall_back_with_warnings(self):
        settings, warnings = self.load(
            "[display]\nstyle = rings\nbar_width = lots\ncolor_critical = red\nbar_chars = x\nwhat = 1\n"
            "[claude]\ninterval = -1\n[extra]\n"
        )
        self.assertEqual(settings, pcu.Settings())
        self.assertEqual(len(warnings), 7, warnings)

    def test_unparseable_file(self):
        settings, warnings = self.load("style = pie\n")
        self.assertEqual(settings, pcu.Settings())
        self.assertEqual(len(warnings), 1)

    def test_example_config_matches_defaults(self):
        # Every setting in the example is commented out at its default value.
        text = (ROOT / "config.example.ini").read_text(encoding="utf-8")
        uncommented = "\n".join(line[2:] if re.match(r"# [a-z_]+ =", line) else line for line in text.splitlines())
        self.path.write_text(uncommented, encoding="utf-8")
        settings, warnings = pcu.load_settings(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(settings, pcu.Settings())


class StateTests(unittest.TestCase):
    now = 1_000_000.0

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pcu-state-"))
        self.paths = pcu.Paths(config=self.directory / "config.ini", cache=self.directory / "cache")
        self.snapshot = pcu.Snapshot(
            (
                pcu.describe_window(pcu.SESSION, 29, self.now + 3600, pcu.Level.NORMAL),
                pcu.describe_window("seven_day_fable", 3, None, None, display_name="Fable"),
            ),
            self.now,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.directory)], check=False)

    def test_round_trip(self):
        state = pcu.State(self.snapshot, pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE, "x"), 2, self.now)
        pcu.write_state(self.paths, state)
        self.assertEqual(pcu.read_state(self.paths), state)

    def test_unreadable_cache_is_empty(self):
        self.paths.cache.mkdir(parents=True)
        self.paths.state_file.write_text("{nope", encoding="utf-8")
        self.assertEqual(pcu.read_state(self.paths), pcu.State())

    def test_refresh_only_when_due(self):
        calls = []

        def fetch():
            calls.append(1)
            return self.snapshot

        settings = pcu.Settings(interval=300)
        first = pcu.refresh(self.paths, settings, force=False, now=self.now, fetch=fetch)
        self.assertEqual(first.snapshot, self.snapshot)
        # Another bar starting a minute later uses what's there.
        pcu.refresh(self.paths, settings, force=False, now=self.now + 60, fetch=fetch)
        self.assertEqual(len(calls), 1)
        # Right-click forces a check.
        pcu.refresh(self.paths, settings, force=True, now=self.now + 61, fetch=fetch)
        self.assertEqual(len(calls), 2)
        # Then the interval applies again.
        pcu.refresh(self.paths, settings, force=False, now=self.now + 61 + 301, fetch=fetch)
        self.assertEqual(len(calls), 3)

    def test_refresh_keeps_numbers_on_failure(self):
        settings = pcu.Settings()
        pcu.refresh(self.paths, settings, force=True, now=self.now, fetch=lambda: self.snapshot)

        def fail():
            raise pcu.UsageError(pcu.UsageError.USAGE_UNAVAILABLE, "busy")

        state = pcu.refresh(self.paths, settings, force=True, now=self.now + 10, fetch=fail)
        self.assertEqual((state.snapshot, state.failures, state.error.detail), (self.snapshot, 1, "busy"))

        def crash():
            raise RuntimeError("surprise")

        state = pcu.refresh(self.paths, settings, force=True, now=self.now + 20, fetch=crash)
        self.assertEqual((state.failures, state.error.kind), (2, pcu.UsageError.CLI_FAILED))
        # Backing off: 2 failures → twice the interval.
        self.assertEqual(state.due_at(300), self.now + 20 + 600)

    def test_due_soon_after_a_reset(self):
        state = pcu.State(self.snapshot, None, 0, self.now)
        self.assertEqual(state.due_at(7200), self.now + 3600 + 20)
        self.assertEqual(pcu.State().due_at(300), 0)


class TailTests(unittest.TestCase):
    """Runs the script the way polybar does, with a fake Claude Code."""

    def test_prints_and_refreshes_on_sigusr1(self):
        directory = Path(tempfile.mkdtemp(prefix="pcu-tail-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(directory)], check=False)
        counter = directory / "count"
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        # Each run reports one more percent than the last.
        claude.write_text(
            "#!/bin/sh\n"
            f"n=$(cat '{counter}' 2>/dev/null || echo 10); n=$((n + 1)); echo $n > '{counter}'\n"
            'echo \'{"usage_report":{"rate_limits":{"limits":[{"kind":"session","group":"session","percent":\'$n\',"resets_at":null}]}}}\'\n',
            encoding="utf-8",
        )
        claude.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(directory),
            "XDG_CONFIG_HOME": str(directory / "config"),
            "XDG_CACHE_HOME": str(directory / "cache"),
        }
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "polybar_claude_usage.py"), "--style", "text"],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )

        def stop():
            process.kill()
            process.wait()
            process.stdout.close()

        self.addCleanup(stop)
        self.assertEqual(process.stdout.readline().strip(), "5 11%")
        process.send_signal(signal.SIGUSR1)
        self.assertEqual(process.stdout.readline().strip(), "5 12%")

        details = subprocess.run(
            [sys.executable, str(ROOT / "polybar_claude_usage.py"), "--details"], env=env, capture_output=True, text=True
        )
        self.assertEqual(details.stdout.splitlines()[0], "Session: 12% · No active window")


if __name__ == "__main__":
    unittest.main()
