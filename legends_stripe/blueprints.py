"""Drop-in Astro v7, Node.js, and Python blueprints for Stripe Checkout, Customer Portal, and Soft-Land Webhooks."""
from __future__ import annotations

from typing import Any

from .api import SOFT_LAND_WEBHOOK_EVENTS, StripeError

BLUEPRINTS: dict[str, dict[str, str]] = {
    "astro-checkout": {
        "target_path": "src/pages/api/stripe/checkout.ts",
        "runtime": "Astro v7 Server Endpoint (TypeScript, zero SDK dependency)",
        "description": "Resolves Stripe Price by lookup_key across test/live modes, guards sk_test_ branding assets, and creates a Hosted Checkout Session.",
        "code": """export const prerender = false;

import type { APIRoute } from "astro";

const STRIPE_API = "https://api.stripe.com/v1";

export const POST: APIRoute = async ({ request, url }) => {
  const secretKey = process.env.STRIPE_SECRET_KEY || import.meta.env.STRIPE_SECRET_KEY || "";
  if (!secretKey.startsWith("sk_") && !secretKey.startsWith("rk_")) {
    return new Response(JSON.stringify({ ok: false, error: "stripe_not_configured" }), { status: 500 });
  }
  const isTestMode = secretKey.startsWith("sk_test_") || secretKey.startsWith("rk_test_");

  const body = await request.json().catch(() => ({}));
  const lookupKey = String(body.lookup_key || "").trim();
  const explicitPriceId = String(body.price_id || "").trim();
  const customerEmail = String(body.email || "").trim();

  let priceId = explicitPriceId;
  let mode: "payment" | "subscription" = body.mode === "subscription" ? "subscription" : "payment";

  if (!priceId && lookupKey) {
    const q = new URLSearchParams({ "lookup_keys[0]": lookupKey, active: "true", limit: "1" });
    const priceResp = await fetch(`${STRIPE_API}/prices?${q.toString()}`, {
      headers: { Authorization: `Bearer ${secretKey}` },
    });
    const priceData = await priceResp.json();
    const resolved = priceData?.data?.[0];
    if (!resolved?.id) {
      return new Response(JSON.stringify({ ok: false, error: "price_lookup_key_not_found", lookup_key: lookupKey }), {
        status: 404,
      });
    }
    priceId = resolved.id;
    mode = resolved.type === "recurring" ? "subscription" : "payment";
  }

  if (!priceId) {
    return new Response(JSON.stringify({ ok: false, error: "missing_price_or_lookup_key" }), { status: 400 });
  }

  const origin = url.origin;
  const form = new URLSearchParams({
    mode,
    "line_items[0][price]": priceId,
    "line_items[0][quantity]": "1",
    success_url: `${origin}/checkout/success?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${origin}/checkout/cancel`,
  });
  if (customerEmail) {
    form.set("customer_email", customerEmail);
  }
  // Cross-mode safety: never pass live file_... branding IDs when running under sk_test_
  const liveLogoFile = process.env.STRIPE_LIVE_LOGO_FILE_ID || "";
  if (!isTestMode && liveLogoFile) {
    form.set("branding_settings[logo][type]", "file");
    form.set("branding_settings[logo][file]", liveLogoFile);
  }

  const sessionResp = await fetch(`${STRIPE_API}/checkout/sessions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${secretKey}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form.toString(),
  });
  const session = await sessionResp.json();
  if (!sessionResp.ok || !session?.url) {
    return new Response(JSON.stringify({ ok: false, error: session?.error?.message || "checkout_create_failed" }), {
      status: sessionResp.status || 500,
    });
  }

  return new Response(JSON.stringify({ ok: true, id: session.id, url: session.url, mode }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};
""",
    },
    "astro-webhook": {
        "target_path": "src/pages/api/stripe/webhook.ts",
        "runtime": "Astro v7 Server Endpoint (TypeScript, Node crypto HMAC-SHA256)",
        "description": "Verifies raw-body Stripe-Signature with constant-time HMAC-SHA256 and routes the 5-event Soft-Land Subscription Lifecycle.",
        "code": """export const prerender = false;

import type { APIRoute } from "astro";
import crypto from "node:crypto";

const TOLERANCE_SECONDS = 300;

export function verifyStripeSignature(rawBody: string, sigHeader: string, secret: string): boolean {
  if (!sigHeader || !secret) return false;
  let timestamp = 0;
  const signatures: string[] = [];
  for (const part of sigHeader.split(",")) {
    const [k, v] = part.trim().split("=", 2);
    if (k === "t") timestamp = Number(v);
    if (k === "v1" && v) signatures.push(v);
  }
  if (!timestamp || signatures.length === 0) return false;
  const age = Math.abs(Math.floor(Date.now() / 1000) - timestamp);
  if (age > TOLERANCE_SECONDS) return false;

  const expected = crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}.${rawBody}`, "utf8")
    .digest("hex");
  const expectedBuf = Buffer.from(expected, "utf8");

  return signatures.some((candidate) => {
    const candBuf = Buffer.from(candidate, "utf8");
    return candBuf.length === expectedBuf.length && crypto.timingSafeEqual(candBuf, expectedBuf);
  });
}

export const POST: APIRoute = async ({ request }) => {
  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET || import.meta.env.STRIPE_WEBHOOK_SECRET || "";
  const sigHeader = request.headers.get("stripe-signature") || "";
  const rawBody = await request.text();

  if (!verifyStripeSignature(rawBody, sigHeader, webhookSecret)) {
    return new Response(JSON.stringify({ ok: false, error: "signature_invalid_or_missing" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const event = JSON.parse(rawBody);
  const type = String(event.type || "");
  const obj = event?.data?.object || {};

  // Soft-Land Subscription Lifecycle Classification
  let transition: "activate" | "past_due_grace" | "soft_land_base" | "ignore" = "ignore";
  if (type === "checkout.session.completed" || type === "invoice.paid") {
    transition = "activate";
  } else if (type === "invoice.payment_failed") {
    transition = "past_due_grace";
  } else if (type === "customer.subscription.deleted") {
    transition = "soft_land_base";
  } else if (type === "customer.subscription.updated") {
    if (obj.status === "active") transition = "activate";
    else if (obj.status === "past_due") transition = "past_due_grace";
    else if (obj.status === "canceled" || obj.status === "unpaid") transition = "soft_land_base";
  }

  return new Response(
    JSON.stringify({
      ok: true,
      event_id: event.id,
      type,
      transition,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
};
""",
    },
    "astro-portal": {
        "target_path": "src/pages/api/stripe/portal.ts",
        "runtime": "Astro v7 Server Endpoint (TypeScript)",
        "description": "Creates a Stripe Billing Customer Portal session for self-service plan changes and cancellation.",
        "code": """export const prerender = false;

import type { APIRoute } from "astro";

export const POST: APIRoute = async ({ request, url }) => {
  const secretKey = process.env.STRIPE_SECRET_KEY || import.meta.env.STRIPE_SECRET_KEY || "";
  if (!secretKey.startsWith("sk_") && !secretKey.startsWith("rk_")) {
    return new Response(JSON.stringify({ ok: false, error: "stripe_not_configured" }), { status: 500 });
  }
  const body = await request.json().catch(() => ({}));
  const customerId = String(body.customer_id || "").trim();
  if (!/^cus_[A-Za-z0-9_]+$/.test(customerId)) {
    return new Response(JSON.stringify({ ok: false, error: "invalid_customer_id" }), { status: 400 });
  }

  let returnUrl = `${url.origin}/account`;
  if (typeof body.return_url === "string" && body.return_url.trim()) {
    try {
      const candidate = new URL(body.return_url.trim(), url.origin);
      if (candidate.origin === url.origin) {
        returnUrl = candidate.toString();
      }
    } catch {
      // Keep default same-origin returnUrl
    }
  }

  const form = new URLSearchParams({
    customer: customerId,
    return_url: returnUrl,
  });

  const resp = await fetch("https://api.stripe.com/v1/billing_portal/sessions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${secretKey}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form.toString(),
  });
  const session = await resp.json();
  if (!resp.ok || !session?.url) {
    return new Response(JSON.stringify({ ok: false, error: session?.error?.message || "portal_create_failed" }), {
      status: resp.status || 500,
    });
  }
  return new Response(JSON.stringify({ ok: true, url: session.url }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};
""",
    },
    "python-webhook": {
        "target_path": "stripe_webhook_handler.py",
        "runtime": "Python 3.11+ Standard Library",
        "description": "Dependency-free Python Stripe-Signature HMAC-SHA256 verifier and Soft-Land event classifier.",
        "code": """import hashlib
import hmac
import json
import time


def classify_soft_land_event(raw_body: bytes, sig_header: str, webhook_secret: str, tolerance: int = 300) -> dict:
    ts = 0
    v1_sigs: list[str] = []
    for item in (sig_header or "").split(","):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        if k == "t":
            try:
                ts = int(v)
            except ValueError as exc:
                raise ValueError("Invalid Stripe-Signature timestamp") from exc
        elif k == "v1" and v:
            v1_sigs.append(v)
    if not ts or not v1_sigs or abs(int(time.time()) - ts) > tolerance:
        raise ValueError("Invalid or expired Stripe-Signature timestamp")
    signed = f"{ts}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(webhook_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, cand) for cand in v1_sigs):
        raise ValueError("Stripe-Signature HMAC mismatch")
    event = json.loads(raw_body.decode("utf-8"))
    etype = event.get("type", "")
    status = (event.get("data", {}).get("object") or {}).get("status", "")
    if etype in ("checkout.session.completed", "invoice.paid") or (etype == "customer.subscription.updated" and status == "active"):
        transition = "activate"
    elif etype == "invoice.payment_failed" or (etype == "customer.subscription.updated" and status == "past_due"):
        transition = "past_due_grace"
    elif etype == "customer.subscription.deleted" or (etype == "customer.subscription.updated" and status in ("canceled", "unpaid")):
        transition = "soft_land_base"
    else:
        transition = "ignore"
    return {"ok": True, "event_id": event.get("id"), "type": etype, "transition": transition}
""",
    },
}


def list_blueprints() -> dict[str, Any]:
    """Return metadata for all built-in integration blueprints."""
    return {
        "ok": True,
        "soft_land_webhook_events": list(SOFT_LAND_WEBHOOK_EVENTS),
        "blueprints": [
            {
                "name": name,
                "target_path": item["target_path"],
                "runtime": item["runtime"],
                "description": item["description"],
            }
            for name, item in BLUEPRINTS.items()
        ],
    }


def get_blueprint(name: str) -> dict[str, Any]:
    """Return a specific integration blueprint by name."""
    if name not in BLUEPRINTS:
        raise StripeError(f"Unknown blueprint '{name}'. Choose from: {', '.join(sorted(BLUEPRINTS))}")
    item = BLUEPRINTS[name]
    return {
        "ok": True,
        "name": name,
        "target_path": item["target_path"],
        "runtime": item["runtime"],
        "description": item["description"],
        "code": item["code"],
    }
