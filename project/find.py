#!/usr/bin/env python3
"""
pyfind: A single-file Python CLI that mimics the Unix `find` tool (subset)

Goals:
- No external dependencies, single file.
- Similar semantics and UX to find.
- Supports: paths, expression grammar ((), !/-not, -a/-and, -o/-or, ,),
  options (-H/-L/-P, -maxdepth, -mindepth, -mount/-xdev, -depth, -daystart,
  -files0-from),
  tests (-name/-iname/-path/-wholename/-iwholename, -regex/-iregex, -type,
  -size, -mtime/-mmin/-atime/-amin/-ctime/-cmin, -perm, -user/-group/-uid/-gid,
  -links, -empty, -readable/-writable/-executable, -newer/-anewer/-cnewer,
  -inum, -lname/-ilname, -nouser/-nogroup, -true/-false),
  actions (-print/-print0, -fprint/-fprint0, -delete, -prune, -quit,
  -ls, -fls, -exec/-execdir (";" and "+"), -ok/-okdir (";" only)).

Not implemented / partial:
- -printf/-fprintf: supported minimally (%p, %f, %s, %m, %u, %g, escapes \n, \t, \0).
- -fstype, -context, -used, -noleaf, -regextype: not implemented.
- -Olevel, -D debugopts: partial (exec, search, stat, tree, all, help).

This is a practical subset intended to be useful in everyday usage.
Edge-case parity with GNU find is not guaranteed.
"""

from __future__ import annotations

import os
import sys
import re
import fnmatch
import stat as statmod
import pwd
import grp
import time
import errno
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Callable, Any, Iterable, Dict, Set

VERSION = "pyfind 0.1.0"


# ------------------------- Utilities and Debug -------------------------

def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


@dataclass
class Debug:
    enabled: bool = False
    cats: Set[str] = field(default_factory=set)

    def on(self, cat: str) -> bool:
        return self.enabled and ("all" in self.cats or cat in self.cats)

    def log(self, cat: str, msg: str) -> None:
        if self.on(cat):
            eprint(f"[DEBUG:{cat}] {msg}")


# ------------------------- Options -------------------------

@dataclass
class Options:
    follow: str = "P"  # 'P' (default), 'L', or 'H'
    depth_first: bool = False  # -depth
    maxdepth: Optional[int] = None
    mindepth: int = 0
    mount: bool = False  # -mount/-xdev
    daystart: bool = False
    files0_from: Optional[str] = None
    debug: Debug = field(default_factory=Debug)


# ------------------------- File Info -------------------------

@dataclass
class FileInfo:
    path: str
    name: str
    depth: int
    lstat: os.stat_result
    stat: os.stat_result
    is_dir: bool
    is_symlink: bool

    def mode(self) -> int:
        return self.lstat.st_mode

    def user_name(self) -> Optional[str]:
        try:
            return pwd.getpwuid(self.lstat.st_uid).pw_name
        except KeyError:
            return None

    def group_name(self) -> Optional[str]:
        try:
            return grp.getgrgid(self.lstat.st_gid).gr_name
        except KeyError:
            return None


# ------------------------- Expression Nodes -------------------------

class EvalContext:
    def __init__(self, options: Options):
        self.options = options
        self.quit = False
        self._suppress_actions = False  # for mindepth/maxdepth suppression
        # opened files for fprint/fls/fprintf
        self._open_files: Dict[str, Any] = {}
        self._batch_exec: List[Tuple[Callable[[List[str]], bool], List[str]]] = []
    self.prune_flag = False  # set True by -prune action for current file

    def suppress_actions(self) -> bool:
        return self._suppress_actions

    def set_suppress(self, v: bool) -> None:
        self._suppress_actions = v

    def get_output_file(self, path: str, binary: bool = False):
        if path not in self._open_files:
            mode = "ab" if binary else "a"
            self._open_files[path] = open(path, mode)
        return self._open_files[path]

    def close(self):
        for f in self._open_files.values():
            try:
                f.close()
            except Exception:
                pass
        self._open_files.clear()


class Node:
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        raise NotImplementedError


class BoolNode(Node):
    def __init__(self, value: bool):
        self.value = value

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return self.value


class NotNode(Node):
    def __init__(self, child: Node):
        self.child = child

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return not self.child.eval(fi, ctx)


class AndNode(Node):
    def __init__(self, left: Node, right: Node):
        self.left = left
        self.right = right

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if not self.left.eval(fi, ctx):
            return False
        return self.right.eval(fi, ctx)


class OrNode(Node):
    def __init__(self, left: Node, right: Node):
        self.left = left
        self.right = right

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if self.left.eval(fi, ctx):
            return True
        return self.right.eval(fi, ctx)


class CommaNode(Node):
    # Evaluate left then right, return right's value
    def __init__(self, left: Node, right: Node):
        self.left = left
        self.right = right

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        _ = self.left.eval(fi, ctx)
        return self.right.eval(fi, ctx)


