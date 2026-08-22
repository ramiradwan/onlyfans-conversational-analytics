"""Fixed-origin launcher for the local Brain runtime."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlencode

import httpx
from dotenv import dotenv_values

from app.core.runtime_paths import (
    DATA_DIRECTORY_ENVIRONMENT_VARIABLE,
    runtime_configuration_file,
    runtime_data_directory,
)
from app.core.extension_identity import extension_identity_from_manifest
from app.packaged_entry import (
    PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE,
    PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE,
)
from app.provisioning.app import PROVISIONING_HANDOFF_PATH, PROVISIONING_REDEEM_PATH


BRIDGE_ORIGIN = "http://bridge.localhost:17871"
LOOPBACK_CONTROL_ORIGIN = "http://127.0.0.1:17871"
BRIDGE_PORT = 17871
BRIDGE_BIND_ADDRESS = "127.0.0.1"
BRIDGE_CONTROL_HOST = "bridge.localhost:17871"
HANDOFF_PATH = "/api/v1/session/handoff"
PROVISIONING_COMPLETE_EXIT_CODE = 75
PROVISIONING_HANDOFF_PATH = PROVISIONING_HANDOFF_PATH
PROVISIONING_REDEEM_PATH = PROVISIONING_REDEEM_PATH
SESSION_COOKIE_NAME = "__Host-bridge_session"
# The pinned `key` member of this manifest fixes the extension's identity, so the
# unpacked installation and the store listing share one ID.
EXTENSION_MANIFEST_FILE = (
    Path(__file__).resolve().parents[1] / "extension" / "manifest.json"
)
_HANDOFF_CODE = re.compile(r"[A-Za-z0-9_-]{32,}")
LAUNCH_FAILURE_EXIT_CODE = 2
LAUNCHER_LOG_FILENAME = "launcher.log"
LAUNCHER_LOG_MAX_BYTES = 64 * 1024
PROVISIONING_HANDOFF_FAILURE_MESSAGE = (
    "The local provisioning browser handoff failed."
)


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ListenerOwner:
    """Kernel listener identity used before any HTTP request is sent."""

    pid: int
    local_address: str
    image_path: Path
    user_sid: str


class PortOwnership(Protocol):
    def listeners(self, port: int) -> Sequence[ListenerOwner]: ...

    def current_user_sid(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LauncherConfiguration:
    """Installed Brain command and immutable local runtime paths."""

    brain_command: tuple[str, ...]
    brain_image_path: Path
    working_directory: Path
    data_directory: Path
    startup_timeout_seconds: float = 15.0
    provisioning_handoff_token: str | None = None

    def __post_init__(self) -> None:
        if not self.brain_command:
            raise ValueError("brain command must not be empty")
        if not self.brain_image_path.is_absolute():
            raise ValueError("Brain image path must be absolute")
        if not self.working_directory.is_absolute():
            raise ValueError("Brain working directory must be absolute")
        if not self.data_directory.is_absolute():
            raise ValueError("runtime data directory must be absolute")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup timeout must be positive")
        if (
            self.provisioning_handoff_token is not None
            and len(self.provisioning_handoff_token) < 32
        ):
            raise ValueError("provisioning handoff token is invalid")


class LaunchFailure(RuntimeError):
    """Structured fail-closed launcher result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


FAILURE_MESSAGES = {
    "launch_failed": "The local launcher failed.",
    "configuration_unavailable": "The local runtime configuration is unavailable.",
    "port_inspection_failed": "The listener on port 17871 could not be verified.",
    "port_conflict": (
        "Port 17871 is owned by another process. Stop that process, then retry. "
        "The launcher did not terminate it."
    ),
    "brain_start_failed": "Brain could not be started.",
    "brain_start_timeout": "Brain did not acquire port 17871 in time.",
    "handoff_failed": "The local browser handoff failed.",
    "provisioning_handoff_token_invalid": PROVISIONING_HANDOFF_FAILURE_MESSAGE,
    "provisioning_handoff_response_rejected": PROVISIONING_HANDOFF_FAILURE_MESSAGE,
    "provisioning_handoff_request_failed": PROVISIONING_HANDOFF_FAILURE_MESSAGE,
    "provisioning_handoff_payload_invalid": PROVISIONING_HANDOFF_FAILURE_MESSAGE,
    "provisioning_exit_failed": "Provisioning stopped before completing.",
    "browser_open_failed": "The system browser could not be opened.",
}


