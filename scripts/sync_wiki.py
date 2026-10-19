"""Publish the four-language ``docs/`` tree as a GitHub Wiki.

GitHub's wiki is a separate git repository (``<owner>/<repo>.wiki.git``) with
flat Markdown pages. This script builds one page per docs README and language
(``auth`` / ``auth-zh`` / ``auth-ja`` / ``auth-ko``), four ``Home`` pages, a
sidebar and a footer, rewrites the docs' relative links to wiki pages (or
GitHub blob URLs for source files), and can push the result to the wiki.

**One-time manual step:** GitHub does not create the wiki repository until a
human saves the first page in the Web UI. Before ``--push`` works, open
``https://github.com/<owner>/<repo>/wiki``, choose "Create the first page",
title it ``Home`` and save anything — this script then replaces that page and
publishes every other page. ``--check`` reports whether the wiki is
initialized; ``--push`` prints the same instruction and exits 3 when it is not.

Usage::

    uv run --no-sync python scripts/sync_wiki.py --check
    uv run --no-sync python scripts/sync_wiki.py --output wiki/
    uv run --no-sync python scripts/sync_wiki.py --push

The generated tree is gitignored (``/wiki/``); only this script is tracked.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
LANGS = ("en", "zh", "ja", "ko")
LANG_SUFFIX = {"en": "", "zh": "-zh", "ja": "-ja", "ko": "-ko"}
README_FILES = {"en": "README.md", "zh": "README.zh.md", "ja": "README.ja.md", "ko": "README.ko.md"}
LANG_NAMES = {"en": "English", "zh": "中文", "ja": "日本語", "ko": "한국어"}
BACK_LABEL = {"en": "Back to index", "zh": "返回目录", "ja": "目次へ戻る", "ko": "목차로 돌아가기"}
HOME_TITLE = {"en": "Sherry Wiki", "zh": "Sherry Wiki", "ja": "Sherry Wiki", "ko": "Sherry Wiki"}
HOME_INTRO = {
    "en": (
        "Mechanism-level documentation for the Sherry agent: context governance, "
        "tools and safety, memory, orchestration, and evaluation. Generated from "
        "the repository's `docs/` tree."
    ),
    "zh": (
        "Sherry Agent 的机制级文档：上下文治理、工具与安全、记忆、任务编排与评估。"
        "由仓库的 `docs/` 目录生成。"
    ),
    "ja": (
        "Sherry Agent の機構ドキュメント：コンテキスト管理、ツールと安全性、記憶、"
        "オーケストレーション、評価。リポジトリの `docs/` から生成。"
    ),
    "ko": (
        "Sherry Agent의 메커니즘 문서: 컨텍스트 거버넌스, 도구와 안전, 메모리, "
        "오케스트레이션, 평가. 저장소의 `docs/` 트리에서 생성."
    ),
}
TOPIC_LABEL = {"en": "Topic", "zh": "主题", "ja": "トピック", "ko": "주제"}
SOURCE_LABEL = {"en": "Source", "zh": "来源", "ja": "ソース", "ko": "소스"}
WIKI_LABEL = "Sherry Wiki"


@dataclass(frozen=True)
class Chapter:
    """One docs directory and its per-language README files."""

    slug: str
    rel_dir: Path
    files: dict[str, Path]

    def title(self, lang: str) -> str:
        """The page title in *lang* (falling back to English)."""
        file = self.files.get(lang) or self.files.get("en")
        if file is None:
            return self.slug
        return _title_of(file.read_text(encoding="utf-8")) or self.slug


def slugify(rel_dir: Path) -> str:
    """A stable, ASCII wiki page slug for one docs directory."""
    text = "-".join(part for part in rel_dir.parts if part not in ("", "."))
    text = text.replace("_", "-").lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "docs"


def page_name(slug: str, lang: str) -> str:
    return f"{slug}{LANG_SUFFIX[lang]}"


def _title_of(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _lang_of_filename(name: str) -> str | None:
    for lang, filename in README_FILES.items():
        if name == filename:
            return lang
    return None


def discover_chapters(docs_root: Path) -> list[Chapter]:
    """Collect every README directory that has at least the English page."""
    grouped: dict[Path, dict[str, Path]] = {}
    for path in docs_root.rglob("README*.md"):
        lang = _lang_of_filename(path.name)
        if lang is None:
            continue
        rel_dir = path.parent.relative_to(docs_root)
        grouped.setdefault(rel_dir, {})[lang] = path
    chapters = [
        Chapter(slug=slugify(rel_dir), rel_dir=rel_dir, files=files)
        for rel_dir, files in grouped.items()
        if "en" in files
    ]
    chapters.sort(key=lambda chapter: chapter.slug)
    return chapters


def build_page_map(docs_root: Path) -> dict[Path, Chapter]:
    return {chapter.rel_dir: chapter for chapter in discover_chapters(docs_root)}


def _github_blob(repo: str, branch: str, path: Path, anchor: str = "") -> str:
    url = f"https://github.com/{repo}/blob/{branch}/{path.as_posix()}"
    return f"{url}#{anchor}" if anchor else url


def _target_page(
    target: str,
    *,
    current_file: Path,
    docs_root: Path,
    repo_root: Path,
    repo: str,
    branch: str,
    pages: dict[Path, Chapter],
) -> str:
    """Resolve one relative Markdown link to a wiki page or a GitHub URL."""
    if not target or target.startswith(("#", "/", "//")):
        return target
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return target
    if "?" in target:
        return target
    raw_path, _, anchor = target.partition("#")
    resolved = Path(os.path.normpath(current_file.parent / raw_path))
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return target
    try:
        rel_to_docs = resolved.relative_to(docs_root)
    except ValueError:
        return _github_blob(repo, branch, resolved.relative_to(repo_root), anchor)

    target_lang: str | None = None
    if resolved.name in README_FILES.values():
        target_lang = _lang_of_filename(resolved.name)
        rel_dir = resolved.parent.relative_to(docs_root)
    elif (resolved / README_FILES["en"]).is_file():
        rel_dir = rel_to_docs
    else:
        return _github_blob(repo, branch, resolved.relative_to(repo_root), anchor)

    chapter = pages.get(rel_dir)
    if chapter is None:
        return _github_blob(repo, branch, resolved.relative_to(repo_root), anchor)
    lang = target_lang or _lang_of_filename(current_file.name) or "en"
    page = page_name(chapter.slug, lang if lang in chapter.files else "en")
    return f"{page}#{anchor}" if anchor else page


_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*?`+)")


def rewrite_links(
    markdown: str,
    *,
    current_file: Path,
    docs_root: Path,
    repo_root: Path,
    repo: str,
    branch: str,
    pages: dict[Path, Chapter],
) -> str:
    """Rewrite relative doc links, leaving fenced and inline code untouched."""
    out: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = _CODE_SPAN_RE.split(line)
        for index, part in enumerate(parts):
            if index % 2:
                continue

            def replace(match: re.Match[str]) -> str:
                label, target = match.group(1), match.group(2).strip()
                resolved = _target_page(
                    target,
                    current_file=current_file,
                    docs_root=docs_root,
                    repo_root=repo_root,
                    repo=repo,
                    branch=branch,
                    pages=pages,
                )
                return f"[{label}]({resolved})"

            parts[index] = _LINK_RE.sub(replace, part)
        out.append("".join(parts))
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


def _strip_language_switcher(markdown: str) -> str:
    """Drop the docs' own language line; the wiki adds its own nav."""
    lines = markdown.splitlines()
    for index, line in enumerate(lines[:12]):
        hits = sum(name in line for name in README_FILES.values())
        if hits >= 3 and ("README." in line or "English" in line):
            del lines[index]
            break
    return "\n".join(lines)