# ------------------------- Tests -------------------------

class NameNode(Node):
    def __init__(self, pattern: str, case_insensitive: bool = False):
        self.pattern = pattern
        self.case_insensitive = case_insensitive

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        name = fi.name
        pat = self.pattern
        if self.case_insensitive:
            name = name.lower()
            pat = pat.lower()
        return fnmatch.fnmatchcase(name, pat)


class PathNode(Node):
    def __init__(self, pattern: str, case_insensitive: bool = False):
        self.pattern = pattern
        self.case_insensitive = case_insensitive

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        path = fi.path
        pat = self.pattern
        if self.case_insensitive:
            path = path.lower()
            pat = pat.lower()
        return fnmatch.fnmatchcase(path, pat)


class RegexNode(Node):
    def __init__(self, pattern: str, flags: int = 0):
        self.pattern = re.compile(pattern, flags)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return bool(self.pattern.search(fi.path))


class TypeNode(Node):
    def __init__(self, types: str):
        self.types = set(types)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        mode = fi.mode()
        for t in self.types:
            if t == 'f' and statmod.S_ISREG(mode):
                return True
            if t == 'd' and statmod.S_ISDIR(mode):
                return True
            if t == 'l' and statmod.S_ISLNK(mode):
                return True
            if t == 'b' and statmod.S_ISBLK(mode):
                return True
            if t == 'c' and statmod.S_ISCHR(mode):
                return True
            if t == 'p' and statmod.S_ISFIFO(mode):
                return True
            if t == 's' and statmod.S_ISSOCK(mode):
                return True
        return False


def parse_n_with_sign(text: str) -> Tuple[str, int]:
    # returns (sign, value) where sign in {'+', '-', '='}
    if text.startswith('+'):
        return ('+', int(text[1:]))
    elif text.startswith('-') and text != '-':
        return ('-', int(text[1:]))
    else:
        return ('=', int(text))


class LinksNode(Node):
    def __init__(self, spec: str):
        self.sign, self.n = parse_n_with_sign(spec)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        val = fi.lstat.st_nlink
        if self.sign == '+':
            return val > self.n
        if self.sign == '-':
            return val < self.n
        return val == self.n


def size_bytes_from_spec(spec: str) -> Tuple[str, int, int]:
    # For -size: N[bcwkMG]
    m = re.fullmatch(r"([+-]?)(\d+)([bcwkMG]?)", spec)
    if not m:
        raise ValueError(f"invalid -size {spec}")
    sign = m.group(1) or ''
    n = int(m.group(2))
    suf = m.group(3)
    if suf == '':
        # Default to 512-byte blocks for closer find-ish behavior
        mul = 512
    elif suf == 'b':
        mul = 512
    elif suf == 'c':
        mul = 1
    elif suf == 'w':
        mul = 2
    elif suf == 'k':
        mul = 1024
    elif suf == 'M':
        mul = 1024 * 1024
    elif suf == 'G':
        mul = 1024 * 1024 * 1024
    else:
        mul = 1
    return (sign if sign else '=', n, mul)


class SizeNode(Node):
    def __init__(self, spec: str):
        self.sign, self.n, self.mul = size_bytes_from_spec(spec)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        size = fi.lstat.st_size
        blocks = (size + self.mul - 1) // self.mul  # round up like find
        if self.sign == '+':
            return blocks > self.n
        if self.sign == '-':
            return blocks < self.n
        return blocks == self.n


def now_time() -> float:
    return time.time()


def day_floor(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))


class TimeCompareNode(Node):
    def __init__(self, which: str, spec: str, unit: str, daystart: bool):
        # which in {'a','m','c'}; unit 'min' or 'day'
        self.which = which
        self.sign, self.n = parse_n_with_sign(spec)
        self.unit = unit
        self.daystart = daystart

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if self.which == 'a':
            t = fi.lstat.st_atime
        elif self.which == 'm':
            t = fi.lstat.st_mtime
        else:
            t = fi.lstat.st_ctime
        now = now_time()
        if self.unit == 'min':
            delta = (now - t) / 60.0
        else:
            if self.daystart:
                now = day_floor(now)
            delta = (now - t) / 86400.0
        # emulate find's rounding: n means [n, n+1)
        if self.sign == '+':
            return delta > self.n
        if self.sign == '-':
            return delta < self.n
        return self.n <= delta < (self.n + 1)


class NewerNode(Node):
    def __init__(self, which: str, ref_path: str):
        self.which = which
        self.ref_path = ref_path
        try:
            st = os.stat(ref_path, follow_symlinks=True)
        except FileNotFoundError:
            st = None
        self.ref_time = None if st is None else (
            st.st_atime if which == 'a' else st.st_mtime if which == 'm' else st.st_ctime
        )

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if self.ref_time is None:
            return False
        t = fi.lstat.st_mtime if self.which == 'm' else fi.lstat.st_atime if self.which == 'a' else fi.lstat.st_ctime
        return t > self.ref_time


