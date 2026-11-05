"""Command-line interface for legends-stripe."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .api import (
    SOFT_LAND_WEBHOOK_EVENTS,
    StripeClient,
    StripeError,
    detect_stripe_cli,
    resolve_credentials,
)
from .blueprints import get_blueprint, list_blueprints
from .catalog import (
    audit_catalog,
    diff_catalog_spec,
    export_catalog_spec,
    generate_ladder_spec,
    run_checkout_canary,
    sync_catalog_spec,
    upsert_product,
)
from .env_tools import audit_env_file, build_price_map
from .resources import resolve as resolve_resources, status as resources_status
from .subscriptions import scan_subscription_radar
from .telemetry import build_treasury_telemetry, render_telemetry_markdown
from .webhooks import (
    audit_webhooks,
    check_custom_domain,
    probe_unsigned_webhook_gate,
    sign_stripe_webhook_payload,
    upsert_webhook_endpoint,
    verify_stripe_webhook_signature,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_output_file(path_str: str, content: str, *, overwrite: bool = False) -> str:
    target = Path(path_str).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    try:
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        raise StripeError(f"Output file already exists (pass --overwrite to replace): {target}") from None
    return str(target)


def _load_payload_arg(value: str) -> str:
    if value.startswith("@"):
        p = Path(value[1:]).expanduser().resolve()
        if not p.is_file():
            raise StripeError(f"Payload file not found: {p}")
        return p.read_text(encoding="utf-8")
    return value


def _make_client(args: argparse.Namespace, *, fallback_env: str | None = None) -> StripeClient:
    env_file = getattr(args, "env_file", None) or fallback_env
    creds = resolve_credentials(env_file)
    return StripeClient(
        creds["secret_key"],
        base_url=creds["base_url"],
        api_version=creds["api_version"],
        stripe_account=creds["stripe_account"],
    )


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    creds = resolve_credentials(getattr(args, "env_file", None))
    key_info = creds["key_info"]
    cli_info = detect_stripe_cli()
    res_status = resources_status()

    result: dict[str, Any] = {
        "ok": res_status.get("status") == "available",
        "status": "pass" if res_status.get("status") == "available" else "incomplete",
        "module": "legends-stripe",
        "version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": sys.version_info >= (3, 11),
        "credentials": {
            "secret_key_present": key_info["present"],
            "key_kind": key_info["kind"],
            "mode": key_info["mode"],
            "valid_secret_key": key_info["valid_secret"],
            "publishable_key_present": creds["publishable_present"],
            "webhook_secret_present": creds["webhook_secret_present"],
            "api_version_override": creds["api_version"],
            "stripe_account_header": creds["stripe_account"],
        },
        "resources": res_status,
        "knowledge_pack_present": res_status.get("status") == "available",
        "optional_stripe_cli": cli_info,
        "soft_land_webhook_events": list(SOFT_LAND_WEBHOOK_EVENTS),
        "paid_calls": False,
    }

    if args.live:
        client = _make_client(args)
        acct = client.request("GET", "/v1/account")
        settings = acct.get("settings") or {}
        bprof = acct.get("business_profile") or {}
        dash = settings.get("dashboard") or {}
        result["live_account"] = {
            "account_id": acct.get("id"),
            "business_name": bprof.get("name") or dash.get("display_name") or acct.get("id"),
            "country": acct.get("country"),
            "default_currency": acct.get("default_currency"),
            "charges_enabled": bool(acct.get("charges_enabled")),
            "payouts_enabled": bool(acct.get("payouts_enabled")),
            "mode": client.mode,
        }
    return result


def cmd_resources(_args: argparse.Namespace) -> dict[str, Any]:
    found = resolve_resources()
    if not found.get("complete"):
        raise StripeError(f"Required package resources are incomplete: {found.get('note')}")
    manifest = found.get("manifest") or {}
    return {
        "ok": True,
        "status": "pass",
        "module": "legends-stripe",
        "version": __version__,
        **found,
        "knowledge_pack": {
            "path": "knowledge/stripe/pack.json",
            "schema": manifest.get("schema"),
            "suggested_home": (manifest.get("ontology") or {}).get("suggested_home"),
            "files": [f.get("path") for f in manifest.get("files", [])],
        },
        "blueprints": list_blueprints()["blueprints"],
        "soft_land_webhook_events": list(SOFT_LAND_WEBHOOK_EVENTS),
    }


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        default=argparse.SUPPRESS,
        help="Optional path to a .env file containing STRIPE_SECRET_KEY.",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Output JSON (default for structured commands).",
    )

    parser = argparse.ArgumentParser(
        prog="legends-stripe",
        description="Portable Stripe payments, catalog governance, treasury telemetry, and webhook fortification CLI.",
    )
    parser.add_argument("--version", action="version", version=f"legends-stripe {__version__}")
    parser.add_argument("--env-file", default=None, help="Optional path to a .env file containing STRIPE_SECRET_KEY.")
    parser.add_argument("--json", action="store_true", default=False, help="Output JSON (default for structured commands).")

    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser(
        "doctor",
        parents=[common],
        help="Check Python runtime, Stripe key classification, and optional live /v1/account readiness.",
    )
    p_doc.add_argument("--live", action="store_true", help="Verify connectivity against GET /v1/account.")

    sub.add_parser(
        "resources",
        parents=[common],
        help="List packaged knowledge pack runbooks, templates, and code blueprints offline.",
    )

    p_tel = sub.add_parser(
        "telemetry",
        parents=[common],
        help="Reconcile Stripe account balance, Gross Revenue, Stripe Drag (fees), and Net Retained Cash.",
    )
    p_tel.add_argument("--max-transactions", type=int, default=500, help="Maximum balance transactions to sample (default: 500).")
    p_tel.add_argument("--redact-pii", action="store_true", help="Mask customer names and emails in itemized output.")
    p_tel.add_argument("--markdown", action="store_true", help="Render telemetry as an Empire Markdown report.")
    p_tel.add_argument("--output", help="Write output to a file path (fails closed if file exists unless --overwrite).")
    p_tel.add_argument("--overwrite", action="store_true", help="Allow overwriting --output file.")

    p_caud = sub.add_parser(
        "catalog-audit",
        parents=[common],
        help="Audit Products, Prices, and Payment Links for field limits, orphans, duplicates, and missing lookup_keys.",
    )
    p_caud.add_argument("--max-items", type=int, default=500, help="Maximum items per resource to inspect.")

    p_cexp = sub.add_parser(
        "catalog-export",
        parents=[common],
        help="Export active Products and Prices into a portable stripe-catalog-spec/v1 JSON specification.",
    )
    p_cexp.add_argument("--prefix", help="Optional product name prefix filter.")
    p_cexp.add_argument("--output", help="Optional file path to save the exported catalog spec.")
    p_cexp.add_argument("--overwrite", action="store_true", help="Allow overwriting --output file.")

    p_pmap = sub.add_parser(
        "price-map",
        parents=[common],
        help="Map active Stripe Products and Prices into JSON, .env variables, or TypeScript constants.",
    )
    p_pmap.add_argument("--prefix", help="Optional product name prefix filter.")
    p_pmap.add_argument("--env-prefix", default="STRIPE_PRICE_", help="Environment variable prefix (default: STRIPE_PRICE_).")
    p_pmap.add_argument("--format", choices=["json", "dotenv", "ts"], default="json", help="Output format: json, dotenv, or ts.")
    p_pmap.add_argument("--output", help="Optional file path to write the price map.")
    p_pmap.add_argument("--overwrite", action="store_true", help="Allow overwriting --output file.")

    p_eaud = sub.add_parser(
        "env-audit",
        parents=[common],
        help="Audit a project .env file for Stripe test/live key mismatches, empty vars, and live Price/Product IDs.",
    )
    p_eaud.add_argument("--target-env", required=True, help="Path to the project .env file to audit.")
    p_eaud.add_argument("--live", action="store_true", help="Verify referenced price_... and prod_... IDs against the Stripe API.")

    p_lad = sub.add_parser(
        "ladder-plan",
        parents=[common],
        help="Generate a validated stripe-catalog-spec/v1 for a tiered or exponential price ladder.",
    )
    p_lad.add_argument("--prefix", required=True, help="Product family prefix (for example 'CTO Legends Provision').")
    p_lad.add_argument("--amounts", default="1,2,4,8,16,32,64,128", help="Comma-separated whole dollar amounts.")
    p_lad.add_argument("--intervals", default="month,year,one_time", help="Comma-separated billing intervals (month, year, week, day, one_time).")
    p_lad.add_argument("--currency", default="usd", help="3-letter ISO currency code (default: usd).")
    p_lad.add_argument("--statement-descriptor", help="Optional 5..22 char ASCII statement descriptor.")
    p_lad.add_argument("--output", help="Optional file path to write the generated spec.")
    p_lad.add_argument("--overwrite", action="store_true", help="Allow overwriting --output file.")

    p_cdiff = sub.add_parser(
        "catalog-diff",
        parents=[common],
        help="Compare a stripe-catalog-spec/v1 JSON file against the target Stripe account.",
    )
    p_cdiff.add_argument("--spec", required=True, help="Path to stripe-catalog-spec/v1 JSON file.")

    p_csync = sub.add_parser(
        "catalog-sync",
        parents=[common],
        help="Preview or apply missing Products and Prices from a stripe-catalog-spec/v1 file.",
    )
    p_csync.add_argument("--spec", required=True, help="Path to stripe-catalog-spec/v1 JSON file.")
    p_csync.add_argument("--apply", action="store_true", help="Execute planned creations.")
    p_csync.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview.")
    p_csync.add_argument("--live", action="store_true", help="Required safety flag when mutating a live Stripe account.")

    p_prod = sub.add_parser(
        "product-upsert",
        parents=[common],
        help="Preview-first create or update a Stripe Product with field validation and readback.",
    )
    p_prod.add_argument("--name", required=True, help="Product display name.")
    p_prod.add_argument("--product-id", help="Existing prod_... ID to update (omit to create).")
    p_prod.add_argument("--description", help="Customer-visible description (max 500 chars).")
    p_prod.add_argument("--statement-descriptor", help="Optional 5..22 char ASCII statement descriptor.")
    p_prod.add_argument("--inactive", action="store_true", help="Set active=false (archive product).")
    p_prod.add_argument("--slug", help="Optional metadata.slug key.")
    p_prod.add_argument("--apply", action="store_true", help="Execute the mutation.")
    p_prod.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview.")
    p_prod.add_argument("--live", action="store_true", help="Required safety flag when mutating a live Stripe account.")

    p_can = sub.add_parser(
        "checkout-canary",
        parents=[common],
        help="Create a disposable zero-charge Checkout Session, verify hosted URL, and immediately expire it.",
    )
    p_can.add_argument("--price-id", help="Optional existing price_... ID.")
    p_can.add_argument("--amount-cents", type=int, default=100, help="Canary amount in minor units if no --price-id (default: 100).")
    p_can.add_argument("--currency", default="usd", help="Currency code (default: usd).")
    p_can.add_argument("--live", action="store_true", help="Allow running canary on a live key (creates and immediately expires an unpaid session).")

    p_sub = sub.add_parser(
        "subscription-radar",
        parents=[common],
        help="Scan subscriptions for past_due dunning risk, pending cancellations, MRR, and duplicate active billing.",
    )
    p_sub.add_argument("--max-items", type=int, default=500, help="Maximum subscriptions to inspect.")
    p_sub.add_argument("--redact-pii", action="store_true", help="Mask customer names and emails.")

    p_waud = sub.add_parser(
        "webhook-audit",
        parents=[common],
        help="Audit registered webhook endpoints for Soft-Land lifecycle event coverage and duplicates.",
    )
    p_waud.add_argument("--expected-url", help="Optional webhook URL to verify registration and Soft-Land coverage.")

    p_wups = sub.add_parser(
        "webhook-upsert",
        parents=[common],
        help="Preview-first create or update a Stripe webhook endpoint.",
    )
    p_wups.add_argument("--url", required=True, help="HTTPS webhook receiver URL.")
    p_wups.add_argument("--endpoint-id", help="Optional existing we_... ID to update.")
    p_wups.add_argument("--events", help="Comma-separated event list (defaults to the 5 Soft-Land lifecycle events).")
    p_wups.add_argument("--description", help="Optional endpoint description.")
    p_wups.add_argument("--apply", action="store_true", help="Execute the mutation.")
    p_wups.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview.")
    p_wups.add_argument("--live", action="store_true", help="Required safety flag when mutating a live Stripe account.")

    p_wprb = sub.add_parser(
        "webhook-probe",
        parents=[common],
        help="Send a safe unsigned POST {} probe to verify a live webhook route rejects unsigned requests.",
    )
    p_wprb.add_argument("--url", required=True, help="Target webhook URL to probe.")

    p_wsig = sub.add_parser(
        "sign-webhook",
        parents=[common],
        help="Generate a valid Stripe-Signature HMAC-SHA256 header for offline webhook testing.",
    )
    p_wsig.add_argument("--payload", required=True, help="Raw JSON payload string or @path/to/payload.json.")
    p_wsig.add_argument("--secret", required=True, help="Test webhook signing secret (whsec_...).")
    p_wsig.add_argument("--timestamp", type=int, help="Optional Unix timestamp override.")

    p_wver = sub.add_parser(
        "verify-webhook",
        parents=[common],
        help="Verify a Stripe-Signature header against a raw payload using constant-time HMAC-SHA256.",
    )
    p_wver.add_argument("--payload", required=True, help="Raw JSON payload string or @path/to/payload.json.")
    p_wver.add_argument("--signature", required=True, help="Stripe-Signature header value (t=...,v1=...).")
    p_wver.add_argument("--secret", required=True, help="Webhook signing secret (whsec_...).")
    p_wver.add_argument("--tolerance", type=int, default=300, help="Timestamp replay tolerance in seconds (default: 300).")
    p_wver.add_argument("--now", type=int, help="Optional current Unix timestamp override for deterministic tests.")

    p_cdom = sub.add_parser(
        "custom-domain-check",
        parents=[common],
        help="Verify CNAME, _acme-challenge TXT, and HTTPS status for a Stripe custom payment domain.",
    )
    p_cdom.add_argument("--hostname", required=True, help="Custom domain hostname (for example 'pay.cto-legends.com').")

    p_bp = sub.add_parser(
        "blueprint",
        parents=[common],
        help="Inspect or output drop-in Astro v7, Node.js, or Python Stripe integration blueprints.",
    )
    p_bp.add_argument("--name", help="Blueprint name: astro-checkout, astro-webhook, astro-portal, or python-webhook.")
    p_bp.add_argument("--list", action="store_true", help="List available blueprints.")
    p_bp.add_argument("--raw", action="store_true", help="Print only the raw source code of the selected blueprint.")
    p_bp.add_argument("--output", help="Optional file path to write the blueprint code.")
    p_bp.add_argument("--overwrite", action="store_true", help="Allow overwriting --output file.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            out = cmd_doctor(args)
        elif args.command == "resources":
            out = cmd_resources(args)
        elif args.command == "telemetry":
            client = _make_client(args)
            report = build_treasury_telemetry(
                client,
                max_transactions=args.max_transactions,
                redact_pii=args.redact_pii,
            )
            if args.markdown:
                md = render_telemetry_markdown(report)
                if args.output:
                    saved = _write_output_file(args.output, md, overwrite=args.overwrite)
                    out = {"ok": True, "written": saved, "mode": client.mode}
                else:
                    sys.stdout.write(md)
                    return 0
            else:
                if args.output:
                    saved = _write_output_file(args.output, json.dumps(report, indent=2) + "\n", overwrite=args.overwrite)
                    out = {"ok": True, "written": saved, "mode": client.mode}
                else:
                    out = report
        elif args.command == "catalog-audit":
            client = _make_client(args)
            out = audit_catalog(client, max_items=args.max_items)
        elif args.command == "catalog-export":
            client = _make_client(args)
            spec = export_catalog_spec(client, name_prefix=args.prefix)
            if args.output:
                saved = _write_output_file(args.output, json.dumps(spec, indent=2) + "\n", overwrite=args.overwrite)
                out = {"ok": True, "written": saved, "product_count": spec["product_count"], "price_count": spec["price_count"]}
            else:
                out = spec
        elif args.command == "price-map":
            client = _make_client(args)
            pmap = build_price_map(client, name_prefix=args.prefix, env_prefix=args.env_prefix)
            if args.format == "dotenv":
                text = pmap["dotenv"]
                if args.output:
                    saved = _write_output_file(args.output, text, overwrite=args.overwrite)
                    out = {"ok": True, "written": saved, "count": pmap["count"], "format": "dotenv"}
                else:
                    sys.stdout.write(text)
                    return 0
            elif args.format == "ts":
                text = pmap["typescript"]
                if args.output:
                    saved = _write_output_file(args.output, text, overwrite=args.overwrite)
                    out = {"ok": True, "written": saved, "count": pmap["count"], "format": "ts"}
                else:
                    sys.stdout.write(text)
                    return 0
            else:
                if args.output:
                    saved = _write_output_file(args.output, json.dumps(pmap, indent=2) + "\n", overwrite=args.overwrite)
                    out = {"ok": True, "written": saved, "count": pmap["count"], "format": "json"}
                else:
                    out = pmap
        elif args.command == "env-audit":
            client = _make_client(args, fallback_env=args.target_env) if args.live else None
            out = audit_env_file(args.target_env, client=client, verify_live=args.live)
            if not out.get("ok"):
                sys.stdout.write(json.dumps(out, indent=2) + "\n")
                return 1
        elif args.command == "ladder-plan":
            amounts = [int(x.strip()) for x in args.amounts.split(",") if x.strip()]
            intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]
            spec = generate_ladder_spec(
                prefix=args.prefix,
                amounts_usd=amounts,
                intervals=intervals,
                currency=args.currency,
                statement_descriptor=args.statement_descriptor,
            )
            if args.output:
                saved = _write_output_file(args.output, json.dumps(spec, indent=2) + "\n", overwrite=args.overwrite)
                out = {"ok": True, "written": saved, "product_count": spec["product_count"], "price_count": spec["price_count"]}
            else:
                out = spec
        elif args.command == "catalog-diff":
            client = _make_client(args)
            spec_path = Path(args.spec).expanduser().resolve()
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            out = diff_catalog_spec(client, spec)
        elif args.command == "catalog-sync":
            client = _make_client(args)
            spec_path = Path(args.spec).expanduser().resolve()
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            out = sync_catalog_spec(
                client,
                spec,
                apply=args.apply,
                confirm_plan=args.confirm_plan,
                allow_live=args.live,
            )
        elif args.command == "product-upsert":
            client = _make_client(args)
            meta = {"slug": args.slug} if args.slug else None
            out = upsert_product(
                client,
                name=args.name,
                product_id=args.product_id,
                description=args.description,
                statement_descriptor=args.statement_descriptor,
                active=not args.inactive,
                metadata=meta,
                apply=args.apply,
                confirm_plan=args.confirm_plan,
                allow_live=args.live,
            )
        elif args.command == "checkout-canary":
            client = _make_client(args)
            out = run_checkout_canary(
                client,
                price_id=args.price_id,
                amount_cents=args.amount_cents,
                currency=args.currency,
                allow_live=args.live,
            )
        elif args.command == "subscription-radar":
            client = _make_client(args)
            out = scan_subscription_radar(client, max_items=args.max_items, redact_pii=args.redact_pii)
        elif args.command == "webhook-audit":
            client = _make_client(args)
            out = audit_webhooks(client, expected_url=args.expected_url)
        elif args.command == "webhook-upsert":
            client = _make_client(args)
            ev_list = [x.strip() for x in args.events.split(",") if x.strip()] if args.events else None
            out = upsert_webhook_endpoint(
                client,
                url=args.url,
                endpoint_id=args.endpoint_id,
                events=ev_list,
                description=args.description,
                apply=args.apply,
                confirm_plan=args.confirm_plan,
                allow_live=args.live,
            )
        elif args.command == "webhook-probe":
            out = probe_unsigned_webhook_gate(args.url)
        elif args.command == "sign-webhook":
            payload_str = _load_payload_arg(args.payload)
            out = sign_stripe_webhook_payload(payload_str, args.secret, timestamp=args.timestamp)
        elif args.command == "verify-webhook":
            payload_str = _load_payload_arg(args.payload)
            out = verify_stripe_webhook_signature(
                payload_str,
                args.signature,
                args.secret,
                tolerance_seconds=args.tolerance,
                now_timestamp=args.now,
            )
            if not out.get("valid"):
                sys.stdout.write(json.dumps(out, indent=2) + "\n")
                return 1
        elif args.command == "custom-domain-check":
            out = check_custom_domain(args.hostname)
        elif args.command == "blueprint":
            if args.list or not args.name:
                out = list_blueprints()
            else:
                bp = get_blueprint(args.name)
                if args.output:
                    saved = _write_output_file(args.output, bp["code"], overwrite=args.overwrite)
                    out = {"ok": True, "name": args.name, "written": saved}
                elif args.raw:
                    sys.stdout.write(bp["code"])
                    return 0
                else:
                    out = bp
        else:
            raise StripeError(f"Unhandled command: {args.command}")

        sys.stdout.write(json.dumps(out, indent=2) + "\n")
        return 0
    except StripeError as exc:
        sys.stdout.write(json.dumps(exc.to_dict(), indent=2) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
