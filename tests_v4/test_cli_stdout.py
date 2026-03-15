from __future__ import annotations

import io
import sys
from contextlib import ExitStack

from click.testing import CliRunner

from astrbot_sdk.cli import _open_cli_protocol_stdout, _resolve_cli_stdout_target, cli


class _DummyStdout:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_run_help_includes_stdout_option() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--stdout" in result.output


def test_worker_help_includes_stdout_option() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0, result.output
    assert "--stdout" in result.output


def test_resolve_stdout_default_is_silent_on_tty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", _DummyStdout(True), raising=False)
    assert _resolve_cli_stdout_target(None) == "silent"


def test_resolve_stdout_default_is_console_when_piped(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", _DummyStdout(False), raising=False)
    assert _resolve_cli_stdout_target(None) == "console"


def test_open_stdout_silent_json_returns_discard_sink() -> None:
    with ExitStack() as stack:
        stdout = _open_cli_protocol_stdout(
            target="silent",
            wire_codec="json",
            stack=stack,
        )
        assert isinstance(stdout, io.BufferedIOBase)
        assert stdout.write(b"hello\n") == len(b"hello\n")
        stdout.flush()


def test_open_stdout_silent_msgpack_returns_discard_sink() -> None:
    with ExitStack() as stack:
        stdout = _open_cli_protocol_stdout(
            target="silent",
            wire_codec="msgpack",
            stack=stack,
        )
        assert isinstance(stdout, io.BufferedIOBase)
        assert stdout.write(b"\x01\x02") == 2
        stdout.flush()


def test_open_stdout_file_json(tmp_path) -> None:
    out_path = tmp_path / "out.json"
    with ExitStack() as stack:
        stdout = _open_cli_protocol_stdout(
            target=str(out_path),
            wire_codec="json",
            stack=stack,
        )
        assert isinstance(stdout, io.TextIOBase)
        stdout.write("hello")
    assert out_path.read_text(encoding="utf-8") == "hello"


def test_open_stdout_file_msgpack(tmp_path) -> None:
    out_path = tmp_path / "out.msgpack"
    with ExitStack() as stack:
        stdout = _open_cli_protocol_stdout(
            target=str(out_path),
            wire_codec="msgpack",
            stack=stack,
        )
        assert isinstance(stdout, io.BufferedIOBase)
        stdout.write(b"\x01\x02")
    assert out_path.read_bytes() == b"\x01\x02"