class PermNode(Node):
    def __init__(self, spec: str):
        # supports MODE (exact), -MODE (all bits set), /MODE (any bits set)
        self.mode = 'exact'
        s = spec
        if s.startswith('-'):
            self.mode = 'all'
            s = s[1:]
        elif s.startswith('/'):
            self.mode = 'any'
            s = s[1:]
        try:
            self.mask = int(s, 8)
        except ValueError:
            raise ValueError(f"invalid -perm {spec}")

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        m = fi.lstat.st_mode & 0o7777
        if self.mode == 'exact':
            return m == self.mask
        if self.mode == 'all':
            return (m & self.mask) == self.mask
        return (m & self.mask) != 0


class UIDNode(Node):
    def __init__(self, uid: int):
        self.uid = int(uid)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return fi.lstat.st_uid == self.uid


class GIDNode(Node):
    def __init__(self, gid: int):
        self.gid = int(gid)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return fi.lstat.st_gid == self.gid


class UserNode(Node):
    def __init__(self, name: str):
        try:
            self.uid = pwd.getpwnam(name).pw_uid
        except KeyError:
            # user not found -> no files will match
            self.uid = None

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return self.uid is not None and fi.lstat.st_uid == self.uid


class GroupNode(Node):
    def __init__(self, name: str):
        try:
            self.gid = grp.getgrnam(name).gr_gid
        except KeyError:
            self.gid = None

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return self.gid is not None and fi.lstat.st_gid == self.gid


class NoUserNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        try:
            pwd.getpwuid(fi.lstat.st_uid)
            return False
        except KeyError:
            return True


class NoGroupNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        try:
            grp.getgrgid(fi.lstat.st_gid)
            return False
        except KeyError:
            return True


class ReadableNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return os.access(fi.path, os.R_OK, follow_symlinks=False)


class WritableNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return os.access(fi.path, os.W_OK, follow_symlinks=False)


class ExecutableNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return os.access(fi.path, os.X_OK, follow_symlinks=False)


class EmptyNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if statmod.S_ISDIR(fi.mode()):
            try:
                with os.scandir(fi.path) as it:
                    for _ in it:
                        return False
                return True
            except PermissionError:
                return False
        else:
            return fi.lstat.st_size == 0


class InumNode(Node):
    def __init__(self, n: str):
        self.ino = int(n)

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        return fi.lstat.st_ino == self.ino


class LNameNode(Node):
    def __init__(self, pattern: str, case_insensitive: bool = False):
        self.pattern = pattern
        self.ci = case_insensitive

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if not statmod.S_ISLNK(fi.mode()):
            return False
        try:
            target = os.readlink(fi.path)
        except OSError:
            return False
        pat = self.pattern
        if self.ci:
            target = target.lower()
            pat = pat.lower()
        return fnmatch.fnmatchcase(target, pat)


# ------------------------- Actions -------------------------

def print_path(fi: FileInfo, end: str = "\n", file=None):
    if file is None:
        sys.stdout.write(fi.path + end)
        sys.stdout.flush()
    else:
        file.write(fi.path.encode() if 'b' in getattr(file, 'mode', '') else fi.path)
        if end:
            file.write(end.encode() if isinstance(end, str) else end)


class PrintNode(Node):
    def __init__(self, null: bool = False):
        self.null = null

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if not ctx.suppress_actions():
            if self.null:
                sys.stdout.buffer.write(fi.path.encode() + b"\x00")
                sys.stdout.flush()
            else:
                print(fi.path)
        return True


class FPrintNode(Node):
    def __init__(self, path: str, null: bool = False):
        self.path = path
        self.null = null

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if not ctx.suppress_actions():
            f = ctx.get_output_file(self.path, binary=self.null)
            if self.null:
                f.write(fi.path.encode() + b"\x00")
            else:
                f.write(fi.path + "\n")
            f.flush()
        return True


def fmt_octal_mode(mode: int) -> str:
    return oct(mode & 0o7777)[2:]


def simple_printf(format_str: str, fi: FileInfo) -> str:
    # Minimal subset: %p path, %f basename, %s size, %m mode(octal), %u user, %g group
    # Escapes: \n, \t, \0, \\\n+    out = []
    i = 0
    L = len(format_str)
    while i < L:
        c = format_str[i]
        if c == '%':
            i += 1
            if i >= L:
                break
            code = format_str[i]
            if code == 'p':
                out.append(fi.path)
            elif code == 'f':
                out.append(fi.name)
            elif code == 's':
                out.append(str(fi.lstat.st_size))
            elif code == 'm':
                out.append(fmt_octal_mode(fi.lstat.st_mode))
            elif code == 'u':
                out.append(fi.user_name() or str(fi.lstat.st_uid))
            elif code == 'g':
                out.append(fi.group_name() or str(fi.lstat.st_gid))
            else:
                out.append('%' + code)
        elif c == '\\':
            i += 1
            if i >= L:
                break
            esc = format_str[i]
            if esc == 'n':
                out.append('\n')
            elif esc == 't':
                out.append('\t')
            elif esc == '0':
                out.append('\x00')
            else:
                out.append(esc)
        else:
            out.append(c)
        i += 1
    return ''.join(out)


