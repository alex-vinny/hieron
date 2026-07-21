"""Unit tests for git_diff_analysis pure functions (no network).

Run:  python -m unittest test_git_diff_analysis -v
"""
import unittest

import git_diff_analysis as g


class PatchStats(unittest.TestCase):
    def test_counts_added_and_removed_excluding_headers(self):
        patch = (
            "--- a/x.cs\n"
            "+++ b/x.cs\n"
            "@@ -1,2 +1,3 @@\n"
            " ctx\n"
            "+added one\n"
            "+added two\n"
            "-removed one\n"
        )
        self.assertEqual(g.patch_stats(patch), {"added": 2, "removed": 1})


class ComputePatch(unittest.TestCase):
    def test_added_line_appears_in_patch(self):
        old = "a\nb\nc\n"
        new = "a\nb\nB2\nc\n"
        patch = g.compute_patch("/x.cs", old, new)
        self.assertIn("+B2", patch)
        self.assertEqual(g.patch_stats(patch)["added"], 1)

    def test_new_file_has_no_removals(self):
        patch = g.compute_patch("/new.cs", None, "line1\nline2\n")
        self.assertEqual(g.patch_stats(patch)["removed"], 0)
        self.assertGreaterEqual(g.patch_stats(patch)["added"], 2)


class ToLines(unittest.TestCase):
    def _results(self, path, patch):
        return [{"files": [{"path": path, "changeType": "edit", "patch": patch}]}]

    def test_added_line_number_matches_hunk(self):
        # new hunk starts at line 1: ' a'=1, '+b'=2, ' c'=3
        patch = "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
        out = g.to_lines(self._results("/x.cs", patch))
        self.assertEqual(out, "/x.cs:2  b")

    def test_removed_lines_do_not_advance_line_number(self):
        # base 20: ' c1'=20->21, '+n1'=21, '-old' no advance, '+n2'=22
        patch = "@@ -10,3 +20,4 @@\n c1\n+n1\n-old\n+n2\n"
        out = g.to_lines(self._results("/y.cs", patch)).splitlines()
        self.assertEqual(out, ["/y.cs:21  n1", "/y.cs:22  n2"])

    def test_skipped_file_emits_nothing(self):
        results = [{"files": [{"path": "/z.cs", "skipped": True}]}]
        self.assertEqual(g.to_lines(results), "")


class ChunkPatch(unittest.TestCase):
    def test_small_patch_is_single_chunk(self):
        patch = "@@ -1 +1 @@\n+x\n"
        self.assertEqual(g.chunk_patch(patch, max_lines=300), [patch])

    def test_empty_patch_is_no_chunks(self):
        self.assertEqual(g.chunk_patch("", max_lines=300), [])

    def test_large_patch_splits_and_each_chunk_anchors_on_hunk_header(self):
        # chunk_patch splits at hunk boundaries, so exercise splitting with several
        # hunks that each push the buffer past max_lines.
        header = "--- a/x.cs\n+++ b/x.cs\n"
        hunk = lambda start: (f"@@ -{start},60 +{start},60 @@\n"
                              + "".join(f"+l{start}_{i}\n" for i in range(60)))
        body = hunk(1) + hunk(100) + hunk(200) + hunk(300)
        chunks = g.chunk_patch(header + body, max_lines=50)
        self.assertGreater(len(chunks), 1)
        # every chunk after the first must start with a hunk header so line
        # reconstruction keeps working across chunk boundaries
        for c in chunks[1:]:
            self.assertTrue(c.lstrip().startswith("@@"), f"chunk did not start with @@: {c[:40]!r}")


if __name__ == "__main__":
    unittest.main()
