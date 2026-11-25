"""The packaged Brain's foreground Windows Hello operation.

Only the admitted desktop owner supplies a ceremony. No renderer-supplied RP,
challenge, window handle or authenticator response crosses this boundary.
"""
from __future__ import annotations

import ctypes as c
import json
import os
import sys
import time
from contextlib import contextmanager
from threading import Lock

from app.core.process_lifetime import parent_lifetime_active
from app.security.account_passkey_enrollment import EnrollmentAttempt
from app.security.account_passkey_webauthn import (
    ENROLLMENT_ORIGIN,
    SAVED_DATA_ORIGIN,
    NativeAssertion,
    NativeRegistration,
)
from app.security.signed_objects import decode, encode


# Version 2 options from the Windows SDK. The response definitions stop at the
# last field we read. Windows owns and frees those buffers.
DWORD, LONG, BOOL = c.c_uint32, c.c_int32, c.c_int32
BYTE_POINTER = c.POINTER(c.c_ubyte)
WSTR, HWND = c.c_wchar_p, c.c_void_p
PUBLIC_KEY = "public-key"
PLATFORM = REQUIRED = ATTESTATION_NONE = 1
OPTIONS_VERSION = 2
MAX_CEREMONY_SECONDS = 120.0
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800


class _Guid(c.Structure):
    _fields_ = [("data1", DWORD), ("data2", c.c_uint16), ("data3", c.c_uint16),
                ("data4", c.c_ubyte * 8)]


class _RP(c.Structure):
    _fields_ = [("version", DWORD), ("id", WSTR), ("name", WSTR), ("icon", WSTR)]


class _User(c.Structure):
    _fields_ = [("version", DWORD), ("id_size", DWORD), ("id", BYTE_POINTER), ("name", WSTR),
                ("icon", WSTR), ("display_name", WSTR)]


class _Data(c.Structure):
    _fields_ = [("version", DWORD), ("size", DWORD), ("content", BYTE_POINTER), ("hash_name", WSTR)]


class _Parameter(c.Structure):
    _fields_ = [("version", DWORD), ("type", WSTR), ("algorithm", LONG)]


class _Parameters(c.Structure):
    _fields_ = [("size", DWORD), ("values", c.POINTER(_Parameter))]


class _Credential(c.Structure):
    _fields_ = [("version", DWORD), ("id_size", DWORD), ("id", BYTE_POINTER), ("type", WSTR)]


class _Credentials(c.Structure):
    _fields_ = [("size", DWORD), ("values", c.POINTER(_Credential))]


class _Extensions(c.Structure):
    _fields_ = [("size", DWORD), ("values", c.c_void_p)]


class _Make(c.Structure):
    _fields_ = [("version", DWORD), ("timeout_ms", DWORD), ("credentials", _Credentials),
                ("extensions", _Extensions), ("attachment", DWORD), ("resident", BOOL),
                ("verification", DWORD), ("attestation", DWORD), ("flags", DWORD),
                ("cancellation", c.POINTER(_Guid))]


class _Get(c.Structure):
    _fields_ = [("version", DWORD), ("timeout_ms", DWORD), ("credentials", _Credentials),
                ("extensions", _Extensions), ("attachment", DWORD), ("verification", DWORD),
                ("flags", DWORD), ("app_id", WSTR), ("used_app_id", c.POINTER(BOOL)),
                ("cancellation", c.POINTER(_Guid))]


class _Attestation(c.Structure):
    _fields_ = [("version", DWORD), ("format", WSTR), ("authenticator_size", DWORD),
                ("authenticator", BYTE_POINTER), ("attestation_size", DWORD),
                ("attestation", BYTE_POINTER), ("decode_type", DWORD), ("decoded", c.c_void_p),
                ("object_size", DWORD), ("object", BYTE_POINTER), ("id_size", DWORD), ("id", BYTE_POINTER)]


class _Assertion(c.Structure):
    _fields_ = [("version", DWORD), ("authenticator_size", DWORD), ("authenticator", BYTE_POINTER),
                ("signature_size", DWORD), ("signature", BYTE_POINTER), ("credential", _Credential),
                ("user_size", DWORD), ("user", BYTE_POINTER)]


