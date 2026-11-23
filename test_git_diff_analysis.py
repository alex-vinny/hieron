"""Unit tests for git_diff_analysis pure functions (no network).

Run:  python -m unittest test_git_diff_analysis -v
"""
import unittest

import git_diff_analysis as g
g._PROGRESS = None  # keep test output clean


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


class NoNewlineAtEof(unittest.TestCase):
    """A last line without a newline used to glue two diff lines together (`-b+c`)."""

    def test_marker_keeps_lines_apart(self):
        patch = g.compute_patch("/x.ts", "a\nb", "a\nc")
        self.assertIn("-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n", patch)
        self.assertEqual(g.patch_stats(patch), {"added": 1, "removed": 1})

    def test_added_line_is_numbered(self):
        patch = g.compute_patch("/x.ts", "a\nb", "a\nc")
        out = g.to_lines([{"files": [{"path": "/x.ts", "patch": patch}]}])
        self.assertEqual(out, "/x.ts:2  c")

    def test_marker_does_not_advance_the_line_counter(self):
        patch = "@@ -1,2 +1,3 @@\n a\n-b\n\\ No newline at end of file\n+b\n+c\n"
        self.assertEqual(list(g.iter_added(patch)), [(2, "b"), (3, "c")])

    def test_adding_the_final_newline_is_a_change(self):
        patch = g.compute_patch("/x.ts", "a\nb", "a\nb\n")
        self.assertEqual(g.patch_stats(patch), {"added": 1, "removed": 1})


class ComputePatchFormat(unittest.TestCase):
    def test_same_text_as_difflib_on_a_small_edit(self):
        import difflib
        old = "".join(f"l{i}\n" for i in range(30))
        new = old.replace("l12\n", "L12\nextra\n")
        expected = "".join(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), "a/x.cs", "b/x.cs", n=5))
        self.assertEqual(g.compute_patch("/x.cs", old, new), expected)

    @staticmethod
    def _test_class(methods):
        body = "".join(
            f"        [Test]\n        public void {name}()\n        {{\n"
            + "".join(f"            Assert.AreEqual({n}, x);\n" for n in asserts)
            + "        }\n\n"
            for name, asserts in methods)
        return "    class T\n    {\n" + body + "    }\n"

    def test_inserted_method_is_anchored_on_its_own_lines(self):
        # The new method shares `}` + blank with the one before it and `[Test]` with
        # the one after: the block can slide. difflib alone marked lines 16-22 (the
        # previous method's `}`); the method really is lines 18-24, as git reports.
        ms = [("M0", [2, 2]), ("M1", [3, 0, 0]), ("M2", [0, 2, 2]), ("M3", [3, 2, 2]), ("M4", [2])]
        old = self._test_class(ms)
        new = self._test_class(ms[:2] + [("Novo", [2, 2])] + ms[2:])
        patch = g.compute_patch("/x.cs", old, new)
        self.assertEqual([n for n, _ in g.iter_added(patch)], list(range(18, 25)))

    def test_align_sliders_prefers_a_block_that_ends_on_a_blank_line(self):
        a = ["x\n", "}\n", "\n", "y\n"]
        b = ["x\n", "}\n", "\n", "}\n", "\n", "y\n"]
        codes = [("equal", 0, 1, 0, 1), ("insert", 1, 1, 1, 3), ("equal", 1, 4, 3, 6)]
        self.assertEqual(g.align_sliders(codes, a, b),
                         [("equal", 0, 3, 0, 3), ("insert", 3, 3, 3, 5), ("equal", 3, 4, 5, 6)])

    def test_identical_content_is_an_empty_patch(self):
        self.assertEqual(g.compute_patch("/x.cs", "a\n", "a\n"), "")

    def test_rename_header_names_both_paths(self):
        patch = g.compute_patch("/new/x.cs", "a\n", "b\n", old_path="/old/x.cs")
        self.assertTrue(patch.startswith("--- a/old/x.cs\n+++ b/new/x.cs\n"))

    def test_only_newline_splits_lines(self):
        # \x0c and \u2028 inside a line must not shift the numbers git and Azure use.
        old = "a\x0cb\nc\n"
        new = "a\x0cb\nc\nd\u2028e\n"
        patch = g.compute_patch("/x.cs", old, new)
        self.assertEqual(list(g.iter_added(patch)), [(3, "d\u2028e")])

    def test_no_autojunk(self):
        # A real case from a random search: with difflib's autojunk on, `}`, blank and
        # `return;` (each >1% of a 200-line file) are junk, and one inserted line comes
        # out as +4/-3. Rare (3 of 400 synthetic files), but wrong when it happens.
        code = {"}": "    }", "_": "", "r": "        return;"}
        tokens = (
            "} } _ } 8 r _ r _ 4 48 _ } 63 50 9 _ } } _ } } _ } _ } _ _ 37 11 _ _ 25 } } } r _ } 8 _ 19 "
            "7 } _ r _ 39 10 62 _ 45 24 } } 58 _ 6 } 37 } } } } _ _ 39 } 14 47 _ } } } r } } _ } 19 7 "
            "31 50 51 45 } } } 26 _ _ } 55 _ } _ r 41 } } _ 1 } 43 _ } } } } } 66 _ } 60 r r } 20 } } "
            "30 4 _ } } } 60 54 8 } } _ r r 13 _ } 18 _ } r 54 } } _ } 30 _ _ 15 0 } r _ 40 } } 14 33 _ "
            "} _ r _ 41 } } } r 42 r _ _ 60 _ 44 } 60 30 56 _ 24 _ 18 r } 45 43 _ } 51 _ 29 54 } } 41 _ "
            "} _"
        ).split()
        old = [code.get(t, f"        Call{t}();") + "\n" for t in tokens]
        new = old[:197] + ["        Added();\n"] + old[197:]
        patch = g.compute_patch("/x.cs", "".join(old), "".join(new))
        self.assertEqual(g.patch_stats(patch), {"added": 1, "removed": 0})


