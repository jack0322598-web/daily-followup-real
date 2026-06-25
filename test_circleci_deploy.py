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
from deploy import sync_deployed


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
            (root / "industry_trend_cache.json").write_text("{}", encoding="utf-8")
            with patch.object(build_public, "ROOT", root), patch.object(build_public, "PUBLIC_DIR", public):
                copied = build_public.build_public()
            self.assertTrue((public / "index.html").exists())
            self.assertTrue((public / "_headers").exists())
            self.assertFalse((public / "main.py").exists())
            self.assertFalse((public / ".env").exists())
            self.assertTrue((public / "_state" / "industry_trend_cache.json").exists())
            self.assertEqual(len(copied), 4)


class SyncDeployedTests(unittest.TestCase):
    def test_sync_treats_unavailable_site_as_first_deployment(self):
        with (
            patch.dict(os.environ, {"SITE_URL": "https://new-site.example"}, clear=True),
            patch.object(
                sync_deployed.urllib.request,
                "urlopen",
                side_effect=sync_deployed.urllib.error.URLError("not deployed yet"),
            ),
        ):
            self.assertEqual(sync_deployed.sync_deployed(), [])

    def test_sync_restores_new_archive_and_state(self):
        responses = {
            "https://site.example/archive_list.js": b'const archiveDates = ["2026-06-20"];',
            "https://site.example/index.html": b"home",
            "https://site.example/share_index.html": b"share",
            "https://site.example/archive_2026-06-20.html": b"archive",
            "https://site.example/_state/industry_trend_cache.json": b"{}",
        }

        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return self.data

        def urlopen(url, timeout=30):
            if url in responses:
                return Response(responses[url])
            raise __import__("urllib.error").error.HTTPError(url, 404, "not found", {}, None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(sync_deployed, "ROOT", root),
                patch.dict(os.environ, {"SITE_URL": "https://site.example"}, clear=True),
                patch.object(sync_deployed.urllib.request, "urlopen", side_effect=urlopen),
            ):
                synced = sync_deployed.sync_deployed()
            self.assertTrue((root / "archive_2026-06-20.html").exists())
            self.assertTrue((root / "industry_trend_cache.json").exists())
            self.assertIn("archive_2026-06-20.html", synced)


class SlackNotifyTests(unittest.TestCase):
    def tearDown(self):
        slack_notify.MARKET_FACTOR_TEXT_CACHE.clear()

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

    def test_success_message_includes_market_factor_summary(self):
        archive_html = """
        <html><body>
          <section id="section-indicators" class="content-section active">
            <article class="indicator-card">
              <div class="metric-label">원/달러 환율</div>
              <div class="metric-value">종가: 1,527.00원</div>
              <div class="chart-canvas" id="chart-fx"></div>
            </article>
            <article class="indicator-card">
              <div class="metric-label">코스피 지수</div>
              <div class="metric-value">3,000.00</div>
              <div class="metric-detail">외국인 순매수</div>
              <div class="chart-canvas" id="chart-kospi"></div>
            </article>
          </section>
          <script>
            const chartData = {"fx": {"values": [1510.0, 1527.0]}, "kospi": {"values": [2970.3, 3000.0]}};
          </script>
        </body></html>
        """
        result = {"dates": ["2026-06-23"], "latest_archive": "2026-06-23"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "archive_2026-06-23.html").write_text(archive_html, encoding="utf-8")
            with (
                patch.object(slack_notify, "ROOT", root),
                patch.object(slack_notify, "fetch_kospi_factor_summary", return_value="반도체 대형주 매수세가 코스피 상승을 견인했습니다."),
                patch.object(slack_notify, "fetch_fx_factor_summary", return_value="연준 매파 기조와 달러 강세가 환율 상승 요인으로 작용했습니다."),
            ):
                message = slack_notify.build_daily_briefing_message(result)

        self.assertIn("[주요 지표]", message)
        self.assertIn("코스피", message)
        self.assertIn("반도체 대형주", message)
        self.assertIn("(3,000.00, +1.00%)", message)
        self.assertIn("환율", message)
        self.assertIn("연준 매파", message)
        self.assertIn("(1,527.00원, +1.13%)", message)

    def test_kb_fx_report_prefers_next_day_report_for_target_archive(self):
        page_text = """
        [금일 달러/원 환율 1,525~1,540원 전망]|차오르는 한일 당국 게이지 2026.06.24 264 0
        [금일 달러/원 환율 1,530~1,545원 전망]|어제의 환율이 오늘의 눈높이 2026.06.23 233 0
        """
        with patch.object(slack_notify, "fetch_url_text", return_value=page_text):
            report = slack_notify.fetch_kb_fx_report(slack_notify.datetime(2026, 6, 23))

        self.assertEqual(report["date"], "2026.06.24")
        self.assertEqual(report["range"], "1,525~1,540원 전망")
        self.assertEqual(report["title"], "차오르는 한일 당국 게이지")


if __name__ == "__main__":
    unittest.main()
