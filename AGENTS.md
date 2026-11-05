# Agent Instructions for legends-stripe

Start with [docs/AGENT-RECIPE.md](docs/AGENT-RECIPE.md) and [wiki/index.md](wiki/index.md).

## Core Operating Rules

1. **Single Router Skill**: Discovery belongs to `skills/cto-legends/SKILL.md`. Do not add secondary skills under `skills/`.
2. **Zero Secret Exposure**: Never print or log `sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`, or `whsec_` values.
3. **Preview-First Mutations**: All mutating commands (`catalog-sync`, `product-upsert`, `webhook-upsert`) require a preview pass followed by `--apply --confirm-plan <sha256>`. Live Mode mutations additionally require `--live` and explicit operator authorization.
4. **Windows Defender, EDR, and Anti-Ransomware Guardrails**: Never spawn hidden consoles, execution-policy bypass shell wrappers, or background localhost daemons. Never read local browser profile databases, DPAPI stores, or third-party credential stores outside the explicit `--env-file`. All `--output` writes are single-file, symlink-refusing, and fail-closed unless `--overwrite` is passed.
5. **Release Hygiene**: Run `python scripts/check_release.py` and `python -m unittest discover -s tests -v` before packaging.
