#!/usr/bin/env python3
"""
git-diff-analysis — Azure DevOps PR and commit diff fetcher with SQLite cache.

Fetches actual unified patches (not full file contents), caches them in SQLite,
and outputs analysis-ready JSON chunks.

Usage:
    python git_diff_analysis.py pr <url> [<url2> ...]  [--output json|summary|md|lines|stats] [--repo <clone>] [--since-iteration N]
    python git_diff_analysis.py commit <commit-url> [<commit-url2> ...]  [--output ...] [--repo <clone>]
    python git_diff_analysis.py tags <project> <repo> <oldTag> <newTag>
    python git_diff_analysis.py cache stats | clear [<url>] | prune [--days N]

Config: reads ~/.azure-connector.json (a JSON file with "pat"/"org"/"baseUrl")
        or env vars: AZURE_PAT, AZURE_ORG, AZURE_BASE_URL
"""

import argparse
import base64
import codecs
import difflib
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

# Force UTF-8 stdout/stderr (PR titles / file names can contain non-ASCII / emoji;
# some consoles default to a legacy code page). A no-op where UTF-8 is already set.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".azure-connector.json"
DB_FILE = Path.home() / ".git-diff-analysis.db"
API_VERSION = "7.1"
CACHE_VERSION = "v3"

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".exe", ".dll", ".so", ".bin", ".pdf",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
}

SKIP_PATTERNS = [
    re.compile(r"\.Designer\.cs$", re.I),
    re.compile(r"\.min\.js$"),
    re.compile(r"\.min\.css$"),
    re.compile(r"node_modules/"),
    re.compile(r"\.resx$"),
    re.compile(r"packages\.lock\.json$"),
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
]

# Files whose first 8000 bytes hold a NUL are binary — git's own heuristic.
BINARY_SNIFF_BYTES = 8000

# Above this many lines, difflib's autojunk heuristic is kept on for speed. Below it,
# autojunk is off: with it on, any line that is >1% of a 200+ line file (`}`, blank,
# `return;`) is treated as junk and the hunks come out misaligned.
AUTOJUNK_MIN_LINES = 40000

DEFAULT_WORKERS = 6


# ── Progress and errors ───────────────────────────────────────────────────────
#
# Progress goes through log() and is silenced by --quiet or a redirected stdout.
# Errors always reach stderr: silencing them used to turn every failure into a
# bare exit 1 with no message.

_PROGRESS = sys.stderr


def log(msg):
    if _PROGRESS is not None:
        print(msg, file=_PROGRESS)


def warn(msg):
    print(msg, file=sys.stderr)


def load_config():
    # PAT and org come from ~/.azure-connector.json or AZURE_* env vars; nothing is
    # hardcoded. The org is usually taken from the PR/commit URL, so only the `tags`
    # command with a bare project name needs a configured org.
    cfg = {"pat": "", "org": "", "baseUrl": "https://dev.azure.com"}
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg["pat"] = os.environ.get("AZURE_PAT", cfg.get("pat", ""))
    cfg["org"] = os.environ.get("AZURE_ORG", cfg.get("org", ""))
    cfg["baseUrl"] = os.environ.get("AZURE_BASE_URL", cfg.get("baseUrl", "https://dev.azure.com"))
    return cfg


def parse_pr_url(raw_url: str) -> Optional[dict]:
    try:
        parsed = urllib.parse.urlparse(raw_url)
        parts = [p for p in parsed.path.split("/") if p]
        # [org, project, _git, repo, pullrequest, prId]
        try:
            git_idx = parts.index("_git")
        except ValueError:
            return None
        try:
            pr_idx = parts.index("pullrequest")
        except ValueError:
            return None
        return {
            "type": "pr",
            "org": parts[0],
            "project": urllib.parse.unquote(parts[git_idx - 1]),
            "repo": urllib.parse.unquote(parts[git_idx + 1]),
            "pr_id": int(parts[pr_idx + 1]),
        }
    except Exception:
        return None


def parse_commit_url(raw_url: str) -> Optional[dict]:
    try:
        parsed = urllib.parse.urlparse(raw_url)
        parts = [p for p in parsed.path.split("/") if p]
        # /{org}/{project}/_git/{repo}/commit/{sha}
        if "_git" not in parts or "commit" not in parts:
            return None
        git_idx = parts.index("_git")
        commit_idx = parts.index("commit")
        if commit_idx <= git_idx + 1:
            return None
        return {
            "type": "commit",
            "org": parts[0],
            "project": urllib.parse.unquote(parts[git_idx - 1]),
            "repo": urllib.parse.unquote(parts[git_idx + 1]),
            "commit_id": parts[commit_idx + 1],
        }
    except Exception:
        return None


# Resolve a repo argument that may be either a full Azure DevOps repo URL or a
# bare repo name. When a bare name is given, the project is taken from the
# AZURE_PROJECT env var, or the configured default project.
def parse_repo_arg(raw_url: str, cfg: dict) -> Optional[dict]:
    try:
        parsed = urllib.parse.urlparse(raw_url)
        parts = [p for p in parsed.path.split("/") if p]
        git_idx = parts.index("_git")
        return {
            "org": parts[0],
            "project": urllib.parse.unquote(parts[git_idx - 1]),
            "repo": urllib.parse.unquote(parts[git_idx + 1]),
        }
    except Exception:
        return None


# ── Azure DevOps API ──────────────────────────────────────────────────────────

class HttpError(RuntimeError):
    """A failed Azure DevOps request. `code` is the HTTP status, 0 for a network error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _auth_header(pat: str) -> str:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Basic {token}"


def _retry_delay(retry_after, attempt):
    try:
        return min(max(float(retry_after), 1.0), 60.0)
    except (TypeError, ValueError):
        return min(2.0 ** (attempt + 1), 30.0)


def _api_request(url: str, pat: str, accept: str = "application/json",
                 method: str = "GET", body: Optional[dict] = None,
                 raw: bool = False, retries: int = 3) -> any:
    headers = {"Authorization": _auth_header(pat), "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                # A rejected PAT is not a 401: Azure DevOps answers 203 with its HTML
                # sign-in page, which would otherwise be read as the requested content.
                if getattr(resp, "status", None) == 203:
                    raise HttpError(203,
                        f"HTTP 203 sign-in page: Azure DevOps rejected the PAT "
                        f"(expired, revoked or for another org): {url}")
                payload = resp.read()
        except urllib.error.HTTPError as e:
            # 429 is Azure DevOps throttling; Retry-After says how long to back off.
            if e.code in (429, 503) and attempt < retries:
                delay = _retry_delay(e.headers.get("Retry-After") if e.headers else None, attempt)
                log(f"  HTTP {e.code}; retrying in {delay:.0f}s...")
                time.sleep(delay)
                continue
            err = e.read().decode("utf-8", errors="replace")[:300]
            raise HttpError(e.code, f"HTTP {e.code} {e.reason}: {url}\n{err}")
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(_retry_delay(None, attempt))
                continue
            raise HttpError(0, f"network error ({e}): {url}")
        if raw:
            return payload
        text = payload.decode("utf-8", errors="replace")
        return json.loads(text) if accept == "application/json" else text


def _repo_base(cfg, org, project, repo):
    return (
        f"{cfg['baseUrl']}/{org}"
        f"/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
    )


def api_get_pr(cfg, org, project, repo, pr_id):
    return _api_request(
        f"{_repo_base(cfg, org, project, repo)}/pullRequests/{pr_id}?api-version={API_VERSION}",
        cfg["pat"])


def api_get_iterations(cfg, org, project, repo, pr_id):
    return _api_request(
        f"{_repo_base(cfg, org, project, repo)}/pullRequests/{pr_id}/iterations?api-version={API_VERSION}",
        cfg["pat"])


def api_get_commit(cfg, org, project, repo, commit_id):
    return _api_request(
        f"{_repo_base(cfg, org, project, repo)}/commits/"
        f"{urllib.parse.quote(commit_id, safe='')}?api-version={API_VERSION}",
        cfg["pat"])


def paginate_changes(fetch_page, page_size=500, max_pages=200):
    """
    Every change of a `diffs/commits` comparison, page by page.

    The endpoint pages with $top/$skip and only marks the LAST page with
    `allChangesIncluded: true`. Stopping on a short page is not safe: the server may
    cap a page below the size asked for, which silently truncated big PRs.
    """
    changes, skip, first = [], 0, None
    for _ in range(max_pages):
        page = fetch_page(page_size, skip)
        if first is None:
            first = page
        batch = page.get("changes") or []
        changes.extend(batch)
        if page.get("allChangesIncluded") or not batch:
            return first, changes
        skip += len(batch)
    raise RuntimeError(f"diffs/commits: more than {max_pages} pages; refusing to continue")


def api_get_changes(cfg, org, project, repo, base, base_type, target, target_type):
    q = urllib.parse.quote
    root = _repo_base(cfg, org, project, repo)

    def fetch_page(top, skip):
        return _api_request(
            f"{root}/diffs/commits"
            f"?baseVersion={q(base, safe='')}&baseVersionType={base_type}"
            f"&targetVersion={q(target, safe='')}&targetVersionType={target_type}"
            f"&$top={top}&$skip={skip}&api-version={API_VERSION}",
            cfg["pat"])

    return paginate_changes(fetch_page)


def api_get_file_content(cfg, org, project, repo, file_path, branch=None, commit_id=None):
    version = commit_id or branch
    vtype = "commit" if commit_id else "branch"
    if not version:
        raise ValueError("Either branch or commit_id is required to fetch file content.")
    url = (
        f"{_repo_base(cfg, org, project, repo)}/items"
        f"?path={urllib.parse.quote(file_path, safe='')}"
        f"&versionDescriptor.version={urllib.parse.quote(version, safe='')}"
        f"&versionDescriptor.versionType={vtype}&api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"], accept="text/plain")


def api_get_blob(cfg, org, project, repo, file_path, version, version_type="commit"):
    """A file's raw bytes at a commit (or branch). Binary-safe."""
    url = (
        f"{_repo_base(cfg, org, project, repo)}/items"
        f"?path={urllib.parse.quote(file_path, safe='')}"
        f"&versionDescriptor.version={urllib.parse.quote(version, safe='')}"
        f"&versionDescriptor.versionType={version_type}&api-version={API_VERSION}"
    )
    try:
        return _api_request(url, cfg["pat"], accept="application/octet-stream", raw=True)
    except HttpError as e:
        raise HttpError(e.code, f"{file_path} @ {version[:12]}: {e}") from e


