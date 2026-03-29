from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_sdk.runtime.workers_manifest import load_remote_workers_manifest


def test_load_remote_workers_manifest_parses_minimal_valid_config(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "workers.yaml"
    manifest.write_text(
        """
workers:
  - id: remote-alpha
    url: wss://worker.example/ws
    tls:
      ca_file: certs/ca.pem
      cert_file: certs/client.pem
      key_file: certs/client.key
      server_hostname: worker.internal
""".strip()
        + "\n",
        encoding="utf-8",
    )

    workers = load_remote_workers_manifest(manifest)

    assert len(workers) == 1
    assert workers[0].id == "remote-alpha"
    assert workers[0].url == "wss://worker.example/ws"
    assert workers[0].tls.ca_file == (tmp_path / "certs" / "ca.pem").resolve()
    assert workers[0].tls.cert_file == (tmp_path / "certs" / "client.pem").resolve()
    assert workers[0].tls.key_file == (tmp_path / "certs" / "client.key").resolve()
    assert workers[0].tls.server_hostname == "worker.internal"


def test_load_remote_workers_manifest_rejects_plain_ws_url(tmp_path: Path) -> None:
    manifest = tmp_path / "workers.yaml"
    manifest.write_text(
        """
workers:
  - id: remote-alpha
    url: ws://worker.example/ws
    tls:
      ca_file: ca.pem
      cert_file: cert.pem
      key_file: key.pem
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wss://"):
        load_remote_workers_manifest(manifest)


def test_load_remote_workers_manifest_rejects_static_plugin_declarations(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "workers.yaml"
    manifest.write_text(
        """
workers:
  - id: remote-alpha
    url: wss://worker.example/ws
    plugins:
      - alpha
    tls:
      ca_file: ca.pem
      cert_file: cert.pem
      key_file: key.pem
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not declare plugins"):
        load_remote_workers_manifest(manifest)
