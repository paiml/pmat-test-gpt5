from __future__ import annotations

from pathlib import Path

from findlike.cli import main


def touch(p: Path, data: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_cli_prints_entries(tmp_path: Path, capsys):
    (tmp_path / "a").mkdir()
    touch(tmp_path / "a" / "b.txt")

    rc = main([str(tmp_path), "-type", "f"])  # only files
    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.strip().split("\n") if line]
    assert any(line.endswith("b.txt") for line in lines)


def test_cli_print0(tmp_path: Path, capsys):
    touch(tmp_path / "x")
    rc = main([str(tmp_path), "-type", "f", "-print0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\0" in out