class WhitespaceOnly(unittest.TestCase):
    def test_reindent_is_whitespace_only(self):
        patch = g.compute_patch("/x.cs", "if (a)\n  b();\n", "if (a)\n    b();\n")
        self.assertTrue(g.patch_is_whitespace_only(patch))
        self.assertTrue(g.make_entry({"path": "/x.cs", "change_type": "edit"}, patch)["whitespaceOnly"])

    def test_real_change_is_not(self):
        patch = g.compute_patch("/x.cs", "b();\n", "c();\n")
        self.assertFalse(g.patch_is_whitespace_only(patch))

    def test_added_blank_line_is_not(self):
        patch = g.compute_patch("/x.cs", "a\nb\n", "a\n\nb\n")
        self.assertFalse(g.patch_is_whitespace_only(patch))


class NormalizeChanges(unittest.TestCase):
    # Shape taken from a real diffs/commits response with renamed files.
    RAW = [
        {"item": {"path": "/Core", "isFolder": True}, "changeType": "edit"},
        {"item": {"path": "/Core/Models/A.cs"}, "sourceServerItem": "/Core/Service/A.cs",
         "changeType": "edit, rename"},
        {"item": {"path": "/Core/Service/A.cs"}, "changeType": "delete, sourceRename"},
        {"item": {"path": "/Core/B.cs"}, "changeType": "add"},
    ]

    def test_rename_is_one_entry_with_its_old_path(self):
        self.assertEqual(g.normalize_changes(self.RAW), [
            {"path": "/Core/Models/A.cs", "change_type": "edit, rename", "old_path": "/Core/Service/A.cs"},
            {"path": "/Core/B.cs", "change_type": "add"},
        ])


class PaginateChanges(unittest.TestCase):
    def _pages(self, pages):
        calls = []

        def fetch(top, skip):
            calls.append(skip)
            return pages[len(calls) - 1]
        return fetch, calls

    def test_a_short_page_does_not_stop_the_loop(self):
        # The server may cap a page below $top; only allChangesIncluded ends it.
        fetch, calls = self._pages([
            {"changes": [1, 2], "baseCommit": "b"},
            {"changes": [3, 4]},
            {"changes": [5], "allChangesIncluded": True},
        ])
        first, changes = g.paginate_changes(fetch, page_size=500)
        self.assertEqual(changes, [1, 2, 3, 4, 5])
        self.assertEqual(calls, [0, 2, 4])
        self.assertEqual(first["baseCommit"], "b")

    def test_an_empty_page_stops_the_loop(self):
        fetch, _ = self._pages([{"changes": [1]}, {"changes": []}])
        self.assertEqual(g.paginate_changes(fetch)[1], [1])


