"""Process-local memory broadcaster for legacy internal notifications.

It is not an authoritative persistence or distribution boundary. The shipped
single-process topology deliberately rejects external broker adapters.
"""

from broadcaster import Broadcast

from app.core.config import settings

broadcast: Broadcast = Broadcast(settings.broadcast_url)
