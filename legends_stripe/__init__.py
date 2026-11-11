"""legends-stripe: portable Stripe payments, catalog governance, treasury telemetry, and webhook fortification CLI."""

__version__ = "0.1.2"

from .api import StripeClient, StripeError, classify_key, classify_stripe_key, load_dotenv_file, scrub_secrets
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

__all__ = [
    "__version__",
    "StripeClient",
    "StripeError",
    "classify_key",
    "classify_stripe_key",
    "load_dotenv_file",
    "scrub_secrets",
    "audit_env_file",
    "build_price_map",
    "build_treasury_telemetry",
    "render_telemetry_markdown",
    "audit_catalog",
    "export_catalog_spec",
    "generate_ladder_spec",
    "diff_catalog_spec",
    "sync_catalog_spec",
    "upsert_product",
    "run_checkout_canary",
    "scan_subscription_radar",
    "audit_webhooks",
    "upsert_webhook_endpoint",
    "probe_unsigned_webhook_gate",
    "sign_stripe_webhook_payload",
    "verify_stripe_webhook_signature",
    "check_custom_domain",
    "list_blueprints",
    "get_blueprint",
]