class WindowsHelloRefused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Windows Hello could not complete the passkey request")


def _result(status: int) -> None:
    if status == 0:
        return
    reason = {
        0x800704C7: "cancelled",  # ERROR_CANCELLED
        0x80090036: "cancelled",  # NTE_USER_CANCELLED
        0x80070005: "permission_required",  # E_ACCESSDENIED
        0x80090010: "permission_required",  # NTE_PERM
        0x80090011: "credential_unavailable",  # NTE_NOT_FOUND
    }.get(status & 0xFFFFFFFF, "unavailable")
    raise WindowsHelloRefused(reason)


def _bytes(pointer, size: int, maximum: int) -> bytes:
    if not pointer or not 0 < size <= maximum:
        raise WindowsHelloRefused("unavailable")
    return c.string_at(pointer, size)


def _input(raw: bytes):
    return (c.c_ubyte * len(raw)).from_buffer_copy(raw)


def _document(kind: str, origin: str, challenge: bytes) -> bytes:
    if len(challenge) != 32:
        raise WindowsHelloRefused("unavailable")
    return json.dumps(
        {"type": kind, "challenge": encode(challenge), "origin": origin, "crossOrigin": False},
        separators=(",", ":"),
    ).encode("ascii")


def _function(library, name, returns, *parameters):
    function = getattr(library, name)
    function.restype = returns
    function.argtypes = parameters
    return function


