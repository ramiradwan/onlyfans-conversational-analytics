from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.launcher import (
    BRIDGE_ORIGIN,
    HANDOFF_PATH,
    LaunchFailure,
    Launcher,
    LauncherConfiguration,
    ListenerOwner,
    PROVISIONING_COMPLETE_EXIT_CODE,
    PROVISIONING_HANDOFF_PATH,
    PROVISIONING_REDEEM_PATH,
    WindowsPortOwnership,
    _start_brain,
)
from app.core.runtime_paths import runtime_configuration_file
from app.packaged_entry import (
    PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE,
    PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: Any
    headers: dict[str, str]

    def json(self) -> Any:
        return self.payload


class FakeClient:
    def __init__(
        self,
        response: FakeResponse,
        requests: list[tuple[str, dict[str, str]]],
        events: list[str],
    ) -> None:
        self.response = response
        self.requests = requests
        self.events = events
        self.cookies: dict[str, str] = {}

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, path: str, *, headers: dict[str, str]) -> FakeResponse:
        self.events.append("post")
        self.requests.append((path, headers))
        return self.response


class FakeOwnership:
    def __init__(
        self,
        listener_results: list[list[ListenerOwner]],
        events: list[str],
        sid: str = "S-1-5-21-current",
    ) -> None:
        self.listener_results = listener_results
        self.events = events
        self.sid = sid

    def listeners(self, port: int) -> list[ListenerOwner]:
        assert port == 17871
        self.events.append("ownership")
        if len(self.listener_results) > 1:
            return self.listener_results.pop(0)
        return self.listener_results[0]

    def current_user_sid(self) -> str:
        self.events.append("sid")
        return self.sid


class FakeProcess:
    pid = 8123

    def poll(self) -> None:
        return None


class CompletedProvisioningProcess:
    pid = 8124

    def __init__(self, configuration: LauncherConfiguration) -> None:
        self.configuration = configuration
        self.completed = False

    def poll(self) -> int:
        if not self.completed:
            configuration_file = runtime_configuration_file(self.configuration.data_directory)
            configuration_file.parent.mkdir(parents=True)
            configuration_file.touch()
            self.completed = True
        return PROVISIONING_COMPLETE_EXIT_CODE


def configuration(tmp_path: Path) -> LauncherConfiguration:
    result = LauncherConfiguration(
        brain_command=(str((tmp_path / "Brain.exe").resolve()), "--serve"),
        brain_image_path=(tmp_path / "Brain.exe").resolve(),
        working_directory=tmp_path.resolve(),
        data_directory=(tmp_path / "data").resolve(),
        startup_timeout_seconds=1,
    )
    runtime_configuration_file(result.data_directory).parent.mkdir(parents=True)
    runtime_configuration_file(result.data_directory).touch()
    return result


def trusted_owner(config: LauncherConfiguration) -> ListenerOwner:
    return ListenerOwner(
        pid=4412,
        local_address="127.0.0.1",
        image_path=config.brain_image_path,
        user_sid="S-1-5-21-current",
    )


def launcher_with_response(
    tmp_path: Path,
    response: FakeResponse,
    *,
    owners: list[list[ListenerOwner]] | None = None,
) -> tuple[Launcher, FakeClient, list[str], list[str], list[tuple[str, dict[str, str]]]]:
    config = configuration(tmp_path)
    events: list[str] = []
    browser_urls: list[str] = []
    requests: list[tuple[str, dict[str, str]]] = []
    client = FakeClient(response, requests, events)
    ownership = FakeOwnership(owners or [[trusted_owner(config)]], events)
    launcher = Launcher(
        config,
        ownership=ownership,
        process_starter=lambda _: (_ for _ in ()).throw(
            AssertionError("verified Brain must be reused")
        ),
        client_factory=lambda: client,
        browser_open=lambda url: browser_urls.append(url) is None or True,
        credential_loader=lambda _: "b" * 40,
    )
    return launcher, client, events, browser_urls, requests


