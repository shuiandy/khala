"""Several workers on one database: every check-then-change is one statement, so a second connection acting at the
worst moment cannot get a second use out of anything single-use, and concurrent startups do not trip over each
other. The second DB() connection stands in for another worker process."""
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from base import ALICE, Base
from khala.auth import AuthError, Factors
from khala.db import COLUMNS, DB, VERSION, digest
from khala.inbox import Inbox
from khala.login import LoginFlow, check_code


def before_first(obj, name, fn):
    """Run fn() just before the next call of obj.name, once: the other worker acts between our check and change."""
    orig = getattr(obj, name)
    fired = []

    def wrapper(*args, **kw):
        setattr(obj, name, orig)
        fired.append(fn())
        return orig(*args, **kw)
    setattr(obj, name, wrapper)
    return fired


class ConcurrencyTests(Base):
    def setUp(self):
        super().setUp()
        self.other = DB(self.db.path)
        self.addCleanup(self.other.conn.close)
        self.addCleanup(setattr, self.db, "changed", self.db.changed)

    def test_one_email_code_signs_in_once(self):
        self.db.q("INSERT INTO login_codes(req, email, code_hash, expires, attempts) VALUES (?,?,?,?,0)",
                  "k", ALICE, digest("123456"), time.time() + 600)
        fired = before_first(self.db, "changed", lambda: check_code(self.other, "k", ALICE, "123456"))
        mine = check_code(self.db, "k", ALICE, "123456")
        self.assertEqual(fired, [(True, "")])
        self.assertEqual(mine, (False, "expired"))

    def test_one_half_login_finishes_once(self):
        factors = self.app.state.factors
        codes = factors.new_recovery(self.alice_id)
        other_factors = Factors(self.other, factors.box, "http://localhost")
        mine_flow = LoginFlow(self.db, self.mailer, factors)
        other_flow = LoginFlow(self.other, self.mailer, other_factors)
        self.db.q("INSERT INTO half_logins(key, account_id, attempts, expires) VALUES (?,?,0,?)",
                  "h", self.alice_id, time.time() + 600)
        fired = before_first(self.db, "changed",
                             lambda: other_flow.second("h", "recovery", codes[1], "", "test")[0])
        account, _ = mine_flow.second("h", "recovery", codes[0], "", "test")
        self.assertEqual(fired[0]["id"], self.alice_id)
        self.assertIsNone(account)

    def test_a_passkey_challenge_is_taken_once(self):
        factors = self.app.state.factors
        factors._save_challenge("c", b"challenge-bytes", "login")
        other = Factors(self.other, factors.box, "http://localhost")
        self.assertEqual(other._take_challenge("c", "login")[0], b"challenge-bytes")
        with self.assertRaises(AuthError):
            factors._take_challenge("c", "login")

    def test_a_challenge_for_another_purpose_is_still_voided(self):
        factors = self.app.state.factors
        factors._save_challenge("c", b"x", "register")
        with self.assertRaises(AuthError):
            factors._take_challenge("c", "login")
        self.assertIsNone(self.db.one("SELECT 1 FROM challenges WHERE key='c'"))

    def test_an_authorization_code_is_exchanged_once(self):
        client_id, verifier, req = self.start()
        self.client.get(self.verify(req, ALICE))
        r = self.consent(req)
        code = r.headers["location"].split("code=")[1].split("&")[0]
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1:9999/cb",
                "client_id": client_id, "code_verifier": verifier}
        self.assertEqual(self.client.post("/token", data=data).status_code, 200)
        again = self.client.post("/token", data=data)
        self.assertEqual(again.status_code, 400)

    def test_reconcile_in_two_workers_at_once_adds_each_note_once(self):
        t = self.login(ALICE)[0]["access_token"]
        nid = self.call(t, "memory_note", scope="team-shared", text="a fact")["id"]
        row = dict(self.db.one("SELECT * FROM notes WHERE id=?", nid))
        self.db.q("DELETE FROM notes WHERE id=?", nid)                    # as if the database write was lost
        inbox = Inbox(self.db, self.app.state.store, self.app.state.memory)
        cols = ", ".join(row)
        fired = before_first(inbox.store, "note_text", lambda: self.other.q(
            "INSERT INTO notes(%s) VALUES (%s)" % (cols, ",".join("?" * len(row))), *row.values()))
        self.assertEqual(inbox.reconcile(), [])
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM notes WHERE id=?", nid)["n"], 1)

    def start_together(self, version, rounds=1):
        """Open six connections at once on a database at `version` that lacks the later columns."""
        for i in range(rounds):
            path = Path(self.tmp.name) / ("shared-%d-%d.db" % (version, i))
            DB(path).conn.close()
            c = sqlite3.connect(path)
            c.executescript("".join("ALTER TABLE %s DROP COLUMN %s; " % (t, col) for t, col, _ in COLUMNS)
                            + "PRAGMA user_version=%d;" % version)
            c.close()
            self.assertEqual(self.open_together(path), [])
            check = DB(path)
            self.addCleanup(check.conn.close)
            self.assertEqual(check.one("PRAGMA user_version")[0], VERSION)
            self.assertEqual(check._missing_columns(), [])

    def test_workers_starting_together_migrate_once(self):
        self.start_together(4)

    def test_workers_starting_together_add_missing_columns_once(self):
        self.start_together(VERSION, rounds=5)         # the version is current, only columns are missing

    def open_together(self, path):
        gate, errors, opened = threading.Barrier(6), [], []

        def start():
            gate.wait()
            try:
                opened.append(DB(path))
            except Exception as exc:                                     # noqa: BLE001 - any failure fails the test
                errors.append(exc)
        threads = [threading.Thread(target=start) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        for db in opened:
            db.conn.close()
        return errors


if __name__ == "__main__":
    unittest.main()
