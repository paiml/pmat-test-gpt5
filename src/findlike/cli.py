from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence

from .core import Options, parse_size, search


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="findpy",
        description="A fast, find-like filesystem traversal CLI in pure Python.",
    )
    p.add_argument("paths", nargs="*", default=["."], help="Root paths to search")

    name_group = p.add_mutually_exclusive_group()
    name_group.add_argument("-name", dest="name", help="Glob pattern to match names")
    name_group.add_argument(
        "-iname", dest="iname", help="Case-insensitive glob pattern to match names"
    )

    p.add_argument(
        "-type",
        dest="type",
        choices=["f", "d", "l"],
        help="Filter by entry type: f=file, d=dir, l=symlink",
    )
    p.add_argument("-maxdepth", type=int, help="Descend at most N levels (0 means roots only)")
    p.add_argument("-mindepth", type=int, default=0, help="Don't print entries shallower than N")
    p.add_argument("-empty", action="store_true", help="Match empty files and directories")
    p.add_argument(
        "-size",
        dest="size",
        help="Filter by size, e.g. 10k, +1M, -512. Units: b,k,m,g,t",
    )
    p.add_argument("--follow", action="store_true", help="Follow symlinks to directories")
    p.add_argument("--ignore-hidden", action="store_true", help="Skip hidden names (.*)")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob patterns to exclude matching names (can repeat)",
    )
    p.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="Glob patterns to exclude directories from descent (can repeat)",
    )
    p.add_argument(
        "-print0",
        action="store_true",
        help="Separate output with NUL instead of newline",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress error messages")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    p = build_parser()
    ns = p.parse_args(argv)

    try:
        size = parse_size(ns.size) if ns.size else None
    except ValueError as e:
        print(f"findpy: invalid -size: {e}", file=sys.stderr)
        return 2

    opts = Options(
        name=ns.name,
        iname=ns.iname,
        type=ns.type,
        maxdepth=ns.maxdepth,
        mindepth=ns.mindepth,
        follow_symlinks=ns.follow,
        empty=ns.empty,
        size=size,
        ignore_hidden=ns.ignore_hidden,
        exclude=tuple(ns.exclude or ()),
        exclude_dir=tuple(ns.exclude_dir or ()),
        quiet=ns.quiet,
    )

    try:
        if ns.print0:
            for path in search(ns.paths, opts):
                sys.stdout.write(path)
                sys.stdout.write("\0")
        else:
            first = True
            for path in search(ns.paths, opts):
                if first:
                    sys.stdout.write(path)
                    first = False
                else:
                    sys.stdout.write("\n" + path)
            sys.stdout.write("\n")
        sys.stdout.flush()
    except BrokenPipeError:
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
