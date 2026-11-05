## Summary

Describe the change and why it is needed.

## Verification Checklist

- [ ] `python scripts/check_release.py --root .` passes (`0 issues`)
- [ ] `python -m unittest discover -s tests -v` passes
- [ ] Zero runtime dependencies added (`dependencies = []` in `pyproject.toml`)
- [ ] No secrets (`sk_live_`, `sk_test_`, `rk_`, `whsec_`) or private paths included
