"""Independent expected canonical fields for admitted synthetic capture."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Fixture:
    def __init__(self, size, clock, account="synthetic-continuous-owner"):
        self.size, self.account = size, account
        self.clock_ms = int(datetime.fromisoformat(clock).timestamp() * 1000)
        self.messages = {}
        half = size // 2
        for index in range(size):
            chat = 0 if index < half else 1 + (index - half) % 100
            position = index if index < half else (index - half) // 100
            self._put(f"matrix-input-{index}", chat, position % 2 == 1,
                f"Synthetic support message {index}",
                int(self.clock_ms - 48 * 3600000 + index * 48 * 3600000 / size))

    def _put(self, identity, chat, outbound, text, milliseconds):
        conversation = f"chat-{chat}"
        sent = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds))
        self.messages[identity] = [identity, conversation, self.account if outbound else conversation,
            text, sent.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "outbound" if outbound else "inbound"]

    def append(self, identity, chat):
        self._put(identity, chat, False, "Synthetic current message", self.clock_ms - 1)

    def edit(self):
        for index in range(100):
            self.messages[f"matrix-input-{index}"][3] = f"Synthetic changed support {index}"

    def delete(self):
        for index in range(100, 200):
            del self.messages[f"matrix-input-{index}"]

    def history_batch(self, batch):
        for offset in range(100):
            index = batch * 100 + offset
            self._put(f"matrix-history-{index}", 0, False, f"Synthetic historical message {index}",
                      self.clock_ms - 30 * 86400000 + index * 1000)
        self._put(f"matrix-interleaved-live-{batch}", 1 + batch, False,
                  "Synthetic interleaved message", self.clock_ms - 100 + batch)

    def rows(self):
        return [self.messages[key] for key in sorted(self.messages)]