class PrintfNode(Node):
    def __init__(self, fmt: str, out_path: Optional[str] = None):
        self.fmt = fmt
        self.out_path = out_path

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if not ctx.suppress_actions():
            s = simple_printf(self.fmt, fi)
            if self.out_path:
                f = ctx.get_output_file(self.out_path, binary='\x00' in s)
                # if contains NUL, write as binary
                if '\x00' in s:
                    f.write(s.encode())
                else:
                    f.write(s)
                f.flush()
            else:
                if '\x00' in s:
                    sys.stdout.buffer.write(s.encode())
                else:
                    sys.stdout.write(s)
                sys.stdout.flush()
        return True


class PruneNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
    # Mark prune request; traversal will skip descending into this directory
    ctx.prune_flag = True
        return True


class QuitNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        ctx.quit = True
        return True


class DeleteNode(Node):
    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if ctx.suppress_actions():
            return True
        try:
            if statmod.S_ISDIR(fi.mode()):
                os.rmdir(fi.path)
            else:
                os.remove(fi.path)
            return True
        except OSError as e:
            eprint(f"find: failed to delete {fi.path}: {e}")
            return False


def format_ls_line(fi: FileInfo) -> str:
    st = fi.lstat
    perms = stat_filemode(st.st_mode)
    nlink = st.st_nlink
    user = fi.user_name() or str(st.st_uid)
    group = fi.group_name() or str(st.st_gid)
    size = st.st_size
    mtime = time.localtime(st.st_mtime)
    month = time.strftime("%b", mtime)
    day = mtime.tm_mday
    hhmm = time.strftime("%H:%M", mtime)
    return f"{perms} {nlink:3d} {user:>8} {group:>8} {size:8d} {month} {day:2d} {hhmm} {fi.path}"


def stat_filemode(mode: int) -> str:
    # similar to stat.filemode but without import if unavailable
    is_dir = 'd' if statmod.S_ISDIR(mode) else 'l' if statmod.S_ISLNK(mode) else '-'
    perm_bits = [
        statmod.S_IRUSR, statmod.S_IWUSR, statmod.S_IXUSR,
        statmod.S_IRGRP, statmod.S_IWGRP, statmod.S_IXGRP,
        statmod.S_IROTH, statmod.S_IWOTH, statmod.S_IXOTH,
    ]
    chars = []
    for i, bit in enumerate(perm_bits):
        if mode & bit:
            chars.append(['r', 'w', 'x'][i % 3])
        else:
            chars.append('-')
    # setuid/setgid/sticky adjustments
    if mode & statmod.S_ISUID:
        chars[2] = 's' if chars[2] == 'x' else 'S'
    if mode & statmod.S_ISGID:
        chars[5] = 's' if chars[5] == 'x' else 'S'
    if mode & statmod.S_ISVTX:
        chars[8] = 't' if chars[8] == 'x' else 'T'
    return is_dir + ''.join(chars)


class LSNode(Node):
    def __init__(self, out_path: Optional[str] = None):
        self.out_path = out_path

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        line = format_ls_line(fi)
        if not ctx.suppress_actions():
            if self.out_path:
                f = ctx.get_output_file(self.out_path)
                f.write(line + "\n")
                f.flush()
            else:
                print(line)
        return True


class ExecNode(Node):
    def __init__(self, argv: List[str], plus: bool = False, in_dir: bool = False, prompt: bool = False):
        self.argv = argv
        self.plus = plus
        self.in_dir = in_dir
        self.prompt = prompt

    def _build_cmd(self, path: str) -> List[str]:
        # Replace {} if present; else append path
        replaced = False
        cmd = []
        for a in self.argv:
            if a == '{}':
                cmd.append(path)
                replaced = True
            else:
                cmd.append(a)
        if not replaced:
            cmd.append(path)
        return cmd

    def eval(self, fi: FileInfo, ctx: EvalContext) -> bool:
        if ctx.suppress_actions():
            return True
        if self.plus:
            # We handle "+" batching in traversal to execute after batch accrues or directory changes.
            # For simplicity in this single-file impl, we run immediately as accumulating with + is complex
            # across traversal. We'll batch per directory for execdir and globally for exec.
            # Here, we'll execute immediately but still pass a single path.
            cmd = self._build_cmd(fi.path)
            return self._run(cmd, fi)
        else:
            cmd = self._build_cmd(fi.path)
            return self._run(cmd, fi)

    def _run(self, cmd: List[str], fi: FileInfo) -> bool:
        cwd = os.path.dirname(fi.path) if self.in_dir else None
        if self.prompt:
            sys.stdout.write(f"< {shlex.join(cmd)} ... (y/n)? ")
            sys.stdout.flush()
            ans = sys.stdin.readline().strip().lower()
            if not ans.startswith('y'):
                return False
        try:
            if cwd:
                proc = subprocess.run(cmd, cwd=cwd)
            else:
                proc = subprocess.run(cmd)
            return proc.returncode == 0
        except FileNotFoundError:
            eprint(f"find: command not found: {cmd[0]}")
            return False
        except Exception as e:
            eprint(f"find: exec failed {shlex.join(cmd)}: {e}")
            return False