class SkipReason(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(g.skip_reason("/a/logo.png"), "binary-extension")
        self.assertEqual(g.skip_reason("/a/Form.Designer.cs"), "generated")
        self.assertIsNone(g.skip_reason("/a/Form.cs"))

    def test_include_brings_back_a_skipped_file(self):
        self.assertEqual(g.skip_reason("/R/Strings.resx"), "generated")
        self.assertIsNone(g.skip_reason("/R/Strings.resx", include=["*.resx"]))

    def test_exclude_wins_and_matches_full_paths(self):
        self.assertEqual(g.skip_reason("/src/Migrations/001.cs", exclude=["src/Migrations/*"]), "excluded")
        self.assertEqual(g.skip_reason("/x.resx", include=["*.resx"], exclude=["*.resx"]), "excluded")


class BinaryBlobs(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + (640).to_bytes(4, "big") + (480).to_bytes(4, "big") + b"\x00" * 20)

    def test_nul_byte_means_binary(self):
        self.assertEqual(g.decode_blob(b"ab\x00c"), (None, True))

    def test_utf16_with_bom_is_text(self):
        text, binary = g.decode_blob("olá\n".encode("utf-16"))
        self.assertFalse(binary)
        self.assertEqual(text, "olá\n")

    def test_image_dimensions(self):
        self.assertEqual(g.image_size(self.PNG), (640, 480))
        gif = b"GIF89a" + (16).to_bytes(2, "little") + (8).to_bytes(2, "little")
        self.assertEqual(g.image_size(gif), (16, 8))
        self.assertIsNone(g.image_size(b"not an image"))

    def test_binary_entry_describes_both_sides(self):
        e = g.entry_from_blobs({"path": "/i.png", "change_type": "edit"}, self.PNG, self.PNG + b"x")
        self.assertEqual(e["binary"]["old"]["dimensions"], "640x480")
        self.assertEqual(e["binary"]["new"]["size"], len(self.PNG) + 1)
        self.assertEqual(e["patch"], "")


class GitOutputParsing(unittest.TestCase):
    def test_name_status_z(self):
        data = b"M\0a.cs\0R087\0old/b.cs\0new/b.cs\0A\0c d.cs\0D\0e.cs\0"
        self.assertEqual(g.parse_name_status_z(data), [
            {"status": "M", "old": "a.cs", "new": "a.cs"},
            {"status": "R", "old": "old/b.cs", "new": "new/b.cs"},
            {"status": "A", "old": None, "new": "c d.cs"},
            {"status": "D", "old": "e.cs", "new": None},
        ])

    def test_blocks_by_new_path(self):
        text = (
            "diff --git a/old/b.cs b/new/b.cs\n"
            "similarity index 90%\n"
            "rename from old/b.cs\n"
            "rename to new/b.cs\n"
            "index 1..2 100644\n"
            "--- a/old/b.cs\n"
            "+++ b/new/b.cs\n"
            "@@ -1 +1 @@ class B\n"
            "-x\n"
            "+y\n"
            "diff --git a/img.png b/img.png\n"
            "index 3..4 100644\n"
            "Binary files a/img.png and b/img.png differ\n"
            "diff --git a/same.cs b/moved/same.cs\n"
            "similarity index 100%\n"
            "rename from same.cs\n"
            "rename to moved/same.cs\n"
            "diff --git a/gone.cs b/gone.cs\n"
            "deleted file mode 100644\n"
            "--- a/gone.cs\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-z\n"
        )
        out = g.parse_git_diff(text)
        self.assertEqual(sorted(out), ["gone.cs", "img.png", "moved/same.cs", "new/b.cs"])
        self.assertTrue(out["new/b.cs"]["patch"].startswith("--- a/old/b.cs\n+++ b/new/b.cs\n@@"))
        self.assertTrue(out["img.png"]["binary"])
        self.assertEqual(out["moved/same.cs"]["patch"], "")
        self.assertEqual(g.patch_stats(out["gone.cs"]["patch"]), {"added": 0, "removed": 1})

    def test_quoted_path(self):
        text = ('diff --git "a/t\\303\\251st.cs" "b/t\\303\\251st.cs"\n'
                '--- "a/t\\303\\251st.cs"\n+++ "b/t\\303\\251st.cs"\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertIn("tést.cs", g.parse_git_diff(text))


class DiffViaApi(unittest.TestCase):
    """The REST backend with the network replaced: renames, failures, cache."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (g.DB_FILE, g.api_get_changes, g.api_get_blob)
        g.DB_FILE = g.Path(self.tmp.name) / "cache.db"
        self.db = g.get_db()
        self.downloads = []
        g.api_get_changes = lambda *a: ({}, [
            {"item": {"path": "/new/A.cs"}, "sourceServerItem": "/old/A.cs", "changeType": "edit, rename"},
            {"item": {"path": "/old/A.cs"}, "changeType": "delete, sourceRename"},
            {"item": {"path": "/B.cs"}, "changeType": "edit"},
        ])
        self.blobs = {("/old/A.cs", "base"): b"a\nb\n", ("/new/A.cs", "head"): b"a\nB\n",
                      ("/B.cs", "base"): b"x\n", ("/B.cs", "head"): b"x\ny\n"}

        def blob(cfg, org, project, repo, path, version, vtype="commit"):
            self.downloads.append((path, version))
            if (path, version) not in self.blobs:
                raise g.HttpError(500, f"HTTP 500: {path}")
            return self.blobs[(path, version)]
        g.api_get_blob = blob

    def tearDown(self):
        self.db.close()
        g.DB_FILE, g.api_get_changes, g.api_get_blob = self._saved
        self.tmp.cleanup()

    def _run(self):
        return g.diff_via_api({}, self.db, "o", "p", "r", "base", "head", "ns", {"workers": 2})

    def test_rename_reads_the_old_path_and_is_one_file(self):
        files = self._run()
        self.assertEqual([f["path"] for f in files], ["/B.cs", "/new/A.cs"])
        self.assertEqual(files[1]["oldPath"], "/old/A.cs")
        self.assertEqual(files[1]["stats"], {"added": 1, "removed": 1})
        self.assertIn(("/old/A.cs", "base"), self.downloads)

    def test_second_run_is_served_from_the_cache(self):
        self._run()
        self.downloads.clear()
        files = self._run()
        self.assertEqual(self.downloads, [])
        self.assertEqual(files[1]["oldPath"], "/old/A.cs")

    def test_a_failed_download_raises_instead_of_becoming_an_added_file(self):
        del self.blobs[("/B.cs", "base")]
        with self.assertRaises(RuntimeError):
            self._run()
        self.assertIsNone(g.cache_get_entry(self.db, "ns#U5", "/B.cs"))


class DiffViaGit(unittest.TestCase):
    """The local-clone backend against a throwaway repository."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")
        cls.tmp = tempfile.TemporaryDirectory()
        r = cls.repo = cls.tmp.name

        def git(*a):
            return subprocess.run(["git", "-C", r, *a], check=True, capture_output=True,
                                  text=True).stdout.strip()

        def write(path, data):
            p = g.Path(r) / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("config", "core.autocrlf", "false")
        body = "".join(f"    public void M{i}()\n    {{\n        Call{i}();\n    }}\n" for i in range(20))
        write("src/Old.cs", ("class C\n{\n" + body + "}\n").encode())
        write("tail.ts", b"a\nb")
        write("img.png", BinaryBlobs.PNG)
        write("gone.cs", b"z\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        cls.base = git("rev-parse", "HEAD")
        git("mv", "src/Old.cs", "src/New.cs")
        edited = body.replace("        Call7();\n", "        Call7();\n        Added();\n")
        write("src/New.cs", ("class C\n{\n" + edited + "}\n").encode())
        write("tail.ts", b"a\nc")
        write("img.png", BinaryBlobs.PNG + b"more")
        (g.Path(r) / "gone.cs").unlink()
        git("add", "-A")
        git("commit", "-qm", "head")
        cls.head = git("rev-parse", "HEAD")
        cls.files = {f["path"]: f for f in g.diff_via_git(r, cls.base, cls.head, {})}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_rename_with_edit(self):
        f = self.files["/src/New.cs"]
        self.assertEqual((f["changeType"], f["oldPath"]), ("edit, rename", "/src/Old.cs"))
        self.assertEqual(f["stats"], {"added": 1, "removed": 0})

    def test_hunk_header_names_the_method(self):
        header = [l for l in self.files["/src/New.cs"]["patch"].split("\n") if l.startswith("@@")][0]
        self.assertIn("public void M", header)

    def test_line_numbers_match_the_rest_backend(self):
        self.assertEqual(list(g.iter_added(self.files["/tail.ts"]["patch"])), [(2, "c")])

    def test_binary_is_described(self):
        self.assertEqual(self.files["/img.png"]["binary"]["new"]["dimensions"], "640x480")

    def test_deletion(self):
        self.assertEqual(self.files["/gone.cs"]["stats"], {"added": 0, "removed": 1})

    def test_missing_commit_is_an_error(self):
        with self.assertRaises(RuntimeError):
            g.diff_via_git(self.repo, "0" * 40, self.head, {})


if __name__ == "__main__":
    unittest.main()