def _change_kinds(change_type) -> set:
    """'edit, rename' -> {'edit', 'rename'}. Azure reports a combined, comma-separated type."""
    return {k.strip().lower() for k in (change_type or "edit").split(",") if k.strip()}


def normalize_changes(raw_changes) -> list:
    """
    Files of a `diffs/commits` response as {path, change_type, old_path?}.

    A rename arrives twice: `edit, rename` on the new path (old path in
    `sourceServerItem`) and `delete, sourceRename` on the old one. The second is
    dropped, or the same file shows up once fully added and once fully deleted.
    """
    out = []
    for c in raw_changes or []:
        item = c.get("item") or {}
        if item.get("isFolder") or not item.get("path"):
            continue
        kinds = _change_kinds(c.get("changeType"))
        if "delete" in kinds and "sourcerename" in kinds:
            continue
        entry = {"path": item["path"], "change_type": (c.get("changeType") or "edit").lower()}
        old = c.get("sourceServerItem") or c.get("originalPath")
        if "rename" in kinds and old and old != item["path"]:
            entry["old_path"] = old
        if item.get("gitObjectType") == "commit":
            # A submodule (gitlink): its "content" is the pinned commit, already here.
            entry["submodule"] = {"old": item.get("originalObjectId"), "new": item.get("objectId")}
        out.append(entry)
    return out


def submodule_blob(sha: Optional[str]) -> Optional[bytes]:
    """A gitlink rendered the way `git diff` shows it."""
    return f"Subproject commit {sha}\n".encode() if sha else None


# ── Tag comparison ──────────────────────────────────────────────────────────--

# Repo URL form: https://dev.azure.com/<org>/<project>/_git/<repo>
def parse_repo_url(raw_url: str) -> Optional[dict]:
    try:
        parsed = urllib.parse.urlparse(raw_url)
        parts = [p for p in parsed.path.split("/") if p]
        git_idx = parts.index("_git")
        return {
            "org": parts[0],
            "project": urllib.parse.unquote(parts[git_idx - 1]),
            "repo": urllib.parse.unquote(parts[git_idx + 1]),
        }
    except Exception:
        return None


def compare_tags(cfg, org, project, repo, old_tag, new_tag):
    base = _repo_base(cfg, org, project, repo)

    # 1. diff between tags → changed files + base/target commit ids (paginated)
    diff, all_changes = api_get_changes(cfg, org, project, repo, old_tag, "tag", new_tag, "tag")

    # 2. commits between tags (commitsBatch with item/compare version)
    commits = _api_request(
        f"{base}/commitsBatch?api-version={API_VERSION}",
        cfg["pat"], method="POST",
        body={
            "itemVersion": {"version": old_tag, "versionType": "tag"},
            "compareVersion": {"version": new_tag, "versionType": "tag"},
            "$top": 1000,
        },
    )

    return {
        "project": project, "repo": repo, "oldTag": old_tag, "newTag": new_tag,
        "aheadCount": diff.get("aheadCount"),
        "behindCount": diff.get("behindCount"),
        "baseCommit": diff.get("baseCommit"),
        "targetCommit": diff.get("targetCommit"),
        "changeCounts": diff.get("changeCounts"),
        "commits": [
            {
                "id": c["commitId"][:10],
                "author": (c.get("author") or {}).get("name"),
                "date": (c.get("author") or {}).get("date"),
                "comment": (c.get("comment") or "").split("\n")[0],
            }
            for c in (commits.get("value") or [])
        ],
        "changes": [
            {"change": c.get("changeType"), "path": (c.get("item") or {}).get("path")}
            for c in all_changes
            if (c.get("item") or {}).get("gitObjectType") == "blob"
            or (c.get("item") and not c["item"].get("isFolder"))
        ],
    }


# ── SQLite cache ──────────────────────────────────────────────────────────────
#
# Diffs are keyed by the commit pair they compare. A commit never changes, so those
# entries never go stale and need no TTL: a new PR iteration has a new source commit
# and therefore a new key. Only the full file contents are NOT stored any more (they
# were most of the database's size); `cache prune` reclaims old rows.

