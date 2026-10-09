"""pre-receive hook: actually installed in a temporary bare repository, pushed to from a working copy."""
import json
import os
import subprocess
import sys
import unittest

from base import OWNER, Base, git, rec


class HookTests(Base):
    def setUp(self):
        super().setUp()
        hook = self.hub / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexec %s -I -m khala.hooks pre-receive\n" % sys.executable)
        os.chmod(hook, 0o755)
        git(self.work, "pull", "-q", str(self.hub), "main")

    def push(self, files, ref="HEAD:main", delete=()):
        for name, text in files.items():
            (self.work / name).write_text(text)
        for name in delete:
            (self.work / name).unlink()
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "change")
        r = subprocess.run(["git", "-C", str(self.work), "push", "-q", str(self.hub), ref], capture_output=True, text=True)
        return r.returncode, r.stderr

    def log(self):
        path = self.hub / "push-check.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_ordinary_pushes_pass_quietly(self):
        code, err = self.push({"project_ok.md": rec("personal"), "sync.py": "print('script')\n",
                               "feedback_global_pref.md": rec("global", status="active", body="approved in a mirror")},
                              delete=["project_audit.md"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("memory:", err)
        self.assertEqual(self.log(), [])

    def test_a_scope_that_reads_two_ways_is_refused(self):
        before = git(self.hub, "rev-parse", "main")
        deep = "---\nname: n\ndescription: d\nmetadata:\n    scope: global\n    status: active\n---\n\nx\n"
        code, err = self.push({"feedback_deep.md": deep})
        self.assertNotEqual(code, 0)
        self.assertIn("reads differently", err)
        self.assertEqual(git(self.hub, "rev-parse", "main"), before)

    def test_a_description_that_reads_two_ways_only_warns(self):
        text = rec("personal").replace("description: personal note", "description: merged (PR #6) today")
        code, err = self.push({"project_hash.md": text})
        self.assertEqual(code, 0, err)
        self.assertIn("description reads differently", err)

    def test_credentials_and_huge_records_are_refused(self):
        before = git(self.hub, "rev-parse", "main")
        code, err = self.push({"project_leak.md": rec("personal", body="key ghp_" + "A" * 36)})
        self.assertNotEqual(code, 0)
        self.assertIn("credential", err)
        self.assertEqual(git(self.hub, "rev-parse", "main"), before)
        git(self.work, "reset", "-q", "--hard", "HEAD~1")
        code, err = self.push({"project_big.md": rec("personal", body="x" * 70000)})
        self.assertIn("64 KB", err)
        self.assertEqual(git(self.hub, "rev-parse", "main"), before)

    def test_a_credential_added_then_removed_inside_one_push_is_refused(self):
        before = git(self.hub, "rev-parse", "main")
        (self.work / "project_tmp.md").write_text(rec("personal", body="key ghp_" + "B" * 36))
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "oops")
        (self.work / "project_tmp.md").write_text(rec("personal", body="clean"))
        git(self.work, "commit", "-qam", "fix")
        r = subprocess.run(["git", "-C", str(self.work), "push", "-q", str(self.hub), "HEAD:main"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("credential (commit", r.stderr)
        self.assertEqual(git(self.hub, "rev-parse", "main"), before)

    def test_only_main_may_be_pushed(self):
        code, err = self.push({"note.md": "x\n"}, ref="HEAD:refs/khala/inbox")
        self.assertNotEqual(code, 0)
        self.assertIn("only refs/heads/main", err)
        r = subprocess.run(["git", "-C", str(self.work), "push", "-q", str(self.hub), "HEAD:refs/khala/proposals/p-1"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_permission_problems_warn_then_refuse(self):
        # the alice scope belongs to Alice; the pusher (the first admin) has no write access to it
        code, err = self.push({"project_hers.md": rec("alice")})
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("cannot write to scope 'alice'", err)
        self.assertFalse(self.log()[-1]["rejected"])
        code, err = self.push({"feedback_new_rule.md": rec("global", status="active")})
        self.assertIn("must start as proposed", err)

        self.app.state.memory.enforce = True
        self.app.state.memory._policy = None
        self.app.state.memory.records()
        code, err = self.push({"project_hers2.md": rec("alice")})
        self.assertNotEqual(code, 0)
        self.assertTrue(self.log()[-1]["rejected"])
        self.web_login(OWNER)
        self.assertIn("cannot write to scope", self.client.get("/app/admin").text)

    def test_dry_run_is_silent(self):
        os.remove(self.hub / "push-policy.json")
        r = subprocess.run([sys.executable, "-I", "-m", "khala.hooks", "pre-receive"], input="",
                           capture_output=True, text=True, env=dict(os.environ, GIT_DIR=str(self.hub)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(self.log(), [])

    def test_without_a_policy_the_hard_rules_still_hold(self):
        (self.hub / "push-check.log").unlink(missing_ok=True)
        os.remove(self.hub / "push-policy.json")
        code, err = self.push({"project_hers.md": rec("alice")})
        self.assertEqual(code, 0)
        self.assertIn("push-policy.json is missing", err)
        code, err = self.push({"project_leak.md": rec("personal", body="sk-" + "a" * 40)})
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
