from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_launcher import FakeClient, FakeOwnership, FakeResponse, configuration, trusted_owner

from app.core.runtime_paths import runtime_configuration_file
from app.launcher import Launcher, LaunchFailure
from app.provisioning.launcher_handoff import FILENAME, load_launcher_handoff, save_launcher_handoff, remove_launcher_handoff
from app.provisioning.session import ProvisioningSessionManager, PROVISIONING_SESSION_COOKIE_NAME


def test_relaunch_reuses_same_process_authority_after_browser_session_expires(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    runtime_configuration_file(config.data_directory).unlink()
    owner = trusted_owner(config)
    token = "a" * 43
    save_launcher_handoff(config.data_directory, token=token, pid=owner.pid)
    assert token.encode() not in (config.data_directory / FILENAME).read_bytes()
    clock = [0.0]
    sessions = ProvisioningSessionManager(token, monotonic=lambda: clock[0])
    opened: list[str] = []

    class Client(FakeClient):
        def post(self, path: str, *, headers: dict[str, str]) -> FakeResponse:
            return FakeResponse(200, {"handoff_code": sessions.issue_handoff_code(headers["Authorization"])}, {})

    def unexpected() -> str:
        raise AssertionError("an existing provisioning process must reuse its credential")

    launcher = Launcher(config, ownership=FakeOwnership([[owner]], []),
        client_factory=lambda: Client(FakeResponse(200, {}, {}), [], []),
        browser_open=lambda url: opened.append(url) is None,
        provisioning_token_factory=unexpected)
    first = launcher.launch()
    first_session = sessions.redeem_handoff_code(parse_qs(urlsplit(first).query)["code"][0])
    clock[0] = 301
    request = Request({"type": "http", "headers": [
        (b"host", b"bridge.localhost:17871"),
        (b"cookie", f"{PROVISIONING_SESSION_COOKIE_NAME}={first_session.identifier}".encode()),
    ]})
    with pytest.raises(HTTPException):
        sessions.require_session(request)
    second = launcher.launch()
    session = sessions.redeem_handoff_code(parse_qs(urlsplit(second).query)["code"][0])
    assert session.identifier != first_session.identifier and session.expires_at == 601
    assert len(opened) == 2


def test_reentry_refuses_stale_process_credential_before_http(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    runtime_configuration_file(config.data_directory).unlink()
    owner = trusted_owner(config)
    save_launcher_handoff(config.data_directory, token="a" * 43, pid=owner.pid + 1)
    launcher = Launcher(config, ownership=FakeOwnership([[owner]], []),
        client_factory=lambda: pytest.fail("stale process must not receive a secret"))
    with pytest.raises(LaunchFailure, match="configuration"):
        launcher.launch()
    remove_launcher_handoff(config.data_directory, pid=owner.pid)
    assert (config.data_directory / FILENAME).exists()
    remove_launcher_handoff(config.data_directory, pid=owner.pid + 1)
    assert not (config.data_directory / FILENAME).exists()


def test_handoff_tampering_refuses_decryption(tmp_path: Path) -> None:
    save_launcher_handoff(tmp_path, token="b" * 43, pid=5)
    path = tmp_path / FILENAME
    payload = bytearray(path.read_bytes())
    payload[-3] ^= 1
    path.write_bytes(payload)
    with pytest.raises(Exception):
        load_launcher_handoff(tmp_path, pid=5)