def _nav(slug: str, lang: str) -> str:
    links = []
    for other in LANGS:
        name = page_name(slug, other)
        label = LANG_NAMES[other]
        links.append(f"**{label}**" if other == lang else f"[{label}]({name})")
    return (
        f"> **{WIKI_LABEL}** · " + " · ".join(links) + "\n"
        f">\n> [← {BACK_LABEL[lang]}](Home{LANG_SUFFIX[lang]})\n"
    )


def _insert_after_title(markdown: str, block: str) -> str:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            lines[index + 1 : index + 1] = ["", block]
            return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")
    return block + "\n\n" + markdown


def build_page(
    chapter: Chapter,
    lang: str,
    *,
    docs_root: Path,
    repo_root: Path,
    repo: str,
    branch: str,
    pages: dict[Path, Chapter],
) -> str:
    file = chapter.files.get(lang) or chapter.files["en"]
    body = file.read_text(encoding="utf-8")
    body = _strip_language_switcher(body)
    body = rewrite_links(
        body,
        current_file=file,
        docs_root=docs_root,
        repo_root=repo_root,
        repo=repo,
        branch=branch,
        pages=pages,
    )
    body = _insert_after_title(body, _nav(chapter.slug, lang))
    return re.sub(r"\n{3,}", "\n\n", body)


