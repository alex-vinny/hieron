# Hieron — Azure DevOps diff fetcher

Fetches Azure DevOps **PR** and **commit** diffs as **actual unified patches**
(not full file contents), caches them in SQLite, and emits analysis-ready JSON
chunks for AI review. Also compares two **tags** of a repo (commits + changed
files).

```
python git_diff_analysis.py <command>
```

The unified diff carries only changed lines, so a PR that would be several
megabytes of full-file content becomes a compact, chunked patch — small enough
to hand to a model within its context window.

## Requirements

- **Python 3.8+** (standard library only — no third-party packages). On a machine where a legacy Python 2
  comes first on `PATH`, bare `python` fails with `SyntaxError: Non-ASCII character '\xe2'`: run it through the
  `git-diff-analysis.cmd` shim (it calls `py -3`) or `py -3 git_diff_analysis.py`. The `python` in the examples
  below means a Python 3 interpreter.
- An Azure DevOps **PAT**, read from `~/.azure-connector.json` or the `AZURE_PAT`
  environment variable.

## Setup

Provide a PAT one of two ways:

- Set `AZURE_PAT` (and optionally `AZURE_ORG`, `AZURE_BASE_URL`) in your shell, or
- Write `~/.azure-connector.json` with `{ "pat": "...", "org": "...",
  "baseUrl": "https://dev.azure.com" }` (only `pat` is required).

The org is normally read from the PR/commit URL; only `tags` with a bare project
name falls back to the configured org.

## Diffs are pinned to the latest iteration

A PR diff is computed between the PR's **merge base** (`commonRefCommit`) and its
**source commit** (`sourceRefCommit`) from the latest iteration — not between
live branch tips. So it:

- works on **completed / abandoned** PRs (branch tips may have moved or been deleted), and
- produces right-side line numbers that match what Azure DevOps anchors inline
  comments against (so `--output lines` numbers line up with an inline-comment tool).

The iteration list is read on every run (one call), and everything else is cached under the
**commit pair**. A new push is a new iteration with a new source commit, so the cache can never
serve a stale diff and `--force` is only needed to rule the cache out.

## Two backends: the REST API (default) or a local clone (`--repo`)

