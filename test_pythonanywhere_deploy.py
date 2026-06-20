import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from deploy import pythonanywhere_daily as daily
from deploy import pythonanywhere_wsgi as wsgi


class PythonAnywhereDailyTests(unittest.TestCase):
    def test_pending_dates_continue_after_latest_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "archive_2026-06-17.html").write_text("ok", encoding="utf-8")
            with (
                patch.object(daily, "ROOT", root),
                patch.object(daily, "yesterday_kst", return_value=date(2026, 6, 19)),
                patch.dict(os.environ, {"MAX_BACKFILL_DAYS": "7"}),
            ):
                self.assertEqual(daily.pending_dates(), [date(2026, 6, 18), date(2026, 6, 19)])

    def test_validate_archive_rejects_too_few_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "archive_2026-06-19.html").write_text('<div class="news-card"></div>', encoding="utf-8")
            with patch.object(daily, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "only 1 news cards"):
                    daily.validate_archive(date(2026, 6, 19))

    def test_load_env_file_does_not_override_real_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("EXISTING=from-file\nNEW_VALUE=loaded\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXISTING": "from-environment"}, clear=True):
                daily.load_env_file(env_file)
                self.assertEqual(os.environ["EXISTING"], "from-environment")
                self.assertEqual(os.environ["NEW_VALUE"], "loaded")


class PythonAnywhereWsgiTests(unittest.TestCase):
    def request(self, path):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(wsgi.application({"REQUEST_METHOD": "GET", "PATH_INFO": path}, start_response))
        return captured, body

    def test_serves_index_at_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "index.html").write_text("briefing", encoding="utf-8")
            with patch.object(wsgi, "ROOT", root):
                response, body = self.request("/")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(body, b"briefing")
        self.assertEqual(response["headers"]["Cache-Control"], "no-cache")

    def test_never_serves_dotenv_or_python_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text("SECRET=value", encoding="utf-8")
            (root / "main.py").write_text("secret", encoding="utf-8")
            with patch.object(wsgi, "ROOT", root):
                env_response, _ = self.request("/.env")
                py_response, _ = self.request("/main.py")
        self.assertEqual(env_response["status"], "404 Not Found")
        self.assertEqual(py_response["status"], "404 Not Found")


if __name__ == "__main__":
    unittest.main()
