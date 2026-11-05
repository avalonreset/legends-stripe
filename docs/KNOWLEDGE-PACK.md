# Knowledge pack: attach and update

`legends-stripe` ships an attachable Empire knowledge pack. It is a versioned reference shelf, not a vault and not a standalone skill.

## Pack identity

- Manifest: `knowledge/stripe/pack.json`
- Schema: `legends.knowledge-pack/v1`
- Id: `stripe`
- Module: `legends-stripe`
- Version: `0.1.1`
- Entrypoint: `index.md`
- Suggested home: `wiki/library/legends-stripe/stripe`
- Ontology kind: `library`
- Files: declared Markdown under sibling `vault/`, each pinned by SHA-256

## Ownership

| Material | Location | Update behavior |
|---|---|---|
| Released knowledge | Installed module `knowledge/stripe/vault/` | Updated with the module |
| Attached reference snapshot | Selected Empire shelf `reference/` | Updated only when the last shipped bytes are unchanged |
| User knowledge | Shelf `index.md` plus user project, receipt, and treasury notes | Never overwritten by pack updates |

## Attach or update

1. Resolve the manifest path in machine-readable form:

   ```bash
   legends-stripe resources
   ```

   Read `pack_manifest` (absolute path to `pack.json`) and confirm `complete` is `true`.
2. Preview attachment into an Empire vault using `legends-empire`:

   ```bash
   cto-legends run legends-empire -- knowledge attach /abs/path/to/pack.json --vault /abs/path/to/Empire --operation-id attach-stripe-001 --generated-at 2026-11-05T12:00:00Z
   ```

3. Review the proposed transaction and apply with `--apply --approved-plan-sha256 <reviewed-hash>` when authorized.

## Private work templates

The pack includes reusable templates under `vault/templates/`:
- [private-operation-receipt.md](../templates/private-operation-receipt.md): dated operational evidence for Stripe catalog or webhook changes.
- [treasury-telemetry.md](../templates/treasury-telemetry.md): cent-level treasury reconciliation and Stripe Drag report template.
- [webhook-fortification-receipt.md](../templates/webhook-fortification-receipt.md): proof of unsigned gate rejection, HMAC-SHA256 verification, and Soft-Land lifecycle coverage.
