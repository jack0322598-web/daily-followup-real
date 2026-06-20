import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from deploy import build_public
from deploy import daily_ci
from deploy import slack_notify


class DailyCiTests(unittest.TestCase):
    def test_pending_dates_continue_after_latest_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "archive_2026-06-17.html").write_text("ok", encoding="utf-8")
            with (
                patch.object(daily_ci, "ROOT", root),
                patch.object(daily_ci, "yesterday_kst", return_value=date(2026, 6, 19)),
                patch.dict(os.environ, {"MAX_BACKFILL_DAYS": "7"}),
            ):
                self.assertEqual(daily_ci.pending_dates(), [date(2026, 6, 18), date(2026, 6, 19)])

    def test_validate_archive_rejects_too_few_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "archive_2026-06-19.html").write_text('<div class="news-card"></div>', encoding="utf-8")
            with patch.object(daily_ci, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "only 1 news cards"):
                    daily_ci.validate_archive(date(2026, 6, 19))

    def test_write_result_records_latest_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_file = root / "deploy_result.json"
            (root / "archive_2026-06-19.html").write_text("ok", encoding="utf-8")
            with patch.object(daily_ci, "ROOT", root), patch.object(daily_ci, "RESULT_FILE", result_file):
                daily_ci.write_result("updated", [date(2026, 6, 19)])
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["latest_archive"], "2026-06-19")
            self.assertEqual(payload["status"], "updated")


class BuildPublicTests(unittest.TestCase):
    def test_build_copies_public_assets_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            public = root / "public"
            (root / "index.html").write_text("home", encoding="utf-8")
            (root / "archive_2026-06-19.html").write_text("archive", encoding="utf-8")
            (root / "archive_list.js").write_text("const dates = [];", encoding="utf-8")
            (root / "main.py").write_text("secret source", encoding="utf-8")
            (root / ".env").write_text("SECRET=value", encoding="utf-8")
            with patch.object(build_public, "ROOT", root), patch.object(build_public, "PUBLIC_DIR", public):
                copied = build_public.build_public()
            self.assertTrue((public / "index.html").exists())
            self.assertTrue((public / "_headers").exists())
            self.assertFalse((public / "main.py").exists())
            self.assertFalse((public / ".env").exists())
            self.assertEqual(len(copied), 3)


class SlackNotifyTests(unittest.TestCase):
    def test_success_message_links_latest_archive(self):
        result = {"dates": ["2026-06-19"], "latest_archive": "2026-06-19"}
        with patch.dict(
            os.environ,
            {"SITE_URL": "https://briefing.pages.dev", "CIRCLE_BUILD_URL": "https://circleci.example/build"},
            clear=True,
        ):
            title, message, color = slack_notify.build_message("success", result)
        self.assertIn("배포 완료", title)
        self.assertIn("archive_2026-06-19.html", message)
        self.assertIn("실행 로그", message)
        self.assertEqual(color, "#2EB67D")


if __name__ == "__main__":
    unittest.main()