def _home(chapters: list[Chapter], lang: str, *, repo: str, branch: str) -> str:
    rows = [
        f"| [{chapter.title(lang)}]({page_name(chapter.slug, lang)}) "
        f"| `docs/{chapter.rel_dir.as_posix()}` |"
        for chapter in chapters
    ]
    language_links = " · ".join(
        f"**{LANG_NAMES[other]}**"
        if other == lang
        else f"[{LANG_NAMES[other]}](Home{LANG_SUFFIX[other]})"
        for other in LANGS
    )
    return (
        f"# {HOME_TITLE[lang]}\n\n"
        f"> **{WIKI_LABEL}** · {language_links}\n\n"
        f"{HOME_INTRO[lang]}\n\n"
        f"| {TOPIC_LABEL[lang]} | {SOURCE_LABEL[lang]} |\n"
        "| :--- | :--- |\n" + "\n".join(rows) + f"\n\n[Repository](https://github.com/{repo}) · "
        f"[Docs source](https://github.com/{repo}/tree/{branch}/docs)\n"
    )


def _sidebar(repo: str, branch: str) -> str:
    language_links = " · ".join(f"[{LANG_NAMES[lang]}](Home{LANG_SUFFIX[lang]})" for lang in LANGS)
    return (
        f"**{WIKI_LABEL}**\n\n"
        f"{language_links}\n\n"
        f"[Docs source](https://github.com/{repo}/tree/{branch}/docs)\n"
    )


def _footer(repo: str, branch: str) -> str:
    return (
        f"Generated from the repository's `docs/` tree. "
        f"[Source](https://github.com/{repo}/tree/{branch}/docs)\n"
    )


def generate(
    output_dir: Path,
    *,
    docs_root: Path = DOCS_ROOT,
    repo_root: Path = REPO_ROOT,
    repo: str,
    branch: str = "main",
    clean: bool = False,
) -> int:
    """Write the wiki tree; return the number of pages generated."""
    chapters = discover_chapters(docs_root)
    pages = {chapter.rel_dir: chapter for chapter in chapters}
    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for old in output_dir.glob("*.md"):
            old.unlink()
    written = 0
    for lang in LANGS:
        (output_dir / f"Home{LANG_SUFFIX[lang]}.md").write_text(
            _home(chapters, lang, repo=repo, branch=branch), encoding="utf-8"
        )
        written += 1
    for chapter in chapters:
        for lang in LANGS:
            if lang not in chapter.files:
                continue
            page = build_page(
                chapter,
                lang,
                docs_root=docs_root,
                repo_root=repo_root,
                repo=repo,
                branch=branch,
                pages=pages,
            )
            (output_dir / f"{page_name(chapter.slug, lang)}.md").write_text(page, encoding="utf-8")
            written += 1
    (output_dir / "_Sidebar.md").write_text(_sidebar(repo, branch), encoding="utf-8")
    (output_dir / "_Footer.md").write_text(_footer(repo, branch), encoding="utf-8")
    return written


# ── Publishing ──────────────────────────────────────────────────────────────


def current_branch() -> str:
    """The checkout's branch (fallback ``main`` for a detached HEAD)."""
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    name = completed.stdout.strip()
    return name if completed.returncode == 0 and name and name != "HEAD" else "main"


def detect_repo() -> str:
    """Resolve ``owner/repo`` from the checkout's origin remote."""
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", completed.stdout.strip())
    if not match:
        raise SystemExit("cannot resolve the GitHub owner/repo from `git remote get-url origin`")
    return f"{match.group(1)}/{match.group(2)}"


def wiki_url(repo: str) -> str:
    return f"https://github.com/{repo}.wiki.git"


def wiki_web_url(repo: str) -> str:
    return f"https://github.com/{repo}/wiki"


