from __future__ import annotations

import re
from pathlib import Path


SKIP_DIRS = {
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    ".idea",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".gradle",
    "vendor",
    "coverage",
    ".next",
    ".turbo",
    ".cursor",
}


def iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in _walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            files.append(base / name)
    return files


def _walk(root: Path):
    # os.walk is faster than Path.rglob on large trees
    import os

    yield from os.walk(root)


def rel(root: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def expand_braces(pattern: str) -> list[str]:
    if "{" not in pattern:
        return [pattern]
    start = pattern.index("{")
    end = pattern.index("}", start)
    body = pattern[start + 1 : end]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    out: list[str] = []
    for option in body.split(","):
        out.extend(expand_braces(prefix + option + suffix))
    return out


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a glob with ** / * / ? into a full-match regex."""
    i = 0
    out: list[str] = ["^"]
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            if i + 2 < len(pattern) and pattern[i + 2] == "/":
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def match_glob(root: Path, pattern: str, files: list[Path], limit: int = 40) -> list[Path]:
    regexes = [glob_to_regex(p[2:] if p.startswith("./") else p) for p in expand_braces(pattern)]
    hits: list[Path] = []
    seen: set[str] = set()
    for file_path in files:
        posix = rel(root, Path(file_path)).replace("\\", "/")
        name = Path(file_path).name
        if any(rx.match(posix) or rx.match(name) for rx in regexes):
            if posix not in seen:
                seen.add(posix)
                hits.append(Path(file_path))
        if len(hits) >= limit:
            break
    return hits


def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        return Path(path).read_bytes()[:limit].decode("utf-8", errors="ignore")
    except OSError:
        return ""
