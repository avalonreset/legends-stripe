"""Catalog lint, field-limit validation, declarative test/live catalog diff & sync, price ladder planner, and checkout canary."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .api import (
    StripeClient,
    StripeError,
    plan_digest,
    validate_price_fields,
    validate_product_fields,
    validate_statement_descriptor,
)


def slugify(value: str) -> str:
    """Convert a product name into a deterministic lowercase underscore slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return cleaned or "product"


def audit_catalog(client: StripeClient, *, max_items: int = 500) -> dict[str, Any]:
    """Audit Products, Prices, and Payment Links for field limits, orphans, duplicates, and lookup_key coverage."""
    products = client.list_all("/v1/products", {"limit": 100}, max_items=max_items)
    prices = client.list_all("/v1/prices", {"limit": 100}, max_items=max_items)
    payment_links = client.list_all("/v1/payment_links", {"limit": 100}, max_items=min(max_items, 200))

    product_by_id = {p.get("id"): p for p in products if p.get("id")}
    active_products = [p for p in products if p.get("active")]
    active_prices = [pr for pr in prices if pr.get("active")]

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # 1. Product checks & duplicate name tracking
    by_name: dict[str, list[str]] = {}
    active_prices_by_product: dict[str, list[dict[str, Any]]] = {p["id"]: [] for p in active_products if p.get("id")}

    for prod in active_products:
        pid = prod.get("id", "unknown")
        name = (prod.get("name") or "").strip()
        desc = prod.get("description")
        stmt = prod.get("statement_descriptor")
        by_name.setdefault(name.lower(), []).append(pid)

        if desc and len(desc) > 500:
            errors.append({
                "kind": "description_exceeds_500_chars",
                "product_id": pid,
                "name": name,
                "length": len(desc),
                "message": f"Product '{name}' ({pid}) description is {len(desc)} chars (exceeds 500-char invoice line-item limit).",
            })
        elif not desc:
            warnings.append({
                "kind": "missing_product_description",
                "product_id": pid,
                "name": name,
                "message": f"Active product '{name}' ({pid}) has no description for customer receipts/Checkout.",
            })

        for issue in validate_statement_descriptor(stmt):
            errors.append({
                "kind": "invalid_statement_descriptor",
                "product_id": pid,
                "name": name,
                "message": f"Product '{name}' ({pid}): {issue}",
            })

    for norm_name, pids in by_name.items():
        if norm_name and len(pids) > 1:
            warnings.append({
                "kind": "duplicate_active_product_name",
                "name": norm_name,
                "product_ids": pids,
                "message": f"{len(pids)} active products share the identical name '{norm_name}': {', '.join(pids)}",
            })

    # 2. Price checks (orphans, missing lookup_key, duplicate price shapes)
    price_shapes: dict[tuple[Any, ...], list[str]] = {}
    missing_lookup_key_count = 0

    for pr in active_prices:
        pr_id = pr.get("id", "unknown")
        prod_ref = pr.get("product")
        prod_id = prod_ref.get("id") if isinstance(prod_ref, dict) else str(prod_ref or "")
        parent = product_by_id.get(prod_id)
        if parent and parent.get("active"):
            active_prices_by_product.setdefault(prod_id, []).append(pr)
        elif parent and not parent.get("active"):
            warnings.append({
                "kind": "orphaned_active_price_on_archived_product",
                "price_id": pr_id,
                "product_id": prod_id,
                "message": f"Active price {pr_id} belongs to archived product {prod_id} ('{parent.get('name')}').",
            })

        if not pr.get("lookup_key"):
            missing_lookup_key_count += 1

        rec = pr.get("recurring") or {}
        shape = (
            prod_id,
            (pr.get("currency") or "").lower(),
            pr.get("unit_amount"),
            rec.get("interval"),
            rec.get("interval_count", 1) if rec else None,
        )
        price_shapes.setdefault(shape, []).append(pr_id)

    for shape, pr_ids in price_shapes.items():
        if shape[2] is not None and len(pr_ids) > 1:
            prod_id, cur, amt, interval, icount = shape
            warnings.append({
                "kind": "duplicate_active_price_shape",
                "product_id": prod_id,
                "price_ids": pr_ids,
                "message": f"Product {prod_id} has {len(pr_ids)} identical active prices ({amt} {cur}, interval={interval}/{icount}): {', '.join(pr_ids)}",
            })

    for prod in active_products:
        pid = prod.get("id", "")
        if pid and not active_prices_by_product.get(pid):
            warnings.append({
                "kind": "active_product_without_active_price",
                "product_id": pid,
                "name": prod.get("name"),
                "message": f"Active product '{prod.get('name')}' ({pid}) has 0 active prices.",
            })

    active_links = [pl for pl in payment_links if pl.get("active")]
    return {
        "ok": len(errors) == 0,
        "mode": client.mode,
        "summary": {
            "total_products": len(products),
            "active_products": len(active_products),
            "archived_products": len(products) - len(active_products),
            "total_prices": len(prices),
            "active_prices": len(active_prices),
            "active_prices_with_lookup_key": len(active_prices) - missing_lookup_key_count,
            "active_prices_missing_lookup_key": missing_lookup_key_count,
            "active_payment_links": len(active_links),
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
    }


def export_catalog_spec(
    client: StripeClient,
    *,
    name_prefix: str | None = None,
    max_items: int = 500,
) -> dict[str, Any]:
    """Export active Products and Prices into a portable stripe-catalog-spec/v1 document."""
    products = client.list_all("/v1/products", {"active": True, "limit": 100}, max_items=max_items)
    prices = client.list_all("/v1/prices", {"active": True, "limit": 100}, max_items=max_items)

    prices_by_prod: dict[str, list[dict[str, Any]]] = {}
    for pr in prices:
        prod_ref = pr.get("product")
        pid = prod_ref.get("id") if isinstance(prod_ref, dict) else str(prod_ref or "")
        prices_by_prod.setdefault(pid, []).append(pr)

    spec_products: list[dict[str, Any]] = []
    for prod in sorted(products, key=lambda x: (x.get("name") or "").lower()):
        name = (prod.get("name") or "").strip()
        if name_prefix and not name.lower().startswith(name_prefix.lower()):
            continue
        pid = prod.get("id", "")
        meta = dict(prod.get("metadata") or {})
        slug = meta.get("slug") or slugify(name)
        prod_prices: list[dict[str, Any]] = []
        for pr in sorted(
            prices_by_prod.get(pid, []),
            key=lambda x: (
                int(x.get("unit_amount") or 0),
                ((x.get("recurring") or {}).get("interval") or "one_time"),
            ),
        ):
            rec = pr.get("recurring") or {}
            interval = rec.get("interval")
            interval_count = int(rec.get("interval_count", 1)) if interval else 1
            cur = (pr.get("currency") or "usd").lower()
            amt = int(pr.get("unit_amount") or 0)
            lkey = pr.get("lookup_key") or f"{slug}_{cur}_{amt}_{interval or 'one_time'}"
            prod_prices.append({
                "lookup_key": lkey,
                "currency": cur,
                "unit_amount": amt,
                "interval": interval,
                "interval_count": interval_count,
                "nickname": pr.get("nickname"),
                "metadata": dict(pr.get("metadata") or {}),
            })
        spec_products.append({
            "slug": slug,
            "name": name,
            "description": prod.get("description"),
            "statement_descriptor": prod.get("statement_descriptor"),
            "metadata": meta,
            "prices": prod_prices,
        })

    return {
        "schema": "stripe-catalog-spec/v1",
        "source_mode": client.mode,
        "product_count": len(spec_products),
        "price_count": sum(len(p["prices"]) for p in spec_products),
        "products": spec_products,
    }


def generate_ladder_spec(
    *,
    prefix: str,
    amounts_usd: list[int],
    intervals: list[str],
    currency: str = "usd",
    statement_descriptor: str | None = None,
    description_template: str = "{prefix} tier (${amount_usd} {currency_upper} / {interval_label}).",
) -> dict[str, Any]:
    """Generate a validated stripe-catalog-spec/v1 for an exponential or tiered pricing ladder."""
    cur = currency.lower()
    products: list[dict[str, Any]] = []
    validation_errors: list[str] = []

    for idx, amt_usd in enumerate(amounts_usd, start=1):
        unit_amount = int(round(amt_usd * 100))
        slug = f"{slugify(prefix)}_rung_{idx:02d}_{amt_usd}{cur}"
        name = f"{prefix} - ${amt_usd}"
        desc = description_template.format(
            prefix=prefix,
            rung=idx,
            amount_usd=amt_usd,
            unit_amount=unit_amount,
            currency=cur,
            currency_upper=cur.upper(),
            interval_label="/".join(intervals),
        )
        validation_errors.extend(
            validate_product_fields(
                name=name,
                description=desc,
                statement_descriptor=statement_descriptor,
                metadata={"slug": slug, "rung": str(idx)},
            )
        )
        price_entries: list[dict[str, Any]] = []
        for iv in intervals:
            interval_val = None if iv in ("one_time", "once", "none") else iv
            lkey = f"{slug}_{iv}"
            validation_errors.extend(
                validate_price_fields(
                    currency=cur,
                    unit_amount=unit_amount,
                    interval=interval_val,
                    lookup_key=lkey,
                    nickname=f"{name} ({iv})",
                )
            )
            price_entries.append({
                "lookup_key": lkey,
                "currency": cur,
                "unit_amount": unit_amount,
                "interval": interval_val,
                "interval_count": 1,
                "nickname": f"{name} ({iv})",
                "metadata": {"slug": slug, "rung": str(idx), "cadence": iv},
            })
        products.append({
            "slug": slug,
            "name": name,
            "description": desc,
            "statement_descriptor": statement_descriptor,
            "metadata": {"slug": slug, "rung": str(idx)},
            "prices": price_entries,
        })

    if validation_errors:
        raise StripeError("Ladder specification failed validation: " + "; ".join(validation_errors))

    return {
        "schema": "stripe-catalog-spec/v1",
        "product_count": len(products),
        "price_count": sum(len(p["prices"]) for p in products),
        "products": products,
    }


def diff_catalog_spec(client: StripeClient, spec: dict[str, Any], *, max_items: int = 500) -> dict[str, Any]:
    """Compare a stripe-catalog-spec/v1 against the active catalog in the target Stripe account."""
    if spec.get("schema") != "stripe-catalog-spec/v1":
        raise StripeError("Catalog spec must have schema 'stripe-catalog-spec/v1'")

    remote_products = client.list_all("/v1/products", {"active": True, "limit": 100}, max_items=max_items)
    remote_prices = client.list_all("/v1/prices", {"active": True, "limit": 100}, max_items=max_items)

    prod_by_slug: dict[str, dict[str, Any]] = {}
    prod_by_name: dict[str, dict[str, Any]] = {}
    for rp in remote_products:
        meta = rp.get("metadata") or {}
        if meta.get("slug"):
            prod_by_slug[meta["slug"]] = rp
        if rp.get("name"):
            prod_by_name[rp["name"].strip().lower()] = rp

    price_by_lookup: dict[str, dict[str, Any]] = {}
    prices_by_prod: dict[str, list[dict[str, Any]]] = {}
    for rpr in remote_prices:
        if rpr.get("lookup_key"):
            price_by_lookup[rpr["lookup_key"]] = rpr
        pref = rpr.get("product")
        pid = pref.get("id") if isinstance(pref, dict) else str(pref or "")
        prices_by_prod.setdefault(pid, []).append(rpr)

    matched_products: list[dict[str, Any]] = []
    missing_products: list[dict[str, Any]] = []
    matched_prices: list[dict[str, Any]] = []
    missing_prices: list[dict[str, Any]] = []

    for sp in spec.get("products", []):
        slug = sp.get("slug") or slugify(sp["name"])
        rp = prod_by_slug.get(slug) or prod_by_name.get(sp["name"].strip().lower())
        if rp:
            matched_products.append({"slug": slug, "name": sp["name"], "remote_product_id": rp["id"]})
        else:
            missing_products.append({
                "slug": slug,
                "name": sp["name"],
                "description": sp.get("description"),
                "statement_descriptor": sp.get("statement_descriptor"),
                "metadata": {**(sp.get("metadata") or {}), "slug": slug},
            })

        for spr in sp.get("prices", []):
            lkey = spr.get("lookup_key")
            rpr = price_by_lookup.get(lkey) if lkey else None
            if not rpr and rp:
                # Fallback match by (currency, unit_amount, interval, interval_count) on matched product
                for candidate in prices_by_prod.get(rp["id"], []):
                    rec = candidate.get("recurring") or {}
                    c_interval = rec.get("interval")
                    c_icount = int(rec.get("interval_count", 1)) if c_interval else 1
                    if (
                        (candidate.get("currency") or "").lower() == (spr.get("currency") or "usd").lower()
                        and int(candidate.get("unit_amount") or 0) == int(spr.get("unit_amount") or 0)
                        and c_interval == spr.get("interval")
                        and c_icount == int(spr.get("interval_count") or 1)
                    ):
                        rpr = candidate
                        break
            if rpr:
                matched_prices.append({
                    "lookup_key": lkey,
                    "product_slug": slug,
                    "remote_price_id": rpr["id"],
                    "remote_product_id": rp["id"] if rp else rpr.get("product"),
                })
            else:
                missing_prices.append({
                    "product_slug": slug,
                    "remote_product_id": rp["id"] if rp else None,
                    "lookup_key": lkey,
                    "currency": (spr.get("currency") or "usd").lower(),
                    "unit_amount": int(spr.get("unit_amount") or 0),
                    "interval": spr.get("interval"),
                    "interval_count": int(spr.get("interval_count") or 1),
                    "nickname": spr.get("nickname"),
                    "metadata": spr.get("metadata") or {},
                })

    plan_payload = {
        "mode": client.mode,
        "missing_products": missing_products,
        "missing_prices": missing_prices,
    }
    digest = plan_digest("catalog-sync", plan_payload)
    return {
        "ok": True,
        "mode": client.mode,
        "in_sync": not missing_products and not missing_prices,
        "summary": {
            "matched_products": len(matched_products),
            "missing_products": len(missing_products),
            "matched_prices": len(matched_prices),
            "missing_prices": len(missing_prices),
        },
        "plan_id": digest,
        "plan_sha256": digest,
        "matched_products": matched_products,
        "missing_products": missing_products,
        "matched_prices": matched_prices,
        "missing_prices": missing_prices,
    }


def sync_catalog_spec(
    client: StripeClient,
    spec: dict[str, Any],
    *,
    apply: bool = False,
    confirm_plan: str | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Preview or apply missing Products and Prices from a stripe-catalog-spec/v1 with deterministic idempotency keys."""
    diff = diff_catalog_spec(client, spec)
    if not apply:
        return {
            **diff,
            "preview": True,
            "next": (
                "Catalog is already in sync."
                if diff["in_sync"]
                else f"Re-run with --apply --confirm-plan {diff['plan_sha256']}"
                + (" --live" if client.mode == "live" else "")
            ),
        }

    client.require_mutation_gate(allow_live=allow_live)
    if confirm_plan != diff["plan_sha256"]:
        raise StripeError(
            f"Confirmation hash mismatch: expected --confirm-plan {diff['plan_sha256']}, got {confirm_plan!r}."
        )

    created_products: dict[str, dict[str, Any]] = {}
    for mp in diff["missing_products"]:
        slug = mp["slug"]
        params: dict[str, Any] = {
            "name": mp["name"],
            "metadata": mp.get("metadata") or {"slug": slug},
        }
        if mp.get("description"):
            params["description"] = mp["description"]
        if mp.get("statement_descriptor"):
            params["statement_descriptor"] = mp["statement_descriptor"]
        idem = hashlib.sha256(f"legends-stripe:product:{client.mode}:{slug}:{diff['plan_sha256'][:16]}".encode()).hexdigest()[:48]
        created = client.request("POST", "/v1/products", params, idempotency_key=idem)
        readback = client.request("GET", f"/v1/products/{created['id']}")
        created_products[slug] = readback

    created_prices: list[dict[str, Any]] = []
    for mpr in diff["missing_prices"]:
        slug = mpr["product_slug"]
        prod_id = mpr.get("remote_product_id") or (created_products.get(slug) or {}).get("id")
        if not prod_id:
            raise StripeError(f"Cannot create price '{mpr.get('lookup_key')}': parent product '{slug}' was not resolved.")
        params = {
            "product": prod_id,
            "currency": mpr["currency"],
            "unit_amount": mpr["unit_amount"],
        }
        if mpr.get("lookup_key"):
            params["lookup_key"] = mpr["lookup_key"]
            params["transfer_lookup_key"] = True
        if mpr.get("nickname"):
            params["nickname"] = mpr["nickname"]
        if mpr.get("metadata"):
            params["metadata"] = mpr["metadata"]
        if mpr.get("interval"):
            params["recurring"] = {
                "interval": mpr["interval"],
                "interval_count": mpr.get("interval_count", 1),
            }
        idem_seed = mpr.get("lookup_key") or f"{prod_id}:{mpr['currency']}:{mpr['unit_amount']}:{mpr.get('interval')}"
        idem = hashlib.sha256(f"legends-stripe:price:{client.mode}:{idem_seed}:{diff['plan_sha256'][:16]}".encode()).hexdigest()[:48]
        created_pr = client.request("POST", "/v1/prices", params, idempotency_key=idem)
        readback_pr = client.request("GET", f"/v1/prices/{created_pr['id']}")
        created_prices.append(readback_pr)

    return {
        "ok": True,
        "preview": False,
        "mode": client.mode,
        "plan_id": diff["plan_sha256"],
        "plan_sha256": diff["plan_sha256"],
        "created_products_count": len(created_products),
        "created_prices_count": len(created_prices),
        "created_products": [
            {"id": p["id"], "name": p.get("name"), "slug": (p.get("metadata") or {}).get("slug")}
            for p in created_products.values()
        ],
        "created_prices": [
            {
                "id": p["id"],
                "product": p.get("product"),
                "lookup_key": p.get("lookup_key"),
                "unit_amount": p.get("unit_amount"),
                "currency": p.get("currency"),
            }
            for p in created_prices
        ],
    }


def upsert_product(
    client: StripeClient,
    *,
    name: str,
    product_id: str | None = None,
    description: str | None = None,
    statement_descriptor: str | None = None,
    active: bool = True,
    metadata: dict[str, Any] | None = None,
    apply: bool = False,
    confirm_plan: str | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Preview-first product create or update with API readback."""
    issues = validate_product_fields(
        name=name,
        description=description,
        statement_descriptor=statement_descriptor,
        metadata=metadata,
    )
    if issues:
        raise StripeError("Product validation failed: " + "; ".join(issues))

    params: dict[str, Any] = {"name": name, "active": active}
    if description is not None:
        params["description"] = description
    if statement_descriptor is not None:
        params["statement_descriptor"] = statement_descriptor
    if metadata:
        params["metadata"] = metadata

    action = "product-update" if product_id else "product-create"
    prices_to_deactivate: list[str] = []
    if product_id and not active:
        child_prices = client.list_all("/v1/prices", {"product": product_id, "active": True}, max_items=100)
        prices_to_deactivate = sorted(str(p["id"]) for p in child_prices if p.get("id"))

    plan_obj: dict[str, Any] = {"mode": client.mode, "product_id": product_id, "params": params}
    if prices_to_deactivate:
        plan_obj["prices_to_deactivate"] = prices_to_deactivate
    digest = plan_digest(action, plan_obj)
    if not apply:
        return {
            "ok": True,
            "preview": True,
            "action": action,
            "mode": client.mode,
            "plan_id": digest,
            "plan_sha256": digest,
            "planned": plan_obj,
            "next": f"Pass --apply --confirm-plan {digest}" + (" --live" if client.mode == "live" else ""),
        }

    client.require_mutation_gate(allow_live=allow_live)
    if confirm_plan != digest:
        raise StripeError(f"Confirmation hash mismatch: expected --confirm-plan {digest}, got {confirm_plan!r}.")

    deactivated_prices: list[str] = []
    for pr_id in prices_to_deactivate:
        client.request(
            "POST",
            f"/v1/prices/{pr_id}",
            {"active": False},
            idempotency_key=f"legends-stripe:price-deactivate:{pr_id}:{digest[:24]}",
        )
        deactivated_prices.append(pr_id)

    path = f"/v1/products/{product_id}" if product_id else "/v1/products"
    created = client.request("POST", path, params, idempotency_key=f"legends-stripe:{action}:{digest[:32]}")
    readback = client.request("GET", f"/v1/products/{created['id']}")
    return {
        "ok": True,
        "preview": False,
        "action": action,
        "mode": client.mode,
        "plan_id": digest,
        "plan_sha256": digest,
        "deactivated_prices": deactivated_prices,
        "product": {
            "id": readback.get("id"),
            "name": readback.get("name"),
            "description": readback.get("description"),
            "active": readback.get("active"),
            "statement_descriptor": readback.get("statement_descriptor"),
            "metadata": readback.get("metadata"),
        },
    }


def run_checkout_canary(
    client: StripeClient,
    *,
    price_id: str | None = None,
    amount_cents: int = 100,
    currency: str = "usd",
    product_name: str = "Legends Stripe Canary Verification",
    allow_live: bool = False,
) -> dict[str, Any]:
    """Create a zero-charge disposable Checkout Session, verify its URL and state, and immediately expire it."""
    client.require_mutation_gate(allow_live=allow_live)
    if price_id:
        line_item: dict[str, Any] = {"price": price_id, "quantity": 1}
    else:
        line_item = {
            "price_data": {
                "currency": currency.lower(),
                "unit_amount": amount_cents,
                "product_data": {
                    "name": product_name,
                    "description": "Disposable checkout session canary (immediately expired, never charged).",
                },
            },
            "quantity": 1,
        }

    params = {
        "mode": "payment",
        "line_items": [line_item],
        "success_url": "https://example.com/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": "https://example.com/checkout/cancel",
        "metadata": {"legends_stripe_canary": "true"},
    }
    created = client.request("POST", "/v1/checkout/sessions", params)
    session_id = created.get("id")
    if not session_id:
        raise StripeError("Stripe did not return a Checkout Session ID.")

    readback_open = client.request("GET", f"/v1/checkout/sessions/{session_id}")
    expired = client.request("POST", f"/v1/checkout/sessions/{session_id}/expire")

    return {
        "ok": True,
        "mode": client.mode,
        "session_id": session_id,
        "created_status": readback_open.get("status"),
        "expired_status": expired.get("status"),
        "hosted_url_present": bool(readback_open.get("url")),
        "amount_total": readback_open.get("amount_total"),
        "currency": readback_open.get("currency"),
        "livemode": bool(readback_open.get("livemode")),
        "cleaned_up": expired.get("status") == "expired",
    }
