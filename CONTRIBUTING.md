# Contributing

Contributions to `legends-stripe` must preserve the dependency-free Python 3.11+ standard-library runtime, preview-first SHA-256 mutation safety, Windows Defender / EDR hygiene, and source-cited knowledge pack integrity.

## Development Setup

```bash
git clone https://github.com/avalonreset/legends-stripe.git
cd legends-stripe
python -m pip install -e .
```

## Quality and Release Checks

Before opening a pull request, run the offline release gate and full unit test suite:

```bash
python scripts/check_release.py --root .
python -m unittest discover -s tests -v
python scripts/smoke_installed_resources.py
```

## House Standards

- Keep runtime dependencies empty (`dependencies = []` in `pyproject.toml`); use only the Python 3.11+ standard library.
- Never log, print, or store raw `sk_live_`, `sk_test_`, `rk_`, or `whsec_` secrets.
- Ensure all Markdown files use standard ASCII punctuation (no em-dashes or en-dashes), contain no private host paths or account identifiers, and have unbroken relative links.
- Follow the project `CODE_OF_CONDUCT.md`.
