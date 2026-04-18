# Python 3.10 Compatibility Design

## Goal

Promote AstrBot SDK from documented `>=3.12` support to official `>=3.10`
support without weakening runtime guarantees or hiding compatibility gaps.

## Scope

- Lower package metadata and documentation minimum version to `3.10`.
- Expand CI to validate a Python version matrix starting at `3.10`.
- Run the SDK in a real Python `3.10` environment and fix concrete
  compatibility issues in source or tests.
- Keep public APIs and runtime architecture unchanged.

## Approach

1. Audit source and dependencies for Python `3.10` blockers.
2. Update packaging metadata, classifiers, docs, templates, and workflow files.
3. Create a real Python `3.10` environment, install the project, and run direct
   validation commands.
4. Fix any runtime or test failures exposed by Python `3.10`.

## Risks

- Tooling or transitive dependencies may behave differently on `3.10`.
- Tests may encode `3.12` assumptions in fixtures or documentation checks.
- CI runtime expansion may reveal platform-specific issues later, but version
  matrix coverage reduces that risk.

## Validation

- `ruff format .`
- `ruff check . --fix`
- `python -m pytest tests -q`
- Repeat relevant validation under real Python `3.10`
