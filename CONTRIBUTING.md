# Contributing

Start with the bundled MX-1 casebook and Python 3.11 or later on macOS or Linux. In a fresh checkout, run:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

For a bug report, include your package version, Python version, command, and the smallest fictional case that demonstrates the behavior. Share synthetic inputs and redact machine-specific paths from logs. Use the [security policy](SECURITY.md) for confidential reports.

Changes to a decision rule should demonstrate the affected question, the decisive passage and the expected answer before and after the change. Preserve exact source bytes and explain changes to passage offsets or expected digests. Cover verification failures as well as successful replay. Changes to the distribution must update the package allowlist in `pyproject.toml` and pass the archive-content test.

## Check an installed wheel

From the checkout with the development environment active, build a source archive and a wheel. Install the wheel into a separate environment to exercise its packaged data, console scripts and local MCP transport.

```sh
python -m hatchling build
python -I tests/test_distribution.py dist
python3 -m venv .venv-wheel
wheel=$(echo dist/*.whl)
.venv-wheel/bin/python -m pip install "${wheel}[mcp]"
.venv-wheel/bin/python -I scripts/check_installed.py
```

Use an empty `dist/` for this example so the glob selects a single wheel. The check generates a temporary signing key, verifies a signed review and checks how verification handles an edited report. CI also exercises the dependency-free install and compares a directly built wheel with one rebuilt from the source archive.

Project code, schemas and documentation use MIT. Contributions to the fictional MX-1 example use CC BY 4.0 and must have documented origins. [License and attribution](README.md#license-and-attribution)
