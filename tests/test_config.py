"""Settings: the public URL is required, and names from before the rename keep working."""
import datetime
import tempfile
import unittest

from starlette.testclient import TestClient

from khala import clock, rules, ui
from khala.app import create_app
from khala.config import Config, ConfigError, db_path
from khala.login import FakeMailer


class ConfigTests(unittest.TestCase):
    def test_the_server_refuses_to_start_without_a_public_url(self):
        for env in ({}, {"KHALA_ISSUER": ""}, {"KHALA_ISSUER": "memory.example.com"}):
            with self.assertRaises(ConfigError) as cm:
                Config(dict(env, KHALA_SECRET_KEY="k"))
            self.assertIn("KHALA_ISSUER", str(cm.exception))

    def test_allowed_hosts_default_to_the_public_urls_host(self):
        cfg = Config({"KHALA_ISSUER": "https://memory.example.com:8443/", "KHALA_SECRET_KEY": "k"})
        self.assertEqual(cfg.issuer, "https://memory.example.com:8443")
        self.assertEqual(cfg.allowed_hosts, ["memory.example.com:8443"])
        self.assertEqual(cfg.origin, "https://memory.example.com:8443")
        cfg = Config({"KHALA_ISSUER": "https://a.example", "KHALA_ALLOWED_HOSTS": "a.example, b.example",
                      "KHALA_SECRET_KEY": "k"})
        self.assertEqual(cfg.allowed_hosts, ["a.example", "b.example"])

    def test_legacy_memory_names_still_work_and_are_reported(self):
        cfg = Config({"MEMORY_ISSUER": "https://old.example", "MEMORY_DB": "/tmp/x.db", "KHALA_SECRET_KEY": "k"})
        self.assertEqual(cfg.issuer, "https://old.example")
        self.assertEqual(str(cfg.db), "/tmp/x.db")
        self.assertEqual(cfg.legacy_names, ["MEMORY_DB", "MEMORY_ISSUER"])
        self.assertEqual(str(db_path({"MEMORY_DB": "/tmp/y.db"})), "/tmp/y.db")

    def test_the_new_name_wins_over_the_legacy_one(self):
        cfg = Config({"KHALA_ISSUER": "https://new.example", "MEMORY_ISSUER": "https://old.example",
                      "KHALA_SECRET_KEY": "k"})
        self.assertEqual(cfg.issuer, "https://new.example")
        self.assertEqual(cfg.legacy_names, [])


class InstanceSettingsTests(unittest.TestCase):
    base = {"KHALA_ISSUER": "https://a.example", "KHALA_SECRET_KEY": "k"}

    def test_limits_default_and_must_be_positive(self):
        cfg = Config(self.base)
        self.assertEqual((cfg.writes_per_hour, cfg.writes_per_day, cfg.notes_per_hour, cfg.lease_minutes),
                         (60, 300, 120, 30))
        self.assertEqual(Config(dict(self.base, KHALA_WRITES_PER_DAY="1000")).writes_per_day, 1000)
        for bad in ("0", "-5", "ten", "1.5"):
            with self.assertRaises(ConfigError) as cm:
                Config(dict(self.base, KHALA_WRITES_PER_HOUR=bad))
            self.assertIn("KHALA_WRITES_PER_HOUR", str(cm.exception))

    def test_the_instance_name_shows_on_pages_and_in_agent_metadata(self):
        self.addCleanup(ui.env.globals.__setitem__, "instance_name", "Khala")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(dict(self.base, KHALA_ISSUER="http://localhost", KHALA_INSTANCE_NAME="Team memory",
                              KHALA_REPO=tmp + "/v.git", KHALA_DB=tmp + "/k.db", KHALA_INBOX_LEASE_MINUTES="5"))
            app = create_app(cfg, mailer=FakeMailer())
            self.addCleanup(app.state.db.conn.close)
            self.assertEqual(app.state.inbox.lease, 300)
            self.assertEqual(app.state.factors.name, "Team memory")
            with TestClient(app, base_url="http://localhost") as client:
                self.assertIn("<title>Sign in · Team memory</title>", client.get("/app/login").text)


class ClockTests(unittest.TestCase):
    """Dates come from the instance's time zone, not the server's. UTC+14 and UTC-12 are 26 hours apart, so their
    dates always differ, whenever the test runs."""
    def setUp(self):
        self.addCleanup(clock.configure, "UTC")

    def test_the_time_zone_setting_is_checked_and_defaults_to_utc(self):
        self.assertEqual(Config({"KHALA_ISSUER": "https://a.example", "KHALA_SECRET_KEY": "k"}).timezone, "UTC")
        with self.assertRaises(ConfigError):
            Config({"KHALA_ISSUER": "https://a.example", "KHALA_SECRET_KEY": "k", "KHALA_TIMEZONE": "Mars/Base"})

    def test_today_and_shown_times_follow_the_configured_zone(self):
        clock.configure("Pacific/Kiritimati")
        east, east_shown = clock.today(), ui.when(1_700_000_000, "%Y-%m-%d %H")
        clock.configure("Etc/GMT+12")
        west, west_shown = clock.today(), ui.when(1_700_000_000, "%Y-%m-%d %H")
        self.assertGreater(east, west)
        self.assertEqual((east_shown, west_shown), ("2023-11-15 12", "2023-11-14 10"))

    def test_review_due_and_proposed_dates_use_the_instance_date(self):
        clock.configure("Etc/GMT+12")
        west = clock.today()
        record = {"verified": (west - datetime.timedelta(days=30)).isoformat(), "review_after": "30d"}
        self.assertFalse(rules.review_due(record))              # due tomorrow in the western zone
        clock.configure("Pacific/Kiritimati")
        self.assertTrue(rules.review_due(record))               # already tomorrow out east
        text = ("---\nname: x\ndescription: d\nmetadata:\n  type: project\n  scope: s\n  status: active\n---\n\nb\n")
        out, _ = rules.check_write(text, None, lambda s: True, lambda s: True)
        self.assertIn("proposed_at: %s" % clock.today().isoformat(), out)


if __name__ == "__main__":
    unittest.main()