def get_db():
    db = sqlite3.connect(str(DB_FILE))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS pr_meta (
            cache_key  TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS file_diff (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_url        TEXT    NOT NULL,
            file_path     TEXT    NOT NULL,
            change_type   TEXT    NOT NULL,
            source_branch TEXT    NOT NULL,
            target_branch TEXT    NOT NULL,
            old_content   TEXT,
            new_content   TEXT,
            patch         TEXT,
            fetched_at    INTEGER NOT NULL,
            UNIQUE(pr_url, file_path)
        )
    """)
    cols = {r[1] for r in db.execute("PRAGMA table_info(file_diff)")}
    if "meta" not in cols:
        db.execute("ALTER TABLE file_diff ADD COLUMN meta TEXT")
    db.commit()
    return db


def cache_get_pr(db, cache_key, max_age_ms=30*60*1000):
    """max_age_ms=None: the entry never expires (commit-keyed data)."""
    row = db.execute("SELECT data, fetched_at FROM pr_meta WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    if max_age_ms is not None and (time.time() * 1000 - row[1]) > max_age_ms:
        return None
    return json.loads(row[0])


def cache_set_pr(db, cache_key, data):
    db.execute(
        "INSERT OR REPLACE INTO pr_meta (cache_key, data, fetched_at) VALUES (?, ?, ?)",
        (cache_key, json.dumps(data), int(time.time() * 1000))
    )
    db.commit()


def cache_get_entry(db, ns, file_path):
    """A cached file entry (as built by make_entry), or None."""
    row = db.execute(
        "SELECT patch, meta FROM file_diff WHERE pr_url = ? AND file_path = ?",
        (ns, file_path)
    ).fetchone()
    if not row or row[1] is None:
        return None
    entry = json.loads(row[1])
    entry["patch"] = row[0] or ""
    return entry


def cache_set_entry(db, ns, entry):
    meta = {k: v for k, v in entry.items() if k != "patch"}
    db.execute("""
        INSERT OR REPLACE INTO file_diff
          (pr_url, file_path, change_type, source_branch, target_branch, old_content, new_content,
           patch, fetched_at, meta)
        VALUES (?, ?, ?, '', '', NULL, NULL, ?, ?, ?)
    """, (ns, entry["path"], entry.get("changeType") or "edit", entry.get("patch") or "",
          int(time.time() * 1000), json.dumps(meta)))


def cache_stats(db):
    prs = db.execute("SELECT COUNT(*) FROM pr_meta").fetchone()[0]
    files = db.execute("SELECT COUNT(*) FROM file_diff").fetchone()[0]
    oldest = db.execute("SELECT MIN(fetched_at) FROM file_diff").fetchone()[0]
    size = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    return {"prs": prs, "files": files, "oldest_ms": oldest, "bytes": size}


def cache_clear(db, pr_url=None):
    if pr_url:
        db.execute("DELETE FROM file_diff WHERE pr_url LIKE ?", (f"{pr_url}%",))
        db.execute("DELETE FROM pr_meta WHERE cache_key LIKE ?", (f"{pr_url}%",))
    else:
        db.execute("DELETE FROM file_diff")
        db.execute("DELETE FROM pr_meta")
    db.commit()


def cache_prune(db, days=30):
    """Drop rows older than `days`, drop stored file contents, and VACUUM."""
    cutoff = int((time.time() - days * 86400) * 1000)
    removed = db.execute("DELETE FROM file_diff WHERE fetched_at < ?", (cutoff,)).rowcount
    removed += db.execute("DELETE FROM pr_meta WHERE fetched_at < ?", (cutoff,)).rowcount
    db.execute("UPDATE file_diff SET old_content = NULL, new_content = NULL "
               "WHERE old_content IS NOT NULL OR new_content IS NOT NULL")
    db.commit()
    db.execute("VACUUM")
    # In WAL mode the main file only shrinks once the log is checkpointed.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return removed


# ── File selection ────────────────────────────────────────────────────────────

def skip_reason(file_path: str, include=(), exclude=()) -> Optional[str]:
    """
    Why a file is left out of the diff, or None to diff it.

    --exclude globs win, then --include globs bring back a file the defaults skip
    (e.g. --include '*.resx'). A glob matches the full path or the file name.
    """
    low = file_path.lstrip("/").lower()
    name = low.rsplit("/", 1)[-1]

    def matches(patterns):
        return any(fnmatch.fnmatchcase(low, p.lower().lstrip("/"))
                   or fnmatch.fnmatchcase(name, p.lower()) for p in patterns)

    if exclude and matches(exclude):
        return "excluded"
    if include and matches(include):
        return None
    if Path(file_path).suffix.lower() in SKIP_EXTENSIONS:
        return "binary-extension"
    for pat in SKIP_PATTERNS:
        if pat.search(file_path):
            return "generated"
    return None


def should_analyze(file_path: str) -> bool:
    return skip_reason(file_path) is None


# ── Diff computation ──────────────────────────────────────────────────────────

def _split_lines(text):
    """
    Lines split on '\\n' only, each keeping its terminator.

    str.splitlines() also splits on \\x0c, \\x1c, \\u2028 and friends, which shifts
    every following line number away from what git and Azure DevOps count.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _format_range(start, stop):
    beginning, length = start + 1, stop - start
    if length == 1:
        return f"{beginning}"
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def _emit(out, prefix, line):
    if line.endswith("\n"):
        out.append(prefix + line)
    else:
        # A last line without a newline gets git's marker; without it the next
        # diff line was glued onto this one (`-b+c`).
        out.append(prefix + line + "\n")
        out.append("\\ No newline at end of file\n")


def align_sliders(opcodes, a, b):
    """
    Place each ambiguous insertion/deletion the way git does.

    An inserted method often has neighbours that end the same way (`}` + blank), so
    the block can slide up or down over identical lines and still be a correct diff.
    difflib takes whatever its matcher found — sometimes two lines too high, which
    anchors comments on the previous method's closing brace. Like git's xdiff, the
    block is slid over its whole range and placed at the lowest position where it
    ends on a blank line; with no such position, as low as it goes.
    """
    codes = [list(c) for c in opcodes]
    for k in range(1, len(codes) - 1):
        prev, cur, nxt = codes[k - 1], codes[k], codes[k + 1]
        if prev[0] != "equal" or nxt[0] != "equal" or cur[0] not in ("insert", "delete"):
            continue
        seq, lo, hi, other = (b, 3, 4, 1) if cur[0] == "insert" else (a, 1, 2, 3)

        def shift(d):
            for idx in (lo, hi, other, other + 1):
                cur[idx] += d
            prev[2] += d
            prev[4] += d
            nxt[1] += d
            nxt[3] += d

        while prev[2] > prev[1] and seq[cur[lo] - 1] == seq[cur[hi] - 1]:
            shift(-1)
        best = cur[lo] if not seq[cur[hi] - 1].strip() else None
        while nxt[2] > nxt[1] and seq[cur[lo]] == seq[cur[hi]]:
            shift(+1)
            if not seq[cur[hi] - 1].strip():
                best = cur[lo]
        while best is not None and cur[lo] > best:
            shift(-1)
    return [tuple(c) for c in codes if not (c[0] == "equal" and c[1] == c[2])]


def group_opcodes(codes, n):
    """difflib.SequenceMatcher.get_grouped_opcodes, over an already computed opcode list."""
    codes = list(codes) or [("equal", 0, 1, 0, 1)]
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)
    group = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > n + n:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def compute_patch(file_path: str, old_content: Optional[str], new_content: Optional[str],
                  context: int = 5, old_path: Optional[str] = None) -> str:
    a = _split_lines(old_content)
    b = _split_lines(new_content)
    if a == b:
        return ""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=(len(a) + len(b) > AUTOJUNK_MIN_LINES))
    out = [f"--- a{old_path or file_path}\n", f"+++ b{file_path}\n"]
    hunks = 0
    for group in group_opcodes(align_sliders(sm.get_opcodes(), a, b), context):
        first, last = group[0], group[-1]
        out.append(f"@@ -{_format_range(first[1], last[2])} +{_format_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in b[j1:j2]:
                    _emit(out, " ", line)
                continue
            if tag in ("replace", "delete"):
                for line in a[i1:i2]:
                    _emit(out, "-", line)
            if tag in ("replace", "insert"):
                for line in b[j1:j2]:
                    _emit(out, "+", line)
        hunks += 1
    return "".join(out) if hunks else ""


def _patch_body(patch: str):
    """
    The patch without its leading file header (`diff `, `---`, `+++`), the same rule
    chunk_patch uses. Past the header, `---`/`+++` are content: an added `++i;`
    reads `+++i;` and a removed `-- note` reads `--- note`.
    """
    in_header = True
    for line in (patch or "").split("\n"):
        if in_header and line.startswith(("diff ", "---", "+++")):
            continue
        in_header = False
        if line:
            yield line


def patch_stats(patch: str) -> dict:
    body = list(_patch_body(patch))
    added = sum(1 for l in body if l.startswith("+"))
    removed = sum(1 for l in body if l.startswith("-"))
    return {"added": added, "removed": removed}


def _ws_key(line):
    return "".join(line.split())


def patch_is_whitespace_only(patch: str) -> bool:
    """
    True when every change in the patch is whitespace: the removed lines and the
    added lines read the same once whitespace is dropped. Flagged, never hidden —
    a whitespace change inside a string literal is a real change.
    """
    removed, added = [], []
    for line in _patch_body(patch):
        if line.startswith("-"):
            removed.append(_ws_key(line[1:]))
        elif line.startswith("+"):
            added.append(_ws_key(line[1:]))
    return bool(removed or added) and removed == added


def chunk_patch(patch: str, max_lines: int = 300) -> list:
    if not patch:
        return []
    lines = patch.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return [patch]

    chunks = []
    header = []
    current = []
    in_header = True
    last_hunk_header = None

    for line in lines:
        if in_header and (line.startswith("---") or line.startswith("+++") or line.startswith("diff ")):
            header.append(line)
            continue
        in_header = False

        if line.startswith("@@"):
            last_hunk_header = line
            if len(current) >= max_lines:
                chunks.append("".join(header + current))
                current = []
        current.append(line)

    if current:
        chunks.append("".join(header + current))

    # Ensure every chunk after the first starts with a hunk header so that
    # line-number reconstruction (--output lines) and consumers can anchor
    # comments correctly.
    if len(chunks) > 1:
        for i in range(1, len(chunks)):
            chunk_lines = chunks[i].splitlines(keepends=True)
            if chunk_lines and not chunk_lines[0].startswith("@@"):
                if last_hunk_header:
                    chunk_lines.insert(0, last_hunk_header)
                    chunks[i] = "".join(chunk_lines)
    return chunks if chunks else [patch]


# ── Binary files ──────────────────────────────────────────────────────────────

def decode_blob(data: Optional[bytes]):
    """(text, is_binary). UTF-16 with a BOM is text; a NUL in the first 8000 bytes is binary."""
    if data is None:
        return None, False
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), False
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def image_size(data: bytes):
    """(width, height) of a PNG, GIF or JPEG, read from its header; None otherwise."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        return None
    return None


def blob_info(data: Optional[bytes]):
    """What a reviewer can use from a binary: size, short hash, image dimensions."""
    if data is None:
        return None
    info = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()[:12]}
    dims = image_size(data)
    if dims:
        info["dimensions"] = f"{dims[0]}x{dims[1]}"
    return info


# ── File entries ──────────────────────────────────────────────────────────────

def make_entry(change: dict, patch: str = "", binary: Optional[dict] = None) -> dict:
    entry = {"path": change["path"], "changeType": change["change_type"]}
    if change.get("old_path"):
        entry["oldPath"] = change["old_path"]
    if binary is not None:
        entry.update(binary=binary, patch="", stats={"added": 0, "removed": 0})
        return entry
    entry["patch"] = patch
    entry["stats"] = patch_stats(patch)
    if patch and patch_is_whitespace_only(patch):
        entry["whitespaceOnly"] = True
    return entry


def entry_from_blobs(change: dict, old: Optional[bytes], new: Optional[bytes], context: int = 5) -> dict:
    old_text, old_bin = decode_blob(old)
    new_text, new_bin = decode_blob(new)
    if old_bin or new_bin:
        return make_entry(change, binary={"old": blob_info(old), "new": blob_info(new)})
    patch = compute_patch(change["path"], old_text, new_text, context, old_path=change.get("old_path"))
    return make_entry(change, patch)


def _sort_files(files):
    files.sort(key=lambda f: (f.get("skipped", False), f["path"]))
    return files


# ── Diff through the REST API ─────────────────────────────────────────────────

def diff_via_api(cfg, db, org, project, repo, base, target, ns, opts,
                 base_type="commit", target_type="commit", cacheable=True):
    """
    File entries for base..target, downloading each file's two versions.

    Any download failure raises: an unreadable file is not an added or deleted one.
    """
    force = opts.get("force", False)
    context = opts.get("context", 5)
    include, exclude = opts.get("include") or (), opts.get("exclude") or ()
    ns = f"{ns}#U{context}"

    changes_key = f"{ns}:changes"
    changes = None if (force or not cacheable) else cache_get_pr(db, changes_key, max_age_ms=None)
    if changes is None:
        log("  Fetching changed file list...")
        _, raw = api_get_changes(cfg, org, project, repo, base, base_type, target, target_type)
        changes = normalize_changes(raw)
        if cacheable:
            cache_set_pr(db, changes_key, changes)

    files, to_fetch = [], []
    for c in changes:
        reason = skip_reason(c["path"], include, exclude)
        if reason:
            files.append({"path": c["path"], "changeType": c["change_type"], "skipped": True, "reason": reason})
            continue
        cached = None if (force or not cacheable) else cache_get_entry(db, ns, c["path"])
        if cached:
            files.append(cached)
            continue
        to_fetch.append(c)

    if to_fetch:
        log(f"  Fetching {len(to_fetch)} files ({len(files)} cached or skipped)...")

    def download(c):
        kinds = _change_kinds(c["change_type"])
        if c.get("submodule"):
            sub = c["submodule"]
            return (c, None if "add" in kinds else submodule_blob(sub.get("old")),
                    None if "delete" in kinds else submodule_blob(sub.get("new")))
        old = new = None
        if "add" not in kinds:
            old = api_get_blob(cfg, org, project, repo, c.get("old_path") or c["path"], base, base_type)
        if "delete" not in kinds:
            new = api_get_blob(cfg, org, project, repo, c["path"], target, target_type)
        return c, old, new

    workers = max(1, int(opts.get("workers") or DEFAULT_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (c, old, new) in enumerate(pool.map(download, to_fetch), 1):
            entry = entry_from_blobs(c, old, new, context)
            if cacheable:
                cache_set_entry(db, ns, entry)
            files.append(entry)
            if i % 10 == 0 or i == len(to_fetch):
                log(f"  {i}/{len(to_fetch)} files processed...")
    if cacheable:
        db.commit()
    return _sort_files(files)


# ── Diff through a local clone ────────────────────────────────────────────────
#
# `git diff` on a local clone: no per-file download, git's histogram algorithm,
# real rename detection, and the enclosing method in each hunk header. Objects only
# are read — the working tree and the checked-out branch are never touched.

# Built-in git funcname drivers, so a hunk header names its enclosing method/class.
# A repo's own .gitattributes still wins: core.attributesFile has the lowest priority.
FUNCNAME_DRIVERS = {
    "csharp": "*.cs",
    "java": "*.java",
    "python": "*.py",
    "golang": "*.go",
    "rust": "*.rs",
    "php": "*.php",
    "ruby": "*.rb",
    "kotlin": "*.kt *.kts",
    "cpp": "*.c *.h *.cpp *.hpp *.cc",
    "css": "*.css *.scss *.less",
    "html": "*.html *.htm *.cshtml *.razor",
    "markdown": "*.md",
    "bash": "*.sh",
}


def _funcname_attributes_file():
    path = Path(tempfile.gettempdir()) / "git-diff-analysis.gitattributes"
    lines = [f"{glob} diff={driver}" for driver, globs in FUNCNAME_DRIVERS.items()
             for glob in globs.split()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _git(repo, *args, check=True) -> bytes:
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", *args],
                       capture_output=True)
    if check and r.returncode != 0:
        msg = r.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise RuntimeError(f"git {' '.join(a for a in args if not a.startswith('core.'))[:80]}: {msg}")
    return r.stdout


def _git_has_commit(repo, sha) -> bool:
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def git_ensure_commits(repo, shas, fallback_refspecs=()):
    """Fetch the commits the diff needs when the clone lacks them; objects only."""
    missing = [s for s in shas if not _git_has_commit(repo, s)]
    if not missing:
        return
    log(f"  Fetching {len(missing)} commit(s) into {repo}...")
    subprocess.run(["git", "-C", repo, "fetch", "--no-tags", "--quiet", "origin", *missing],
                   capture_output=True)
    missing = [s for s in missing if not _git_has_commit(repo, s)]
    if missing and fallback_refspecs:
        subprocess.run(["git", "-C", repo, "fetch", "--no-tags", "--quiet", "origin", *fallback_refspecs],
                       capture_output=True)
        missing = [s for s in missing if not _git_has_commit(repo, s)]
    if missing:
        raise RuntimeError(
            f"commit(s) {', '.join(s[:10] for s in missing)} not found in {repo} even after a fetch. "
            f"Is --repo a clone of the PR's repository? Drop --repo to read through the API.")


def git_origin_matches(repo, repo_name) -> bool:
    """Does the clone's origin point at `_git/<repo_name>`? (case-insensitive, URL-decoded)"""
    url = _git(repo, "remote", "get-url", "origin", check=False).decode("utf-8", "replace").strip()
    url = urllib.parse.unquote(url).rstrip("/").lower()
    return url.endswith(f"/_git/{repo_name.lower()}") or url.endswith(f"/{repo_name.lower()}")