def wiki_initialized(repo: str) -> tuple[bool, str]:
    """Whether the wiki git repository exists (GitHub 404s until the first page)."""
    completed = subprocess.run(
        ["git", "ls-remote", "--exit-code", wiki_url(repo), "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.returncode == 0, completed.stderr.strip()


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    completed = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SystemExit("no GitHub token: set GITHUB_TOKEN or run `gh auth login`")
    return completed.stdout.strip()


def _git_env(token: str) -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
    """A GIT_ASKPASS helper that hands git the token without putting it in argv."""
    holder = tempfile.TemporaryDirectory(prefix="sherry-wiki-auth-")
    script = Path(holder.name) / "askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *Username*) echo x-access-token ;;\n"
        '  *) echo "$SHERRY_WIKI_TOKEN" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    env = {
        **os.environ,
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
        "SHERRY_WIKI_TOKEN": token,
    }
    return env, holder


def _run_git(
    args: list[str], *, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def publish(repo: str, *, branch: str = "main") -> int:
    """Clone the wiki, regenerate it, and push (after the one-time UI step)."""
    initialized, _ = wiki_initialized(repo)
    if not initialized:
        print(
            "SKIPPED: the GitHub wiki repository does not exist yet.\n"
            f"Open {wiki_web_url(repo)} and click "
            '"Create the first page" (title: Home, any body), then rerun --push.\n'
            "GitHub creates the wiki git repository only after that first UI save.",
            file=sys.stderr,
        )
        return 3
    env, holder = _git_env(_token())
    cache = Path.home() / ".cache" / "sherry_wiki" / repo.replace("/", "-")
    try:
        if (cache / ".git").is_dir():
            pulled = _run_git(["pull", "--ff-only"], env=env, cwd=cache)
            if pulled.returncode != 0:
                print(pulled.stderr.strip(), file=sys.stderr)
                return 1
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(cache, ignore_errors=True)
            cloned = _run_git(["clone", wiki_url(repo), str(cache)], env=env)
            if cloned.returncode != 0:
                print(cloned.stderr.strip(), file=sys.stderr)
                return 1
        count = generate(cache, repo=repo, branch=branch, clean=True)
        staged = _run_git(["add", "-A"], env=env, cwd=cache)
        if staged.returncode != 0:
            print(staged.stderr.strip(), file=sys.stderr)
            return 1
        status = _run_git(["status", "--porcelain"], env=env, cwd=cache)
        if not status.stdout.strip():
            print(f"wiki already up to date ({count} pages)")
            return 0
        committed = _run_git(
            [
                "-c",
                "user.name=sherry-wiki",
                "-c",
                "user.email=sherry-wiki@users.noreply.github.com",
                "commit",
                "-m",
                "docs(wiki): sync the four-language wiki from docs/",
            ],
            env=env,
            cwd=cache,
        )
        if committed.returncode != 0:
            print(committed.stderr.strip(), file=sys.stderr)
            return 1
        pushed = _run_git(["push", "origin", "HEAD:master"], env=env, cwd=cache)
        if pushed.returncode != 0:
            print(pushed.stderr.strip(), file=sys.stderr)
            return 1
        print(f"published {count} pages to {wiki_web_url(repo)}")
        return 0
    finally:
        holder.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="", help="owner/repo (default: origin remote)")
    parser.add_argument(
        "--branch", default="", help="repo branch for source links (default: current)"
    )
    parser.add_argument("--output", type=Path, default=None, help="generate into this directory")
    parser.add_argument("--push", action="store_true", help="publish to the GitHub wiki")
    parser.add_argument("--check", action="store_true", help="report whether the wiki repo exists")
    args = parser.parse_args()
    repo = args.repo or detect_repo()
    branch = args.branch or current_branch()
    if args.check:
        initialized, detail = wiki_initialized(repo)
        print("initialized" if initialized else "uninitialized")
        if detail and not initialized:
            print(detail, file=sys.stderr)
        raise SystemExit(0 if initialized else 3)
    if args.push:
        raise SystemExit(publish(repo, branch=branch))
    output = args.output or (REPO_ROOT / "wiki")
    count = generate(output, repo=repo, branch=branch)
    print(f"generated {count} wiki pages in {output}")


if __name__ == "__main__":
    main()
