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


class ClassifyAddedLines(unittest.TestCase):
    """Added-line composition — the basis of --output stats."""

    def test_csharp_line_and_block_comments(self):
        patch = (
            "--- a/x.cs\n"
            "+++ b/x.cs\n"
            "+// a line comment\n"
            "+var x = 1;\n"
            "+\n"
            "+/* a block\n"
            "+   still the block\n"
            "+*/\n"
            "+var y = 2;\n"
            "-var gone = 3;\n"
            " context;\n"
        )
        s = g.classify_added_lines("x.cs", patch)
        self.assertEqual(s, {"code": 2, "comment": 4, "blank": 1})

    def test_removed_and_context_lines_are_not_counted(self):
        patch = "--- a/x.cs\n+++ b/x.cs\n-removed();\n context();\n+added();\n"
        self.assertEqual(g.classify_added_lines("x.cs", patch),
                         {"code": 1, "comment": 0, "blank": 0})

    def test_block_that_closes_with_trailing_code_counts_as_code(self):
        patch = "+++ b/x.cs\n+/* note */ var x = 1;\n"
        self.assertEqual(g.classify_added_lines("x.cs", patch)["code"], 1)

    def test_python_hash_comments(self):
        patch = "+++ b/x.py\n+# note\n+x = 1\n"
        s = g.classify_added_lines("x.py", patch)
        self.assertEqual((s["code"], s["comment"]), (1, 1))

    def test_sql_double_dash_is_a_comment_not_a_removed_line(self):
        # '--' opens a SQL comment; the patch marker is the leading '+'.
        patch = "+++ b/x.sql\n+-- comment\n+SELECT 1;\n"
        s = g.classify_added_lines("x.sql", patch)
        self.assertEqual((s["code"], s["comment"]), (1, 1))

    def test_html_style_comments_in_cshtml(self):
        patch = "+++ b/x.cshtml\n+<!-- note -->\n+<div>hi</div>\n"
        s = g.classify_added_lines("x.cshtml", patch)
        self.assertEqual((s["code"], s["comment"]), (1, 1))

    def test_unknown_extension_counts_everything_as_code(self):
        patch = "+++ b/x.unknownext\n+whatever\n+// not a known comment syntax here\n"
        self.assertEqual(g.classify_added_lines("x.unknownext", patch)["code"], 2)


class ReviewChecks(unittest.TestCase):
    def test_extract_tailwind_only_takes_arbitrary_values(self):
        lines = [("a.cshtml", 10, '<div class="tw-text-[10px] tw-flex tw-max-w-[110px]">')]
        found = g.extract_tailwind_classes(lines)
        self.assertEqual(sorted(found), ["tw-max-w-[110px]", "tw-text-[10px]"])
        self.assertEqual(found["tw-text-[10px]"], ["a.cshtml:10"])

    def test_tailwind_compiled_matches_the_escaped_selector(self):
        # Tailwind escapes the brackets in the emitted selector; a raw substring
        # search would report every arbitrary-value class as missing.
        css = r".tw-text-\[10px\]{font-size:10px}"
        self.assertTrue(g.tailwind_compiled(css, "tw-text-[10px]"))
        self.assertFalse(g.tailwind_compiled(css, "tw-text-[11px]"))

    def test_extract_resource_keys(self):
        lines = [("a.cshtml", 5, '@Engine.Get("Chat_Title") and @Engine.Get("Chat_Sub")')]
        found = g.extract_resource_keys(lines)
        self.assertEqual(sorted(found), ["Chat_Sub", "Chat_Title"])

    def test_resx_has_key_is_exact(self):
        resx = '<data name="Chat_Title" xml:space="preserve"><value>Hi</value></data>'
        self.assertTrue(g.resx_has_key(resx, "Chat_Title"))
        self.assertFalse(g.resx_has_key(resx, "Chat_Titl"))

    def test_added_lines_tracks_new_file_line_numbers(self):
        results = [{"files": [{"path": "/a.cs", "patch":
            "@@ -1,2 +10,3 @@\n ctx\n+first\n+second\n"}]}]
        self.assertEqual(g.added_lines(results),
                         [("/a.cs", 11, "first"), ("/a.cs", 12, "second")])