def parse_name_status_z(data: bytes) -> list:
    """`git diff --name-status -z -M` → [{status, old, new}] (old/new None when absent)."""
    tokens = data.decode("utf-8", errors="replace").split("\0")
    out, i = [], 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i]
        letter = status[0]
        if letter in "RC":
            out.append({"status": letter, "old": tokens[i + 1], "new": tokens[i + 2]})
            i += 3
        else:
            path = tokens[i + 1]
            out.append({"status": letter,
                        "old": None if letter == "A" else path,
                        "new": None if letter == "D" else path})
            i += 2
    return out


def _unquote_git_path(p: str) -> str:
    p = p.rstrip("\t")
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        return codecs.escape_decode(p[1:-1].encode("utf-8"))[0].decode("utf-8", errors="replace")
    return p


def _strip_prefix(p, prefix):
    if p == "/dev/null":
        return None
    return p[len(prefix):] if p.startswith(prefix) else p


def parse_git_diff(text: str) -> dict:
    """
    Split `git diff` output per file: {path: {"patch", "binary"}}, keyed by the new
    path (the old one for a deletion). The patch starts at its `---` line.
    """
    out = {}
    for block in re.split(r"^(?=diff --git )", text, flags=re.M):
        if not block.startswith("diff --git "):
            continue
        lines = block.split("\n")
        old = new = None
        binary = False
        body_start = None
        for i, raw_line in enumerate(lines[1:], 1):
            line = raw_line.rstrip("\r")
            if line.startswith(("rename from ", "copy from ")):
                old = _unquote_git_path(line.split(" from ", 1)[1])
            elif line.startswith(("rename to ", "copy to ")):
                new = _unquote_git_path(line.split(" to ", 1)[1])
            elif line.startswith("Binary files "):
                binary = True
                m = re.match(r"Binary files (.+) and (.+) differ$", line)
                if m:
                    old = old or _strip_prefix(_unquote_git_path(m.group(1)), "a/")
                    new = new or _strip_prefix(_unquote_git_path(m.group(2)), "b/")
            elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                old = old or _strip_prefix(_unquote_git_path(line[4:]), "a/")
                new = new or _strip_prefix(_unquote_git_path(lines[i + 1].rstrip("\r")[4:]), "b/")
                body_start = i
                break
        if old is None and new is None:
            # Mode-only change or exact rename: the header is the only place left.
            rest = lines[0][len("diff --git "):]
            half = (len(rest) - 5) // 2
            if half > 0 and rest.startswith("a/") and rest[half + 2:half + 5] == " b/":
                old = new = rest[2:half + 2]
        key = new or old
        if key is None:
            continue
        patch = "\n".join(lines[body_start:]) if body_start is not None else ""
        if patch and not patch.endswith("\n"):
            patch += "\n"
        out[key] = {"patch": patch, "binary": binary}
    return out


