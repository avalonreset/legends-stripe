"""Stripe .env auditing and active Price-to-env/TypeScript mapping utilities."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .api import StripeClient, StripeError, classify_stripe_key
from .telemetry import format_cents

SECRET_KEY_VARS = {
    "STRIPE_SECRET_KEY",
    "STRIPE_RESTRICTED_KEY",
    "STRIPE_API_KEY",
}

PUBLISHABLE_KEY_VARS = {
    "STRIPE_PUBLISHABLE_KEY",
    "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
    "PUBLIC_STRIPE_PUBLISHABLE_KEY",
    "PUBLIC_STRIPE_KEY",
    "VITE_STRIPE_PUBLISHABLE_KEY",
}

WEBHOOK_SECRET_VARS = {
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_ENDPOINT_SECRET",
}


def _slugify_env_token(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text.strip().upper()).strip("_")
    return cleaned or "UNNAMED"


def audit_env_file(
    target_env_path: str,
    client: StripeClient | None = None,
    *,
    verify_live: bool = False,
) -> dict[str, Any]:
    """Audit a project .env file for Stripe key parity, empty vars, and live Price/Product validity."""
    path = Path(target_env_path).expanduser().resolve()
    if not path.is_file():
        raise StripeError(f"Target .env file not found: {path}")

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    empty_variables: list[dict[str, Any]] = []
    key_findings: list[dict[str, Any]] = []
    referenced_ids: list[dict[str, Any]] = []

    secret_modes: set[str] = set()
    publishable_modes: set[str] = set()

    for idx, raw_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        var_name = k.strip()
        val = v.strip().strip('"').strip("'")
        upper_name = var_name.upper()

        if "STRIPE" in upper_name and not val:
            empty_variables.append({"variable": var_name, "line": idx})
            warnings.append(
                {
                    "kind": "empty_stripe_variable",
                    "variable": var_name,
                    "line": idx,
                    "message": f"{var_name} on line {idx} is empty.",
                }
            )
            continue

        if upper_name in SECRET_KEY_VARS or ("STRIPE" in upper_name and val.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_"))):
            info = classify_stripe_key(val)
            key_findings.append(
                {
                    "variable": var_name,
                    "line": idx,
                    "role": "secret_key",
                    "kind": info["kind"],
                    "mode": info["mode"],
                    "valid_role": bool(info["valid_secret"]),
                }
            )
            if not info["valid_secret"]:
                errors.append(
                    {
                        "kind": "invalid_secret_key_format",
                        "variable": var_name,
                        "line": idx,
                        "message": f"{var_name} on line {idx} has kind '{info['kind']}' instead of a secret/restricted key (sk_ or rk_).",
                    }
                )
            elif info["mode"] in {"test", "live"}:
                secret_modes.add(info["mode"])
            continue

        if upper_name in PUBLISHABLE_KEY_VARS or val.startswith(("pk_test_", "pk_live_")):
            info = classify_stripe_key(val)
            is_pub = info["kind"] == "publishable"
            key_findings.append(
                {
                    "variable": var_name,
                    "line": idx,
                    "role": "publishable_key",
                    "kind": info["kind"],
                    "mode": info["mode"],
                    "valid_role": is_pub,
                }
            )
            if not is_pub:
                errors.append(
                    {
                        "kind": "invalid_publishable_key_format",
                        "variable": var_name,
                        "line": idx,
                        "message": f"{var_name} on line {idx} does not start with pk_test_ or pk_live_.",
                    }
                )
            elif info["mode"] in {"test", "live"}:
                publishable_modes.add(info["mode"])
            continue

        if upper_name in WEBHOOK_SECRET_VARS or val.startswith("whsec_"):
            info = classify_stripe_key(val)
            is_wh = info["kind"] == "webhook_secret"
            key_findings.append(
                {
                    "variable": var_name,
                    "line": idx,
                    "role": "webhook_secret",
                    "kind": info["kind"],
                    "mode": "agnostic",
                    "valid_role": is_wh,
                }
            )
            if not is_wh:
                errors.append(
                    {
                        "kind": "invalid_webhook_secret_format",
                        "variable": var_name,
                        "line": idx,
                        "message": f"{var_name} on line {idx} does not start with whsec_.",
                    }
                )
            continue

        if val.startswith("price_"):
            referenced_ids.append(
                {
                    "variable": var_name,
                    "line": idx,
                    "resource_type": "price",
                    "id": val,
                }
            )
        elif val.startswith("prod_"):
            referenced_ids.append(
                {
                    "variable": var_name,
                    "line": idx,
                    "resource_type": "product",
                    "id": val,
                }
            )
        elif ("STRIPE_PRICE" in upper_name or "STRIPE_PRODUCT" in upper_name) and val:
            errors.append(
                {
                    "kind": "malformed_resource_id",
                    "variable": var_name,
                    "line": idx,
                    "value": val,
                    "message": f"{var_name} on line {idx} does not start with price_ or prod_ ('{val}').",
                }
            )

    if secret_modes and publishable_modes and secret_modes != publishable_modes:
        errors.append(
            {
                "kind": "key_mode_mismatch",
                "secret_modes": sorted(secret_modes),
                "publishable_modes": sorted(publishable_modes),
                "message": (
                    f"Secret key mode ({sorted(secret_modes)}) does not match publishable key mode "
                    f"({sorted(publishable_modes)})."
                ),
            }
        )

    if verify_live and client is not None:
        for item in referenced_ids:
            rid = item["id"]
            rtype = item["resource_type"]
            try:
                if rtype == "price":
                    pr = client.request("GET", f"/v1/prices/{rid}", params={"expand": ["product"]})
                    prod = pr.get("product") if isinstance(pr.get("product"), dict) else {}
                    rec = pr.get("recurring") or {}
                    amt = pr.get("unit_amount")
                    curr = str(pr.get("currency") or "usd").lower()
                    interval = str(rec.get("interval")) if rec else "one_time"
                    price_active = bool(pr.get("active", True))
                    prod_active = bool(prod.get("active", True)) if prod else True
                    item.update(
                        {
                            "verified": True,
                            "exists": True,
                            "price_active": price_active,
                            "product_id": prod.get("id") if prod else pr.get("product"),
                            "product_name": prod.get("name") if prod else None,
                            "product_active": prod_active,
                            "unit_amount_cents": amt,
                            "formatted_amount": format_cents(int(amt), curr) if amt is not None else None,
                            "currency": curr,
                            "interval": interval,
                            "lookup_key": pr.get("lookup_key"),
                            "livemode": bool(pr.get("livemode")),
                        }
                    )
                    if not price_active:
                        warnings.append(
                            {
                                "kind": "archived_price_in_env",
                                "variable": item["variable"],
                                "price_id": rid,
                                "message": f"{item['variable']} points to archived Price {rid}.",
                            }
                        )
                    if not prod_active:
                        warnings.append(
                            {
                                "kind": "archived_product_in_env",
                                "variable": item["variable"],
                                "price_id": rid,
                                "product_id": item["product_id"],
                                "message": f"{item['variable']} points to Price {rid} on archived Product {item['product_id']}.",
                            }
                        )
                else:
                    prod = client.request("GET", f"/v1/products/{rid}")
                    prod_active = bool(prod.get("active", True))
                    item.update(
                        {
                            "verified": True,
                            "exists": True,
                            "product_name": prod.get("name"),
                            "product_active": prod_active,
                            "livemode": bool(prod.get("livemode")),
                        }
                    )
                    if not prod_active:
                        warnings.append(
                            {
                                "kind": "archived_product_in_env",
                                "variable": item["variable"],
                                "product_id": rid,
                                "message": f"{item['variable']} points to archived Product {rid}.",
                            }
                        )
            except StripeError as exc:
                item.update(
                    {
                        "verified": True,
                        "exists": False,
                        "error": str(exc),
                        "stripe_code": exc.stripe_code,
                    }
                )
                errors.append(
                    {
                        "kind": "missing_or_wrong_mode_resource",
                        "variable": item["variable"],
                        "id": rid,
                        "message": f"{item['variable']} ({rid}) could not be found in {client.mode} mode: {exc}",
                    }
                )

    return {
        "ok": len(errors) == 0,
        "target_env": str(path),
        "verified_against_mode": client.mode if (verify_live and client) else None,
        "summary": {
            "keys_detected": len(key_findings),
            "secret_modes": sorted(secret_modes),
            "publishable_modes": sorted(publishable_modes),
            "referenced_ids_count": len(referenced_ids),
            "empty_stripe_vars_count": len(empty_variables),
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "keys": key_findings,
        "referenced_ids": referenced_ids,
        "empty_variables": empty_variables,
        "errors": errors,
        "warnings": warnings,
    }


def build_price_map(
    client: StripeClient,
    *,
    name_prefix: str | None = None,
    env_prefix: str = "STRIPE_PRICE_",
) -> dict[str, Any]:
    """Build a structured map of active Products and Prices with ready-to-paste .env and TypeScript exports."""
    products = client.list_all("/v1/products", params={"active": True}, max_items=500)
    prices = client.list_all("/v1/prices", params={"active": True}, max_items=500)

    prod_by_id: dict[str, dict[str, Any]] = {}
    for p in products:
        pid = str(p.get("id") or "")
        name = str(p.get("name") or "")
        if name_prefix and not name.startswith(name_prefix):
            continue
        prod_by_id[pid] = p

    entries: list[dict[str, Any]] = []
    seen_env_keys: dict[str, int] = {}

    for pr in prices:
        pid = str(pr.get("product") or "")
        if pid not in prod_by_id:
            continue
        prod = prod_by_id[pid]
        prod_name = str(prod.get("name") or "")
        rec = pr.get("recurring") or {}
        interval = str(rec.get("interval")) if rec else "one_time"
        amt = int(pr.get("unit_amount") or 0)
        curr = str(pr.get("currency") or "usd").lower()
        lookup_key = pr.get("lookup_key")

        base_token = _slugify_env_token(lookup_key if lookup_key else f"{prod_name}_{interval}")
        env_key = f"{env_prefix}{base_token}"
        if env_key in seen_env_keys:
            seen_env_keys[env_key] += 1
            env_key = f"{env_key}_{seen_env_keys[env_key]}"
        else:
            seen_env_keys[env_key] = 1

        entries.append(
            {
                "env_key": env_key,
                "ts_key": base_token.lower(),
                "price_id": pr.get("id"),
                "product_id": pid,
                "product_name": prod_name,
                "unit_amount_cents": amt,
                "formatted_amount": format_cents(amt, curr),
                "currency": curr,
                "interval": interval,
                "lookup_key": lookup_key,
            }
        )

    entries.sort(key=lambda x: (x["product_name"], x["unit_amount_cents"], x["interval"]))

    dotenv_lines = [f"# Generated by legends-stripe price-map (mode: {client.mode})"]
    for e in entries:
        dotenv_lines.append(f"# {e['product_name']} ({e['formatted_amount']} / {e['interval']})")
        dotenv_lines.append(f"{e['env_key']}={e['price_id']}")

    ts_lines = [
        f"// Generated by legends-stripe price-map (mode: {client.mode})",
        "export const STRIPE_PRICES = {",
    ]
    for e in entries:
        ts_lines.append(
            f"  {json_escape(e['ts_key'])}: {json_escape(str(e['price_id']))}, // {e['product_name']} ({e['formatted_amount']} / {e['interval']})"
        )
    ts_lines.append("} as const;\n")

    return {
        "ok": True,
        "mode": client.mode,
        "count": len(entries),
        "entries": entries,
        "dotenv": "\n".join(dotenv_lines) + "\n",
        "typescript": "\n".join(ts_lines),
    }


def json_escape(val: str) -> str:
    import json

    return json.dumps(val)
