"""SMTP: implicit TLS, STARTTLS or a plain local relay, always with certificate checks, signing in only when a user
name is set. smtplib is replaced by a recorder; nothing goes over the network."""
import ssl
import unittest
from unittest import mock

from khala import login
from khala.config import Config, ConfigError


class Recorder:
    def __init__(self, calls, kind):
        self.calls, self.kind = calls, kind

    def __call__(self, host, port, timeout=None, context=None):
        self.calls.append((self.kind, host, port, context))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls", context))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"]))


class MailTests(unittest.TestCase):
    def send(self, security, port="", username="u"):
        calls = []
        with mock.patch.object(login.smtplib, "SMTP_SSL", Recorder(calls, "ssl")), \
                mock.patch.object(login.smtplib, "SMTP", Recorder(calls, "plain")):
            login.SMTPMailer("smtp.example.com", port, username, "p", "khala@example.com", security,
                             name="Team memory").send("a@example.com", "123456")
        return calls

    def assertVerifies(self, context):
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_implicit_tls_checks_the_certificate(self):
        calls = self.send("ssl")
        self.assertEqual(calls[0][:3], ("ssl", "smtp.example.com", 465))
        self.assertVerifies(calls[0][3])
        self.assertEqual(calls[1:], [("login", "u"), ("send", "a@example.com", "Team memory sign-in code: 123456")])

    def test_starttls_upgrades_before_signing_in(self):
        calls = self.send("starttls")
        self.assertEqual(calls[0][:3], ("plain", "smtp.example.com", 587))
        self.assertEqual(calls[1][0], "starttls")
        self.assertVerifies(calls[1][1])
        self.assertEqual([c[0] for c in calls[2:]], ["login", "send"])

    def test_a_local_relay_without_a_user_name_is_not_signed_in_to(self):
        calls = self.send("none", port="2525", username="")
        self.assertEqual(calls[0][:3], ("plain", "smtp.example.com", 2525))
        self.assertEqual([c[0] for c in calls[1:]], ["send"])

    def test_the_security_setting_is_checked(self):
        base = {"KHALA_ISSUER": "https://a.example", "KHALA_SECRET_KEY": "k"}
        self.assertEqual(Config(base).smtp["security"], "ssl")
        self.assertEqual(Config(dict(base, SMTP_SECURITY="STARTTLS")).smtp["security"], "starttls")
        with self.assertRaises(ConfigError):
            Config(dict(base, SMTP_SECURITY="tls"))


if __name__ == "__main__":
    unittest.main()