def _git_blob(repo, rev, path) -> Optional[bytes]:
    r = subprocess.run(["git", "-C", repo, "cat-file", "blob", f"{rev}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


_STATUS_CHANGE = {"A": "add", "D": "delete", "M": "edit", "T": "edit", "R": "edit, rename", "C": "add, copy"}


def diff_via_git(repo, base, target, opts, fallback_refspecs=()):
    context = opts.get("context", 5)
    include, exclude = opts.get("include") or (), opts.get("exclude") or ()
    git_ensure_commits(repo, [base, target], fallback_refspecs)

    status = parse_name_status_z(_git(repo, "diff", "--name-status", "-z", "-M", "--no-ext-diff", base, target))
    args = ["-c", f"core.attributesFile={_funcname_attributes_file()}",
            "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
            "--diff-algorithm=histogram", "--src-prefix=a/", "--dst-prefix=b/", f"-U{context}"]
    if opts.get("function_context"):
        args.append("--function-context")
    log("  Diffing in the local clone (histogram)...")
    blocks = parse_git_diff(_git(repo, *args, base, target).decode("utf-8", errors="replace"))

    files = []
    for st in status:
        rel = st["new"] or st["old"]
        change = {"path": "/" + rel, "change_type": _STATUS_CHANGE.get(st["status"], "edit")}
        if st["status"] in "RC" and st["old"]:
            change["old_path"] = "/" + st["old"]
        reason = skip_reason(change["path"], include, exclude)
        block = blocks.get(rel)
        if reason and reason != "binary-extension":
            files.append({"path": change["path"], "changeType": change["change_type"],
                          "skipped": True, "reason": reason})
            continue
        if reason == "binary-extension" or (block and block["binary"]):
            # Local blobs are cheap: describe the binary instead of only skipping it.
            # Also catches UTF-16 text, which git calls binary but decode_blob reads.
            # A copy is reported as an added file, so it has no old side.
            old = _git_blob(repo, base, st["old"]) if st["old"] and st["status"] != "C" else None
            new =_git_blob(repo, target, st["new"]) if st["new"] else None
            entry = entry_from_blobs(change, old, new, context)
            if reason and "binary" not in entry:
                entry = {"path": change["path"], "changeType": change["change_type"],
                         "skipped": True, "reason": reason}
            files.append(entry)
            continue
        files.append(make_entry(change, block["patch"] if block else ""))
    return _sort_files(files)


# ── Fetch PR ──────────────────────────────────────────────────────────────────

def fetch_pr(cfg, db, org, project, repo, pr_id, pr_url, force=False, opts=None):
    """
    Diff a PR between its merge base (`commonRefCommit`) and its source commit
    (`sourceRefCommit`) from the latest iteration — not between live branch tips.
    That works on completed PRs and yields right-side line numbers that match what
    Azure anchors inline comments against.
    """
    opts = dict(opts or {})
    opts["force"] = force or opts.get("force", False)

    log(f"  Fetching PR #{pr_id} metadata and iterations...")
    data = api_get_pr(cfg, org, project, repo, pr_id)
    iterations = (api_get_iterations(cfg, org, project, repo, pr_id).get("value")) or []
    latest = iterations[-1] if iterations else {}
    source_commit = (latest.get("sourceRefCommit") or {}).get("commitId")
    base_commit = (latest.get("commonRefCommit") or {}).get("commitId")

    compared_from = "merge base"
    target_merged = False
    since = opts.get("since_iteration")
    if since:
        previous = next((it for it in iterations if it.get("id") == since), None)
        if previous is None:
            raise RuntimeError(f"PR #{pr_id} has no iteration {since} (it has {len(iterations)})")
        if previous is latest:
            raise RuntimeError(f"iteration {since} is the PR's latest; there is nothing newer to compare")
        prev_base = (previous.get("commonRefCommit") or {}).get("commitId")
        target_merged = bool(prev_base and base_commit and prev_base != base_commit)
        base_commit = (previous.get("sourceRefCommit") or {}).get("commitId")
        compared_from = f"iteration {since}"
        if target_merged:
            # The author merged (or rebased onto) the target between the two rounds,
            # so iteration N..latest also carries everything that came from the target.
            warn(f"WARNING: PR #{pr_id}'s merge base moved after iteration {since} (the target was merged "
                 f"in). This diff includes the target's changes, not only the author's.")

    source_ref = (data.get("sourceRefName") or "").replace("refs/heads/", "")
    target_ref = (data.get("targetRefName") or "").replace("refs/heads/", "")
    pinned = bool(source_commit and base_commit)
    pr_meta = {
        "id": data.get("pullRequestId"),
        "title": data.get("title"),
        "author": data.get("createdBy", {}).get("displayName"),
        "status": data.get("status"),
        "sourceRef": source_ref,
        "targetRef": target_ref,
        "sourceCommit": source_commit,
        "baseCommit": base_commit,
        "iteration": latest.get("id"),
        "iterations": len(iterations),
        "comparedFrom": compared_from,
        "targetMergedSince": target_merged,
        "pinned": pinned,
        "createdDate": data.get("creationDate"),
        "description": data.get("description"),
    }

    if pinned:
        log(f"  Diff {base_commit[:10]}..{source_commit[:10]} ({compared_from} -> iteration {latest.get('id')})")
        if opts.get("repo"):
            files = diff_via_git(opts["repo"], base_commit, source_commit, opts,
                                 fallback_refspecs=(f"+refs/pull/{pr_id}/merge", f"refs/heads/{target_ref}"))
        else:
            ns = f"{pr_url}#{CACHE_VERSION}@{base_commit}..{source_commit}"
            files = diff_via_api(cfg, db, org, project, repo, base_commit, source_commit, ns, opts)
    else:
        if opts.get("repo") or since:
            raise RuntimeError(f"PR #{pr_id} reports no iteration commits; --repo and "
                               f"--since-iteration need them")
        warn(f"WARNING: PR #{pr_id} reports no iteration commits; diffing the live branch tips "
             f"({source_ref} vs {target_ref}). Line numbers may not match Azure's anchors.")
        files = diff_via_api(cfg, db, org, project, repo, target_ref, source_ref, pr_url, opts,
                             base_type="branch", target_type="branch", cacheable=False)
    return {"pr": pr_meta, "files": files}


# ── Fetch commit ──────────────────────────────────────────────────────────────

def fetch_commit(cfg, db, org, project, repo, commit_id, commit_url, force=False, opts=None):
    """A commit against its first parent."""
    opts = dict(opts or {})
    opts["force"] = force or opts.get("force", False)

    meta_key = f"{commit_url}#{CACHE_VERSION}:meta"
    commit_meta = None if opts["force"] else cache_get_pr(db, meta_key, max_age_ms=None)
    if not commit_meta:
        log(f"  Fetching commit {commit_id[:10]} metadata...")
        data = api_get_commit(cfg, org, project, repo, commit_id)
        full_id = data.get("commitId", commit_id)
        commit_meta = {
            "id": full_id,
            "shortId": full_id[:10],
            "author": (data.get("author") or {}).get("name"),
            "date": (data.get("author") or {}).get("date"),
            "shortMessage": (data.get("comment") or "").split("\n")[0],
            "fullMessage": data.get("comment") or "",
            "parents": data.get("parents") or [],
        }
        cache_set_pr(db, meta_key, commit_meta)

    parents = commit_meta.get("parents") or []
    if not parents:
        raise RuntimeError(f"commit {commit_id[:10]} has no parent (root commit); nothing to diff against")
    parent_id, full_id = parents[0], commit_meta["id"]

    if opts.get("repo"):
        files = diff_via_git(opts["repo"], parent_id, full_id, opts)
    else:
        ns = f"{commit_url}#{CACHE_VERSION}@{parent_id}..{full_id}"
        files = diff_via_api(cfg, db, org, project, repo, parent_id, full_id, ns, opts)
    return {"commit": commit_meta, "files": files}


# ── Output formatters ─────────────────────────────────────────────────────────

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def iter_added(patch: str):
    """(new_line_no, text) for every added line, numbered as in the new file."""
    new_line = None
    for line in (patch or "").split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if new_line is None or not line:
            continue  # file header above the first hunk, or the trailing split
        if line.startswith("\\"):
            continue  # "\ No newline at end of file" is a marker, not a line
        if line.startswith("-"):
            continue  # removed line — does not advance the new-file counter
        if line.startswith("+"):
            yield new_line, line[1:].rstrip("\r")
        new_line += 1


def _patch_sources(f):
    """The patch text of a file entry; JSON results carry chunks instead (each anchored)."""
    if f.get("patch"):
        return [f["patch"]]
    return f.get("chunks") or []


def _binary_desc(b):
    def side(x):
        if not x:
            return "—"
        dims = f" {x['dimensions']}" if x.get("dimensions") else ""
        return f"{x['size']:,} B{dims} #{x['sha256']}"
    return f"{side(b.get('old'))} -> {side(b.get('new'))}"


def _change_marker(f):
    kinds = _change_kinds(f.get("changeType"))
    if f.get("binary"):
        return "[B]"
    if "rename" in kinds:
        return "[R]"
    if "add" in kinds:
        return "[+]"
    if "delete" in kinds:
        return "[-]"
    return "[~]"


def print_summary(results):
    sep1 = "=" * 70
    sep2 = "-" * 70
    for r in results:
        pr = r.get("pr")
        commit = r.get("commit")
        files = r["files"]
        analyzed = [f for f in files if not f.get("skipped")]
        skipped = [f for f in files if f.get("skipped")]
        total_added = sum(f.get("stats", {}).get("added", 0) for f in analyzed)
        total_removed = sum(f.get("stats", {}).get("removed", 0) for f in analyzed)
        print(f"\n{sep1}")
        if pr:
            print(f"PR #{pr['id']}: {pr['title']}")
            print(f"Author : {pr['author']}")
            print(f"Branch : {pr['sourceRef']} -> {pr['targetRef']}")
            print(f"Status : {pr['status']}")
            if pr.get("pinned"):
                print(f"Diff   : {pr['baseCommit'][:10]}..{pr['sourceCommit'][:10]} "
                      f"({pr.get('comparedFrom')} -> iteration {pr.get('iteration')} of {pr.get('iterations')})")
        elif commit:
            print(f"Commit {commit['shortId']}: {commit['shortMessage']}")
            print(f"Author : {commit['author']}")
            print(f"Date   : {commit['date']}")
        print(f"Files  : {len(analyzed)} analyzed, {len(skipped)} skipped")
        print(f"Lines  : +{total_added} / -{total_removed}")
        print(sep2)
        for f in analyzed:
            a = f.get("stats", {}).get("added", 0)
            rm = f.get("stats", {}).get("removed", 0)
            name = f"{f['oldPath']} -> {f['path']}" if f.get("oldPath") else f["path"]
            if f.get("binary"):
                print(f"  [B] {name}  ({_binary_desc(f['binary'])})")
                continue
            note = "  whitespace only" if f.get("whitespaceOnly") else ""
            print(f"  {_change_marker(f)} {name}  (+{a}/-{rm}){note}")
        if skipped:
            by_reason = {}
            for f in skipped:
                by_reason[f.get("reason") or "skipped"] = by_reason.get(f.get("reason") or "skipped", 0) + 1
            print("  ... skipped: " + ", ".join(f"{n} {k}" for k, n in sorted(by_reason.items())))


def _json_file(f, chunk_size):
    out = {"path": f["path"], "changeType": f["changeType"]}
    if f.get("oldPath"):
        out["oldPath"] = f["oldPath"]
    out["stats"] = f.get("stats")
    if f.get("binary"):
        out["binary"] = f["binary"]
    if f.get("whitespaceOnly"):
        out["whitespaceOnly"] = True
    out["chunks"] = chunk_patch(f.get("patch") or "", chunk_size)
    return out


def to_analysis_json(results, chunk_size=300):
    output = []
    for r in results:
        pr = r.get("pr")
        commit = r.get("commit")
        header = {}
        if pr:
            header["pr"] = pr
        elif commit:
            header["commit"] = commit
        files = [
            _json_file(f, chunk_size)
            for f in r["files"]
            if not f.get("skipped") and (f.get("patch") or f.get("binary") or f.get("oldPath"))
        ]
        skipped = [{"path": f["path"], "reason": f.get("reason")} for f in r["files"] if f.get("skipped")]
        item = {**header, "files": files}
        if skipped:
            item["skipped"] = skipped
        output.append(item)
    return output


def to_lines(results):
    """Emit 'file:line  content' for every added line — useful for pinpointing
    exact new-file line numbers before posting inline PR comments."""
    output = []
    for r in results:
        for f in r["files"]:
            if f.get("skipped"):
                continue
            for source in _patch_sources(f):
                for line_no, text in iter_added(source):
                    output.append(f"{f['path']}:{line_no}  {text}")
    return "\n".join(output)


# Line-comment openers and block-comment delimiters by file extension. Only the
# languages that actually appear in these repos — a general-purpose parser would be
# wrong more often than this table is.
_COMMENT_SYNTAX = {
    "//": (".cs .js .jsx .ts .tsx .java .go .rs .c .h .cpp .hpp .scss .less .php "
           ".kt .swift .dart .gradle"),
    "#": ".py .sh .bash .ps1 .psm1 .yml .yaml .toml .rb .pl .r .tf .dockerfile .env .conf",
    "--": ".sql .lua",
    "%": ".tex .erl",
    "'": ".vb .vbs",
}
_BLOCK_DELIMS = {
    "c-style": ("/*", "*/", ".cs .js .jsx .ts .tsx .java .go .rs .c .h .cpp .hpp "
                          ".css .scss .less .php .kt .swift .dart"),
    "html": ("<!--", "-->", ".html .htm .xml .cshtml .razor .vue .svg .resx .csproj .config .props"),
    "sql": ("/*", "*/", ".sql"),
    "python": ('"""', '"""', ".py"),
}


def _comment_markers(path: str):
    """(line_openers, block_pairs) for a path, by extension."""
    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    line_openers = [op for op, exts in _COMMENT_SYNTAX.items() if ext in exts.split()]
    blocks = [(o, c) for (o, c, exts) in _BLOCK_DELIMS.values() if ext in exts.split()]
    return line_openers, blocks


def classify_added_lines(path: str, patch: str) -> dict:
    """
    Split a patch's ADDED lines into code / comment / blank.

    Why added-only: a review judges what is being introduced. Counting the whole
    file measures the codebase's history instead of this change.

    Block comments are tracked with a running flag so a multi-line /* ... */ is not
    counted as code after its first line. A line that both closes a block and
    carries trailing code counts as code — that is the honest reading.
    """
    line_openers, blocks = _comment_markers(path)
    code = comment = blank = 0
    in_block = None  # the closing delimiter we are waiting for
    for raw in _patch_body(patch):
        if not raw.startswith("+"):
            continue
        line = raw[1:].strip()
        if in_block:
            comment += 1
            if in_block in line:
                after = line.split(in_block, 1)[1].strip()
                in_block = None
                if after:
                    comment -= 1
                    code += 1
            continue
        if not line:
            blank += 1
            continue
        # A block comment opening this line.
        matched_block = False
        for open_d, close_d in blocks:
            if not line.startswith(open_d):
                continue
            matched_block = True
            rest = line[len(open_d):]
            if close_d in rest:
                # Opens and closes here. Trailing code after the close makes it code.
                after = rest.split(close_d, 1)[1].strip()
                if after:
                    code += 1
                else:
                    comment += 1
            else:
                comment += 1
                in_block = close_d
            break
        if matched_block:
            continue

        if any(line.startswith(op) for op in line_openers):
            comment += 1
        else:
            code += 1
    return {"code": code, "comment": comment, "blank": blank}


def to_stats(results) -> str:
    """
    Per-file added-line composition, plus a comment:code ratio.

    This exists so "this change is 241 comment lines to 128 of code" is measured the
    same way every time, rather than re-derived with an ad-hoc grep per review.
    A ratio is evidence, not a verdict — read it next to what the code does.
    """
    rows = []
    tot_code = tot_comment = tot_blank = 0
    for r in results:
        for f in r.get("files", []):
            if f.get("skipped") or f.get("binary"):
                continue
            s = classify_added_lines(f["path"], f.get("patch") or "")
            if not (s["code"] or s["comment"] or s["blank"]):
                continue
            rows.append((f["path"], s))
            tot_code += s["code"]
            tot_comment += s["comment"]
            tot_blank += s["blank"]
    if not rows:
        return "No added lines."
    width = max(len(p) for p, _ in rows)
    width = min(max(width, 20), 70)
    out = [f"{'FILE'.ljust(width)}  {'CODE':>6} {'COMMENT':>8} {'BLANK':>6}  RATIO"]
    for path, s in sorted(rows, key=lambda x: -(x[1]["code"] + x[1]["comment"])):
        shown = path if len(path) <= width else "…" + path[-(width - 1):]
        ratio = (s["comment"] / s["code"]) if s["code"] else float("inf")
        rtxt = "  —  " if s["comment"] == 0 else (f"{ratio:.2f}" if s["code"] else " all ")
        out.append(f"{shown.ljust(width)}  {s['code']:>6} {s['comment']:>8} {s['blank']:>6}  {rtxt}")
    total_ratio = (tot_comment / tot_code) if tot_code else float("inf")
    out.append("-" * (width + 30))
    out.append(f"{'TOTAL'.ljust(width)}  {tot_code:>6} {tot_comment:>8} {tot_blank:>6}  "
               + (f"{total_ratio:.2f}" if tot_code else " all "))
    out.append("")
    out.append(f"Added lines: {tot_code} code, {tot_comment} comment, {tot_blank} blank.")
    return "\n".join(out)


def to_markdown(results):
    lines = []
    for r in results:
        pr = r.get("pr")
        commit = r.get("commit")
        if pr:
            lines.append(f"## PR #{pr['id']} — {pr['title']}")
            lines.append(f"**Author:** {pr['author']} | **Branch:** `{pr['sourceRef']}` → `{pr['targetRef']}`")
        elif commit:
            lines.append(f"## Commit {commit['id']} — {commit['shortMessage']}")
            lines.append(f"**Author:** {commit['author']} | **Date:** {commit['date']}")
        lines.append("")
        for f in r["files"]:
            if f.get("skipped"):
                continue
            a = f.get("stats", {}).get("added", 0)
            rm = f.get("stats", {}).get("removed", 0)
            kinds = _change_kinds(f["changeType"])
            ct = ("RENAMED" if "rename" in kinds else "ADDED" if "add" in kinds
                  else "DELETED" if "delete" in kinds else "MODIFIED")
            name = f"`{f['oldPath']}` → `{f['path']}`" if f.get("oldPath") else f"`{f['path']}`"
            if f.get("binary"):
                lines.append(f"### {name} (BINARY, {_binary_desc(f['binary'])})")
                lines.append("")
                continue
            note = ", whitespace only" if f.get("whitespaceOnly") else ""
            lines.append(f"### {name} ({ct}, +{a}/-{rm}{note})")
            if f.get("patch"):
                lines.append("```diff")
                lines.append(f["patch"].strip())
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _add_diff_flags(p, pr=False):
    p.add_argument("--output", choices=["json", "summary", "md", "lines", "stats"], default="json",
                   help="Output format (default: json). 'lines' emits file:line for every added line.")
    p.add_argument("--chunk", type=int, default=300, help="Max lines per JSON patch chunk (default: 300)")
    p.add_argument("--force", action="store_true", help="Bypass the cache and re-fetch everything")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress progress on stderr (also off when stdout is redirected). Errors still show.")
    p.add_argument("--repo", metavar="CLONE",
                   help="Diff in this local clone with git (histogram, renames, method names in hunk "
                        "headers). Reads objects only; fetches missing commits. Default: the REST API.")
    p.add_argument("--context", "-U", type=int, default=5, help="Context lines per hunk (default: 5)")
    p.add_argument("--function-context", action="store_true",
                   help="With --repo: show the whole enclosing function around each change")
    p.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="Diff files the defaults skip (repeatable), e.g. --include '*.resx'")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="Skip more files (repeatable), e.g. --exclude 'Migrations/*'")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Parallel file downloads through the API (default: {DEFAULT_WORKERS})")
    if pr:
        p.add_argument("--since-iteration", type=int, metavar="N",
                       help="Diff iteration N's source commit against the latest one: what changed "
                            "since a previous review round")


