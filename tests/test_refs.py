"""Notes and proposals live under refs/khala/*. An instance from before 0.3 has them under refs/heads/inbox and
refs/proposals/*; they move in one transaction, at startup or with khala migrate-refs, and keep working. The branch
that holds records is a setting, and the push hook accepts only that branch."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from base import OWNER, Base, git, rec
from khala import cli, hooks
from khala.app import create_app
from khala.login import FakeMailer
from khala.store import INBOX, PROPOSALS, Store


class MigrationTests(Base):
    def make_old(self):
        """A note and an open proposal, then the refs renamed to what an instance before 0.3 used."""
        t = self.login(OWNER)[0]["access_token"]
        nid = self.call(t, "memory_note", scope="personal", text="remember the move")["id"]
        self.call(t, "memory_inbox", claim=True)
        pid = self.call(t, "memory_write", name="project_moved.md", notes=[nid],
                        content=rec("personal", body="moved"))["proposal"]
        inbox, prop = git(self.hub, "rev-parse", INBOX), git(self.hub, "rev-parse", PROPOSALS + pid)
        git(self.hub, "update-ref", "refs/heads/inbox", inbox)
        git(self.hub, "update-ref", "refs/proposals/" + pid, prop)
        git(self.hub, "update-ref", "-d", INBOX)
        git(self.hub, "update-ref", "-d", PROPOSALS + pid)
        return t, nid, pid, inbox, prop

    def refs(self):
        return sorted(git(self.hub, "for-each-ref", "--format=%(refname)").splitlines())

    def test_old_refs_move_together_and_keep_working(self):
        t, nid, pid, inbox, prop = self.make_old()
        moved = Store(self.hub).migrate_refs()
        self.assertEqual(sorted(moved), ["refs/heads/inbox", "refs/proposals/" + pid])
        self.assertEqual(self.refs(), ["refs/heads/main", INBOX, PROPOSALS + pid])
        self.assertEqual((git(self.hub, "rev-parse", INBOX), git(self.hub, "rev-parse", PROPOSALS + pid)),
                         (inbox, prop))
        self.assertEqual(Store(self.hub).migrate_refs(), [])                       # nothing left to move
        self.web_login(OWNER)
        self.assertIn("remember the move", self.client.get("/app/inbox/" + nid).text)
        self.post("/app/review/proposals/%s" % pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.assertIn("moved", git(self.hub, "show", "main:project_moved.md"))

    def test_the_server_moves_them_at_startup(self):
        _, _, pid, _, _ = self.make_old()
        app = create_app(self.app.state.cfg, mailer=FakeMailer())
        self.addCleanup(app.state.db.conn.close)
        self.assertEqual(self.refs(), ["refs/heads/main", INBOX, PROPOSALS + pid])
        events = self.db.q("SELECT detail FROM audit_events WHERE action='instance.refs_migrated'")
        self.assertEqual(sorted(json.loads(events[0]["detail"])["refs"]), ["refs/heads/inbox", "refs/proposals/" + pid])

    def test_a_name_already_taken_under_refs_khala_is_left_alone(self):
        _, _, _, inbox, _ = self.make_old()
        main = git(self.hub, "rev-parse", "main")
        git(self.hub, "update-ref", INBOX, main)                     # something already there
        moved = Store(self.hub).migrate_refs()
        self.assertNotIn("refs/heads/inbox", moved)
        self.assertEqual((git(self.hub, "rev-parse", INBOX), git(self.hub, "rev-parse", "refs/heads/inbox")),
                         (main, inbox))

    def test_the_command_line_moves_them(self):
        self.make_old()
        os.environ["KHALA_REPO"] = str(self.hub)
        self.addCleanup(os.environ.pop, "KHALA_REPO", None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["migrate-refs"])
        self.assertIn("moved 2 refs", out.getvalue())


class BranchTests(unittest.TestCase):
    def test_records_and_pushes_follow_the_configured_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "v.git"
            store = Store(repo, "trunk")
            store.ensure()
            _, commit, _ = store.write("project_a.md", lambda existing: rec("personal"), "A", "a@example.com", "add")
            self.assertEqual(git(repo, "rev-parse", "refs/heads/trunk"), commit)
            (repo / hooks.POLICY).write_text(json.dumps({"version": 1, "branch": "trunk", "scopes": {}}))
            zero = "0" * 40
            os.environ["GIT_DIR"] = str(repo)                    # the hook's git commands run in the pushed-to repo
            self.addCleanup(os.environ.pop, "GIT_DIR", None)
            for ref, expect in (("refs/heads/trunk", 0), ("refs/heads/main", 1)):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = hooks.pre_receive(["%s %s %s" % (zero, commit, ref)], str(repo))
                self.assertEqual(code, expect, err.getvalue())
            self.assertIn("only refs/heads/trunk may be pushed", err.getvalue())
            subprocess.run(["git", "--git-dir", str(repo), "rev-parse", "trunk"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
