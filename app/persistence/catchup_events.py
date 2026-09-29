"""Post-commit invalidations from capture authority mutations."""

from functools import wraps
from threading import RLock
from weakref import WeakMethod


_listeners = set()
_lock = RLock()


def subscribe_authority_changes(callback):
    with _lock:
        _listeners.difference_update(ref for ref in tuple(_listeners) if ref() is None)
        _listeners.add(WeakMethod(callback))


def capture_authority_change(operation):
    @wraps(operation)
    def changed(self, *args, **kwargs):
        store = getattr(self, "authentication", self)
        with store.database.read() as c:
            before = store._authorization_epoch(c)
        at = store._now()
        result = operation(self, *args, **kwargs)
        with store.database.read() as c:
            after = store._authorization_epoch(c)
        if after != before:
            with _lock:
                callbacks = [callback for ref in _listeners if (callback := ref()) is not None]
            for callback in callbacks:
                callback(store.database.path, at)
        return result
    return changed
