# AstrBot SDK Vendor Snapshot

This directory is the minimized subtree payload consumed by the AstrBot main
repository.

- `astrbot_sdk/` is synchronized from `src/astrbot_sdk/`
- `pyproject.toml` is rewritten for the flattened vendor layout so consumers
  can inspect package metadata from `vendor-branch`
- `VENDORED.md` describes the vendoring contract
- tests, docs, CI files, and other source-repo-only content stay outside this directory
