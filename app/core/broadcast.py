"""Process-local broadcaster for legacy internal notifications.

The single-process runtime uses the broadcaster package's memory backend by
default. An external adapter, such as Redis, must be selected explicitly with
``BROADCAST_URL``.
"""

from broadcaster import Broadcast

from app.core.config import settings

broadcast: Broadcast = Broadcast(settings.broadcast_url)
