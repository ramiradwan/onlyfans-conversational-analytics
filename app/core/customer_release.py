"""Release-owned API routing and optional explicit setup-transfer receiver."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


CUSTOMER_RELEASE_SCHEMA = "ofca-customer-release/v2"
CUSTOMER_RELEASE_PATH = Path(__file__).with_name("customer-release.json")
DEVELOPMENT_CUSTOMER_RELEASE_ENV = "LOCAL_CUSTOMER_RELEASE_CONFIG"
HOSTED_ORIGIN_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HOSTED_ORIGIN"


class CustomerReleaseConfigurationError(ValueError):
    """Raised when release-owned customer routing is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class CustomerReleaseConfig:
    setup_transfer_receiver_url: str
    hosted_api_origin: str
    authentication_providers: tuple[str, ...] = ("default",)

    @property
    def hosted_configured(self) -> bool:
        return bool(self.hosted_api_origin)

    @property
    def setup_transfer_configured(self) -> bool:
        return bool(self.setup_transfer_receiver_url)


def _https_url(value: object, *, name: str, origin_only: bool) -> str:
    if not isinstance(value, str):
        raise CustomerReleaseConfigurationError(f"{name} must be a string")
    if value == "":
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise CustomerReleaseConfigurationError(f"{name} has an invalid authority") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.hostname.endswith(".invalid")
        or "*" in parsed.netloc
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise CustomerReleaseConfigurationError(f"{name} must be a credential-free HTTPS URL")
    if origin_only and parsed.path not in {"", "/"}:
        raise CustomerReleaseConfigurationError(f"{name} must be an HTTPS origin")
    path = "" if origin_only else (parsed.path or "/")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host + (f":{port}" if port is not None and port != 443 else "")
    return urlunsplit(("https", authority, path, "", ""))


def validate_customer_release_document(
    document: object,
    *,
    require_hosted: bool = False,
) -> CustomerReleaseConfig:
    if not isinstance(document, Mapping) or set(document) != {
        "schema",
        "setup_transfer_receiver_url",
        "hosted_api_origin",
        "authentication_providers",
    }:
        raise CustomerReleaseConfigurationError("customer release document has an invalid shape")
    if document["schema"] != CUSTOMER_RELEASE_SCHEMA:
        raise CustomerReleaseConfigurationError("customer release schema is unsupported")
    providers = document["authentication_providers"]
    if (not isinstance(providers, list) or not providers or any(type(value) is not str for value in providers)
            or len(set(providers)) != len(providers) or "default" not in providers
            or not set(providers) <= {"google", "microsoft-personal", "default"}):
        raise CustomerReleaseConfigurationError("customer authentication providers are invalid")

    receiver_url = _https_url(
        document["setup_transfer_receiver_url"],
        name="setup_transfer_receiver_url",
        origin_only=False,
    )
    api_origin = _https_url(
        document["hosted_api_origin"],
        name="hosted_api_origin",
        origin_only=True,
    )
    if receiver_url:
        receiver = urlsplit(receiver_url)
        if (not api_origin or receiver.path != "/public/onboarding/setup"
                or urlunsplit((receiver.scheme, receiver.netloc, "", "", "")) != api_origin):
            raise CustomerReleaseConfigurationError(
                "setup transfer receiver must be the API origin's explicit receiving page"
            )
    if require_hosted and not api_origin:
        raise CustomerReleaseConfigurationError(
            "a production release requires a hosted API origin"
        )
    return CustomerReleaseConfig(receiver_url, api_origin, tuple(providers))


def load_customer_release_config(
    path: str | Path | None = None,
    *,
    require_hosted: bool = False,
) -> CustomerReleaseConfig:
    if path is None:
        path = CUSTOMER_RELEASE_PATH
        # A source qualification run may bind a complete release artifact without
        # editing the production template. Frozen packages always use their bundle.
        if not getattr(sys, "frozen", False):
            path = os.environ.get(DEVELOPMENT_CUSTOMER_RELEASE_ENV) or path
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CustomerReleaseConfigurationError(
            "customer release configuration is unavailable"
        ) from error
    return validate_customer_release_document(document, require_hosted=require_hosted)


def resolve_hosted_api_origin(config: CustomerReleaseConfig | None = None) -> str:
    """Resolve the release origin with a local environment fallback."""
    if config is None:
        config = load_customer_release_config()
    return config.hosted_api_origin or os.environ.get(HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CUSTOMER_RELEASE_PATH)
    parser.add_argument("--require-hosted", action="store_true")
    arguments = parser.parse_args()
    config = load_customer_release_config(
        arguments.config,
        require_hosted=arguments.require_hosted,
    )
    print(
        json.dumps(
            {
                "schema": CUSTOMER_RELEASE_SCHEMA,
                "setup_transfer_receiver_url": config.setup_transfer_receiver_url,
                "hosted_api_origin": config.hosted_api_origin,
                "authentication_providers": list(config.authentication_providers),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