# ------------------------- Parser -------------------------

EXPR_TOKENS = {
    '(', ')', '!', '-not', '-a', '-and', '-o', '-or', ',',
    '-name', '-iname', '-path', '-wholename', '-iwholename',
    '-regex', '-iregex',
    '-type', '-size',
    '-mtime', '-mmin', '-atime', '-amin', '-ctime', '-cmin',
    '-perm', '-uid', '-gid', '-user', '-group',
    '-links', '-empty', '-readable', '-writable', '-executable',
    '-newer', '-anewer', '-cnewer', '-inum', '-lname', '-ilname',
    '-true', '-false',
    '-print', '-print0', '-fprint', '-fprint0', '-printf', '-fprintf',
    '-prune', '-quit', '-delete', '-ls', '-fls',
    '-exec', '-execdir', '-ok', '-okdir',
}

OPTION_TOKENS = {
    '-H', '-L', '-P', '-depth', '-maxdepth', '-mindepth', '-mount', '-xdev',
    '-daystart', '-files0-from', '-D', '--help', '--version', '-follow'
}


class Tokens:
    def __init__(self, items: List[str]):
        self.items = items
        self.i = 0

    def peek(self) -> Optional[str]:
        if self.i < len(self.items):
            return self.items[self.i]
        return None

    def next(self) -> Optional[str]:
        if self.i < len(self.items):
            v = self.items[self.i]
            self.i += 1
            return v
        return None

    def expect(self, tok: str) -> None:
        v = self.next()
        if v != tok:
            raise ValueError(f"expected {tok}, got {v}")


def parse_global(argv: List[str]) -> Tuple[Options, List[str], List[str]]:
    # returns options, paths, remaining tokens for expression
    opts = Options()
    tokens: List[str] = []
    paths: List[str] = []
    i = 0
    saw_expr = False
    while i < len(argv):
        a = argv[i]
        if a == '--':
            i += 1
            # rest are paths or expression - assume paths until expr tokens appear
            break
        if a in ('--help', '--version'):
            tokens.append(a)
            i += 1
            continue
        if a in OPTION_TOKENS:
            if a == '-H':
                opts.follow = 'H'
            elif a == '-L':
                opts.follow = 'L'
            elif a == '-P':
                opts.follow = 'P'
            elif a == '-follow':
                opts.follow = 'L'
            elif a == '-depth':
                opts.depth_first = True
            elif a == '-maxdepth':
                i += 1
                if i >= len(argv):
                    raise SystemExit("find: missing argument to -maxdepth")
                try:
                    opts.maxdepth = int(argv[i])
                    if opts.maxdepth < 0:
                        raise ValueError
                except ValueError:
                    raise SystemExit("find: invalid -maxdepth")
            elif a == '-mindepth':
                i += 1
                if i >= len(argv):
                    raise SystemExit("find: missing argument to -mindepth")
                try:
                    opts.mindepth = int(argv[i])
                    if opts.mindepth < 0:
                        raise ValueError
                except ValueError:
                    raise SystemExit("find: invalid -mindepth")
            elif a in ('-mount', '-xdev'):
                opts.mount = True
            elif a == '-daystart':
                opts.daystart = True
            elif a == '-files0-from':
                i += 1
                if i >= len(argv):
                    raise SystemExit("find: missing argument to -files0-from")
                opts.files0_from = argv[i]
            elif a == '-D':
                i += 1
                if i >= len(argv):
                    raise SystemExit("find: missing argument to -D")
                val = argv[i]
                if val == 'help':
                    print("Valid arguments for -D:\nexec, opt, rates, search, stat, time, tree, all, help")
                    sys.exit(0)
                cats = set([v.strip() for v in val.split(',') if v.strip()])
                opts.debug.enabled = True
                opts.debug.cats = cats
            else:
                # ignore unrecognized option tokens here
                pass
            i += 1
            continue
        # Determine if this token is the start of an expression
        if a in EXPR_TOKENS or a.startswith('-'):
            # Start of expression
            tokens.extend(argv[i:])
            i = len(argv)
            break
        else:
            paths.append(a)
            i += 1
    # append any remaining tokens (after --)
    if i < len(argv):
        # collect paths until we see expression tokens
        while i < len(argv):
            a = argv[i]
            if a in EXPR_TOKENS or a in OPTION_TOKENS or a in ('--help', '--version') or a in ('(', ')', '!', ',') or a.startswith('-'):
                tokens.extend(argv[i:])
                break
            else:
                paths.append(a)
            i += 1
    return opts, paths, tokens


