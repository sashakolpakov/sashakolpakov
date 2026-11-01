import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import call, patch

from scripts import update_clone_stats as stats


URL = "https://api.github.com/repos/sashakolpakov/burau-switch/traffic/clones?per=day"


def response(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


def http_error(code):
    return urllib.error.HTTPError(
        URL, code, "API error", {}, io.BytesIO(b'{"message": "API unavailable"}')
    )


class ApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch.object(stats.urllib.request, "urlopen"))
        self.sleep = self.enterContext(patch.object(stats.time, "sleep"))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_success_does_not_retry(self):
        self.urlopen.return_value = response({"clones": []})

        self.assertEqual(stats.api_get_json(URL, "test-token"), {"clones": []})

        self.urlopen.assert_called_once()
        self.sleep.assert_not_called()

    def test_recovers_from_temporary_server_errors(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = [http_error(code), response({"count": 12})]

                self.assertEqual(stats.api_get_json(URL, "test-token"), {"count": 12})

                self.assertEqual(self.urlopen.call_count, 2)
                self.sleep.assert_called_once_with(2)

    def test_recovers_from_network_failures(self):
        for error in (
            urllib.error.URLError("Temporary DNS failure"),
            TimeoutError("Read timed out"),
            ConnectionResetError("Connection reset"),
        ):
            with self.subTest(error=type(error).__name__):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = [error, response({"count": 12})]

                self.assertEqual(stats.api_get_json(URL, "test-token"), {"count": 12})

                self.assertEqual(self.urlopen.call_count, 2)
                self.sleep.assert_called_once_with(2)

    def test_persistent_server_errors_stop_after_bounded_backoff(self):
        errors = [http_error(500) for _ in range(4)]
        self.urlopen.side_effect = errors

        with self.assertRaises(RuntimeError) as raised:
            stats.api_get_json(URL, "test-token")

        self.assertEqual(self.urlopen.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])
        self.assertIn(URL, str(raised.exception))
        self.assertIn("500", str(raised.exception))
        self.assertIn("API unavailable", str(raised.exception))
        self.assertIs(raised.exception.__cause__, errors[-1])
        self.assertIn("attempt 4/4", self.stderr.getvalue())

    def test_persistent_network_failures_stop_after_bounded_backoff(self):
        error = TimeoutError("Read timed out")
        self.urlopen.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            stats.api_get_json(URL, "test-token")

        self.assertEqual(self.urlopen.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])
        self.assertIn(URL, str(raised.exception))
        self.assertIs(raised.exception.__cause__, error)

    def test_permanent_http_errors_fail_immediately(self):
        for code in (400, 401, 403, 404, 422, 429, 501):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                error = http_error(code)
                self.urlopen.side_effect = error

                with self.assertRaises(RuntimeError) as raised:
                    stats.api_get_json(URL, "test-token")

                self.urlopen.assert_called_once()
                self.sleep.assert_not_called()
                self.assertIs(raised.exception.__cause__, error)


class LeaderboardRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.readme = self.directory / "README.md"
        self.history = self.directory / "clone_stats_history.json"
        self.readme.write_text("Existing leaderboard\n", encoding="utf-8")
        self.history.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "sashakolpakov",
                    "repos": {
                        "burau-switch": {"daily": {"2026-10-01": {"count": 7}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        self.enterContext(
            patch.dict(os.environ, {"TRAFFIC_TOKEN": "test-token"}, clear=True)
        )
        self.enterContext(
            patch(
                "sys.argv",
                [
                    "update_clone_stats.py",
                    "--history",
                    str(self.history),
                    "--readme",
                    str(self.readme),
                ],
            )
        )
        self.urlopen = self.enterContext(patch.object(stats.urllib.request, "urlopen"))
        self.enterContext(patch.object(stats.time, "sleep"))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_recovered_request_updates_leaderboard_and_preserves_old_counts(self):
        self.urlopen.side_effect = [
            response([{"name": "burau-switch"}]),
            http_error(500),
            response({"clones": [{"timestamp": "2026-11-01T00:00:00Z", "count": 12}]}),
        ]

        self.assertEqual(stats.main(), 0)

        history = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual(stats.tracked_clone_total(history["repos"]["burau-switch"]), 19)
        self.assertIn("<code>19</code>", self.readme.read_text(encoding="utf-8"))

    def test_exhausted_retries_leave_both_files_unchanged(self):
        original_readme = self.readme.read_bytes()
        original_history = self.history.read_bytes()
        self.urlopen.side_effect = [
            response([{"name": "first-repo"}, {"name": "burau-switch"}]),
            response({"clones": [{"timestamp": "2026-11-01T00:00:00Z", "count": 12}]}),
            *[http_error(500) for _ in range(4)],
        ]

        with self.assertRaises(RuntimeError):
            stats.main()

        self.assertEqual(self.readme.read_bytes(), original_readme)
        self.assertEqual(self.history.read_bytes(), original_history)


if __name__ == "__main__":
    unittest.main()