def _diff_opts(args):
    return {
        "force": args.force,
        "repo": args.repo,
        "context": args.context,
        "function_context": args.function_context,
        "include": args.include,
        "exclude": args.exclude,
        "workers": args.workers,
        "since_iteration": getattr(args, "since_iteration", None),
    }


def _validate_diff_args(args):
    if args.function_context and not args.repo:
        warn("ERROR: --function-context needs --repo <clone> (it uses git's funcname drivers).")
        sys.exit(2)
    if args.context < 0:
        warn("ERROR: --context must be >= 0.")
        sys.exit(2)
    if args.repo and not Path(args.repo).exists():
        warn(f"ERROR: --repo {args.repo} does not exist.")
        sys.exit(2)


def _require_pat(cfg):
    if not cfg["pat"]:
        warn('ERROR: Azure PAT not configured. Set the AZURE_PAT env var, or add "pat" to ~/.azure-connector.json')
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="git-diff-analysis",
        description="Fetch Azure DevOps PR unified diffs, cache in SQLite, output analysis-ready JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch diff as JSON chunks for AI analysis (default)
  python git_diff_analysis.py pr https://dev.azure.com/your-org/YourProject/_git/your-repo/pullrequest/123

  # Same diff from a local clone: faster, histogram algorithm, method names in hunk headers
  python git_diff_analysis.py pr <url> --repo path/to/your-repo --output md

  # What changed since iteration 2 (second review round)
  python git_diff_analysis.py pr <url> --since-iteration 2 --output summary

  # Fetch a single commit diff by commit URL
  python git_diff_analysis.py commit https://dev.azure.com/your-org/YourProject/_git/your-repo/commit/abc123

  # Show cache stats; drop rows older than 30 days and compact the file
  python git_diff_analysis.py cache stats
  python git_diff_analysis.py cache prune --days 30

