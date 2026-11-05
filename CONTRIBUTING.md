# Contributing

Contributions to `legends-stripe` must preserve the dependency-free Python 3.11+ standard-library runtime, preview-first mutation safety, and source-cited knowledge pack integrity.

## Quality Checks

Before submitting changes, run:

```bash
python scripts/check_release.py --root .
python -m unittest discover -s tests -v
```

Ensure all Markdown files use standard ASCII punctuation (no em-dashes or en-dashes), contain no private host paths or account identifiers, and have unbroken relative links.
