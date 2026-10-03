"""Release document precedence at the hosted origin composition points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat, pytest.mark.serial]

from app.core import customer_release

ORIGIN = "https://api.example.com"
OTHER_ORIGIN = "https://other.example.com"


def _release_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    path = tmp_path / "customer-release.json"
    path.write_text(
        json.dumps(
            {
                "schema": customer_release.CUSTOMER_RELEASE_SCHEMA,
                "hosted_onboarding_url": (
                    "https://setup.example.com/start" if origin else ""
                ),
                "hosted_api_origin": origin,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(customer_release.DEVELOPMENT_CUSTOMER_RELEASE_ENV, str(path))


@pytest.mark.parametrize(
    ("bundled_origin", "environment_origin", "expected"),
    [
        (ORIGIN, None, ORIGIN),
        (ORIGIN, OTHER_ORIGIN, ORIGIN),
        ("", OTHER_ORIGIN, OTHER_ORIGIN),
        ("", None, ""),
    ],
    ids=["release-only", "release-over-environment", "environment-only", "empty"],
)
def test_resolver_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundled_origin: str,
    environment_origin: str | None,
    expected: str,
) -> None:
    _release_document(tmp_path, monkeypatch, bundled_origin)
    if environment_origin is None:
        monkeypatch.delenv("LOCAL_PROVISIONING_HOSTED_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("LOCAL_PROVISIONING_HOSTED_ORIGIN", environment_origin)

    assert customer_release.resolve_hosted_api_origin() == expected


_COMPOSITION_PROGRAM = r"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

mode, environment_origin, temp_parent = sys.argv[1:]
with tempfile.TemporaryDirectory(dir=temp_parent) as temporary:
    root = Path(temporary)
    os.environ["TEMP"] = temporary
    os.environ["TMP"] = temporary
    os.environ["LOCAL_ANALYTICS_DATA_DIR"] = str(root / "runtime")
    os.environ["CANONICAL_PERSISTENCE_BACKEND"] = "memory"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["WEBSOCKET_AUTH_MODE"] = "development_stub"
    os.environ.pop("LOCAL_PROVISIONING_HANDOFF_TOKEN", None)
    os.environ.pop("LOCAL_CUSTOMER_RELEASE_CONFIG", None)
    if environment_origin == "unset":
        os.environ.pop("LOCAL_PROVISIONING_HOSTED_ORIGIN", None)
    else:
        os.environ["LOCAL_PROVISIONING_HOSTED_ORIGIN"] = environment_origin

    from app.core import customer_release

    release = root / "customer-release.json"
    release.write_text(json.dumps({
        "schema": customer_release.CUSTOMER_RELEASE_SCHEMA,
        "hosted_onboarding_url": "https://setup.example.com/start",
        "hosted_api_origin": "https://api.example.com",
    }), encoding="utf-8")
    os.environ[customer_release.DEVELOPMENT_CUSTOMER_RELEASE_ENV] = str(release)
    captured = {}

    if mode == "invalid":
        import pytest

        release.write_text("{invalid", encoding="utf-8")
        with pytest.raises(
            customer_release.CustomerReleaseConfigurationError, match="unavailable"
        ):
            customer_release.resolve_hosted_api_origin()
        captured["resolver_error"] = True

        import app.provisioning.progress_reporting as progress_reporting

        progress_reporting.configured_runtime_onboarding_progress.cache_clear()
        try:
            with pytest.raises(
                customer_release.CustomerReleaseConfigurationError, match="unavailable"
            ):
                progress_reporting.configured_runtime_onboarding_progress()
            captured["progress_error"] = True
        finally:
            progress_reporting.configured_runtime_onboarding_progress.cache_clear()
    elif mode == "runtime":
        import app.core.resource_paths as resource_paths

        original_resource_path = resource_paths.resource_path
        dist = root / "dist"
        dist.mkdir()
        resource_paths.resource_path = lambda reference, **kwargs: (
            dist if str(reference) == "app/static/dist"
            else original_resource_path(reference, **kwargs)
        )

        from app.core.runtime_paths import runtime_configuration_file
        from app.packaged_entry import select_brain_application

        runtime = root / "runtime"
        runtime.mkdir()
        runtime_configuration_file(runtime).touch()
        application = select_brain_application(runtime)
        import app.main as main
        import app.provisioning.claim_submission as claim_submission
        import app.provisioning.progress_reporting as progress_reporting
        import app.security.capability_license_composition as delivery_composition
        import app.security.capability_license_redemption as redemption_composition

        assert application is main.app
        progress_factory = progress_reporting.durable_onboarding_progress
        delivery_composition.CapabilityLicenseHTTPTransport = (
            lambda base_url, *, journal: captured.setdefault("delivery", base_url)
        )
        redemption_composition.CapabilityLicenseHTTPTransport = (
            lambda base_url, *, journal: captured.setdefault("redemption", base_url)
        )
        claim_submission.HTTPXHostedTransport = (
            lambda base_url: captured.setdefault("refresh", base_url)
        )
        progress_reporting.HTTPXHostedTransport = (
            lambda base_url: captured.setdefault("progress", base_url)
        )
        progress_reporting.WindowsCNGInstallationKeyProvider = lambda: object()
        progress_reporting.InstallationKeyAuthority = lambda store, provider: object()
        progress_reporting.HostedGrantClient = lambda transport, authority, store: object()

        def capture_delivery(open_store, *, hosted_origin):
            delivery_composition._production_transport(hosted_origin, object())
            return object()

        def capture_redemption(open_store, *, hosted_origin):
            redemption_composition._production_transport(hosted_origin, object())
            return object()

        def capture_refresh(open_store, *, hosted_origin, **kwargs):
            claim_submission.hosted_transport(hosted_origin)
            return SimpleNamespace(start=lambda: None)

        def capture_progress(open_store, *, hosted_origin):
            coordinator = progress_factory(open_store, hosted_origin=hosted_origin)
            coordinator._open_client(object())
            return coordinator

        main.durable_capability_license_delivery = capture_delivery
        main.durable_capability_license_opaque_redemption = capture_redemption
        main.configured_grant_refresh = capture_refresh
        main.settings = SimpleNamespace(
            identity_binding_source="verified_grants",
            websocket_auth_mode="local_session",
        )
        main._grant_refresh = None
        progress_reporting.durable_onboarding_progress = capture_progress
        progress_reporting.configured_runtime_onboarding_progress.cache_clear()
        main.configure_capability_license_delivery()
        main.start_grant_refresh()
        progress_reporting.configured_runtime_onboarding_progress()
    else:
        import app.provisioning.app as provisioning_app
        import app.provisioning.binding_acquisition as binding_acquisition
        import app.provisioning.claim_submission as claim_submission
        import app.provisioning.completion as completion
        import app.provisioning.creator_association as creator_association
        import app.security.capability_license_composition as delivery_composition
        import app.security.grant_refresh as grant_refresh
        from app.packaged_entry import select_brain_application

        completion.durable_authentication_store = lambda directory: lambda: object()
        provisioning_app.create_provisioning_app = lambda **kwargs: object()

        def capture(name, result):
            def constructor(open_store, *, hosted_origin, **kwargs):
                captured[name] = hosted_origin
                return result
            return constructor

        claim_submission.durable_claim_submission = capture("claim", object())
        creator_association.durable_creator_association_initiation = capture(
            "association", object()
        )
        binding_acquisition.durable_creator_account_binding_acquisition = capture(
            "binding", object()
        )
        grant_refresh.configured_grant_refresh = capture(
            "refresh", SimpleNamespace(stop=lambda: None)
        )
        delivery_composition.durable_capability_license_delivery = capture(
            "delivery", object()
        )
        select_brain_application(root / "runtime")

    print(json.dumps(captured))
"""


