"""E2E entry with explicit synthetic installation and text-analysis fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from itertools import islice
from pathlib import Path


def _startup_phase(phase):
    if phase not in {"entry_started", "uvicorn_imported", "endpoints_imported",
                     "main_imported", "helpers_installed", "uvicorn_entered"}:
        return
    try:
        print(json.dumps({"schema": "browser-startup-phase/v1", "phase": phase},
                         separators=(",", ":")), flush=True)
    except (OSError, ValueError):
        pass


_startup_phase("entry_started")
PRODUCT_ROOT = Path(__file__).resolve().parents[3]
HELPER_ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(PRODUCT_ROOT), str(HELPER_ROOT)]


def _selection(arguments):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--text-analysis-fixture', choices=('joint', 'split-partial', 'empty', 'saved-only'))
    if arguments and (len(arguments) != 2 or arguments[0] != '--text-analysis-fixture'):
        parser.error('Expected one explicit fixture selection')
    return parser.parse_args(arguments).text_analysis_fixture


_selected_fixture = _selection(sys.argv[1:]) if __name__ == '__main__' else None

import uvicorn  # noqa: E402
from uvicorn import server as uvicorn_server  # noqa: E402

_startup_phase("uvicorn_imported")
if _selected_fixture is not None:
    from app.core.config import settings
    from text_analysis_fixture import prepare_paths
    _fixture_paths, _fixture_marker = prepare_paths(settings, _selected_fixture)
from app.api.endpoints import companion_pairing, companion_session  # noqa: E402
_startup_phase("endpoints_imported")
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app import main as application  # noqa: E402
_startup_phase("main_imported")
from app.persistence.auth import SQLiteAuthenticationStore  # noqa: E402
from app.security.companion_pairing import CompanionPairingService  # noqa: E402
from pairing_fixture import SyntheticPairingAuthority  # noqa: E402
from session_diagnostics import install_session_diagnostics  # noqa: E402


SYNTHETIC_PROVIDER_NAME = "E2E Synthetic Installation Key Provider"
_original_pairing_service = companion_pairing.companion_pairing_service


def _e2e_pairing_service() -> CompanionPairingService:
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    reference = store.installation_key_reference()
    if reference is None or reference.provider_name != SYNTHETIC_PROVIDER_NAME:
        return _original_pairing_service()
    return CompanionPairingService(store, SyntheticPairingAuthority(reference))


companion_pairing.companion_pairing_service = _e2e_pairing_service
install_session_diagnostics(companion_session)
_startup_phase("helpers_installed")


def _shutdown_phase(phase):
    if phase not in {"command_seen", "control_validated", "control_rejected", "server_shutdown_entered",
                     "application_shutdown_entered", "application_shutdown_returned", "server_returned",
                     "owner_close_returned", "fixture_coroutine_returning", "event_loop_returned",
                     "server_tasks_wait_entered", "server_waiting_connections", "server_waiting_tasks",
                     "server_listener_wait_entered", "server_listener_wait_returned", "server_tasks_wait_returned"}:
        return
    try:
        print(json.dumps({"schema": "browser-fixture-shutdown-phase.v1", "phase": phase},
                         separators=(",", ":")), flush=True)
    except (OSError, ValueError):
        pass


class _ShutdownLogger:
    def __init__(self, original):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)

    def info(self, message, *args, **kwargs):
        if type(message) is str:
            if message == "Waiting for connections to close. (CTRL+C to force quit)":
                _shutdown_phase("server_waiting_connections")
            elif message == "Waiting for background tasks to complete. (CTRL+C to force quit)":
                _shutdown_phase("server_waiting_tasks")
        return self._original.info(message, *args, **kwargs)


class _ShutdownListener:
    def __init__(self, original):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)

    def _residual(self):
        names = ('transports', 'closing', 'pending_write', 'buffered', 'closed_socket',
                 'called_connection_lost')
        counts = dict.fromkeys(names)
        try:
            if sys.version_info[:2] == (3, 13) and type(self._original) is asyncio.Server:
                clients = tuple(islice(self._original._clients, 4097))
                if len(clients) <= 4096:
                    counts['transports'] = len(clients)
                    readers = {
                        'closing': lambda transport: transport._closing,
                        'pending_write': lambda transport: transport._write_fut is not None,
                        'buffered': lambda transport: bool(transport._buffer),
                        'closed_socket': lambda transport: transport._sock is None or transport._sock.fileno() == -1,
                        'called_connection_lost': lambda transport: transport._called_connection_lost,
                    }
                    for name, read in readers.items():
                        values = []
                        for transport in clients:
                            if type(transport).__module__ != 'asyncio.proactor_events':
                                break
                            try:
                                value = read(transport)
                            except Exception:
                                break
                            if type(value) is not bool:
                                break
                            values.append(value)
                        if len(values) == len(clients):
                            counts[name] = sum(values)
        except Exception:
            counts = dict.fromkeys(names)
        try:
            print(json.dumps({'schema': 'browser-fixture-listener-residual.v1', **counts},
                             separators=(',', ':')), flush=True)
        except Exception:
            # Diagnostic collection cannot change listener completion.
            pass

    async def wait_closed(self, *args, **kwargs):
        _shutdown_phase("server_listener_wait_entered")
        close_clients = getattr(self._original, "close_clients", None)
        if callable(close_clients):
            close_clients()
        observation = asyncio.get_running_loop().call_later(5, self._residual)
        try:
            result = await self._original.wait_closed(*args, **kwargs)
        finally:
            observation.cancel()
        _shutdown_phase("server_listener_wait_returned")
        return result


class _FixtureServer(uvicorn.Server):
    async def shutdown(self, sockets=None):
        _shutdown_phase("server_shutdown_entered")
        original_logger = uvicorn_server.logger
        original_servers = self.servers
        original_listeners = tuple(original_servers)
        listeners = [_ShutdownListener(listener) for listener in original_listeners]
        try:
            uvicorn_server.logger = _ShutdownLogger(original_logger)
            original_servers[:] = listeners
            return await super().shutdown(sockets=sockets)
        finally:
            original_servers[:] = original_listeners
            self.servers = original_servers
            uvicorn_server.logger = original_logger

    async def _wait_tasks_to_complete(self):
        _shutdown_phase("server_tasks_wait_entered")
        result = await super()._wait_tasks_to_complete()
        _shutdown_phase("server_tasks_wait_returned")
        return result


async def _serve_fixture(selected):
    from text_analysis_fixture import install_fixture, read_stop
    callbacks = app.router.on_shutdown
    positions = [index for index, callback in enumerate(callbacks) if callback is application.shutdown_event]
    if len(positions) != 1:
        raise RuntimeError('text_analysis_fixture_shutdown_handler_unavailable')
    original_shutdown = callbacks[positions[0]]
    async def observed_shutdown(*args, **kwargs):
        _shutdown_phase("application_shutdown_entered")
        result = await original_shutdown(*args, **kwargs)
        _shutdown_phase("application_shutdown_returned")
        return result
    callbacks[positions[0]] = observed_shutdown
    owner = install_fixture(application, _fixture_paths, selected, _fixture_marker)
    server = _FixtureServer(uvicorn.Config(app, host='127.0.0.1', port=17871,
        workers=1, access_log=False, log_level='warning'))
    command = {'valid': False}
    def stop_reader():
        try:
            command['valid'] = read_stop(sys.stdin.buffer, command_seen=lambda: _shutdown_phase("command_seen"))
        except (OSError, ValueError):
            command['valid'] = False
        finally:
            _shutdown_phase("control_validated" if command['valid'] else "control_rejected")
            server.should_exit = True
    reader = threading.Thread(target=stop_reader, name='browser-fixture-control', daemon=True)
    reader.start()
    drained = False
    try:
        await server.serve()
        _shutdown_phase("server_returned")
    finally:
        drained = await owner.close()
        _shutdown_phase("owner_close_returned")
        reader.join(timeout=1)
    if (reader.is_alive() or not command['valid'] or not drained
            or not getattr(app.state, 'runtime_shutdown_drained', False)):
        try:
            print(json.dumps({'schema': 'browser-fixture-shutdown-failure.v1',
                'reader_alive': bool(reader.is_alive()), 'stop_valid': bool(command['valid']),
                'owner_drained': bool(drained),
                'runtime_drained': bool(getattr(app.state, 'runtime_shutdown_drained', False))},
                separators=(",", ":")), flush=True)
        except (OSError, ValueError):
            pass
        raise RuntimeError('text_analysis_fixture_shutdown_failed')
    _shutdown_phase("fixture_coroutine_returning")


if __name__ == "__main__":
    _startup_phase("uvicorn_entered")
    if _selected_fixture is None:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=17871,
            workers=1,
            access_log=False,
            log_level="warning",
        )
    else:
        asyncio.run(_serve_fixture(_selected_fixture))
        _shutdown_phase("event_loop_returned")