Config: ~/.azure-connector.json (a JSON file with "pat"/"org"/"baseUrl")
        or env vars AZURE_PAT, AZURE_ORG, AZURE_BASE_URL
        """
    )
    subparsers = parser.add_subparsers(dest="command")

    pr_parser = subparsers.add_parser("pr", help="Fetch and diff one or more PRs")
    pr_parser.add_argument("urls", nargs="+", help="Azure DevOps PR URL(s)")
    _add_diff_flags(pr_parser, pr=True)

    commit_parser = subparsers.add_parser("commit", help="Fetch and diff one or more commits")
    commit_parser.add_argument("urls", nargs="+", help="Azure DevOps commit URL(s)")
    _add_diff_flags(commit_parser)

    cache_parser = subparsers.add_parser("cache", help="Manage the SQLite cache")
    cache_parser.add_argument("action", choices=["stats", "clear", "prune"], help="stats, clear or prune")
    cache_parser.add_argument("pr_url", nargs="?", help="PR or commit URL (optional, for clear)")
    cache_parser.add_argument("--days", type=int, default=30, help="prune: keep rows newer than this (default 30)")

    tags_parser = subparsers.add_parser(
        "tags", help="Compare two tags of a repo: commits + changed files")
    tags_parser.add_argument("project", help="Project name, or a repo URL "
                             "(https://dev.azure.com/<org>/<project>/_git/<repo>)")
    tags_parser.add_argument("repo", nargs="?", help="Repo name (omit if first arg is a URL)")
    tags_parser.add_argument("old_tag", help="Old/base tag")
    tags_parser.add_argument("new_tag", help="New/target tag")

    tw_parser = subparsers.add_parser(
        "check-tailwind",
        help="Are the arbitrary-value Tailwind classes added by a PR actually compiled?")
    tw_parser.add_argument("urls", nargs="+", help="PR URL(s)")
    tw_parser.add_argument("--css", help="Repo path of the COMPILED css, read at the PR's head commit")
    tw_parser.add_argument("--css-local", dest="css_local",
                           help="Read the css from this local file instead (CI-built, not committed)")
    tw_parser.add_argument("--prefix", default="tw-", help="Utility class prefix (default: tw-)")
    tw_parser.add_argument("--force", action="store_true", help="Bypass the diff cache")

    resx_parser = subparsers.add_parser(
        "check-resx",
        help="Does every resource key a PR adds exist in all localized resx files?")
    resx_parser.add_argument("urls", nargs="+", help="PR URL(s)")
    resx_parser.add_argument("--root",
                             help="Read the resx from this local clone instead of the PR's head "
                                  "commit (warns when the clone is on another branch)")
    resx_parser.add_argument("--resx", required=True,
                             help="Repo path of the resx with {locale} as the placeholder, "
                                  "e.g. src/App/Resources/Strings.{locale}.resx")
    resx_parser.add_argument("--locales", default="pt-BR,en-US,es-ES", help="Comma-separated locales")
    resx_parser.add_argument("--getter", default="Engine.Get",
                             help="Resource accessor to scan for (default: Engine.Get)")
    resx_parser.add_argument("--force", action="store_true", help="Bypass the diff cache")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        _run(args)
    except RuntimeError as e:
        # HttpError, git failures, bad iteration: a real error, never a partial diff.
        warn(f"ERROR: {e}")
        sys.exit(2)


def _run(args):
    global _PROGRESS
    if args.command in ("check-tailwind", "check-resx"):
        cfg = load_config()
        _require_pat(cfg)
        db = get_db()
        if args.command == "check-tailwind":
            if bool(args.css) == bool(args.css_local):
                warn("ERROR: pass exactly one of --css <repo-path> (read at the PR head) or --css-local <file>.")
                sys.exit(2)
            sys.exit(cmd_check_tailwind(args, cfg, db))
        sys.exit(cmd_check_resx(args, cfg, db))

    if args.command == "cache":
        db = get_db()
        if args.action == "stats":
            stats = cache_stats(db)
            print(f"Cache DB: {DB_FILE}")
            print(f"  Size                : {stats['bytes'] / 1e6:.1f} MB")
            print(f"  PR metadata entries : {stats['prs']}")
            print(f"  File diff entries   : {stats['files']}")
            if stats["oldest_ms"]:
                age_days = (time.time() * 1000 - stats["oldest_ms"]) / 86400000
                print(f"  Oldest entry        : {age_days:.1f} days ago")
        elif args.action == "clear":
            cache_clear(db, args.pr_url)
            print(f"Cache cleared for {args.pr_url}" if args.pr_url else "Full cache cleared.")
        elif args.action == "prune":
            before = cache_stats(db)["bytes"]
            removed = cache_prune(db, args.days)
            after = cache_stats(db)["bytes"]
            print(f"Pruned {removed} rows older than {args.days} days; "
                  f"{before / 1e6:.1f} MB -> {after / 1e6:.1f} MB.")
        return

    if args.command == "tags":
        cfg = load_config()
        _require_pat(cfg)
        # URL form: `tags <repo-url> <oldTag> <newTag>` → repo arg is absent.
        if args.repo is None:
            info = parse_repo_url(args.project)
            if not info:
                warn(f"ERROR: Cannot parse repo URL: {args.project}")
                sys.exit(1)
            org, project, repo = info["org"], info["project"], info["repo"]
        else:
            org, project, repo = cfg["org"], args.project, args.repo
        out = compare_tags(cfg, org, project, repo, args.old_tag, args.new_tag)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    # pr / commit
    # Progress is silenced when stdout is redirected (or --quiet), so
    # `... pr <url> --output json > out.json` produces clean JSON. Errors still reach stderr.
    if args.quiet or not sys.stdout.isatty():
        _PROGRESS = None
    _validate_diff_args(args)
    cfg = load_config()
    _require_pat(cfg)
    db = get_db()
    opts = _diff_opts(args)
    results = []

    for raw_url in args.urls:
        if args.command == "commit":
            parsed = parse_commit_url(raw_url)
            if not parsed:
                warn(f"ERROR: Cannot parse commit URL: {raw_url}")
                sys.exit(1)
        else:
            parsed = parse_pr_url(raw_url)
            if not parsed:
                warn(f"ERROR: Cannot parse PR URL: {raw_url}")
                sys.exit(1)
        if args.repo and not git_origin_matches(args.repo, parsed["repo"]):
            warn(f"WARNING: --repo {args.repo} has an origin that does not look like "
                 f"_git/{parsed['repo']}; the diff will fail if the commits are not there.")
        if args.command == "commit":
            log(f"\nProcessing commit {parsed['commit_id'][:10]} ({parsed['repo']})...")
            result = fetch_commit(cfg, db, org=parsed["org"], project=parsed["project"],
                                  repo=parsed["repo"], commit_id=parsed["commit_id"],
                                  commit_url=raw_url, opts=opts)
        else:
            log(f"\nProcessing PR #{parsed['pr_id']} ({parsed['repo']})...")
            result = fetch_pr(cfg, db, org=parsed["org"], project=parsed["project"],
                              repo=parsed["repo"], pr_id=parsed["pr_id"],
                              pr_url=raw_url, opts=opts)
        results.append(result)

    emit_output(results, args)


# ── Review checks over added lines ────────────────────────────────────────────
#
# Each check answers a question about the lines a change ADDS, using the same
# reconstruction as --output lines, and compares them with a file read at the PR's
# head commit.


def added_lines(results):
    """[(path, line_no, text)] for every added line across the fetched results."""
    out = []
    for r in results:
        for f in r.get("files", []):
            if f.get("skipped"):
                continue
            for source in _patch_sources(f):
                for line_no, text in iter_added(source):
                    out.append((f["path"], line_no, text))
    return out


def extract_tailwind_classes(lines, prefix="tw-"):
    """
    Arbitrary-value utility classes (`tw-text-[10px]`) introduced by a change.

    Only arbitrary-value classes are collected: a plain utility is either in the
    safelist or in the content scan, while an arbitrary value is compiled only if
    the scanner actually saw that exact string — which is the failure worth checking.
    """
    pat = re.compile(re.escape(prefix) + r"[a-z0-9-]*\[[^\]\s\"']+\]")
    found = {}
    for path, line_no, text in lines:
        for cls in pat.findall(text):
            found.setdefault(cls, []).append(f"{path}:{line_no}")
    return found


def tailwind_compiled(css: str, cls: str) -> bool:
    """Is this class present as a compiled selector in the built CSS?

    Tailwind escapes the brackets in the emitted selector (`.tw-text-\\[10px\\]`),
    so a naive substring search for the raw class name misses every one of them.
    Both forms are checked before concluding the class was not compiled.
    """
    escaped = cls.replace("[", r"\[").replace("]", r"\]")
    return (escaped in css) or (cls in css)


def cmd_check_tailwind(args, cfg, db):
    results = _fetch_results(args, cfg, db)
    lines = added_lines(results)
    classes = extract_tailwind_classes(lines, args.prefix)
    if not classes:
        print(f"No arbitrary-value {args.prefix}* classes in the added lines.")
        return 0

    # Default: read the CSS at the PR's head commit, so the answer is about the same
    # tree the added lines came from. --css-local is the escape hatch for a build
    # artifact that is produced in CI and never committed.
    if args.css_local:
        try:
            css = open(args.css_local, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print(f"ERROR: cannot read --css-local {args.css_local}: {e}", file=sys.stderr)
            return 2
        source = f"{args.css_local} (local file — NOT the PR's tree)"
    else:
        try:
            css = fetch_at_pr_head(cfg, results[0], args.css)
        except RuntimeError as e:
            print(f"ERROR: cannot read {args.css} at the PR's head commit: {e}", file=sys.stderr)
            return 2
        if css is None:
            print(f"ERROR: {args.css} does not exist at the PR's head commit.", file=sys.stderr)
            print("       Check the repo path, or pass --css-local if the CSS is built in CI "
                  "and not committed.", file=sys.stderr)
            return 2
        head = ((results[0].get("pr") or {}).get("sourceCommit") or "")[:10]
        source = f"{args.css} @ {head or (results[0].get('pr') or {}).get('sourceRef')}"
    print(f"compiled CSS: {source} ({len(css):,} bytes)")
    print(f"arbitrary-value classes added: {len(classes)}\n")
    missing = []
    for cls in sorted(classes):
        ok = tailwind_compiled(css, cls)
        if not ok:
            missing.append(cls)
        print(f"  {'OK     ' if ok else 'MISSING'}  {cls}")
        if not ok:
            for where in classes[cls][:3]:
                print(f"             {where}")
    print()
    if missing:
        print(f"{len(missing)} class(es) are NOT in the compiled CSS — they will render as nothing.")
        if args.css_local:
            print("You used --css-local: confirm that file is the build produced AFTER these lines,")
            print("and from this PR's code. Otherwise this list means nothing.")
        return 1
    print("Every added arbitrary-value class is compiled.")
    return 0


def extract_resource_keys(lines, getter="Engine.Get"):
    """Resource keys introduced by a change, e.g. Engine.Get("Greeting_Title")."""
    pat = re.compile(re.escape(getter) + r"\(\s*\"([A-Za-z0-9_.]+)\"\s*\)")
    found = {}
    for path, line_no, text in lines:
        for key in pat.findall(text):
            found.setdefault(key, []).append(f"{path}:{line_no}")
    return found


def resx_has_key(resx_text: str, key: str) -> bool:
    return re.search(r'name\s*=\s*"' + re.escape(key) + r'"', resx_text) is not None


def cmd_check_resx(args, cfg, db):
    results = _fetch_results(args, cfg, db)
    keys = extract_resource_keys(added_lines(results), args.getter)
    if not keys:
        print(f"No new {args.getter}(...) keys in the added lines.")
        return 0
    locales = [l.strip() for l in args.locales.split(",") if l.strip()]
    pr = results[0].get("pr") or {}
    pr_branch = pr.get("sourceRef")
    head = (pr.get("sourceCommit") or "")[:10]

    print(f"new keys: {len(keys)}    locales: {', '.join(locales)}")
    if args.root:
        # Opt-in local mode. A key "missing" from a clone that is not at the PR's
        # head means nothing, so say so before the findings rather than leaving the
        # reader to reach a wrong conclusion.
        clone_branch = _git_branch(args.root)
        print(f"reading:  {args.root}  @ {clone_branch or '(unknown)'}   [LOCAL TREE]")
        if pr_branch:
            print(f"PR head:  {pr_branch}")
        if pr_branch and clone_branch and pr_branch != clone_branch:
            print()
            print("  ⚠  THE CLONE IS NOT ON THE PR'S BRANCH. Any key reported missing below is")
            print("     probably just absent from the checked-out tree, not from the change.")
            print("     Drop --root to read the resx at the PR's head commit instead.")
    else:
        print(f"reading:  {pr.get('repo')} @ {head or pr_branch}   [PR HEAD]")
    print()

    failed = False
    for locale in locales:
        rel = args.resx.replace("{locale}", locale)
        if args.root:
            path = os.path.join(args.root, rel)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError as e:
                print(f"  {locale:<8} ERROR reading {path}: {e}")
                failed = True
                continue
        else:
            try:
                text = fetch_at_pr_head(cfg, results[0], rel)
            except RuntimeError as e:
                print(f"ERROR: cannot read {rel} at the PR's head commit: {e}", file=sys.stderr)
                return 2
            if text is None:
                print(f"  {locale:<8} ERROR: {rel} does not exist at the PR's head commit")
                failed = True
                continue
        missing = [k for k in sorted(keys) if not resx_has_key(text, k)]
        if missing:
            failed = True
            print(f"  {locale:<8} MISSING {len(missing)}: {', '.join(missing)}")
            for k in missing[:5]:
                print(f"           {k}  <- {keys[k][0]}")
        else:
            print(f"  {locale:<8} all {len(keys)} present")
    return 1 if failed else 0


def fetch_at_pr_head(cfg, result, file_path):
    """
    A file's content as it exists at the PR's head commit.

    Reading the same file from a local clone answers a question about whichever
    branch happens to be checked out, so a key or class the PR does add can come
    back "missing". Pinned to the COMMIT, not the branch name, so a branch that
    moves mid-review cannot change the answer.

    Returns None only when the file does not exist at that commit (HTTP 404). Any
    other failure (auth, network, missing coordinates) raises RuntimeError: it is
    not evidence that the file is absent.
    """
    pr = (result or {}).get("pr") or {}
    commit = pr.get("sourceCommit")
    branch = pr.get("sourceRef")
    org, project, repo = pr.get("org"), pr.get("project"), pr.get("repo")
    if not (org and project and repo):
        raise RuntimeError("the PR result carries no org/project/repo to read the file from")
    try:
        return api_get_file_content(
            cfg, org, project, repo, file_path,
            commit_id=commit, branch=(None if commit else branch),
        )
    except RuntimeError as e:
        if str(e).startswith("HTTP 404"):
            return None
        raise
    except OSError as e:  # URLError, timeout, connection reset
        raise RuntimeError(str(e)) from e


def _git_branch(root):
    """Current branch of a clone, or None when it isn't a git working tree."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _fetch_results(args, cfg, db):
    """Fetch the PR(s) named on the command line — shared by the check commands."""
    results = []
    for raw_url in args.urls:
        parsed = parse_pr_url(raw_url)
        if not parsed:
            print(f"ERROR: Cannot parse PR URL: {raw_url}", file=sys.stderr)
            sys.exit(1)
        result = fetch_pr(
            cfg, db,
            org=parsed["org"], project=parsed["project"],
            repo=parsed["repo"], pr_id=parsed["pr_id"],
            pr_url=raw_url, force=getattr(args, "force", False),
        )
        # The repo coordinates are attached here rather than inside pr_meta, which
        # is cached: an older cache entry may not carry them.
        result.setdefault("pr", {}).update({
            "org": parsed["org"], "project": parsed["project"], "repo": parsed["repo"],
        })
        results.append(result)
    return results


def emit_output(results, args):
    if args.output == "summary":
        print_summary(results)
    elif args.output == "md":
        print(to_markdown(results))
    elif args.output == "stats":
        print(to_stats(results))
    elif args.output == "lines":
        print(to_lines(results))
    else:
        print(json.dumps(to_analysis_json(results, args.chunk), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
