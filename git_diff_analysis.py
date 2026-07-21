#!/usr/bin/env python3
"""
git-diff-analysis — Azure DevOps PR and commit diff fetcher with SQLite cache.

Fetches actual unified patches (not full file contents), caches them in SQLite,
and outputs analysis-ready JSON chunks.

Usage:
    python git_diff_analysis.py pr <url> [<url2> ...]  [--output json|summary|md|lines] [--chunk N] [--force]
    python git_diff_analysis.py commit <commit-url> [<commit-url2> ...]  [--output json|summary|md|lines] [--chunk N] [--force]
    python git_diff_analysis.py tags <project> <repo> <oldTag> <newTag>
    python git_diff_analysis.py cache stats
    python git_diff_analysis.py cache clear [<url>]

Config: reads ~/.azure-connector.json  (shared with azure-connector node tool)
        or env vars: AZURE_PAT, AZURE_ORG, AZURE_BASE_URL
"""

import argparse
import base64
import difflib
import json
import os
import re
import sqlite3
import sys

# Force UTF-8 output on Windows (pr titles / file names can contain non-ASCII / emoji)
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

def _auth_header(pat: str) -> str:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Basic {token}"


def _api_request(url: str, pat: str, accept: str = "application/json",
                 method: str = "GET", body: Optional[dict] = None) -> any:
    headers = {"Authorization": _auth_header(pat), "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
            if accept == "application/json":
                return json.loads(body_text)
            return body_text
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {url}\n{err}")


def api_get_pr(cfg, org, project, repo, pr_id):
    base = f"{cfg['baseUrl']}/{org}"
    url = (
        f"{base}/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
        f"/pullRequests/{pr_id}?api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"])


def api_get_iterations(cfg, org, project, repo, pr_id):
    base = f"{cfg['baseUrl']}/{org}"
    url = (
        f"{base}/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
        f"/pullRequests/{pr_id}/iterations?api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"])


def api_get_branch_diff(cfg, org, project, repo, source_ref, target_ref):
    base = f"{cfg['baseUrl']}/{org}"
    url = (
        f"{base}/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
        f"/diffs/commits"
        f"?baseVersion={urllib.parse.quote(target_ref, safe='')}&baseVersionType=branch"
        f"&targetVersion={urllib.parse.quote(source_ref, safe='')}&targetVersionType=branch"
        f"&$top=500&api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"])


def api_get_commit_diff(cfg, org, project, repo, commit_id, parent_id=None):
    base = f"{cfg['baseUrl']}/{org}"
    q = urllib.parse.quote
    parent = parent_id or (commit_id + "~1")
    url = (
        f"{base}/{q(project, safe='')}"
        f"/_apis/git/repositories/{q(repo, safe='')}"
        f"/diffs/commits"
        f"?baseVersion={q(parent, safe='')}&baseVersionType=commit"
        f"&targetVersion={q(commit_id, safe='')}&targetVersionType=commit"
        f"&$top=500&api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"])


def api_get_commit(cfg, org, project, repo, commit_id):
    base = f"{cfg['baseUrl']}/{org}"
    url = (
        f"{base}/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
        f"/commits/{urllib.parse.quote(commit_id, safe='')}?api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"])


def api_get_file_content(cfg, org, project, repo, file_path, branch=None, commit_id=None):
    base = f"{cfg['baseUrl']}/{org}"
    version = commit_id or branch
    vtype = "commit" if commit_id else "branch"
    if not version:
        raise ValueError("Either branch or commit_id is required to fetch file content.")
    url = (
        f"{base}/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
        f"/items"
        f"?path={urllib.parse.quote(file_path, safe='')}"
        f"&versionDescriptor.version={urllib.parse.quote(version, safe='')}"
        f"&versionDescriptor.versionType={vtype}&api-version={API_VERSION}"
    )
    return _api_request(url, cfg["pat"], accept="text/plain")


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


def _repo_base(cfg, org, project, repo):
    return (
        f"{cfg['baseUrl']}/{org}"
        f"/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}"
    )


