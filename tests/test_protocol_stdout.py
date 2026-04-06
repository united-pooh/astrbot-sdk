from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

from click.testing import CliRunner

from astrbot_sdk import cli


class _FakeStream(io.StringIO):
    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_resolve_protocol_stdout_defaults_to_silent_on_tty(monkeypatch) -> None:
    fake_stdout = _FakeStream(is_tty=True)
    monkeypatch.setattr("sys.stdout", fake_stdout)

    transport_stdout, opened_stdout = cli._resolve_protocol_stdout(None)

    assert opened_stdout is not None
    assert transport_stdout is opened_stdout
    assert getattr(transport_stdout, "name", None) == os.devnull
    opened_stdout.close()


def test_resolve_protocol_stdout_defaults_to_console_when_stdout_is_piped(
    monkeypatch,
) -> None:
    fake_stdout = _FakeStream(is_tty=False)
    monkeypatch.setattr("sys.stdout", fake_stdout)

    transport_stdout, opened_stdout = cli._resolve_protocol_stdout(None)

    assert transport_stdout is fake_stdout
    assert opened_stdout is None


def test_resolve_protocol_stdout_supports_file_path(
    monkeypatch, tmp_path: Path
) -> None:
    fake_stdout = _FakeStream(is_tty=True)
    output_path = tmp_path / "protocol.log"
    monkeypatch.setattr("sys.stdout", fake_stdout)

    transport_stdout, opened_stdout = cli._resolve_protocol_stdout(str(output_path))

    assert opened_stdout is not None
    assert transport_stdout is opened_stdout
    assert getattr(transport_stdout, "name", None) == str(output_path)
    opened_stdout.close()


def test_snapshot_watch_files_skips_build_artifacts_and_caches(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "module.pyc").write_bytes(b"pyc")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "artifact.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("ignored\n", encoding="utf-8")

    snapshot = cli._snapshot_watch_files(tmp_path)

    assert sorted(snapshot) == ["main.py", "nested/keep.txt"]


def test_local_dev_state_dispatch_kwargs_normalize_fields() -> None:
    kwargs = cli._LocalDevState(
        session_id=123,  # type: ignore[arg-type]
        user_id=456,  # type: ignore[arg-type]
        platform="qq",
        group_id=None,
        event_type="message",
    ).dispatch_kwargs()

    assert kwargs == {
        "session_id": "123",
        "user_id": "456",
        "platform": "qq",
        "group_id": None,
        "event_type": "message",
    }


def test_run_command_resolves_protocol_stdout_to_stream(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_supervisor(
        *,
        plugins_dir: Path,
        stdout=None,
        workers_manifest: Path | None = None,
        wire_codec: str | None = None,
        **_,
    ) -> None:
        captured["plugins_dir"] = plugins_dir
        captured["stdout_name"] = getattr(stdout, "name", None)
        captured["workers_manifest"] = workers_manifest
        captured["wire_codec"] = wire_codec

    def fake_run_async_entrypoint(entrypoint, **_) -> None:
        asyncio.run(entrypoint)

    monkeypatch.setattr(cli, "run_supervisor", fake_run_supervisor)
    monkeypatch.setattr(cli, "_run_async_entrypoint", fake_run_async_entrypoint)

    runner = CliRunner()
    result = runner.invoke(
        cli.cli,
        [
            "run",
            "--plugins-dir",
            str(tmp_path),
            "--workers-manifest",
            str(tmp_path / "workers.yaml"),
            "--protocol-stdout",
            "silent",
            "--wire-codec",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "plugins_dir": tmp_path,
        "stdout_name": os.devnull,
        "workers_manifest": tmp_path / "workers.yaml",
        "wire_codec": "json",
    }


def test_worker_command_resolves_protocol_stdout_to_stream(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    output_path = tmp_path / "worker-protocol.log"

    async def fake_run_plugin_worker(
        *,
        plugin_dir: Path | None = None,
        group_metadata: Path | None = None,
        stdout=None,
        wire_codec: str | None = None,
        **_,
    ) -> None:
        captured["plugin_dir"] = plugin_dir
        captured["group_metadata"] = group_metadata
        captured["stdout_name"] = getattr(stdout, "name", None)
        captured["wire_codec"] = wire_codec

    def fake_run_async_entrypoint(entrypoint, **_) -> None:
        asyncio.run(entrypoint)

    monkeypatch.setattr(cli, "run_plugin_worker", fake_run_plugin_worker)
    monkeypatch.setattr(cli, "_run_async_entrypoint", fake_run_async_entrypoint)

    runner = CliRunner()
    result = runner.invoke(
        cli.cli,
        [
            "worker",
            "--plugin-dir",
            str(plugin_dir),
            "--protocol-stdout",
            str(output_path),
            "--wire-codec",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "plugin_dir": plugin_dir,
        "group_metadata": None,
        "stdout_name": str(output_path),
        "wire_codec": "json",
    }


def test_serve_worker_command_passes_websocket_parameters(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    ca_file = tmp_path / "ca.pem"
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    for path in (ca_file, cert_file, key_file):
        path.write_text("pem", encoding="utf-8")

    async def fake_run_websocket_server(
        *,
        worker_id: str | None,
        plugin_dirs: list[Path] | None,
        host: str,
        port: int,
        path: str,
        tls_ca_file: Path,
        tls_cert_file: Path,
        tls_key_file: Path,
        wire_codec: str | None = None,
        **_,
    ) -> None:
        captured["worker_id"] = worker_id
        captured["plugin_dirs"] = plugin_dirs
        captured["host"] = host
        captured["port"] = port
        captured["path"] = path
        captured["tls_ca_file"] = tls_ca_file
        captured["tls_cert_file"] = tls_cert_file
        captured["tls_key_file"] = tls_key_file
        captured["wire_codec"] = wire_codec

    def fake_run_async_entrypoint(entrypoint, **_) -> None:
        asyncio.run(entrypoint)

    monkeypatch.setattr(cli, "run_websocket_server", fake_run_websocket_server)
    monkeypatch.setattr(cli, "_run_async_entrypoint", fake_run_async_entrypoint)

    runner = CliRunner()
    result = runner.invoke(
        cli.cli,
        [
            "serve-worker",
            "--worker-id",
            "remote-alpha",
            "--plugin-dir",
            str(plugin_dir),
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--path",
            "/ws",
            "--tls-ca-file",
            str(ca_file),
            "--tls-cert-file",
            str(cert_file),
            "--tls-key-file",
            str(key_file),
            "--wire-codec",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "worker_id": "remote-alpha",
        "plugin_dirs": [plugin_dir],
        "host": "0.0.0.0",
        "port": 9000,
        "path": "/ws",
        "tls_ca_file": ca_file,
        "tls_cert_file": cert_file,
        "tls_key_file": key_file,
        "wire_codec": "json",
    }