def _safe_failure_code(failure: LaunchFailure) -> str:
    """Return a reason code that is safe to persist or present."""
    if isinstance(failure.code, str) and failure.code in FAILURE_MESSAGES:
        return failure.code
    return "launch_failed"


class Launcher:
    """Verify or start Brain, request a handoff, and open the browser."""

    def __init__(
        self,
        configuration: LauncherConfiguration,
        *,
        ownership: PortOwnership | None = None,
        process_starter: Callable[[LauncherConfiguration], ProcessHandle] | None = None,
        client_factory: Callable[[], Any] | None = None,
        browser_open: Callable[[str], bool] | None = None,
        credential_loader: Callable[[Path], str] | None = None,
        provisioning_token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.configuration = configuration
        self.ownership = ownership or WindowsPortOwnership()
        self.process_starter = process_starter or _start_brain
        self.client_factory = client_factory or _http_client
        self.browser_open = browser_open or webbrowser.open
        self.credential_loader = credential_loader or _load_launcher_credential
        self.provisioning_token_factory = provisioning_token_factory
        self.monotonic = monotonic
        self.sleep = sleep

    def launch(self) -> str:
        """Open one authenticated handoff URL or the reusable fixed origin."""
        credential_path = runtime_configuration_file(self.configuration.data_directory)
        if not credential_path.exists():
            return self._launch_provisioning()
        owner = self._verified_listener()
        if owner is None:
            owner = self._start_and_wait()
        try:
            credential = self.credential_loader(credential_path)
        except Exception as error:
            raise LaunchFailure(
                "configuration_unavailable",
                FAILURE_MESSAGES["configuration_unavailable"],
            ) from error
        target = self._request_browser_target(credential)
        try:
            opened = self.browser_open(target)
        except Exception as error:
            raise LaunchFailure(
                "browser_open_failed", FAILURE_MESSAGES["browser_open_failed"]
            ) from error
        if not opened:
            raise LaunchFailure(
                "browser_open_failed", FAILURE_MESSAGES["browser_open_failed"]
            )
        return target

    def _launch_provisioning(self) -> str:
        token = self.provisioning_token_factory()
        if len(token) < 32:
            raise LaunchFailure(
                "provisioning_handoff_token_invalid",
                FAILURE_MESSAGES["provisioning_handoff_token_invalid"],
            )
        configuration = LauncherConfiguration(
            brain_command=self.configuration.brain_command,
            brain_image_path=self.configuration.brain_image_path,
            working_directory=self.configuration.working_directory,
            data_directory=self.configuration.data_directory,
            startup_timeout_seconds=self.configuration.startup_timeout_seconds,
            provisioning_handoff_token=token,
        )
        owner = self._verified_listener()
        if owner is None:
            process = self._start_and_wait_process(configuration)
        else:
            process = None
        target = self._request_provisioning_browser_target(token)
        try:
            opened = self.browser_open(target)
        except Exception as error:
            raise LaunchFailure(
                "browser_open_failed", FAILURE_MESSAGES["browser_open_failed"]
            ) from error
        if not opened:
            raise LaunchFailure(
                "browser_open_failed", FAILURE_MESSAGES["browser_open_failed"]
            )
        if process is None:
            return target
        return self._supervise_provisioning(process)

    def _start_and_wait(
        self, configuration: LauncherConfiguration | None = None
    ) -> ProcessHandle:
        return self._start_and_wait_process(configuration)

    def _start_and_wait_process(
        self, configuration: LauncherConfiguration | None = None
    ) -> ProcessHandle:
        active_configuration = configuration or self.configuration
        try:
            process = self.process_starter(active_configuration)
        except Exception as error:
            raise LaunchFailure(
                "brain_start_failed", FAILURE_MESSAGES["brain_start_failed"]
            ) from error
        deadline = self.monotonic() + self.configuration.startup_timeout_seconds
        while True:
            owner = self._verified_listener()
            if owner is not None:
                return process
            if process.poll() is not None:
                raise LaunchFailure(
                    "brain_start_failed", FAILURE_MESSAGES["brain_start_failed"]
                )
            if self.monotonic() >= deadline:
                raise LaunchFailure(
                    "brain_start_timeout", FAILURE_MESSAGES["brain_start_timeout"]
                )
            self.sleep(0.05)

    def _supervise_provisioning(self, process: ProcessHandle) -> str:
        """Wait for the bounded app's controlled restart into runtime mode."""
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            self.sleep(0.1)
        if exit_code != PROVISIONING_COMPLETE_EXIT_CODE:
            raise LaunchFailure(
                "provisioning_exit_failed", FAILURE_MESSAGES["provisioning_exit_failed"]
            )
        if not runtime_configuration_file(self.configuration.data_directory).exists():
            raise LaunchFailure(
                "configuration_unavailable", FAILURE_MESSAGES["configuration_unavailable"]
            )
        return self.launch()

    def _verified_listener(self) -> ListenerOwner | None:
        try:
            owners = tuple(self.ownership.listeners(BRIDGE_PORT))
        except Exception as error:
            raise LaunchFailure(
                "port_inspection_failed", FAILURE_MESSAGES["port_inspection_failed"]
            ) from error
        if not owners:
            return None
        if len(owners) != 1:
            raise LaunchFailure("port_conflict", FAILURE_MESSAGES["port_conflict"])
        owner = owners[0]
        try:
            current_sid = self.ownership.current_user_sid()
        except Exception as error:
            raise LaunchFailure(
                "port_inspection_failed", FAILURE_MESSAGES["port_inspection_failed"]
            ) from error
        if (
            owner.local_address != BRIDGE_BIND_ADDRESS
            or not _same_image(owner.image_path, self.configuration.brain_image_path)
            or owner.user_sid.casefold() != current_sid.casefold()
        ):
            raise LaunchFailure("port_conflict", FAILURE_MESSAGES["port_conflict"])
        return owner

    def _request_provisioning_browser_target(self, token: str) -> str:
        try:
            with self.client_factory() as client:
                response = client.post(
                    PROVISIONING_HANDOFF_PATH,
                    headers={
                        "Host": BRIDGE_CONTROL_HOST,
                        "Authorization": f"Provisioning {token}",
                    },
                )
                if _client_has_cookies(client, response) or response.status_code != 200:
                    raise LaunchFailure(
                        "provisioning_handoff_response_rejected",
                        FAILURE_MESSAGES["provisioning_handoff_response_rejected"],
                    )
                payload = response.json()
        except LaunchFailure:
            raise
        except Exception as error:
            raise LaunchFailure(
                "provisioning_handoff_request_failed",
                FAILURE_MESSAGES["provisioning_handoff_request_failed"],
            ) from error
        code = payload.get("handoff_code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or _HANDOFF_CODE.fullmatch(code) is None:
            raise LaunchFailure(
                "provisioning_handoff_payload_invalid",
                FAILURE_MESSAGES["provisioning_handoff_payload_invalid"],
            )
        return f"{BRIDGE_ORIGIN}{PROVISIONING_REDEEM_PATH}?{urlencode({'code': code})}"

    def _request_browser_target(self, credential: str) -> str:
        try:
            with self.client_factory() as client:
                response = client.post(
                    HANDOFF_PATH,
                    headers={
                        "Host": BRIDGE_CONTROL_HOST,
                        "Authorization": f"Bootstrap {credential}",
                    },
                )
                if _client_has_cookies(client, response):
                    raise LaunchFailure(
                        "handoff_failed", FAILURE_MESSAGES["handoff_failed"]
                    )
                if response.status_code == 409:
                    detail = _response_detail(response)
                    if detail == "launcher_bootstrap_already_consumed":
                        return BRIDGE_ORIGIN
                if response.status_code != 200:
                    raise LaunchFailure(
                        "handoff_failed", FAILURE_MESSAGES["handoff_failed"]
                    )
                payload = response.json()
        except LaunchFailure:
            raise
        except Exception as error:
            raise LaunchFailure(
                "handoff_failed", FAILURE_MESSAGES["handoff_failed"]
            ) from error
        code = payload.get("handoff_code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or _HANDOFF_CODE.fullmatch(code) is None:
            raise LaunchFailure("handoff_failed", FAILURE_MESSAGES["handoff_failed"])
        return f"{BRIDGE_ORIGIN}{HANDOFF_PATH}?{urlencode({'code': code})}"


def default_launcher_configuration() -> LauncherConfiguration:
    """Build the source and packaged-runtime launcher configuration."""
    product_root = Path(__file__).resolve().parents[1]
    executable = Path(sys.executable).resolve()
    process_image = Path(getattr(sys, "_base_executable", executable)).resolve()
    brain_command = (
        (str(executable), "--brain")
        if getattr(sys, "frozen", False)
        else (str(executable), "-m", "app.packaged_entry", "--brain")
    )
    return LauncherConfiguration(
        brain_command=brain_command,
        brain_image_path=process_image,
        working_directory=product_root,
        data_directory=runtime_data_directory(),
    )


def _load_launcher_credential(configuration_file: Path) -> str:
    values = dotenv_values(configuration_file, interpolate=False)
    credential = values.get("LOCAL_SESSION_BOOTSTRAP_TOKEN") or ""
    if len(credential) < 32:
        raise ValueError("launcher bootstrap credential is invalid")
    return credential


def _http_client() -> httpx.Client:
    return httpx.Client(
        base_url=LOOPBACK_CONTROL_ORIGIN,
        timeout=5.0,
        follow_redirects=False,
        trust_env=False,
    )


def _client_has_cookies(client: Any, response: Any) -> bool:
    set_cookie = response.headers.get("set-cookie")
    return bool(set_cookie) or len(client.cookies) != 0


def _response_detail(response: Any) -> str | None:
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    return detail if isinstance(detail, str) else None


def _extension_identity() -> str:
    """Derive the extension ID Chrome assigns to the pinned manifest key.

    The ID is the first 128 bits of the SHA-256 digest of the SPKI DER public
    key, each hex digit rendered as the letter at that offset from ``a``.
    """
    if getattr(sys, "frozen", False):
        from app.core.packaged_extension_identity import EXTENSION_ID

        if not isinstance(EXTENSION_ID, str):
            raise RuntimeError("frozen package has no embedded extension identity")
        return EXTENSION_ID
    return extension_identity_from_manifest(EXTENSION_MANIFEST_FILE)


def _same_image(actual: Path, expected: Path) -> bool:
    actual_path = os.path.normcase(str(actual.resolve(strict=False)))
    expected_path = os.path.normcase(str(expected.resolve(strict=False)))
    return actual_path == expected_path


def _start_brain(configuration: LauncherConfiguration) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment[DATA_DIRECTORY_ENVIRONMENT_VARIABLE] = str(
        configuration.data_directory
    )
    if configuration.provisioning_handoff_token is not None:
        environment[PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE] = (
            configuration.provisioning_handoff_token
        )
        environment[PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE] = (
            _extension_identity()
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        configuration.brain_command,
        cwd=configuration.working_directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("state", wintypes.DWORD),
        ("local_address", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("owning_pid", wintypes.DWORD),
    ]


class _MibTcp6RowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("local_address", ctypes.c_ubyte * 16),
        ("local_scope_id", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", ctypes.c_ubyte * 16),
        ("remote_scope_id", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("state", wintypes.DWORD),
        ("owning_pid", wintypes.DWORD),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class WindowsPortOwnership:
    """Read Windows TCP ownership, executable paths, and token SIDs."""

    _TCP_TABLE_OWNER_PID_LISTENER = 3
    _ERROR_INSUFFICIENT_BUFFER = 122
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows TCP ownership is required")
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        iphlpapi = ctypes.windll.iphlpapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

    def listeners(self, port: int) -> Sequence[ListenerOwner]:
        entries: list[tuple[int, str]] = []
        for row in self._tcp_rows(socket.AF_INET, _MibTcpRowOwnerPid):
            if socket.ntohs(row.local_port & 0xFFFF) == port:
                address = socket.inet_ntoa(struct.pack("<I", row.local_address))
                entries.append((int(row.owning_pid), address))
        for row in self._tcp_rows(socket.AF_INET6, _MibTcp6RowOwnerPid):
            if socket.ntohs(row.local_port & 0xFFFF) == port:
                address = socket.inet_ntop(socket.AF_INET6, bytes(row.local_address))
                entries.append((int(row.owning_pid), address))
        return tuple(
            ListenerOwner(
                pid=pid,
                local_address=address,
                image_path=self._process_image(pid),
                user_sid=self._process_sid(pid),
            )
            for pid, address in entries
        )

    def current_user_sid(self) -> str:
        kernel32 = ctypes.windll.kernel32
        return self._sid_for_process_handle(kernel32.GetCurrentProcess())

    def _tcp_rows(self, family: int, row_type: type[ctypes.Structure]) -> list[Any]:
        function = ctypes.windll.iphlpapi.GetExtendedTcpTable
        size = wintypes.ULONG(0)
        result = function(
            None,
            ctypes.byref(size),
            False,
            family,
            self._TCP_TABLE_OWNER_PID_LISTENER,
            0,
        )
        if result not in {0, self._ERROR_INSUFFICIENT_BUFFER}:
            raise OSError(result, "GetExtendedTcpTable failed")
        if size.value == 0:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        result = function(
            buffer,
            ctypes.byref(size),
            False,
            family,
            self._TCP_TABLE_OWNER_PID_LISTENER,
            0,
        )
        if result != 0:
            raise OSError(result, "GetExtendedTcpTable failed")
        count = wintypes.DWORD.from_buffer_copy(buffer.raw[:4]).value
        row_size = ctypes.sizeof(row_type)
        return [
            row_type.from_buffer_copy(
                buffer.raw[4 + index * row_size : 4 + (index + 1) * row_size]
            )
            for index in range(count)
        ]

    def _open_process(self, pid: int) -> int:
        handle = ctypes.windll.kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        return handle

    def _process_image(self, pid: int) -> Path:
        kernel32 = ctypes.windll.kernel32
        handle = self._open_process(pid)
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(capacity)
            ):
                raise OSError(
                    ctypes.get_last_error(), "QueryFullProcessImageNameW failed"
                )
            return Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(handle)

    def _process_sid(self, pid: int) -> str:
        kernel32 = ctypes.windll.kernel32
        handle = self._open_process(pid)
        try:
            return self._sid_for_process_handle(handle)
        finally:
            kernel32.CloseHandle(handle)

    def _sid_for_process_handle(self, process_handle: int) -> str:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            process_handle, self._TOKEN_QUERY, ctypes.byref(token)
        ):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            required = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                token, self._TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if required.value == 0:
                raise OSError(
                    ctypes.get_last_error(), "GetTokenInformation size failed"
                )
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
            token_user = _TokenUser.from_buffer_copy(buffer)
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                token_user.user.sid, ctypes.byref(sid_text)
            ):
                raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
            try:
                return sid_text.value
            finally:
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)


def _show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Brain", 0x10)
    else:
        print(message, file=sys.stderr)


def launcher_log_file(data_directory: Path) -> Path:
    """Return the per-user bounded launcher diagnostics file."""
    return data_directory / LAUNCHER_LOG_FILENAME


def _record_launch_failure(data_directory: Path, reason_code: str) -> None:
    """Append a bounded non-secret launcher failure record."""
    try:
        data_directory.mkdir(parents=True, exist_ok=True)
        log_file = launcher_log_file(data_directory)
        record = (
            f"timestamp={datetime.now(timezone.utc).isoformat()} "
            f"reason_code={reason_code} exit_code={LAUNCH_FAILURE_EXIT_CODE}\n"
        )
        if log_file.exists() and (
            log_file.stat().st_size + len(record.encode("utf-8"))
            > LAUNCHER_LOG_MAX_BYTES
        ):
            backup = log_file.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            log_file.replace(backup)
        with log_file.open("a", encoding="utf-8") as output:
            output.write(record)
    except OSError:
        return


def main() -> int:
    configuration = default_launcher_configuration()
    try:
        Launcher(configuration).launch()
    except LaunchFailure as error:
        reason_code = _safe_failure_code(error)
        _record_launch_failure(configuration.data_directory, reason_code)
        _show_error(
            f"{FAILURE_MESSAGES[reason_code]} (Reason code: {reason_code})"
        )
        return LAUNCH_FAILURE_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