| | REST API (default) | `--repo <clone>` |
|---|---|---|
| How | downloads both versions of each changed file (6 in parallel), diffs in Python | `git diff` between the two commits |
| Speed | one request per file side | one `git` call; missing commits are fetched by SHA |
| Algorithm | `difflib` with autojunk off + git-style slider placement | git `histogram`, indent heuristic |
| Hunk headers | `@@ -a,b +c,d @@` | `@@ -a,b +c,d @@ public void Method()` (built-in funcname drivers for C#, Java, Python, Go, …; the repo's own `.gitattributes` wins) |
| `--function-context` | no | yes (whole enclosing function) |
| Binaries | NUL-sniffed: size + short sha256 (+ image dimensions) | the same, also for `.png`/`.dll`/… that the REST backend skips |

`--repo` reads git objects only: it never checks out, never moves a branch and never touches the
working tree, so it is safe on a clone with uncommitted work. Both backends give the same
`file:line` anchors in practice; where a block can be aligned two valid ways they may differ by a
few lines (checked on real PRs: 2 of 3 identical, the third 7 lines of 545 placed differently).

## What the diff reports

- **Renames** are one entry with `oldPath` (Azure sends the old path in `sourceServerItem` and a
  separate `delete, sourceRename` entry, which is dropped). Before, a renamed file showed up as a
  full addition plus a full deletion.
- **A file without a final newline** gets git's `\ No newline at end of file` marker; lines are
  split on `\n` only, so `\x0c` or `\u2028` inside a line never shifts the numbering.
- **Whitespace-only** changes are flagged (`whitespaceOnly: true`), never hidden: a whitespace
  change inside a string literal is still a change.
- **Binary files** (a NUL in the first 8000 bytes; UTF-16 with a BOM is read as text) carry
  `binary: {old, new}` with size, short sha256 and, for PNG/GIF/JPEG, dimensions.
- **Submodules** show as `Subproject commit <sha>` lines, as in `git diff`.
- **Skipped files** are listed with a reason (`binary-extension`, `generated`, `excluded`).
  `--include '*.resx'` brings back a file the defaults skip; `--exclude 'Migrations/*'` adds one.
- **Any read failure is an error** (exit 2, message on stderr even when stdout is redirected).
  A file that could not be downloaded used to come out as fully added, and was cached that way.
- Change lists are paged until Azure marks the last page (`allChangesIncluded`): big PRs and tag
  comparisons are no longer cut at 500/1000 entries.

## Second review round: `--since-iteration N`

`pr <url> --since-iteration 2` diffs iteration 2's source commit against the latest one: what the
author changed since you last looked. Line numbers are still the latest iteration's, so anchors stay
valid. If the author merged the target branch in between, the merge base moved and the diff also
carries everything that came from the target; the tool says so (`targetMergedSince: true` and a
warning).

## Commands

| Command | What |
|---|---|
| `pr <url> [<url> ...]` | fetch and diff one or more PRs |
| `commit <url> [<url> ...]` | fetch and diff one or more commits |
| `tags <project> <repo> <oldTag> <newTag>` | compare two tags: commits + changed files |
| `tags <repo-url> <oldTag> <newTag>` | same, project/repo taken from the URL |
| `cache stats` | show cache size and age |
| `cache clear [<url>]` | clear the whole cache, or one PR/commit |
| `cache prune [--days 30]` | drop rows older than N days and compact the file |
| `check-tailwind <pr-url> --css <repo-path> \| --css-local <file> [--prefix tw-]` | are the arbitrary-value Tailwind classes the PR adds actually compiled? |
| `check-resx <pr-url> --resx "<repo path with {locale}>" [--root <clone>] [--locales a,b,c] [--getter Engine.Get]` | does every resource key the PR adds exist in all localized `.resx` files? |

Shared flags for `pr` / `commit`:

| Flag | What |
|---|---|
| `--output json\|summary\|md\|lines\|stats` | output format (default `json`) |
| `--chunk N` | max lines per JSON patch chunk (default 300) |
| `--force` | bypass the cache and re-fetch |
| `--quiet`, `-q` | suppress progress on stderr (also auto-off when stdout is redirected); errors still show |
| `--repo <clone>` | diff in a local clone with git instead of the REST API |
| `--context N`, `-U N` | context lines per hunk (default 5) |
| `--function-context` | with `--repo`: the whole enclosing function around each change |
| `--include GLOB`, `--exclude GLOB` | repeatable; match the full path or the file name |
| `--workers N` | parallel downloads through the API (default 6) |
| `--since-iteration N` | `pr` only: diff iteration N against the latest (see above) |

`--output lines` emits `file:line  content` for every added line, reconstructing
new-file line numbers from the hunk headers — useful for pinpointing exact
line numbers before posting inline PR comments. Because diffs are pinned to the
iteration's source commit, those numbers match Azure's right-side anchor.

`--output stats` breaks the **added** lines down into code / comment / blank per file,
with a comment:code ratio and a total. Added-only is deliberate: a review judges what
is being introduced, and counting whole files measures the codebase's history instead.
Comment syntax is chosen by file extension (`//`, `#`, `--`, `<!-- -->`, `/* */`), and a
multi-line block is tracked so only its first line is not counted as code. Treat the ratio
as evidence, not a verdict.

### The review checks

Both read the PR's **added lines** — the same reconstruction `--output lines` uses — and both
read the file they compare against **at the PR's head commit**, not from your working tree.
A local clone answers a question about whichever branch happens to be checked out, so a key
the PR does add can come back "missing". Pinning to the commit (not the branch name) also
means a push mid-review cannot change the answer.

A file that does not exist at the head commit is reported as such. Any other read failure
(expired PAT, network) is an `ERROR` with exit code 2, never a "missing" finding.

```bash
# every arbitrary-value class the PR introduces, checked against the built CSS
python git_diff_analysis.py check-tailwind <pr-url> --css web/static/css/tailwind.css
```

`--css` is a **repo path**, read at the PR head. Use `--css-local <file>` only when the CSS is
built in CI and never committed — the output labels that mode `NOT the PR's tree`, because then
a "missing" class may just mean a stale local build.

Tailwind escapes brackets in the emitted selector (`.tw-text-\[10px\]`), so searching the CSS
for the raw class name reports every arbitrary-value class as missing. Both forms are tried.

```bash
# every Engine.Get("...") key the PR introduces, checked in each locale
python git_diff_analysis.py check-resx <pr-url> \
  --resx "src/App/Resources/Strings.{locale}.resx" --locales pt-BR,en-US,es-ES
```

`--getter` names the accessor to scan for (default `Engine.Get`, matching `Engine.Get("Key")`);
`--locales` defaults to `pt-BR,en-US,es-ES`.

`--root <clone>` switches `check-resx` to a local tree. It then prints the clone's branch next
to the PR head and **warns loudly when they differ**. Prefer the default. Exit code is 1 when
something is missing and 2 when a file could not be read, so both commands work in a pipeline.

> **PowerShell note:** `%20` and other `%xx` sequences in URLs are expanded by
> PowerShell before reaching the tool. Double the percent signs (`%%20`) or put
> the command in a `.bat` file. A mangled URL ends with `ERROR: Cannot parse PR URL`.

## Examples

```bash
# PR diff as JSON chunks (default, for AI analysis)
python git_diff_analysis.py pr https://dev.azure.com/your-org/YourProject/_git/your-repo/pullrequest/123

# Human-readable file list + line stats
python git_diff_analysis.py pr <pr-url> --output summary

# Markdown with fenced diff blocks
python git_diff_analysis.py pr <pr-url> --output md

# One line per added line (file:line content) for inline-comment anchoring
python git_diff_analysis.py pr <pr-url> --output lines

# A single commit by commit URL
python git_diff_analysis.py commit <commit-url>

# Compare two tags of a repo
python git_diff_analysis.py tags "YourProject" "your-repo" "v1.0.0" "v1.1.0"
python git_diff_analysis.py tags "https://dev.azure.com/your-org/YourProject/_git/your-repo" "v1.0.0" "v1.1.0"

# Force re-fetch, then inspect the cache
python git_diff_analysis.py pr <pr-url> --force
python git_diff_analysis.py cache stats
python git_diff_analysis.py cache clear <pr-url>
```

On Windows, `git-diff-analysis.cmd` is a thin shim so you can drop `python` and
just run `git-diff-analysis <command>`.

### JSON output shape

```json
[
  {
    "pr": {
      "id": 123,
      "title": "feat: add widget endpoint",
      "author": "Ada Lovelace",
      "sourceRef": "features/widget",
      "targetRef": "main"
    },
    "files": [
      {
        "path": "/src/Widget/WidgetService.cs",
        "changeType": "add",
        "stats": { "added": 120, "removed": 0 },
        "chunks": ["@@ -0,0 +1,120 @@\n+using System;\n+..."]
      }
    ]
  }
]
```

Each `chunks` entry is a unified-diff segment ≤ `--chunk` lines with hunk headers
intact, so it can be fed to a model independently. `commit` output uses a
`commit` header (id / shortId / author / date / shortMessage) instead of `pr`.

### `tags` output shape

```json
{
  "project": "YourProject",
  "repo": "your-repo",
  "oldTag": "v1.0.0",
  "newTag": "v1.1.0",
  "aheadCount": 5,
  "behindCount": 0,
  "baseCommit": "2e0ee3fc...",
  "targetCommit": "a1365ce6...",
  "changeCounts": { "Edit": 50, "Add": 2, "Rename": 3 },
  "commits": [ { "id": "a1365ce6de", "author": "...", "date": "...", "comment": "..." } ],
  "changes": [ { "change": "edit", "path": "/path/to/file.cs" } ]
}
```

## Cache

Results are cached in `~/.git-diff-analysis.db` (SQLite), keyed by the commit pair a diff
compares, so they never go stale and need no expiry. Only patches and file metadata are stored,
not full file contents (an older cache that kept them reached ~180 MB; `cache prune` drops old
rows and those contents, then compacts the file). The `--repo` backend is not cached: local git
is already fast.

## Tests

Unit tests (stdlib `unittest`, no network; the `--repo` tests build a throwaway git repository
and are skipped when `git` is not on `PATH`):

```bash
python -m unittest test_git_diff_analysis -v
```

Covers patch building (final-newline marker, slider placement, autojunk), line-number
reconstruction, rename/paging normalization of the Azure response, binary detection, git output
parsing, both backends end to end (network replaced by fakes) and the review checks.

## License

MIT — see [LICENSE](LICENSE).