class FetchAtPrHead(unittest.TestCase):
    """The fix for reading the wrong tree: pin to the PR's head commit."""

    def setUp(self):
        self.calls = []
        self._real = g.api_get_file_content

        def fake(cfg, org, project, repo, file_path, branch=None, commit_id=None):
            self.calls.append(dict(org=org, project=project, repo=repo,
                                   path=file_path, branch=branch, commit_id=commit_id))
            if file_path == "missing.resx":
                raise RuntimeError("HTTP 404 Not Found: https://x")
            if file_path == "denied.resx":
                raise RuntimeError("HTTP 401 Unauthorized: https://x")
            if file_path == "offline.resx":
                raise OSError("connection reset")
            return "<data name=\"K\" />"

        g.api_get_file_content = fake

    def tearDown(self):
        g.api_get_file_content = self._real

    def _result(self, **over):
        pr = {"org": "o", "project": "p", "repo": "r",
              "sourceCommit": "e7ce42687f93f0", "sourceRef": "users/x/branch"}
        pr.update(over)
        return {"pr": pr}

    def test_pins_to_the_commit_not_the_branch(self):
        # A branch name would let a mid-review push change the answer; the commit cannot.
        g.fetch_at_pr_head({}, self._result(), "a.resx")
        self.assertEqual(self.calls[0]["commit_id"], "e7ce42687f93f0")
        self.assertIsNone(self.calls[0]["branch"])

    def test_falls_back_to_the_branch_only_when_no_commit_is_pinned(self):
        g.fetch_at_pr_head({}, self._result(sourceCommit=None), "a.resx")
        self.assertIsNone(self.calls[0]["commit_id"])
        self.assertEqual(self.calls[0]["branch"], "users/x/branch")

    def test_missing_file_is_none_not_an_exception(self):
        self.assertIsNone(g.fetch_at_pr_head({}, self._result(), "missing.resx"))

    def test_auth_failure_raises_instead_of_reading_as_missing(self):
        # A 401 says nothing about the file; reporting it "missing" would be a false finding.
        with self.assertRaises(RuntimeError) as ctx:
            g.fetch_at_pr_head({}, self._result(), "denied.resx")
        self.assertIn("401", str(ctx.exception))

    def test_network_failure_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            g.fetch_at_pr_head({}, self._result(), "offline.resx")

    def test_no_coordinates_raises_without_a_call(self):
        # Coordinates are attached at fetch time; a cached pr_meta lacking them must
        # not produce a "file not found" verdict from a request never made.
        with self.assertRaises(RuntimeError):
            g.fetch_at_pr_head({}, {"pr": {"sourceCommit": "abc"}}, "a.resx")
        self.assertEqual(self.calls, [])


class RejectedPat(unittest.TestCase):
    """Azure answers a bad PAT with 203 + its sign-in HTML, not with a 401."""

    class _Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        self._real = g.urllib.request.urlopen

    def tearDown(self):
        g.urllib.request.urlopen = self._real

    def test_sign_in_page_raises_instead_of_being_returned_as_content(self):
        g.urllib.request.urlopen = lambda req, timeout=30: self._Resp(203, b"<html>Sign In</html>")
        with self.assertRaises(RuntimeError) as ctx:
            g._api_request("https://x/items", "bad", accept="text/plain")
        self.assertIn("rejected the PAT", str(ctx.exception))

    def test_200_still_returns_the_body(self):
        g.urllib.request.urlopen = lambda req, timeout=30: self._Resp(200, b"<root/>")
        self.assertEqual(g._api_request("https://x/items", "ok", accept="text/plain"), "<root/>")


class PlusPlusContent(unittest.TestCase):
    """Inside a hunk, `+++`/`---` are content, not file headers."""

    PATCH = (
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,2 +1,4 @@\n"
        " int i = 0;\n"
        "+++i;\n"
        "+i += 2;\n"
        "---i;\n"
        " return i;\n"
    )

    def _results(self):
        return [{"files": [{"path": "x.c", "patch": self.PATCH}]}]

    def test_patch_stats_counts_the_plus_plus_line(self):
        self.assertEqual(g.patch_stats(self.PATCH), {"added": 2, "removed": 1})

    def test_added_lines_keeps_numbering_after_a_plus_plus_line(self):
        self.assertEqual(g.added_lines(self._results()),
                         [("x.c", 2, "++i;"), ("x.c", 3, "i += 2;")])

    def test_to_lines_keeps_numbering_after_a_plus_plus_line(self):
        self.assertEqual(g.to_lines(self._results()).splitlines(),
                         ["x.c:2  ++i;", "x.c:3  i += 2;"])

    def test_classify_counts_the_plus_plus_line_as_code(self):
        self.assertEqual(g.classify_added_lines("x.c", self.PATCH),
                         {"code": 2, "comment": 0, "blank": 0})


class ToStats(unittest.TestCase):
    def _results(self, *files):
        return [{"files": [{"path": p, "patch": patch} for p, patch in files]}]

    def test_totals_and_ratio(self):
        out = g.to_stats(self._results(
            ("a.cs", "@@ -0,0 +1,4 @@\n+// why\n+int a;\n+int b;\n+\n"),
            ("b.cs", "@@ -0,0 +1,1 @@\n+int c;\n"),
        ))
        self.assertIn("Added lines: 3 code, 1 comment, 1 blank.", out)
        total = [l for l in out.splitlines() if l.startswith("TOTAL")][0]
        self.assertTrue(total.rstrip().endswith("0.33"), total)

    def test_comment_only_file_shows_all_instead_of_a_ratio(self):
        out = g.to_stats(self._results(("a.py", "@@ -0,0 +1,1 @@\n+# only a note\n")))
        row = [l for l in out.splitlines() if l.startswith("a.py")][0]
        self.assertIn("all", row)
        self.assertNotIn("inf", out)

    def test_skipped_files_and_no_additions(self):
        self.assertEqual(g.to_stats([{"files": [{"path": "a.cs", "skipped": True}]}]),
                         "No added lines.")