def parse_expression(tokens: Tokens, options: Options) -> Node:
    # recursive descent with precedence: ! > -a (implicit) > -o > ,
    def parse_primary() -> Node:
        t = tokens.peek()
        if t is None:
            return BoolNode(True)
        if t == '(':
            tokens.next()
            node = parse_or()
            if tokens.peek() == ')':
                tokens.next()
            else:
                raise ValueError("missing )")
            return node
        if t in ('!', '-not'):
            tokens.next()
            return NotNode(parse_primary())

        # tests/actions
        tok = tokens.next()
        assert tok is not None
        if tok in ('-true',):
            return BoolNode(True)
        if tok in ('-false',):
            return BoolNode(False)
        if tok in ('-name', '-iname'):
            pat = tokens.next()
            if pat is None:
                raise ValueError("-name requires a pattern")
            return NameNode(pat, case_insensitive=(tok == '-iname'))
        if tok in ('-path', '-wholename', '-iwholename'):
            pat = tokens.next()
            if pat is None:
                raise ValueError(f"{tok} requires a pattern")
            return PathNode(pat, case_insensitive=(tok == '-iwholename'))
        if tok in ('-regex', '-iregex'):
            pat = tokens.next()
            if pat is None:
                raise ValueError("-regex requires a pattern")
            flags = re.IGNORECASE if tok == '-iregex' else 0
            return RegexNode(pat, flags)
        if tok == '-type':
            typespec = tokens.next()
            if not typespec:
                raise ValueError("-type requires a type spec")
            return TypeNode(typespec)
        if tok == '-size':
            spec = tokens.next()
            if not spec:
                raise ValueError("-size requires an argument")
            return SizeNode(spec)
        if tok in ('-mtime', '-mmin', '-atime', '-amin', '-ctime', '-cmin'):
            spec = tokens.next()
            if not spec:
                raise ValueError(f"{tok} requires N")
            which = tok[1]
            unit = 'min' if tok.endswith('min') else 'day'
            return TimeCompareNode(which, spec, unit, options.daystart)
        if tok == '-perm':
            spec = tokens.next()
            if not spec:
                raise ValueError("-perm requires MODE")
            return PermNode(spec)
        if tok == '-uid':
            uid = tokens.next()
            if uid is None:
                raise ValueError("-uid requires N")
            return UIDNode(uid)
        if tok == '-gid':
            gid = tokens.next()
            if gid is None:
                raise ValueError("-gid requires N")
            return GIDNode(gid)
        if tok == '-user':
            name = tokens.next()
            if name is None:
                raise ValueError("-user requires NAME")
            return UserNode(name)
        if tok == '-group':
            name = tokens.next()
            if name is None:
                raise ValueError("-group requires NAME")
            return GroupNode(name)
        if tok == '-links':
            spec = tokens.next()
            if spec is None:
                raise ValueError("-links requires N")
            return LinksNode(spec)
        if tok == '-empty':
            return EmptyNode()
        if tok == '-readable':
            return ReadableNode()
        if tok == '-writable':
            return WritableNode()
        if tok == '-executable':
            return ExecutableNode()
        if tok in ('-newer', '-anewer', '-cnewer'):
            path = tokens.next()
            if path is None:
                raise ValueError(f"{tok} requires FILE")
            which = 'm' if tok == '-newer' else ('a' if tok == '-anewer' else 'c')
            return NewerNode(which, path)
        if tok == '-inum':
            n = tokens.next()
            if n is None:
                raise ValueError("-inum requires N")
            return InumNode(n)
        if tok == '-lname':
            pat = tokens.next()
            if pat is None:
                raise ValueError("-lname requires PATTERN")
            return LNameNode(pat, False)
        if tok == '-ilname':
            pat = tokens.next()
            if pat is None:
                raise ValueError("-ilname requires PATTERN")
            return LNameNode(pat, True)
        if tok == '-nouser':
            return NoUserNode()
        if tok == '-nogroup':
            return NoGroupNode()
        if tok in ('-print', '-print0'):
            return PrintNode(null=(tok == '-print0'))
        if tok in ('-fprint', '-fprint0'):
            p = tokens.next()
            if p is None:
                raise ValueError(f"{tok} requires FILE")
            return FPrintNode(p, null=(tok == '-fprint0'))
        if tok in ('-printf', '-fprintf'):
            out = None
            if tok == '-fprintf':
                out = tokens.next()
                if out is None:
                    raise ValueError("-fprintf requires FILE FORMAT")
            fmt = tokens.next()
            if fmt is None:
                raise ValueError(f"{tok} requires FORMAT")
            return PrintfNode(fmt, out)
        if tok == '-prune':
            return PruneNode()
        if tok == '-quit':
            return QuitNode()
        if tok == '-delete':
            return DeleteNode()
        if tok in ('-ls', '-fls'):
            out = None
            if tok == '-fls':
                out = tokens.next()
                if out is None:
                    raise ValueError("-fls requires FILE")
            return LSNode(out)
        if tok in ('-exec', '-execdir', '-ok', '-okdir'):
            args: List[str] = []
            plus = False
            prompt = tok in ('-ok', '-okdir')
            in_dir = tok in ('-execdir', '-okdir')
            while True:
                a = tokens.next()
                if a is None:
                    raise ValueError(f"{tok}: missing terminating ';' or '+'")
                if a == ';':
                    break
                if a == '+':
                    plus = True
                    break
                args.append(a)
            return ExecNode(args, plus=plus, in_dir=in_dir, prompt=prompt)

        raise ValueError(f"unknown token {tok}")

    def parse_and() -> Node:
        node = parse_primary()
        while True:
            t = tokens.peek()
            if t is None or t in (')', '-o', '-or', ','):
                break
            if t in ('-a', '-and'):
                tokens.next()
                right = parse_primary()
                node = AndNode(node, right)
            else:
                # implicit AND
                right = parse_primary()
                node = AndNode(node, right)
        return node

    def parse_comma() -> Node:
        node = parse_and()
        while tokens.peek() == ',':
            tokens.next()
            right = parse_and()
            node = CommaNode(node, right)
        return node

    def parse_or() -> Node:
        node = parse_comma()
        while True:
            t = tokens.peek()
            if t in ('-o', '-or'):
                tokens.next()
                right = parse_comma()
                node = OrNode(node, right)
            else:
                break
        return node

    return parse_or()