def compare_tags(cfg, org, project, repo, old_tag, new_tag):
    base = _repo_base(cfg, org, project, repo)
    q = urllib.parse.quote

    # 1. diff between tags → changed files + base/target commit ids (paginated)
    diff = None
    all_changes = []
    skip = 0
    while True:
        page = _api_request(
            f"{base}/diffs/commits"
            f"?baseVersion={q(old_tag, safe='')}&baseVersionType=tag"
            f"&targetVersion={q(new_tag, safe='')}&targetVersionType=tag"
            f"&$top=1000&$skip={skip}&api-version={API_VERSION}",
            cfg["pat"],
        )
        if diff is None:
            diff = page
        pc = page.get("changes") or []
        all_changes.extend(pc)
        if len(pc) < 1000:
            break
        skip += 1000
    diff["changes"] = all_changes

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
            for c in (diff.get("changes") or [])
            if (c.get("item") or {}).get("gitObjectType") == "blob"
            or (c.get("item") and not c["item"].get("isFolder"))
        ],
    }


# ── SQLite cache ──────────────────────────────────────────────────────────────

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
    db.commit()
    return db


def cache_get_pr(db, cache_key, max_age_ms=30*60*1000):
    row = db.execute("SELECT data, fetched_at FROM pr_meta WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    if (time.time() * 1000 - row[1]) > max_age_ms:
        return None
    return json.loads(row[0])


def cache_set_pr(db, cache_key, data):
    db.execute(
        "INSERT OR REPLACE INTO pr_meta (cache_key, data, fetched_at) VALUES (?, ?, ?)",
        (cache_key, json.dumps(data), int(time.time() * 1000))
    )
    db.commit()


def cache_get_file(db, pr_url, file_path, max_age_ms=30*60*1000):
    row = db.execute(
        "SELECT change_type, source_branch, target_branch, old_content, new_content, patch, fetched_at "
        "FROM file_diff WHERE pr_url = ? AND file_path = ?",
        (pr_url, file_path)
    ).fetchone()
    if not row:
        return None
    if (time.time() * 1000 - row[6]) > max_age_ms:
        return None
    return {
        "change_type": row[0], "source_branch": row[1], "target_branch": row[2],
        "old_content": row[3], "new_content": row[4], "patch": row[5],
    }


def cache_set_file(db, pr_url, file_path, change_type, source_branch, target_branch, old_content, new_content, patch):
    db.execute("""
        INSERT OR REPLACE INTO file_diff
          (pr_url, file_path, change_type, source_branch, target_branch, old_content, new_content, patch, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pr_url, file_path, change_type, source_branch, target_branch, old_content, new_content, patch, int(time.time() * 1000)))
    db.commit()


def cache_stats(db):
    prs = db.execute("SELECT COUNT(*) FROM pr_meta").fetchone()[0]
    files = db.execute("SELECT COUNT(*) FROM file_diff").fetchone()[0]
    oldest = db.execute("SELECT MIN(fetched_at) FROM file_diff").fetchone()[0]
    return {"prs": prs, "files": files, "oldest_ms": oldest}


def cache_clear(db, pr_url=None):
    if pr_url:
        db.execute("DELETE FROM file_diff WHERE pr_url = ?", (pr_url,))
        db.execute("DELETE FROM pr_meta WHERE cache_key LIKE ?", (f"{pr_url}%",))
    else:
        db.execute("DELETE FROM file_diff")
        db.execute("DELETE FROM pr_meta")
    db.commit()


# ── Diff computation ──────────────────────────────────────────────────────────

def should_analyze(file_path: str) -> bool:
    ext = Path(file_path).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return False
    for pat in SKIP_PATTERNS:
        if pat.search(file_path):
            return False
    return True


def compute_patch(file_path: str, old_content: Optional[str], new_content: Optional[str]) -> str:
    old = (old_content or "").splitlines(keepends=True)
    new = (new_content or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        old, new,
        fromfile=f"a{file_path}",
        tofile=f"b{file_path}",
        n=5,
    )
    return "".join(diff)


def patch_stats(patch: str) -> dict:
    added = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
    return {"added": added, "removed": removed}


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
            # Remove leading empty/context lines until we find the next hunk
            # header; then prepend the previous hunk header so new_line
            # tracking works across chunks.
            if chunk_lines and not chunk_lines[0].startswith("@@"):
                if last_hunk_header:
                    chunk_lines.insert(0, last_hunk_header)
                    chunks[i] = "".join(chunk_lines)
    return chunks if chunks else [patch]


# ── Fetch PR ──────────────────────────────────────────────────────────────────

def fetch_pr(cfg, db, org, project, repo, pr_id, pr_url, force=False):
    # Cache namespace. Diffs are pinned to the PR's commits (merge base -> source tip),
    # which works on completed PRs and yields right-side line numbers that match what
    # Azure anchors inline comments against.
    ns = f"{pr_url}#v2"
    meta_key = f"{ns}:meta"

    # PR metadata
    pr_meta = None if force else cache_get_pr(db, meta_key)
    if not pr_meta:
        print(f"  Fetching PR #{pr_id} metadata...", file=sys.stderr)
        data = api_get_pr(cfg, org, project, repo, pr_id)
        # Resolve the latest iteration's commits: right side = sourceRefCommit,
        # diff base = commonRefCommit (the merge base — NOT the target tip, which on a
        # completed PR already contains the merged changes and would diff to nothing).
        source_commit = None
        base_commit = None
        try:
            its = api_get_iterations(cfg, org, project, repo, pr_id)
            iterations = its.get("value") or []
            if iterations:
                latest = iterations[-1]
                source_commit = (latest.get("sourceRefCommit") or {}).get("commitId")
                base_commit = (latest.get("commonRefCommit") or {}).get("commitId")
        except Exception:
            pass  # fall back to branch-tip diffing below
        pr_meta = {
            "id": data.get("pullRequestId"),
            "title": data.get("title"),
            "author": data.get("createdBy", {}).get("displayName"),
            "status": data.get("status"),
            "sourceRef": (data.get("sourceRefName") or "").replace("refs/heads/", ""),
            "targetRef": (data.get("targetRefName") or "").replace("refs/heads/", ""),
            "sourceCommit": source_commit,
            "baseCommit": base_commit,
            "createdDate": data.get("creationDate"),
            "description": data.get("description"),
        }
        cache_set_pr(db, meta_key, pr_meta)

    source_ref = pr_meta["sourceRef"]
    target_ref = pr_meta["targetRef"]
    source_commit = pr_meta.get("sourceCommit")
    base_commit = pr_meta.get("baseCommit")
    pinned = bool(source_commit and base_commit)

    # Changed file list
    diff_key = f"{ns}:diff"
    changed_files = None if force else cache_get_pr(db, diff_key)
    if changed_files is None:
        print(f"  Fetching changed file list{' (pinned to merge base)' if pinned else ' (branch tips)'}...", file=sys.stderr)
        if pinned:
            diff_data = api_get_commit_diff(cfg, org, project, repo, source_commit, parent_id=base_commit)
        else:
            diff_data = api_get_branch_diff(cfg, org, project, repo, source_ref, target_ref)
        changed_files = [
            {"path": c["item"]["path"], "change_type": c.get("changeType", "edit")}
            for c in (diff_data.get("changes") or [])
            if not c.get("item", {}).get("isFolder") and c.get("item", {}).get("path")
        ]
        cache_set_pr(db, diff_key, changed_files)

    # Per-file patches
    files = []
    to_fetch = []

    for f in changed_files:
        if not should_analyze(f["path"]):
            files.append({"path": f["path"], "changeType": f["change_type"], "skipped": True})
            continue
        if not force:
            cached = cache_get_file(db, ns, f["path"])
            if cached:
                stats = patch_stats(cached["patch"] or "")
                files.append({"path": f["path"], "changeType": f["change_type"], "patch": cached["patch"], "stats": stats})
                continue
        to_fetch.append(f)

    if to_fetch:
        print(f"  Fetching diffs for {len(to_fetch)} files ({len(files)} cached)...", file=sys.stderr)

    for i, f in enumerate(to_fetch):
        old_content = None
        new_content = None

        if f["change_type"] != "add":
            try:
                if pinned:
                    old_content = api_get_file_content(cfg, org, project, repo, f["path"], commit_id=base_commit)
                else:
                    old_content = api_get_file_content(cfg, org, project, repo, f["path"], branch=target_ref)
            except Exception:
                pass

        if f["change_type"] != "delete":
            try:
                if pinned:
                    new_content = api_get_file_content(cfg, org, project, repo, f["path"], commit_id=source_commit)
                else:
                    new_content = api_get_file_content(cfg, org, project, repo, f["path"], branch=source_ref)
            except Exception:
                pass

        patch = compute_patch(f["path"], old_content, new_content)
        stats = patch_stats(patch)

        cache_set_file(db, ns, f["path"], f["change_type"], source_ref, target_ref, old_content, new_content, patch)
        files.append({"path": f["path"], "changeType": f["change_type"], "patch": patch, "stats": stats})

        if (i + 1) % 5 == 0 or (i + 1) == len(to_fetch):
            print(f"  {i+1}/{len(to_fetch)} files processed...", file=sys.stderr)

    # Sort: analyzable files first
    files.sort(key=lambda f: (f.get("skipped", False), f["path"]))
    return {"pr": pr_meta, "files": files}


# ── Output formatters ─────────────────────────────────────────────────────────

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
            ct = {"add": "[+]", "delete": "[-]"}.get(f["changeType"], "[~]")
            print(f"  {ct} {f['path']}  (+{a}/-{rm})")
        if skipped:
            print(f"  ... {len(skipped)} binary/generated files skipped")


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
            {
                "path": f["path"],
                "changeType": f["changeType"],
                "stats": f.get("stats"),
                "chunks": chunk_patch(f.get("patch") or "", chunk_size),
            }
            for f in r["files"]
            if not f.get("skipped") and f.get("patch")
        ]
        output.append({**header, "files": files})
    return output


def to_lines(results):
    """Emit 'file:line  content' for every added line — useful for pinpointing
    exact new-file line numbers before posting inline PR comments."""
    import re
    output = []
    for r in results:
        for f in r["files"]:
            if f.get("skipped"):
                continue
            path = f["path"]
            for chunk in f.get("chunks") or ([f["patch"]] if f.get("patch") else []):
                new_line = None
                for line in chunk.splitlines():
                    m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                    if m:
                        new_line = int(m.group(1))
                        continue
                    if new_line is None:
                        continue
                    if line.startswith("---") or line.startswith("+++"):
                        continue
                    if line.startswith("-"):
                        continue  # removed line — don't advance new_line
                    if line.startswith("+"):
                        output.append(f"{path}:{new_line}  {line[1:]}")
                        new_line += 1
                    else:
                        new_line += 1  # context line
    return "\n".join(output)


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
            ct = {"add": "ADDED", "delete": "DELETED"}.get(f["changeType"], "MODIFIED")
            lines.append(f"### `{f['path']}` ({ct}, +{a}/-{rm})")
            if f.get("patch"):
                lines.append("```diff")
                lines.append(f["patch"].strip())
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def fetch_commit(cfg, db, org, project, repo, commit_id, commit_url, force=False):
    meta_key = f"{commit_url}:meta"

    # Commit metadata
    commit_meta = None if force else cache_get_pr(db, meta_key)
    if not commit_meta:
        print(f"  Fetching commit {commit_id[:10]} metadata...", file=sys.stderr)
        data = api_get_commit(cfg, org, project, repo, commit_id)
        commit_meta = {
            "id": data.get("commitId", commit_id),
            "shortId": data.get("commitId", commit_id)[:10],
            "author": (data.get("author") or {}).get("name"),
            "date": (data.get("author") or {}).get("date"),
            "shortMessage": (data.get("comment") or "").split("\n")[0],
            "fullMessage": data.get("comment") or "",
        }
        cache_set_pr(db, meta_key, commit_meta)
    else:
        data = api_get_commit(cfg, org, project, repo, commit_id)

    # Changed file list
    diff_key = f"{commit_url}:diff"
    changed_files = None if force else cache_get_pr(db, diff_key)
    if changed_files is None:
        print(f"  Fetching changed file list...", file=sys.stderr)
        parents = data.get("parents") or []
        parent_id = parents[0] if parents else None
        diff_data = api_get_commit_diff(cfg, org, project, repo, commit_id, parent_id)
        changed_files = [
            {"path": c["item"]["path"], "change_type": c.get("changeType", "edit")}
            for c in (diff_data.get("changes") or [])
            if not c.get("item", {}).get("isFolder") and c.get("item", {}).get("path")
        ]
        cache_set_pr(db, diff_key, changed_files)

    # Per-file patches
    files = []
    to_fetch = []

    for f in changed_files:
        if not should_analyze(f["path"]):
            files.append({"path": f["path"], "changeType": f["change_type"], "skipped": True})
            continue
        if not force:
            cached = cache_get_file(db, commit_url, f["path"])
            if cached:
                stats = patch_stats(cached["patch"] or "")
                files.append({"path": f["path"], "changeType": f["change_type"], "patch": cached["patch"], "stats": stats})
                continue
        to_fetch.append(f)

    if to_fetch:
        print(f"  Fetching diffs for {len(to_fetch)} files ({len(files)} cached)...", file=sys.stderr)

    parents = data.get("parents") or []
    parent_id = parents[0] if parents else None

    for i, f in enumerate(to_fetch):
        old_content = None
        new_content = None

        if f["change_type"] != "add":
            try:
                old_content = api_get_file_content(cfg, org, project, repo, f["path"], commit_id=parent_id)
            except Exception:
                pass

        if f["change_type"] != "delete":
            try:
                new_content = api_get_file_content(cfg, org, project, repo, f["path"], commit_id=commit_id)
            except Exception:
                pass

        patch = compute_patch(f["path"], old_content, new_content)
        stats = patch_stats(patch)

        cache_set_file(db, commit_url, f["path"], f["change_type"], commit_id, parent_id or f"{commit_id}~1", old_content, new_content, patch)
        files.append({"path": f["path"], "changeType": f["change_type"], "patch": patch, "stats": stats})

        if (i + 1) % 5 == 0 or (i + 1) == len(to_fetch):
            print(f"  {i+1}/{len(to_fetch)} files processed...", file=sys.stderr)

    files.sort(key=lambda f: (f.get("skipped", False), f["path"]))
    return {"commit": commit_meta, "files": files}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="git-diff-analysis",
        description="Fetch Azure DevOps PR unified diffs, cache in SQLite, output analysis-ready JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch diff as JSON chunks for AI analysis (default)
  python git_diff_analysis.py pr https://dev.azure.com/your-org/YourProject/_git/your-repo/pullrequest/123

  # Fetch a single commit diff by commit URL
  python git_diff_analysis.py commit https://dev.azure.com/your-org/YourProject/_git/your-repo/commit/abc123

  # Fetch multiple PRs at once
  python git_diff_analysis.py pr <url1> <url2> --output summary

  # Force re-fetch (bypass cache)
  python git_diff_analysis.py pr <url> --force

  # Show cache stats
  python git_diff_analysis.py cache stats

  # Clear cache for one PR or commit
  python git_diff_analysis.py cache clear <url>

Config: ~/.azure-connector.json  (shared with azure-connector node tool)
        or env vars AZURE_PAT, AZURE_ORG, AZURE_BASE_URL
        """
    )
    subparsers = parser.add_subparsers(dest="command")

    pr_parser = subparsers.add_parser("pr", help="Fetch and diff one or more PRs")
    pr_parser.add_argument("urls", nargs="+", help="Azure DevOps PR URL(s) or commit URL(s)")
    pr_parser.add_argument("--output", choices=["json", "summary", "md", "lines"], default="json",
                           help="Output format (default: json). 'lines' emits file:line for every added line.")
    pr_parser.add_argument("--chunk", type=int, default=300,
                           help="Max lines per JSON patch chunk (default: 300)")
    pr_parser.add_argument("--force", action="store_true",
                           help="Bypass cache and re-fetch everything")
    pr_parser.add_argument("--quiet", "-q", action="store_true",
                           help="Suppress progress messages on stderr (use when redirecting stdout to a file)")

    commit_parser = subparsers.add_parser("commit", help="Fetch and diff one or more commits")
    commit_parser.add_argument("urls", nargs="+", help="Azure DevOps commit URL(s)")
    commit_parser.add_argument("--output", choices=["json", "summary", "md", "lines"], default="json",
                               help="Output format (default: json)")
    commit_parser.add_argument("--chunk", type=int, default=300,
                               help="Max lines per JSON patch chunk (default: 300)")
    commit_parser.add_argument("--force", action="store_true",
                               help="Bypass cache and re-fetch everything")
    commit_parser.add_argument("--quiet", "-q", action="store_true",
                               help="Suppress progress messages on stderr")

    cache_parser = subparsers.add_parser("cache", help="Manage the SQLite cache")
    cache_parser.add_argument("action", choices=["stats", "clear"], help="stats or clear")
    cache_parser.add_argument("pr_url", nargs="?", help="PR or commit URL (optional, for clear)")

    tags_parser = subparsers.add_parser(
        "tags", help="Compare two tags of a repo: commits + changed files")
    tags_parser.add_argument("project", help="Project name, or a repo URL "
                             "(https://dev.azure.com/<org>/<project>/_git/<repo>)")
    tags_parser.add_argument("repo", nargs="?", help="Repo name (omit if first arg is a URL)")
    tags_parser.add_argument("old_tag", help="Old/base tag")
    tags_parser.add_argument("new_tag", help="New/target tag")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "cache":
        db = get_db()
        if args.action == "stats":
            stats = cache_stats(db)
            print(f"Cache DB: {DB_FILE}")
            print(f"  PR metadata entries : {stats['prs']}")
            print(f"  File diff entries   : {stats['files']}")
            if stats["oldest_ms"]:
                age_min = round((time.time() * 1000 - stats["oldest_ms"]) / 60000)
                print(f"  Oldest entry        : {age_min} minutes ago")
        elif args.action == "clear":
            cache_clear(db, args.pr_url)
            msg = f"Cache cleared for {args.pr_url}" if args.pr_url else "Full cache cleared."
            print(msg)
        return

    if args.command == "tags":
        cfg = load_config()
        if not cfg["pat"]:
            print("ERROR: Azure PAT not configured. Set AZURE_PAT env var or run: azure-connector config --pat <token>", file=sys.stderr)
            sys.exit(1)
        # URL form: `tags <repo-url> <oldTag> <newTag>` → repo arg is absent.
        if args.repo is None:
            info = parse_repo_url(args.project)
            if not info:
                print(f"ERROR: Cannot parse repo URL: {args.project}", file=sys.stderr)
                sys.exit(1)
            org, project, repo = info["org"], info["project"], info["repo"]
        else:
            org, project, repo = cfg["org"], args.project, args.repo
        try:
            out = compare_tags(cfg, org, project, repo, args.old_tag, args.new_tag)
        except Exception as e:
            print(f"ERROR {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    # commit command
    if args.command == "commit":
        if getattr(args, 'quiet', False) or not sys.stdout.isatty():
            sys.stderr = open(os.devnull, 'w')
        cfg = load_config()
        if not cfg["pat"]:
            print("ERROR: Azure PAT not configured. Set AZURE_PAT env var or run: azure-connector config --pat <token>", file=sys.stderr)
            sys.exit(1)
        db = get_db()
        results = []
        for raw_url in args.urls:
            parsed = parse_commit_url(raw_url)
            if not parsed:
                print(f"ERROR: Cannot parse commit URL: {raw_url}", file=sys.stderr)
                sys.exit(1)
            print(f"\nProcessing commit {parsed['commit_id'][:10]} ({parsed['repo']})...", file=sys.stderr)
            result = fetch_commit(
                cfg, db,
                org=parsed["org"], project=parsed["project"],
                repo=parsed["repo"], commit_id=parsed["commit_id"],
                commit_url=raw_url, force=args.force,
            )
            results.append(result)
        emit_output(results, args)
        return

    # pr command
    # Suppress progress on stderr when stdout is redirected to a file (or --quiet passed),
    # so that `python git_diff_analysis.py pr <url> --output json > out.json` produces clean JSON.
    if getattr(args, 'quiet', False) or not sys.stdout.isatty():
        sys.stderr = open(os.devnull, 'w')

    cfg = load_config()
    if not cfg["pat"]:
        print("ERROR: Azure PAT not configured. Set AZURE_PAT env var or run: azure-connector config --pat <token>", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    results = []

    for raw_url in args.urls:
        parsed = parse_pr_url(raw_url)
        if not parsed:
            print(f"ERROR: Cannot parse PR URL: {raw_url}", file=sys.stderr)
            sys.exit(1)
        print(f"\nProcessing PR #{parsed['pr_id']} ({parsed['repo']})...", file=sys.stderr)
        result = fetch_pr(
            cfg, db,
            org=parsed["org"], project=parsed["project"],
            repo=parsed["repo"], pr_id=parsed["pr_id"],
            pr_url=raw_url, force=args.force,
        )
        results.append(result)

    emit_output(results, args)


def emit_output(results, args):
    if args.output == "summary":
        print_summary(results)
    elif args.output == "md":
        print(to_markdown(results))
    elif args.output == "lines":
        json_results = to_analysis_json(results, args.chunk)
        print(to_lines(json_results))
    else:
        print(json.dumps(to_analysis_json(results, args.chunk), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
