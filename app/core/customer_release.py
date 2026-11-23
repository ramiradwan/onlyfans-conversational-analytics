"""Release-owned API routing, setup transfer and public support display."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


CUSTOMER_RELEASE_SCHEMA = "ofca-customer-release/v2"
CUSTOMER_RELEASE_PATH = Path(__file__).with_name("customer-release.json")
DEVELOPMENT_CUSTOMER_RELEASE_ENV = "LOCAL_CUSTOMER_RELEASE_CONFIG"
HOSTED_ORIGIN_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HOSTED_ORIGIN"
SUPPORT_CHANNELS = ("whatsapp", "telegram", "tiktok")
_REQUIRED_FIELDS = {
    "schema", "setup_transfer_receiver_url", "hosted_api_origin", "authentication_providers",
}
_LOCAL_SUFFIXES = {
    "alt", "arpa", "corp", "example", "home", "internal", "intranet", "invalid",
    "lan", "local", "localdomain", "localhost", "onion", "private", "test",
}
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SUPPORT_ADDRESS = re.compile(
    rf"https://(?P<host>(?:{_DNS_LABEL}\.)+{_DNS_LABEL})(?::(?P<port>[0-9]{{1,5}}))?"
    r"(?P<path>/(?:[a-z0-9._~!$&'()*+,;=:@/-]|%[0-9a-f]{2})*)?",
    re.IGNORECASE | re.ASCII,
)
_UNRESERVED = re.compile(r"[A-Za-z0-9._~-]")


class CustomerReleaseConfigurationError(ValueError):
    """Raised when release-owned customer routing is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class CustomerReleaseConfig:
    setup_transfer_receiver_url: str
    hosted_api_origin: str
    authentication_providers: tuple[str, ...] = ("default",)
    support_channels: tuple[tuple[str, str], ...] = ()

    @property
    def hosted_configured(self) -> bool:
        return bool(self.hosted_api_origin)

    @property
    def setup_transfer_configured(self) -> bool:
        return bool(self.setup_transfer_receiver_url)

    @property
    def has_microsoft_picker(self) -> bool:
        """The ordinary sign-in page at this release origin was verified to offer Microsoft."""
        return self.hosted_api_origin == "https://onboarding-dev.dipsy.fi"


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


def _support_address(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    match = _SUPPORT_ADDRESS.fullmatch(value)
    if not match:
        return None
    host = match["host"].lower()
    suffix = host.rsplit(".", 1)[-1]
    # Require a DNS name and refuse special-use suffixes without resolving it.
    if len(host) > 253 or not suffix[0].isalpha() or suffix in _LOCAL_SUFFIXES:
        return None
    port = int(match["port"]) if match["port"] is not None else 443
    if not 1 <= port <= 65535:
        return None

    def canonical_escape(escape: re.Match[str]) -> str:
        character = chr(int(escape[0][1:], 16))
        if not "!" <= character <= "~" or character == "\\":
            raise ValueError
        return character if _UNRESERVED.fullmatch(character) else escape[0].upper()

    try:
        path = re.sub(r"%[0-9a-fA-F]{2}", canonical_escape, match["path"] or "/")
    except ValueError:
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    authority = host if port == 443 else f"{host}:{port}"
    return f"https://{authority}{path}"


def _support_channels(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple((channel, address) for channel in SUPPORT_CHANNELS
                 if (address := _support_address(value.get(channel))) is not None)


def validate_customer_release_document(
    document: object,
    *,
    require_hosted: bool = False,
) -> CustomerReleaseConfig:
    if (not isinstance(document, Mapping)
            or set(document) - {"support_channels"} != _REQUIRED_FIELDS):
        raise CustomerReleaseConfigurationError("customer release document has an invalid shape")
    if document["schema"] != CUSTOMER_RELEASE_SCHEMA:
        raise CustomerReleaseConfigurationError("customer release schema is unsupported")
    providers = document["authentication_providers"]
    if (not isinstance(providers, list) or not providers or any(type(value) is not str for value in providers)
            or len(set(providers)) != len(providers) or "default" not in providers
            or not set(providers) <= {"google", "microsoft-personal", "telegram", "tiktok", "default"}):
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
    return CustomerReleaseConfig(receiver_url, api_origin, tuple(providers),
                                 _support_channels(document.get("support_channels")))


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
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
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