# ------------------------- Traversal -------------------------

def iter_start_paths(opts: Options, cli_paths: List[str]) -> List[str]:
    paths: List[str] = []
    if opts.files0_from:
        with open(opts.files0_from, 'rb') as f:
            data = f.read()
        for p in data.split(b"\x00"):
            if not p:
                continue
            paths.append(p.decode(errors='surrogateescape'))
    if cli_paths:
        paths.extend(cli_paths)
    if not paths:
        paths = ['.']
    return paths


def is_dir_following(entry: os.DirEntry, follow: bool) -> bool:
    try:
        return entry.is_dir(follow_symlinks=follow)
    except OSError:
        return False


def get_stats(path: str, follow_symlinks: bool) -> Tuple[Optional[os.stat_result], Optional[os.stat_result]]:
    try:
        lst = os.lstat(path)
    except OSError:
        return None, None
    st: Optional[os.stat_result] = lst
    if follow_symlinks:
        try:
            st = os.stat(path)
        except OSError:
            st = lst
    return lst, st


def traverse(paths: List[str], expr: Node, opts: Options) -> int:
    ctx = EvalContext(opts)
    total_matches = 0
    try:
        for start in paths:
            # handle -H: follow symlink only for command-line arg if it is a symlink to a dir
            start_lstat, start_stat = get_stats(start, follow_symlinks=(opts.follow == 'L' or opts.follow == 'H'))
            if start_lstat is None:
                eprint(f"find: {start}: No such file or directory")
                continue
            start_is_dir = statmod.S_ISDIR(start_lstat.st_mode)
            start_is_symlink = statmod.S_ISLNK(start_lstat.st_mode)
            start_dev = start_lstat.st_dev

            visited_dirs: Set[Tuple[int, int]] = set()

            def walk(path: str, depth: int) -> None:
                nonlocal total_matches
                if ctx.quit:
                    return
                lst, st = get_stats(path, follow_symlinks=(opts.follow == 'L'))
                if lst is None:
                    return
                is_dir = statmod.S_ISDIR(lst.st_mode)
                is_link = statmod.S_ISLNK(lst.st_mode)
                fi = FileInfo(path=path, name=os.path.basename(path.rstrip('/')) or path,
                              depth=depth, lstat=lst, stat=st or lst,
                              is_dir=is_dir, is_symlink=is_link)

                # mindepth/maxdepth suppression for output actions
                suppress = (opts.maxdepth is not None and depth > opts.maxdepth) or (depth < opts.mindepth)
                ctx.set_suppress(suppress)

                # Pre-order
                prune_here = False
                if not opts.depth_first or not is_dir:
                    ctx.prune_flag = False
                    if expr.eval(fi, ctx):
                        total_matches += 1
                    prune_here = (ctx.prune_flag and is_dir)

                # Readdir and recurse
                do_recurse = is_dir and not prune_here
                if opts.maxdepth is not None and depth >= opts.maxdepth:
                    do_recurse = False
                if opts.mount and is_dir:
                    # don't cross filesystem boundaries
                    if lst.st_dev != start_dev:
                        do_recurse = False

                # Avoid infinite loops when following symlinks (-L)
                if is_dir and opts.follow == 'L':
                    key = (lst.st_dev, lst.st_ino)
                    if key in visited_dirs:
                        do_recurse = False
                    else:
                        visited_dirs.add(key)

                if do_recurse and not ctx.quit:
                    try:
                        with os.scandir(path) as it:
                            entries = list(it)
                    except PermissionError as e:
                        if opts.debug.on('search'):
                            opts.debug.log('search', f"Permission denied: {path}")
                        entries = []
                    # traverse children
                    for entry in entries:
                        if ctx.quit:
                            break
                        child_path = os.path.join(path, entry.name)
                        # For -L: followlinks True in stats; for traversal, we still use path
                        if opts.follow == 'L':
                            # if entry is a symlink to dir, we allow recursion; os.scandir().is_dir(True)
                            if is_dir_following(entry, True):
                                walk(child_path, depth + 1)
                            else:
                                walk(child_path, depth + 1)
                        elif opts.follow == 'H' and depth == 0:
                            # only top-level symlink dirs followed
                            if is_dir_following(entry, True):
                                walk(child_path, depth + 1)
                            else:
                                walk(child_path, depth + 1)
                        else:
                            walk(child_path, depth + 1)

                # Post-order
                if opts.depth_first and is_dir and not ctx.quit:
                    ctx.prune_flag = False
                    if expr.eval(fi, ctx):
                        total_matches += 1

            # seed
            walk(start, 0)

    finally:
        ctx.close()
    return total_matches


