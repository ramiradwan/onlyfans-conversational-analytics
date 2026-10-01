"""Conservative process-local notices; never source or publication authority."""
from threading import RLock
from weakref import WeakMethod


class PendingQuestions:
    """Read only live bounded scheduler state to refuse work while preparing."""
    def __init__(self):
        self._readers = {}
        self._lock = RLock()

    def register(self, reader):
        reference = WeakMethod(reader)
        with self._lock:
            self._readers = {key:value for key,value in self._readers.items() if value() is not None}
            if len(self._readers) < 64:
                self._readers[id(reader.__self__)] = reference

    def is_pending(self, account):
        with self._lock:
            readers = tuple(self._readers.values())
        for reference in readers:
            reader = reference()
            if reader is None:
                continue
            try:
                value = reader(account)
            except Exception:
                continue  # An uncertain notice must use the ordinary reader.
            if (type(value) is tuple and len(value) == 3
                    and all(isinstance(v,str) and v for v in value[:2])
                    and type(value[2]) is int and value[2] >= 0):
                return True
        return False
