"""Release-owned, nonsecret customer entry points for packaged first run."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


CUSTOMER_RELEASE_SCHEMA = "ofca-customer-release/v1"
CUSTOMER_RELEASE_PATH = Path(__file__).with_name("customer-release.json")


class CustomerReleaseConfigurationError(ValueError):
    """Raised when release-owned customer routing is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class CustomerReleaseConfig:
    hosted_onboarding_url: str
    hosted_api_origin: str

    @property
    def hosted_configured(self) -> bool:
        return bool(self.hosted_onboarding_url and self.hosted_api_origin)


def _https_url(value: object, *, name: str, origin_only: bool) -> str:
    if not isinstance(value, str):
        raise CustomerReleaseConfigurationError(f"{name} must be a string")
    if value == "":
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.hostname.endswith(".invalid")
    ):
        raise CustomerReleaseConfigurationError(f"{name} must be a credential-free HTTPS URL")
    if origin_only and parsed.path not in {"", "/"}:
        raise CustomerReleaseConfigurationError(f"{name} must be an HTTPS origin")
    path = "" if origin_only else (parsed.path or "/")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def validate_customer_release_document(
    document: object,
    *,
    require_hosted: bool = False,
) -> CustomerReleaseConfig:
    if not isinstance(document, Mapping) or set(document) != {
        "schema",
        "hosted_onboarding_url",
        "hosted_api_origin",
    }:
        raise CustomerReleaseConfigurationError("customer release document has an invalid shape")
    if document["schema"] != CUSTOMER_RELEASE_SCHEMA:
        raise CustomerReleaseConfigurationError("customer release schema is unsupported")

    onboarding_url = _https_url(
        document["hosted_onboarding_url"],
        name="hosted_onboarding_url",
        origin_only=False,
    )
    api_origin = _https_url(
        document["hosted_api_origin"],
        name="hosted_api_origin",
        origin_only=True,
    )
    if bool(onboarding_url) != bool(api_origin):
        raise CustomerReleaseConfigurationError(
            "hosted onboarding URL and API origin must be configured together"
        )
    if require_hosted and not onboarding_url:
        raise CustomerReleaseConfigurationError(
            "a production release requires hosted customer onboarding"
        )
    return CustomerReleaseConfig(onboarding_url, api_origin)


def load_customer_release_config(
    path: str | Path = CUSTOMER_RELEASE_PATH,
    *,
    require_hosted: bool = False,
) -> CustomerReleaseConfig:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CustomerReleaseConfigurationError(
            "customer release configuration is unavailable"
        ) from error
    return validate_customer_release_document(document, require_hosted=require_hosted)


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
                "hosted_onboarding_url": config.hosted_onboarding_url,
                "hosted_api_origin": config.hosted_api_origin,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