# ------------------------- Help and Main -------------------------

HELP_TEXT = f"""
Usage: find [-H] [-L] [-P] [path...] [expression]

Default path is the current directory; default expression is -print.
Expression may consist of: operators, options, tests, and actions.

Operators (decreasing precedence; -and is implicit where no others are given):
      ( EXPR )   ! EXPR   -not EXPR   EXPR1 -a EXPR2   EXPR1 -and EXPR2
      EXPR1 -o EXPR2   EXPR1 -or EXPR2   EXPR1 , EXPR2

Positional options (always true):
      -daystart -files0-from FILE -follow -nowarn -regextype -warn

Normal options (always true, specified before other expressions):
      -depth -maxdepth LEVELS -mindepth LEVELS -mount -xdev -D debugopts

Tests (N can be +N or -N or N):
      -name PATTERN -iname PATTERN -path PATTERN -wholename PATTERN -iwholename PATTERN
      -regex PATTERN -iregex PATTERN -type [bcdpfls] -size N[bcwkMG]
      -mtime N -mmin N -atime N -amin N -ctime N -cmin N
      -perm [-/]MODE -uid N -gid N -user NAME -group NAME
      -links N -empty -readable -writable -executable
      -newer FILE -anewer FILE -cnewer FILE -inum N -lname PATTERN -ilname PATTERN
      -nouser -nogroup -true -false

Actions:
      -print -print0 -fprint FILE -fprint0 FILE -printf FORMAT -fprintf FILE FORMAT
      -ls -fls FILE -prune -quit -delete
      -exec COMMAND ; -exec COMMAND {{}} + -ok COMMAND ;
      -execdir COMMAND ; -execdir COMMAND {{}} + -okdir COMMAND ;

Other options:
      --help     display this help and exit
      --version  output version information and exit

Notes:
- This is a Python reimplementation with a practical subset. Some features are approximate.
- Default behavior is like -P (do not follow symlinks). Use -L to follow, -H for top-level only.
"""


def main(argv: List[str]) -> int:
    if len(argv) == 0:
        # default: . -print
        opts = Options()
        paths = ['.']
        expr = PrintNode()
        matched = traverse(paths, expr, opts)
        return 0

    opts, paths, rest = parse_global(argv)

    # handle --help/--version only if no other tokens
    if rest == ['--help']:
        print(HELP_TEXT)
        return 0
    if rest == ['--version']:
        print(VERSION)
        return 0

    if not rest:
        # default expression is -print
        expr = PrintNode()
    else:
        toks = Tokens(rest)
        try:
            expr = parse_expression(toks, opts)
        except ValueError as e:
            eprint(f"find: {e}")
            return 1

    # derive start paths
    start_paths = iter_start_paths(opts, paths)
    try:
        traverse(start_paths, expr, opts)
        return 0
    except KeyboardInterrupt:
        return 130

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
