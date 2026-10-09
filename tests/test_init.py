"""khala init and serve: a new instance from one command, its settings file, and admins that are only merged
when an alias says so."""
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from khala import cli, instance
from khala.app import create_app
from khala.config import Config
from khala.db import DB
from khala.login import FakeMailer


class InitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-init-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in [k for k in os.environ if k.startswith(("KHALA_", "MEMORY_", "SMTP_"))]:
            del os.environ[key]
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(list(argv))
        return out.getvalue()

    def test_init_makes_an_instance_that_serves(self):
        out = self.run_cli("init", "--admin", "You@Example.com", "--name", "You", "--timezone", "America/Toronto")
        self.assertIn("khala serve --env khala.env", out)
        env = instance.read_env(self.root / "khala.env")
        data = self.root / "khala-data"
        self.assertEqual(env["KHALA_ISSUER"], "http://localhost:8100")
        self.assertEqual((env["KHALA_REPO"], env["KHALA_MAILER"], env["KHALA_TIMEZONE"]),
                         (str(data / "vault.git"), "log", "America/Toronto"))
        for path, mode in ((data, 0o700), (data / "secret.key", 0o600), (self.root / "khala.env", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        self.assertTrue((data / "vault.git" / "HEAD").exists())

        app = create_app(Config(env), mailer=FakeMailer())
        self.addCleanup(app.state.db.conn.close)
        admin = app.state.db.account_by_email("you@example.com")
        self.assertEqual((admin["name"], admin["is_admin"]), ("You", 1))
        with TestClient(app, base_url="http://localhost:8100") as client:
            self.assertEqual(client.get("/health").status_code, 200)

    def test_a_public_url_asks_for_smtp(self):
        self.run_cli("init", "--admin", "a@example.com", "--url", "https://memory.example.com/")
        env = instance.read_env(self.root / "khala.env")
        self.assertEqual((env["KHALA_ISSUER"], env["KHALA_MAILER"], env["SMTP_PORT"]),
                         ("https://memory.example.com", "smtp", "465"))

    def test_init_never_overwrites_settings_or_the_key(self):
        self.run_cli("init", "--admin", "a@example.com")
        key = (self.root / "khala-data" / "secret.key").read_text()
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("init", "--admin", "b@example.com")
        self.assertIn("already exists", str(cm.exception.code))
        self.run_cli("init", "--admin", "b@example.com", "--env", "second.env")     # same data, new settings file
        self.assertEqual((self.root / "khala-data" / "secret.key").read_text(), key)
        db = DB(self.root / "khala-data" / "state" / "khala.db")
        self.addCleanup(db.conn.close)
        self.assertEqual(sorted(a["email"] for a in db.accounts() if a["is_admin"]), ["a@example.com", "b@example.com"])

    def test_init_checks_its_input(self):
        for args, word in ((["--admin", "nobody"], "--admin"), (["--admin", "a@example.com", "--url", "memory"], "--url"),
                           (["--admin", "a@example.com", "--timezone", "Mars/Base"], "--timezone")):
            with self.assertRaises(SystemExit) as cm:
                self.run_cli("init", *args)
            self.assertIn(word, str(cm.exception.code))
        self.assertFalse((self.root / "khala.env").exists())

    def test_settings_file_is_read_and_the_environment_wins(self):
        (self.root / "x.env").write_text("# comment\nKHALA_ISSUER='https://a.example'\nKHALA_DB=/tmp/a.db\n\n")
        environ = {"KHALA_DB": "/tmp/override.db"}
        instance.load_env(self.root / "x.env", environ)
        self.assertEqual(environ, {"KHALA_ISSUER": "https://a.example", "KHALA_DB": "/tmp/override.db"})

    def test_serve_reads_the_settings_file_and_runs_uvicorn(self):
        self.run_cli("init", "--admin", "a@example.com")
        with mock.patch.object(cli.uvicorn, "run") as run:
            self.run_cli("--env", "khala.env", "serve", "--port", "9123", "--workers", "2")
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("khala.app:create_app",))
        self.assertEqual({k: run.call_args.kwargs[k] for k in ("factory", "port", "workers", "host")},
                         {"factory": True, "port": 9123, "workers": 2, "host": "127.0.0.1"})
        self.assertEqual(os.environ["KHALA_ISSUER"], "http://localhost:8100")

    def test_serve_without_settings_explains_what_is_missing(self):
        with mock.patch.object(cli.uvicorn, "run") as run, self.assertRaises(SystemExit) as cm:
            self.run_cli("serve")
        run.assert_not_called()
        self.assertIn("KHALA_ISSUER", str(cm.exception.code))

    def test_commands_refuse_to_create_a_database_by_accident(self):
        os.environ["KHALA_DB"] = str(self.root / "nowhere" / "khala.db")
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("users")
        self.assertIn("khala init", str(cm.exception.code))
        self.assertFalse((self.root / "nowhere").exists())

    def key_env(self):
        data = self.root / "d"
        return {"KHALA_ISSUER": "http://localhost", "KHALA_REPO": str(data / "v.git"),
                "KHALA_DB": str(data / "state" / "k.db"), "KHALA_SECRET_KEY_FILE": str(data / "secret.key"),
                "KHALA_OWNERS": "a@example.com"}

    def test_a_new_instance_makes_its_own_key(self):
        env = self.key_env()
        app = create_app(Config(env), mailer=FakeMailer())
        app.state.db.conn.close()
        key = Path(env["KHALA_SECRET_KEY_FILE"])
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        first = key.read_text()
        app = create_app(Config(env), mailer=FakeMailer())             # a restart reads it, never replaces it
        app.state.db.conn.close()
        self.assertEqual(key.read_text(), first)

    def test_an_instance_with_accounts_refuses_to_start_without_its_key(self):
        env = self.key_env()
        create_app(Config(env), mailer=FakeMailer()).state.db.conn.close()
        Path(env["KHALA_SECRET_KEY_FILE"]).unlink()
        with self.assertRaises(instance.InitError) as cm:
            create_app(Config(env), mailer=FakeMailer())
        self.assertIn("restore it", str(cm.exception))
        os.environ.update(env)
        with mock.patch.object(cli.uvicorn, "run") as run, self.assertRaises(SystemExit) as cm:
            self.run_cli("serve")                                       # one clear line, before the server starts
        run.assert_not_called()
        self.assertIn("restore it", str(cm.exception.code))
        self.assertFalse(Path(env["KHALA_SECRET_KEY_FILE"]).exists())

    def test_owners_from_the_environment_are_separate_people_unless_aliased(self):
        db = DB(self.root / "o.db")
        self.addCleanup(db.conn.close)
        first = db.ensure_admin("one@example.com", "Owner")
        alias_target = db.create_account("two@example.com", "Two")
        db.add_alias(alias_target, "two@alt.example")
        self.assertNotEqual(db.ensure_admin("three@example.com", "Owner"), first)       # a new, separate admin
        self.assertEqual(db.ensure_admin("two@alt.example", "Owner"), alias_target)     # an alias is that person
        self.assertEqual(len(db.accounts()), 3)
        self.assertTrue(all(a["is_admin"] for a in db.accounts()))


if __name__ == "__main__":
    unittest.main()