class WindowsHelloBroker:
    """Run exactly one foreground operation under the installed desktop parent."""

    @classmethod
    def for_installed_desktop(cls, *, rp_id: str) -> WindowsHelloBroker | None:
        if (sys.platform != "win32" or not getattr(sys, "frozen", False)
                or not parent_lifetime_active()
                or not os.environ.get("LOCAL_PROVISIONING_HANDOFF_TOKEN")):
            return None
        try:
            return cls(rp_id=rp_id, parent_pid=os.getppid())
        except (AttributeError, OSError, WindowsHelloRefused):
            return None

    def __init__(self, *, rp_id: str, parent_pid: int) -> None:
        self._rp_id = rp_id
        self._parent_pid = parent_pid
        api = c.WinDLL("webauthn.dll", winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
        user32 = c.WinDLL("user32.dll", winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
        if _function(api, "WebAuthNGetApiVersionNumber", DWORD)() < 2:
            raise WindowsHelloRefused("unavailable")
        self._foreground = _function(user32, "GetForegroundWindow", HWND)
        self._window_pid = _function(user32, "GetWindowThreadProcessId", DWORD, HWND, c.POINTER(DWORD))
        self._make = _function(api, "WebAuthNAuthenticatorMakeCredential", LONG, HWND, c.POINTER(_RP),
                               c.POINTER(_User), c.POINTER(_Parameters), c.POINTER(_Data),
                               c.POINTER(_Make), c.POINTER(c.POINTER(_Attestation)))
        self._get = _function(api, "WebAuthNAuthenticatorGetAssertion", LONG, HWND, WSTR,
                              c.POINTER(_Data), c.POINTER(_Get), c.POINTER(c.POINTER(_Assertion)))
        self._free_attestation = _function(api, "WebAuthNFreeCredentialAttestation", None, c.POINTER(_Attestation))
        self._free_assertion = _function(api, "WebAuthNFreeAssertion", None, c.POINTER(_Assertion))
        self._new_cancellation = _function(api, "WebAuthNGetCancellationId", LONG, c.POINTER(_Guid))
        self._cancel = _function(api, "WebAuthNCancelCurrentOperation", LONG, c.POINTER(_Guid))
        self._lock = Lock()
        self._active = None
        self._closed = False

    @contextmanager
    def _operation(self, rp_id: str, seconds: float):
        if rp_id != self._rp_id or not 0 < seconds <= MAX_CEREMONY_SECONDS:
            raise WindowsHelloRefused("unavailable")
        with self._lock:
            if self._closed or self._active is not None:
                raise WindowsHelloRefused("unavailable")
            window = self._foreground()
            pid = DWORD()
            if not window or not self._window_pid(window, c.byref(pid)) or pid.value != self._parent_pid:
                raise WindowsHelloRefused("foreground_required")
            cancellation = _Guid()
            _result(self._new_cancellation(c.byref(cancellation)))
            self._active = cancellation
        expires = time.monotonic() + seconds
        # Windows treats its timeout as a hint. Cancel this ID independently
        # and refuse late success in the owning process.
        from threading import Timer

        timer = Timer(seconds, self._cancel_exact, args=(cancellation,))
        timer.daemon = True
        timer.start()
        try:
            yield window, cancellation, max(1, int(seconds * 1000))
            with self._lock:
                if self._closed or time.monotonic() >= expires:
                    raise WindowsHelloRefused("cancelled")
        finally:
            timer.cancel()
            with self._lock:
                self._active = None

    def _cancel_exact(self, cancellation: _Guid) -> None:
        with self._lock:
            if self._active is cancellation:
                self._cancel(c.byref(cancellation))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._cancel(c.byref(self._active))

    def enroll(self, attempt: EnrollmentAttempt, *, seconds: float) -> NativeRegistration:
        raw = _document("webauthn.create", ENROLLMENT_ORIGIN, attempt.challenge)
        data = _Data(1, len(raw), _input(raw), "SHA-256")
        rp = _RP(1, attempt.rp_id, "Dipsy", None)
        user_id = _input(attempt.user_handle)
        user = _User(1, len(user_id), user_id, attempt.display_label, None, attempt.display_label)
        parameter = _Parameter(1, PUBLIC_KEY, -7)
        algorithms = _Parameters(1, c.pointer(parameter))
        with self._operation(attempt.rp_id, seconds) as (window, cancellation, timeout):
            options = _Make(OPTIONS_VERSION, timeout, _Credentials(), _Extensions(), PLATFORM,
                            1, REQUIRED, ATTESTATION_NONE, 0, c.pointer(cancellation))
            output = c.POINTER(_Attestation)()
            try:
                _result(self._make(window, c.byref(rp), c.byref(user), c.byref(algorithms),
                                   c.byref(data), c.byref(options), c.byref(output)))
                if not output or output.contents.format != "none":
                    raise WindowsHelloRefused("unavailable")
                result = output.contents
                return NativeRegistration(encode(_bytes(result.id, result.id_size, 1023)), encode(raw),
                                          encode(_bytes(result.object, result.object_size, 8192)))
            finally:
                if output:
                    self._free_attestation(output)

    def assert_local(self, *, rp_id: str, credential_id: str, challenge: bytes,
                     seconds: float = MAX_CEREMONY_SECONDS) -> NativeAssertion:
        raw = _document("webauthn.get", SAVED_DATA_ORIGIN, challenge)
        data = _Data(1, len(raw), _input(raw), "SHA-256")
        credential_bytes = decode(credential_id, 1023)
        credential = _Credential(1, len(credential_bytes), _input(credential_bytes), PUBLIC_KEY)
        allowed = _Credentials(1, c.pointer(credential))
        with self._operation(rp_id, seconds) as (window, cancellation, timeout):
            options = _Get(OPTIONS_VERSION, timeout, allowed, _Extensions(), PLATFORM, REQUIRED,
                           0, None, None, c.pointer(cancellation))
            output = c.POINTER(_Assertion)()
            try:
                _result(self._get(window, rp_id, c.byref(data), c.byref(options), c.byref(output)))
                if not output or output.contents.credential.type != PUBLIC_KEY:
                    raise WindowsHelloRefused("unavailable")
                result = output.contents
                used = result.credential
                return NativeAssertion(
                    encode(_bytes(used.id, used.id_size, 1023)), encode(raw),
                    encode(_bytes(result.authenticator, result.authenticator_size, 8192)),
                    encode(_bytes(result.signature, result.signature_size, 72)),
                    None if result.user_size == 0 else encode(_bytes(result.user, result.user_size, 32)),
                )
            finally:
                if output:
                    self._free_assertion(output)