def _compose_in_child(
    tmp_path: Path, mode: str, environment_origin: str | None
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _COMPOSITION_PROGRAM,
            mode,
            environment_origin if environment_origin is not None else "unset",
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize(
    "environment_origin",
    [None, OTHER_ORIGIN],
    ids=["environment-unset", "environment-set"],
)
def test_runtime_composes_all_hosted_actions_with_bundled_origin(
    tmp_path: Path, environment_origin: str | None
) -> None:
    assert _compose_in_child(tmp_path, "runtime", environment_origin) == {
        "delivery": ORIGIN,
        "redemption": ORIGIN,
        "refresh": ORIGIN,
        "progress": ORIGIN,
    }


def test_provisioning_composes_all_hosted_actions_with_bundled_origin(
    tmp_path: Path,
) -> None:
    assert _compose_in_child(tmp_path, "provisioning", OTHER_ORIGIN) == {
        "claim": ORIGIN,
        "association": ORIGIN,
        "binding": ORIGIN,
        "refresh": ORIGIN,
        "delivery": ORIGIN,
    }


def test_invalid_release_document_propagates_at_runtime_progress_construction(
    tmp_path: Path,
) -> None:
    assert _compose_in_child(tmp_path, "invalid", None) == {
        "resolver_error": True,
        "progress_error": True,
    }
