"""Replay real GitHub run history through the watchdog's alert logic.

Twice GitHub's API misdescribed the relay's past while every run succeeded,
and the watchdog flooded Discord with "Relay recovered": 28 times on
2026-10-19 (runs marked complete up to 30 min late) and ~40 times on
2026-10-22 (the `created=` run search answering with nothing). These tests
replay the 2026-10-22 history, minute by minute, as each watchdog pass would
have seen it, and check what reaches Discord.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import watchdog  # noqa: E402

with open(os.path.join(HERE, "fixtures", "runs-2026-10-22.json")) as f:
    HISTORY = json.load(f)

# The watchdog passes replayed: from before the flood began (14:56) to its end.
FROM, TO = "2026-10-22T14:30:00Z", "2026-10-22T16:46:00Z"


def stamp(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class Response:
    ok, status, text = True, 200, ""

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeGitHub:
    """The Actions API as it stood at `now`.

    A run is complete only once its recorded updated_at has passed, so late
    finalization shows up exactly as it did live. `search` sets how the
    `created=` filter behaves: "ok", "empty" (always no runs) or "flaky" (no
    runs on 2 calls in 3, as on 2026-10-22).
    """

    def __init__(self, relay_runs, watchdog_passes, search="ok"):
        self.relay, self.passes, self.search = relay_runs, watchdog_passes, search
        self.now = 0.0
        self.searches = 0

    def __call__(self, path, method="GET"):
        url = urlsplit(path)
        query = parse_qs(url.query)
        relay = "pipeline.yml" in url.path
        runs = [r for r in (self.relay if relay else self.passes) if stamp(r["created_at"]) <= self.now]
        if "created" in query:
            self.searches += 1
            if self.search == "empty" or (self.search == "flaky" and self.searches % 3):
                return Response({"workflow_runs": []})
            since = query["created"][0].lstrip(">=")
            runs = [r for r in runs if r["created_at"] >= since]
        runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)[:int(query.get("per_page", ["30"])[0])]
        if relay:
            runs = [self.as_seen(r) for r in runs]
        return Response({"workflow_runs": runs})

    def as_seen(self, run):
        done = stamp(run["updated_at"]) <= self.now
        return {**run, "status": "completed" if done else "in_progress",
                "conclusion": run["conclusion"] if done else None}


def replay(relay_runs=None, search="ok", **patches):
    """Run alert() for each watchdog pass from FROM to TO; return the Discord messages.

    With Discord alerts switched on (WATCHDOG_ALERTS=1) unless a test says
    otherwise, so the alert logic itself is what's tested.
    """
    github = FakeGitHub(relay_runs or HISTORY["relay_runs"], HISTORY["watchdog_passes"], search)
    sent = []
    patches = {"ALERTS": True,
               "gh": github,
               "notify": lambda msg, source=None: sent.append(msg.splitlines()[0]),
               "print": lambda *a, **k: None,
               **patches}
    with ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(watchdog, name, value, create=name == "print"))
        stack.enter_context(mock.patch.object(watchdog.time, "time", lambda: github.now))
        for run in HISTORY["watchdog_passes"]:
            if FROM <= run["created_at"] <= TO:
                github.now = stamp(run["created_at"]) + 5   # a pass checks a few seconds after it's created
                with mock.patch.object(watchdog, "THIS_RUN_ID", str(run["id"])):
                    age = watchdog.last_success_age()
                    watchdog.alert(age is None or age >= watchdog.STALL_AFTER, age, [])
    return sent


def stalled_history():
    """The same day with an outage like 2026-10-19's: runs created 15:00-15:40
    aren't marked complete until 15:45, so no run succeeds for ~38 minutes."""
    runs = []
    for run in HISTORY["relay_runs"]:
        if "2026-10-22T15:00" <= run["created_at"] < "2026-10-22T15:40":
            run = {**run, "updated_at": "2026-10-22T15:45:00Z"}
        runs.append(run)
    return runs


class HealthyRelay(unittest.TestCase):
    """Every run succeeded on 2026-10-22: nothing may reach Discord."""

    def test_search_failing_as_on_2026_10_22(self):
        self.assertEqual(replay(search="flaky"), [])

    def test_search_down_entirely(self):
        self.assertEqual(replay(search="empty"), [])

    def test_search_working(self):
        self.assertEqual(replay(search="ok"), [])

    def test_no_recovery_without_a_gap_in_the_runs(self):
        # Even if judging the previous pass goes wrong some new way, the runs
        # themselves must show an outage before "recovered" is posted.
        self.assertEqual(replay(relay_healthy_at=lambda unix: False), [])


class RealStall(unittest.TestCase):
    """A genuine outage must still be reported, however GitHub's search behaves."""

    def check(self, sent):
        stalls = [m for m in sent if "stalled" in m]
        recoveries = [m for m in sent if "recovered" in m]
        self.assertTrue(sent and "stalled" in sent[0], sent)
        self.assertLessEqual(len(stalls), 2, sent)   # once, plus at most one repeat at the 10-minute edge
        self.assertEqual(len(recoveries), 1, sent)
        self.assertIn("recovered", sent[-1])

    def test_search_working(self):
        self.check(replay(stalled_history(), search="ok"))

    def test_search_failing(self):
        self.check(replay(stalled_history(), search="flaky"))

    def test_search_down_entirely(self):
        self.check(replay(stalled_history(), search="empty"))


class AlertsOff(unittest.TestCase):
    """WATCHDOG_ALERTS unset, the default: Discord hears nothing, even during a stall."""

    def test_stall_sends_nothing(self):
        self.assertEqual(replay(stalled_history(), ALERTS=False), [])

    def test_off_unless_the_variable_says_on(self):
        for value, on in (("", False), ("0", False), ("off", False), ("1", True), ("true", True)):
            with mock.patch.dict(os.environ, {"WATCHDOG_ALERTS": value}):
                spec = importlib.util.spec_from_file_location("watchdog_env", watchdog.__file__)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            self.assertIs(module.ALERTS, on, value)


if __name__ == "__main__":
    unittest.main()