def test_verified_owner_is_reused_and_launcher_cookie_jar_stays_empty(
    tmp_path: Path,
) -> None:
    code = "h" * 43
    launcher, client, events, browser_urls, requests = launcher_with_response(
        tmp_path,
        FakeResponse(200, {"handoff_code": code}, {"cache-control": "no-store"}),
    )

    target = launcher.launch()

    assert target == f"{BRIDGE_ORIGIN}{HANDOFF_PATH}?code={code}"
    assert browser_urls == [target]
    assert requests == [
        (HANDOFF_PATH, {"Authorization": "Bootstrap " + "b" * 40})
    ]
    assert client.cookies == {}
    assert events == ["ownership", "sid", "post"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership API")
def test_windows_inspector_reads_kernel_listener_image_and_sid() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        inspector = WindowsPortOwnership()

        owners = inspector.listeners(port)

    assert len(owners) == 1
    assert owners[0].pid == os.getpid()
    assert owners[0].local_address == "127.0.0.1"
    expected_image = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    assert owners[0].image_path == expected_image
    assert owners[0].user_sid == inspector.current_user_sid()


def test_second_launch_with_consumed_bootstrap_opens_plain_origin(
    tmp_path: Path,
) -> None:
    launcher, client, _, browser_urls, requests = launcher_with_response(
        tmp_path,
        FakeResponse(
            409,
            {"detail": "launcher_bootstrap_already_consumed"},
            {"cache-control": "no-store"},
        ),
    )

    assert launcher.launch() == BRIDGE_ORIGIN
    assert browser_urls == [BRIDGE_ORIGIN]
    assert len(requests) == 1
    assert client.cookies == {}


@pytest.mark.parametrize(
    ("address", "image_name", "sid"),
    [
        ("0.0.0.0", "Brain.exe", "S-1-5-21-current"),
        ("127.0.0.1", "Other.exe", "S-1-5-21-current"),
        ("127.0.0.1", "Brain.exe", "S-1-5-21-other"),
    ],
)
def test_mismatched_port_owner_sends_no_request_and_is_not_terminated(
    tmp_path: Path,
    address: str,
    image_name: str,
    sid: str,
) -> None:
    config = configuration(tmp_path)
    owner = ListenerOwner(
        pid=9912,
        local_address=address,
        image_path=(tmp_path / image_name).resolve(),
        user_sid=sid,
    )
    requests: list[tuple[str, dict[str, str]]] = []
    browser_urls: list[str] = []
    factory_calls: list[str] = []
    launcher = Launcher(
        config,
        ownership=FakeOwnership([[owner]], []),
        process_starter=lambda _: (_ for _ in ()).throw(
            AssertionError("occupied port must not start Brain")
        ),
        client_factory=lambda: factory_calls.append("created"),
        browser_open=lambda url: browser_urls.append(url) is None or True,
        credential_loader=lambda _: "b" * 40,
    )

    with pytest.raises(LaunchFailure) as raised:
        launcher.launch()

    assert raised.value.code == "port_conflict"
    assert "did not terminate it" in str(raised.value)
    assert factory_calls == []
    assert requests == []
    assert browser_urls == []


def test_absent_listener_starts_brain_then_verifies_owner_before_handoff(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    owner = trusted_owner(config)
    events: list[str] = []
    requests: list[tuple[str, dict[str, str]]] = []
    browsers: list[str] = []
    starts: list[LauncherConfiguration] = []
    client = FakeClient(
        FakeResponse(200, {"handoff_code": "z" * 43}, {}), requests, events
    )
    launcher = Launcher(
        config,
        ownership=FakeOwnership([[], [owner]], events),
        process_starter=lambda item: starts.append(item) is None and FakeProcess(),
        client_factory=lambda: client,
        browser_open=lambda url: browsers.append(url) is None or True,
        credential_loader=lambda _: "b" * 40,
        monotonic=lambda: 0,
        sleep=lambda _: None,
    )

    launcher.launch()

    assert starts == [config]
    assert events == ["ownership", "ownership", "sid", "post"]
    assert len(requests) == 1
    assert len(browsers) == 1


def test_provisioning_completion_restarts_the_same_brain_in_runtime_mode(
    tmp_path: Path,
) -> None:
    config = LauncherConfiguration(
        brain_command=(str((tmp_path / "Brain.exe").resolve()), "--serve"),
        brain_image_path=(tmp_path / "Brain.exe").resolve(),
        working_directory=tmp_path.resolve(),
        data_directory=(tmp_path / "data").resolve(),
        startup_timeout_seconds=1,
    )
    owner = trusted_owner(config)
    requests: list[tuple[str, dict[str, str]]] = []
    browsers: list[str] = []
    starts: list[LauncherConfiguration] = []
    client = FakeClient(
        FakeResponse(200, {"handoff_code": "p" * 43}, {}), requests, []
    )
    launcher = Launcher(
        config,
        ownership=FakeOwnership([[], [owner]], []),
        process_starter=lambda item: starts.append(item)
        is None
        and CompletedProvisioningProcess(item),
        client_factory=lambda: client,
        browser_open=lambda url: browsers.append(url) is None or True,
        credential_loader=lambda _: "b" * 40,
        provisioning_token_factory=lambda: "t" * 32,
        monotonic=lambda: 0,
        sleep=lambda _: None,
    )

    target = launcher.launch()

    assert target == f"{BRIDGE_ORIGIN}{HANDOFF_PATH}?code={'p' * 43}"
    assert starts[0].provisioning_handoff_token == "t" * 32
    assert requests == [
        (PROVISIONING_HANDOFF_PATH, {"Authorization": "Provisioning " + "t" * 32}),
        (HANDOFF_PATH, {"Authorization": "Bootstrap " + "b" * 40}),
    ]
    assert browsers == [
        f"{BRIDGE_ORIGIN}{PROVISIONING_REDEEM_PATH}?code={'p' * 43}",
        target,
    ]


def brain_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provisioning_handoff_token: str | None,
) -> dict[str, str]:
    """Capture the child environment the production process starter builds.

    Both provisioning variables are removed from the parent environment first, so
    every captured value was set by the starter itself.
    """
    monkeypatch.delenv(PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.delenv(PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE, raising=False)
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(command: Any, **keywords: Any) -> FakeProcess:
        captured["environment"] = keywords["env"]
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _start_brain(
        LauncherConfiguration(
            brain_command=(str((tmp_path / "Brain.exe").resolve()), "--brain"),
            brain_image_path=(tmp_path / "Brain.exe").resolve(),
            working_directory=tmp_path.resolve(),
            data_directory=(tmp_path / "data").resolve(),
            provisioning_handoff_token=provisioning_handoff_token,
        )
    )
    return captured["environment"]


def test_provisioning_brain_receives_the_handoff_token_and_an_extension_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioning = brain_environment(
        tmp_path, monkeypatch, provisioning_handoff_token="t" * 32
    )
    runtime = brain_environment(tmp_path, monkeypatch, provisioning_handoff_token=None)

    assert provisioning[PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE] == "t" * 32
    assert (
        re.fullmatch(
            r"[a-p]{32}", provisioning[PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE]
        )
        is not None
    )
    assert PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE not in runtime
    assert PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE not in runtime


def test_provisioning_extension_identity_is_the_one_pinned_by_the_manifest_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_file = (
        Path(__file__).resolve().parents[1] / "extension" / "manifest.json"
    )
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()
    pinned = "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest[:32])

    environment = brain_environment(
        tmp_path, monkeypatch, provisioning_handoff_token="t" * 32
    )

    assert environment[PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE] == pinned


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(401, {"detail": "Launcher authorization is invalid"}, {}),
        FakeResponse(409, {"detail": "different_conflict"}, {}),
        FakeResponse(200, {"handoff_code": "not a URL-safe code"}, {}),
        FakeResponse(
            200,
            {"handoff_code": "h" * 43},
            {"set-cookie": "__Host-bridge_session=unexpected"},
        ),
    ],
)
def test_other_handoff_failures_fail_closed(
    tmp_path: Path,
    response: FakeResponse,
) -> None:
    launcher, _, _, browser_urls, _ = launcher_with_response(tmp_path, response)

    with pytest.raises(LaunchFailure) as raised:
        launcher.launch()

    assert raised.value.code == "handoff_failed"
    assert browser_urls == []
