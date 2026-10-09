"""Frontmatter is read as YAML, falls back to the line reader for older hand-written records, and anything written
through the service must read the same both ways."""
import unittest

from base import OWNER, Base, git, rec
from khala import rules

HEAD = "---\nname: n\ndescription: %s\nmetadata:\n  type: project\n  scope: %s\n  status: %s\n---\n\nbody\n"


class ReadingTests(unittest.TestCase):
    def test_yaml_written_by_other_tools_is_understood(self):
        text = ('---\nname: "n"\ndescription: >-\n  folded\n  text\nmetadata: {scope: personal, status: active, '
                'pinned: true, verified: 2026-10-01}\n---\n\nb\n')
        m = rules.meta(text)
        self.assertEqual((m["scope"], m["status"], m["pinned"], m["verified"], m["description"]),
                         ("personal", "active", "true", "2026-10-01", "folded text"))

    def test_invalid_yaml_falls_back_to_the_line_reader(self):
        text = '---\nname: n\ndescription: "C:\\Users\\x"\nmetadata:\n  scope: personal\n  status: active\n---\n'
        self.assertIsNone(rules.yaml_meta(text))
        self.assertEqual(rules.meta(text)["scope"], "personal")

    def test_a_comment_marker_in_a_plain_value_is_reported(self):
        text = HEAD % ("merged (PR #6, 2026-07-12)", "personal", "active")
        self.assertEqual(rules.ambiguities(text), ["description"])
        self.assertEqual(rules.ambiguities(HEAD % ("'merged (PR #6, 2026-07-12)'", "personal", "active")), [])


class WritingTests(Base):
    def setUp(self):
        super().setUp()
        self.t = self.login(OWNER)[0]["access_token"]

    def write(self, name, text):
        return self.call(self.t, "memory_write", name=name, content=text)

    def test_ambiguous_or_invalid_frontmatter_is_refused_with_a_hint(self):
        r = self.write("project_a.md", HEAD % ("merged (PR #6)", "personal", "active"))
        self.assertIn("quote values that contain ' #'", r["error"])
        r = self.write("project_b.md", HEAD % ('"C:\\Users\\x"', "personal", "active"))
        self.assertIn("not valid YAML", r["error"])
        self.assertNotIn("error", self.write("project_c.md", HEAD % ("'merged (PR #6)'", "personal", "active")))

    def test_a_record_hidden_from_the_line_reader_cannot_enter_an_auto_loaded_scope_active(self):
        deep = ("---\nname: n\ndescription: d\nmetadata:\n    type: project\n    scope: global\n    status: active\n"
                "---\n\nsneak\n")
        self.assertEqual(rules.meta(deep)["scope"], "global")             # YAML sees it, the line reader does not
        r = self.write("feedback_deep.md", deep)
        self.assertIn("error", r)
        self.assertNotIn("feedback_deep.md", git(self.hub, "ls-tree", "--name-only", "main"))

    def test_the_status_rewrite_refuses_a_layout_it_cannot_change(self):
        deep = "---\nname: n\ndescription: d\nmetadata:\n    scope: global\n    status: active\n---\n"
        with self.assertRaises(rules.RuleError):
            rules._set_status_proposed(deep, "2026-10-08")
        with self.assertRaises(rules.RuleError):
            rules.deprecate(deep, "old")

    def test_a_reason_with_colons_and_hashes_keeps_the_record_valid(self):
        self.write("project_r.md", rec("personal"))
        r = self.call(self.t, "memory_deprecate", name="project_r.md", reason="see: project_s.md #2, it's newer")
        self.assertNotIn("error", r)
        body = git(self.hub, "show", "main:project_r.md")
        self.assertIsNotNone(rules.yaml_meta(body))
        self.assertEqual(rules.ambiguities(body), [])
        self.assertEqual(rules.meta(body)["status"], "outdated")


if __name__ == "__main__":
    unittest.main()
