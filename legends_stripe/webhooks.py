"""Webhook endpoint fortification, offline HMAC-SHA256 Stripe-Signature signer/verifier, unsigned gate probe, and custom domain DNS checker."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .api import (
    SOFT_LAND_WEBHOOK_EVENTS,
    StripeClient,
    StripeError,
    plan_digest,
)

EXPECTED_STRIPE_CNAME = "hosted-checkout.stripecdn.com"

DNS_RCODE_MAP = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}


def sign_stripe_webhook_payload(
    payload: str | bytes,
    secret: str,
    *,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Generate a valid Stripe-Signature header (t=<timestamp>,v1=<hmac_sha256>) for offline webhook testing."""
    raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    raw_text = raw_bytes.decode("utf-8")
    ts = int(timestamp if timestamp is not None else time.time())
    signed_payload = f"{ts}.{raw_text}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    return {
        "ok": True,
        "timestamp": ts,
        "v1": sig,
        "stripe_signature": header,
        "stripe_signature_header": header,
        "payload_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def verify_stripe_webhook_signature(
    payload: str | bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    """Verify a Stripe-Signature header against raw payload bytes using constant-time HMAC-SHA256."""
    raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    if not signature_header or not signature_header.strip():
        return {"ok": False, "valid": False, "reason": "missing_signature_header"}
    if not secret or not secret.strip():
        return {"ok": False, "valid": False, "reason": "missing_webhook_secret"}

    timestamp: int | None = None
    v1_signatures: list[str] = []
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        if k == "t":
            try:
                timestamp = int(v)
            except ValueError:
                return {"ok": False, "valid": False, "reason": "invalid_timestamp"}
        elif k == "v1" and v:
            v1_signatures.append(v)

    if timestamp is None:
        return {"ok": False, "valid": False, "reason": "missing_timestamp_in_header"}
    if not v1_signatures:
        return {"ok": False, "valid": False, "reason": "missing_v1_signature_in_header"}

    signed_payload = f"{timestamp}.{raw_text}".encode("utf-8")
    expected_sig = hmac.new(secret.strip().encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    matched = any(hmac.compare_digest(expected_sig, candidate) for candidate in v1_signatures)
    if not matched:
        return {"ok": False, "valid": False, "reason": "signature_mismatch", "timestamp": timestamp}

    now_ts = int(now_timestamp if now_timestamp is not None else time.time())
    age_seconds = abs(now_ts - timestamp)
    if tolerance_seconds > 0 and age_seconds > tolerance_seconds:
        return {
            "ok": False,
            "valid": False,
            "reason": "timestamp_outside_tolerance",
            "timestamp": timestamp,
            "age_seconds": age_seconds,
            "tolerance_seconds": tolerance_seconds,
        }

    return {
        "ok": True,
        "valid": True,
        "timestamp": timestamp,
        "age_seconds": age_seconds,
        "tolerance_seconds": tolerance_seconds,
        "payload_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def audit_webhooks(
    client: StripeClient,
    *,
    expected_url: str | None = None,
) -> dict[str, Any]:
    """Audit registered Stripe webhook endpoints for Soft-Land subscription lifecycle coverage and duplicates."""
    endpoints = client.list_all("/v1/webhook_endpoints", {"limit": 100}, max_items=100)
    required_events = set(SOFT_LAND_WEBHOOK_EVENTS)

    by_url: dict[str, list[str]] = {}
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    any_full_coverage = False

    for ep in endpoints:
        ep_id = ep.get("id", "unknown")
        url = (ep.get("url") or "").strip()
        status = ep.get("status") or "unknown"
        enabled = set(ep.get("enabled_events") or [])
        wildcard = "*" in enabled
        missing_soft_land = [] if wildcard else sorted(required_events - enabled)
        covers_soft_land = (status == "enabled") and (wildcard or not missing_soft_land)
        if covers_soft_land:
            any_full_coverage = True
        by_url.setdefault(url, []).append(ep_id)

        rows.append(
            {
                "id": ep_id,
                "url": url,
                "status": status,
                "api_version": ep.get("api_version"),
                "enabled_events": sorted(enabled),
                "covers_soft_land_contract": covers_soft_land,
                "missing_soft_land_events": missing_soft_land,
            }
        )

        if status != "enabled":
            warnings.append(
                {
                    "kind": "disabled_webhook_endpoint",
                    "endpoint_id": ep_id,
                    "url": url,
                    "message": f"Webhook endpoint {ep_id} ({url}) has status '{status}'.",
                }
            )
        elif missing_soft_land:
            warnings.append(
                {
                    "kind": "incomplete_soft_land_events",
                    "endpoint_id": ep_id,
                    "url": url,
                    "missing_events": missing_soft_land,
                    "message": f"Webhook endpoint {ep_id} ({url}) is missing Soft-Land events: {', '.join(missing_soft_land)}",
                }
            )

    for url, ids in by_url.items():
        if url and len(ids) > 1:
            warnings.append(
                {
                    "kind": "duplicate_webhook_url",
                    "url": url,
                    "endpoint_ids": ids,
                    "message": f"{len(ids)} webhook endpoints point to the identical URL '{url}': {', '.join(ids)}",
                }
            )

    expected_status: dict[str, Any] | None = None
    if expected_url:
        target = expected_url.strip()
        matched_rows = [r for r in rows if r["url"] == target]
        expected_status = {
            "expected_url": target,
            "registered": len(matched_rows) > 0,
            "matching_endpoint_ids": [r["id"] for r in matched_rows],
            "enabled_and_covers_soft_land": any(r["covers_soft_land_contract"] for r in matched_rows),
        }

    out: dict[str, Any] = {
        "ok": True,
        "mode": client.mode,
        "endpoint_count": len(rows),
        "soft_land_contract_events": list(SOFT_LAND_WEBHOOK_EVENTS),
        "has_active_soft_land_endpoint": any_full_coverage,
        "endpoints": rows,
        "warnings": warnings,
    }
    if expected_status is not None:
        out["expected_url_status"] = expected_status
    return out


def upsert_webhook_endpoint(
    client: StripeClient,
    *,
    url: str,
    events: list[str] | None = None,
    endpoint_id: str | None = None,
    description: str | None = None,
    apply: bool = False,
    confirm_plan: str | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Preview-first create or update a Stripe webhook endpoint without blind duplicate creation."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise StripeError("Webhook endpoint URL must be a valid https:// URL.")

    chosen_events = list(events or SOFT_LAND_WEBHOOK_EVENTS)
    params: dict[str, Any] = {
        "url": url.strip(),
        "enabled_events": chosen_events,
    }
    if description:
        params["description"] = description

    action = "webhook-update" if endpoint_id else "webhook-create"
    plan_obj = {"mode": client.mode, "endpoint_id": endpoint_id, "params": params}
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

    path = f"/v1/webhook_endpoints/{endpoint_id}" if endpoint_id else "/v1/webhook_endpoints"
    created = client.request("POST", path, params, idempotency_key=f"legends-stripe:{action}:{digest[:32]}")
    # Never print the returned webhook signing secret raw in logs if created; report presence only
    has_secret = bool(created.get("secret"))
    return {
        "ok": True,
        "preview": False,
        "action": action,
        "mode": client.mode,
        "plan_id": digest,
        "plan_sha256": digest,
        "endpoint": {
            "id": created.get("id"),
            "url": created.get("url"),
            "status": created.get("status"),
            "enabled_events": created.get("enabled_events"),
            "signing_secret_returned": has_secret,
        },
    }


def probe_unsigned_webhook_gate(url: str, *, timeout: int = 15) -> dict[str, Any]:
    """Send an unsigned POST {} to a webhook URL and verify it rejects unsigned traffic with HTTP 400/401/403."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise StripeError("Probe URL must be a valid HTTP/HTTPS URL.")

    req = urllib.request.Request(
        url.strip(),
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "legends-stripe-webhook-gate-probe/0.1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(512).decode("utf-8", errors="replace")
            return {
                "ok": False,
                "url": url.strip(),
                "http_status": status,
                "rejects_unsigned_traffic": False,
                "warning": f"Endpoint accepted unsigned POST with HTTP {status}! Enforce Stripe-Signature verification immediately.",
                "response_preview": body[:200],
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace") if exc.fp else ""
        rejected_properly = exc.code in (400, 401, 403)
        return {
            "ok": rejected_properly,
            "url": url.strip(),
            "http_status": exc.code,
            "rejects_unsigned_traffic": rejected_properly,
            "response_preview": body[:200],
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "url": url.strip(),
            "http_status": None,
            "rejects_unsigned_traffic": False,
            "error": str(exc.reason),
        }


def _doh_lookup_detailed(name: str, record_type: str) -> dict[str, Any]:
    """Query Cloudflare DNS-over-HTTPS and return status, records, and authority zone."""
    q = urllib.parse.urlencode({"name": name, "type": record_type})
    req = urllib.request.Request(
        f"https://cloudflare-dns.com/dns-query?{q}",
        headers={"Accept": "application/dns-json", "User-Agent": "legends-stripe/0.1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            rcode = int(payload.get("Status", -1))
            answers = payload.get("Answer") or []
            authority = payload.get("Authority") or []
            records = [str(a.get("data", "")).strip().rstrip(".") for a in answers if a.get("data")]
            auth_zone = str(authority[0].get("name", "")).strip().rstrip(".") if authority else None
            return {
                "status_code": rcode,
                "status": DNS_RCODE_MAP.get(rcode, f"RCODE_{rcode}"),
                "records": records,
                "authority_zone": auth_zone,
            }
    except Exception as exc:
        return {"status_code": -1, "status": "LOOKUP_ERROR", "records": [], "authority_zone": None, "error": str(exc)}


def check_custom_domain(hostname: str) -> dict[str, Any]:
    """Verify public CNAME, _acme-challenge TXT, and HTTPS reachability for a Stripe custom payment domain."""
    host = hostname.strip().lower().rstrip(".")
    if not host or "/" in host:
        raise StripeError("Provide a bare hostname such as 'pay.example.com'.")

    cname_info = _doh_lookup_detailed(host, "CNAME")
    txt_info = _doh_lookup_detailed(f"_acme-challenge.{host}", "TXT")
    cname_records = cname_info["records"]
    txt_records = txt_info["records"]
    cname_matches = any(r.lower() == EXPECTED_STRIPE_CNAME for r in cname_records)

    https_status: int | None = None
    try:
        req = urllib.request.Request(f"https://{host}/", headers={"User-Agent": "legends-stripe/0.1.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            https_status = resp.status
    except urllib.error.HTTPError as exc:
        https_status = exc.code
    except Exception:
        https_status = None

    parts = host.split(".")
    zone_guess = cname_info.get("authority_zone") or (".".join(parts[-2:]) if len(parts) >= 2 else host)

    return {
        "ok": cname_matches and https_status in (200, 204, 301, 302, 404),
        "hostname": host,
        "dns_status": cname_info["status"],
        "authoritative_zone": zone_guess,
        "expected_cname_target": EXPECTED_STRIPE_CNAME,
        "cname_records": cname_records,
        "cname_verified": cname_matches,
        "acme_challenge_txt_records": txt_records,
        "acme_challenge_txt_present": bool(txt_records),
        "https_status": https_status,
        "cloudflare_commands": {
            "cname_preview": f"legends-cloudflare upsert --zone {zone_guess} --type CNAME --name {host} --content {EXPECTED_STRIPE_CNAME} --ttl 300 --proxied false",
            "txt_preview": f"legends-cloudflare upsert --zone {zone_guess} --type TXT --name _acme-challenge.{host} --content \"<STRIPE_ACME_TOKEN>\" --ttl 300",
        },
    }
