"""Generate additive, local-only onboarding contracts and deterministic vectors."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "shared" / "onboarding"
UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
COUNT = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
BOOL = {"type": "boolean"}
DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
JOURNEY = "11111111-1111-4111-8111-111111111111"
EPOCH = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"


def enum(*values):
    return {"enum": list(values)}


def closed(**properties):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


def profile(name, **properties):
    return closed(profile={"const": name + ".v1"}, **properties)


def build():
    known = enum("unknown", "missing", "verified")
    scope = dict(account_generation=COUNT, consent_generation=COUNT)
    pending = {"oneOf": [{"type": "null"}, closed(operation_id=UUID,
        status=enum("pending", "unknown"))]}
    facts = {
        "brain": closed(installation=known, enrollment=known, pairing=known,
                        activation=known, analysis=known),
        "extension": closed(mode=enum("off", "preview", "full"),
            consent=enum("missing", "valid", "review_required"),
            site_access=enum("missing", "granted"),
            account=enum("unknown", "matching", "mismatch"),
            attachment=enum("checking", "ready", "blocked"),
            capture=enum("off", "active", "paused")),
    }
    reasons = {
        "brain": enum("none", "app_unreachable", "enrollment_required", "pairing_required",
            "activation_required", "authorization_expired", "authorization_revoked", "operation_unconfirmed"),
        "extension": enum("none", "consent_required", "permission_required", "signin_required",
            "wrong_account", "attachment_unavailable", "helper_closed", "paused", "operation_unconfirmed"),
    }
    variants = []
    for owner in facts:
        variants.append(profile("local-onboarding-state", kind=enum("snapshot", "event"),
            journey_id=UUID, source={"const": owner}, epoch=UUID, revision=COUNT,
            **scope, facts=facts[owner], pending_operation=pending, reason=reasons[owner]))
    for owner, actions in {"brain": ("pair", "cancel_pairing"),
                           "extension": ("pause", "resume", "reopen_helper")}.items():
        variants.append(profile("local-onboarding-command", journey_id=UUID,
            operation_id=UUID, owner={"const": owner}, action=enum(*actions), **scope))
    variants.append(profile("local-onboarding-result", journey_id=UUID, operation_id=UUID,
        source=enum("brain", "extension"), epoch=UUID, revision=COUNT, **scope,
        status=enum("confirmed", "rejected", "unknown"),
        reason=enum("none", "stale_scope", "not_authorized", "not_ready", "unconfirmed")))
    variants.append(profile("local-installation-discovery", hint={"const": "extension_present"},
        version={"const": 1}))
    draft_scope = closed(scope_id=UUID, disclosure_bundle_id=DIGEST)
    draft = closed(terms_checked=BOOL, risk_checked=BOOL, full_checked=BOOL)
    variants.append(profile("local-onboarding-workspace", version={"const": 1},
        journey_id=UUID, route=enum("extension", "hosted", "provisioning", "bridge"),
        draft_scope=draft_scope, draft=draft))
    variants.append(profile("local-onboarding-continuation", journey_id=UUID,
        continuation_id=UUID, target=enum("provisioning", "bridge"),
        purpose=enum("controlled_restart", "first_enrollment")))
    variants.append(profile("local-first-enrollment-result", status={"const": "registered"},
        # Delivered only over authenticated local HTTPS/loopback; not a navigation field.
        csrf_token={"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}))
    schema = {"$schema": "http://json-schema.org/draft-07/schema#",
              "$id": "urn:ofca:local-onboarding:v1", "oneOf": variants}
    cases = []

    def case(name, value, valid=True):
        cases.append({"id": name, "valid": valid, "value": copy.deepcopy(value)})

    for owner in facts:
        value = dict(profile="local-onboarding-state.v1", kind="snapshot", journey_id=JOURNEY,
            source=owner, epoch=EPOCH, revision=0, account_generation=1, consent_generation=2,
            facts=({key: "verified" for key in facts[owner]["properties"]} if owner == "brain" else
                   dict(mode="preview", consent="valid", site_access="granted", account="matching",
                        attachment="ready", capture="active")), pending_operation=None, reason="none")
        case(owner + "-snapshot", value)
        value["kind"] = "event"
        value["revision"] = 1
        value["pending_operation"] = {"operation_id": OPERATION, "status": "unknown"}
        case(owner + "-event-unknown-outcome", value)
        for key, bad in {"revision": -1, "epoch": "old", "source": "hosted", "reason": "reload_required",
                         "account_generation": True, "consent_generation": 9007199254740992}.items():
            case(owner + "-invalid-" + key, {**value, key: bad}, False)
        case(owner + "-wrong-owner-facts", {**value, "facts": {"commercial_balance": 1}}, False)
        case(owner + "-unscoped-extra", {**value, "copy": "Ready"}, False)
    for owner, action in (("brain", "pair"), ("extension", "pause")):
        value = dict(profile="local-onboarding-command.v1", journey_id=JOURNEY, operation_id=OPERATION,
                     owner=owner, action=action, account_generation=1, consent_generation=2)
        case(owner + "-command", value)
        case(owner + "-wrong-action", {**value, "action": "accept_terms"}, False)
        case(owner + "-result-as-command", {**value, "status": "confirmed"}, False)
    for status in ("confirmed", "rejected", "unknown"):
        case("result-" + status, dict(profile="local-onboarding-result.v1", journey_id=JOURNEY,
             operation_id=OPERATION, source="extension", epoch=EPOCH, revision=1,
             account_generation=1, consent_generation=2, status=status, reason="none"))
    discovery = dict(profile="local-installation-discovery.v1", hint="extension_present", version=1)
    case("discovery", discovery)
    for field in ("account", "installed", "session", "grant", "metadata"):
        case("discovery-reject-" + field, {**discovery, field: "value"}, False)
    workspace = dict(profile="local-onboarding-workspace.v1", version=1, journey_id=JOURNEY,
        route="extension", draft_scope=dict(scope_id=EPOCH, disclosure_bundle_id="a" * 64),
        draft=dict(terms_checked=True, risk_checked=True, full_checked=False))
    case("workspace-draft-not-consent", workspace)
    case("workspace-url-injection", {**workspace, "route": "https://untrusted.example"}, False)
    case("workspace-authority-injection", {**workspace, "consent": "accepted"}, False)
    case("workspace-foreign-draft", {**workspace, "draft": {**workspace["draft"], "account": "alice"}}, False)
    for purpose in ("controlled_restart", "first_enrollment"):
        value = dict(profile="local-onboarding-continuation.v1", journey_id=JOURNEY,
            continuation_id=OPERATION, target="bridge", purpose=purpose)
        case("continuation-" + purpose, value)
        case("continuation-secret-" + purpose, {**value, "session_token": "not-allowed"}, False)
    case("enrollment-result", dict(profile="local-first-enrollment-result.v1", status="registered", csrf_token="a" * 43))
    case("unknown-version", {**discovery, "profile": "local-installation-discovery.v2"}, False)
    return {"schema.json": schema, "vectors.json": {"version": 1, "cases": cases}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = {name: (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
              for name, value in build().items()}
    manifest = {"profile": "local-onboarding-bundle.v1", "max_message_bytes": 4096,
        "classification": "local-only", "files": {name: hashlib.sha256(data).hexdigest()
                                                   for name, data in output.items()}}
    output["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, data in output.items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                raise SystemExit(f"Generated onboarding artifact differs: {name}")
        else:
            path.write_bytes(data)


if __name__ == "__main__":
    main()
