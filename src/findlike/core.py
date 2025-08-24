from __future__ import annotations

import fnmatch
import os
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass

SizeOp = tuple[str, int]  # (op: 'eq'|'lt'|'gt', bytes)


@dataclass(frozen=True)
class Options:
    name: str | None = None
    iname: str | None = None
    type: str | None = None  # 'f' | 'd' | 'l'
    maxdepth: int | None = None
    mindepth: int = 0
    follow_symlinks: bool = False
    empty: bool = False
    size: SizeOp | None = None
    ignore_hidden: bool = False
    exclude: tuple[str, ...] = ()
    exclude_dir: tuple[str, ...] = ()
    quiet: bool = False


_TYPE_MAP = {
    "f": stat.S_IFREG,
    "d": stat.S_IFDIR,
    "l": stat.S_IFLNK,
}


def parse_size(expr: str) -> SizeOp:
    if not expr:
        raise ValueError("size expression cannot be empty")

    op = "eq"
    if expr[0] in "+-":
        op = "gt" if expr[0] == "+" else "lt"
        expr = expr[1:]

    if not expr:
        raise ValueError("size expression missing number")

    num_part = ""
    unit_part = ""
    for ch in expr:
        if ch.isdigit():
            num_part += ch
        else:
            unit_part += ch
    if not num_part:
        raise ValueError("size expression missing digits")

    num = int(num_part)
    unit = unit_part.lower() if unit_part else "b"
    pow_map = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    if unit not in pow_map:
        raise ValueError(f"invalid size unit: {unit}")

    return op, num * pow_map[unit]


def _is_hidden(name: str) -> bool:
    return name.startswith(".") and name not in (".", "..")


def _match_name(name: str, pattern: str | None, case_insensitive: bool = False) -> bool:
    if pattern is None:
        return True
    if case_insensitive:
        name = name.lower()
        pattern = pattern.lower()
    return fnmatch.fnmatch(name, pattern)


def _match_type(entry: os.DirEntry, t: str | None) -> bool:
    if t is None:
        return True
    try:
        if t == "l":
            return entry.is_symlink()
        if t == "d":
            return entry.is_dir(follow_symlinks=False)
        if t == "f":
            return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
    return False


def _match_empty(path: str, entry: os.DirEntry) -> bool:
    try:
        if entry.is_dir(follow_symlinks=False):
            with os.scandir(path) as it:
                for _ in it:
                    return False
            return True
        if entry.is_file(follow_symlinks=False):
            return entry.stat(follow_symlinks=False).st_size == 0
        if entry.is_symlink():
            try:
                os.stat(path)
                return False
            except FileNotFoundError:
                return True
    except OSError:
        return False
    return False


def _match_size(path: str, op_and_bytes: SizeOp | None) -> bool:
    if op_and_bytes is None:
        return True
    op, ref = op_and_bytes
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return False
    if op == "eq":
        return size == ref
    if op == "lt":
        return size < ref
    if op == "gt":
        return size > ref
    return False


def _should_exclude(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def search(
    roots: Sequence[str],
    options: Options,
) -> Iterator[str]:
    if not roots:
        roots = ["."]

    for root in roots:
        yield from _walk_root(root, options)


def _walk_root(root: str, options: Options) -> Iterator[str]:
    try:
        root_stat = os.lstat(root)
    except OSError as e:
        if not options.quiet:
            print(f"findpy: cannot access '{root}': {e.strerror}", file=sys.stderr)
        return

    stack: list[tuple[str, int, Iterator[os.DirEntry] | None]] = []

    def push_dir(path: str, depth: int) -> None:
        try:
            it = os.scandir(path)
        except OSError as e:
            if not options.quiet:
                print(
                    f"findpy: cannot read directory '{path}': {e.strerror}",
                    file=sys.stderr,
                )
            return
        stack.append((path, depth, it))

    if stat.S_ISDIR(root_stat.st_mode):
        if options.maxdepth is None or options.maxdepth >= 0:
            push_dir(root, 0)
        if options.mindepth <= 0 and _matches_for_path(root, None, options):
            yield root
    else:
        dummy = _direntry_for_path(root)
        if options.mindepth <= 0 and _matches_entry(root, dummy, options):
            yield root
        return

    while stack:
        cur_path, depth, it = stack[-1]
        assert it is not None
        try:
            entry = next(it)
        except StopIteration:
            with suppress(Exception):
                it.close()  # type: ignore[attr-defined]
            stack.pop()
            continue
        except OSError as e:
            if not options.quiet:
                print(
                    f"findpy: error reading '{cur_path}': {e.strerror}",
                    file=sys.stderr,
                )
            stack.pop()
            continue

        name = entry.name
        path = os.path.join(cur_path, name)

        if options.ignore_hidden and _is_hidden(name):
            continue
        if _should_exclude(name, options.exclude):
            continue

        is_dir = False
        try:
            is_dir = entry.is_dir(follow_symlinks=options.follow_symlinks)
        except OSError:
            is_dir = False

        if depth + 1 >= options.mindepth and _matches_entry(path, entry, options):
            yield path

        if is_dir:
            if _should_exclude(name, options.exclude_dir):
                continue
            if entry.is_symlink() and not options.follow_symlinks:
                continue
            if options.maxdepth is None or depth + 1 < options.maxdepth:
                push_dir(path, depth + 1)


def _matches_for_path(path: str, entry: os.DirEntry | None, options: Options) -> bool:
    name = os.path.basename(path.rstrip(os.sep)) or path
    if options.name and not _match_name(name, options.name, False):
        return False
    if options.iname and not _match_name(name, options.iname, True):
        return False

    if options.type is not None:
        if entry is None:
            try:
                st = os.lstat(path)
            except OSError:
                return False
            mode = st.st_mode
            if options.type == "d" and not stat.S_ISDIR(mode):
                return False
            if options.type == "f" and not stat.S_ISREG(mode):
                return False
            if options.type == "l" and not stat.S_ISLNK(mode):
                return False
        else:
            if not _match_type(entry, options.type):
                return False

    if options.empty:
        if entry is None:
            try:
                with os.scandir(path) as it:
                    for _ in it:
                        return False
            except NotADirectoryError:
                try:
                    return os.lstat(path).st_size == 0
                except OSError:
                    return False
            except OSError:
                return False
        else:
            if not _match_empty(path, entry):
                return False

    return _match_size(path, options.size)


def _matches_entry(path: str, entry: os.DirEntry, options: Options) -> bool:
    return _matches_for_path(path, entry, options)


def _direntry_for_path(path: str) -> os.DirEntry:
    class _Dummy:
        def __init__(self, p: str) -> None:
            self.path = p
            self.name = os.path.basename(p)

        def is_dir(self, follow_symlinks: bool = False) -> bool:  # noqa: ARG002
            try:
                return stat.S_ISDIR(os.lstat(self.path).st_mode)
            except OSError:
                return False

        def is_file(self, follow_symlinks: bool = False) -> bool:  # noqa: ARG002
            try:
                return stat.S_ISREG(os.lstat(self.path).st_mode)
            except OSError:
                return False

        def is_symlink(self) -> bool:
            try:
                return stat.S_ISLNK(os.lstat(self.path).st_mode)
            except OSError:
                return False

    return _Dummy(path)  # type: ignore[return-value]
