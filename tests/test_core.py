from __future__ import annotations

from pathlib import Path

import pytest

from findlike.core import Options, parse_size, search


def touch(p: Path, data: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_parse_size():
    assert parse_size("10") == ("eq", 10)
    assert parse_size("+1k") == ("gt", 1024)
    assert parse_size("-2M") == ("lt", 2 * 1024 * 1024)
    with pytest.raises(ValueError):
        parse_size("")
    with pytest.raises(ValueError):
        parse_size("abc")


def test_search_basic(tmp_path: Path):
    a = tmp_path / "a.txt"
    bdir = tmp_path / "b"
    c = bdir / "c.log"
    touch(a, b"hello")
    bdir.mkdir()
    touch(c)

    opts = Options()
    results = list(search([str(tmp_path)], opts))
    assert set(results) >= {str(tmp_path), str(a), str(bdir), str(c)}


def test_filters(tmp_path: Path):
    (tmp_path / "dir").mkdir()
    touch(tmp_path / "dir" / "file.txt")
    touch(tmp_path / "dir" / "file.TXT")
    touch(tmp_path / "other.md")

    opts = Options(type="f")
    files = [p for p in search([str(tmp_path)], opts)]
    assert any(Path(p).is_file() for p in files)

    opts = Options(name="*.txt")
    names = set(search([str(tmp_path)], opts))
    assert any(p.endswith("file.txt") for p in names)
    assert not any(p.endswith("file.TXT") for p in names)

    opts = Options(iname="*.txt")
    names = set(search([str(tmp_path)], opts))
    assert any(p.endswith("file.txt") for p in names)
    assert any(p.endswith("file.TXT") for p in names)


def test_depth_and_empty(tmp_path: Path):
    d = tmp_path / "d"
    d.mkdir()
    touch(d / "f")
    empty_d = tmp_path / "empty"
    empty_d.mkdir()

    opts = Options(maxdepth=1)
    res = set(search([str(tmp_path)], opts))
    assert str(tmp_path) in res
    assert str(d) in res
    assert str(empty_d) in res

    opts = Options(mindepth=1)
    res = set(search([str(tmp_path)], opts))
    assert str(tmp_path) not in res
    assert str(d) in res

    opts = Options(empty=True, type="d")
    res = set(search([str(tmp_path)], opts))
    assert str(empty_d) in res
    assert str(d) not in res


def test_size_and_symlink(tmp_path: Path):
    small = tmp_path / "small.bin"
    big = tmp_path / "big.bin"
    touch(small, b"x" * 10)
    touch(big, b"y" * 5000)

    opts = Options(size=("lt", 100))
    res = set(search([str(tmp_path)], opts))
    assert str(small) in res
    assert str(big) not in res

    (tmp_path / "dir").mkdir()
    link = tmp_path / "dirlink"
    try:
        link.symlink_to(tmp_path / "dir")
    except OSError:
        pytest.skip("symlink not supported")

    opts = Options(type="l")
    res = set(search([str(tmp_path)], opts))
    assert str(link) in res
