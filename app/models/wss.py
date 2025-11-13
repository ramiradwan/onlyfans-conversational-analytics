"""  
WebSocket message models with Pydantic discriminated unions.  
  
This file is the single source-of-truth schema for all WS payloads,  
enabling auto-generation of frontend TypeScript types via /api/v1/schemas/wss.  
  
Spec compliance:  
- IncomingWssMessage: Agent ➔ Brain  
- OutgoingWssMessage: Brain ➔ Bridge or Agent  
"""  
  
from typing import Union, Annotated, Literal  
from pydantic import BaseModel, Field  
  
from app.models import core, insights, graph  
from app.models.ingest import CacheUpdatePayload, NewRawMessagePayload  
from app.models.commands import SendMessageCommand  
  
# ============================================================  
# Incoming WS messages (Client ➔ Server)  
# ============================================================  
  
class CacheUpdateMsg(BaseModel):  
    """Full snapshot from Agent's IndexedDB."""  
    type: Literal["cache_update"] = "cache_update"  
    payload: CacheUpdatePayload  
  
  
class NewRawMessageMsg(BaseModel):  
    """Single new message/event from Agent."""  
    type: Literal["new_raw_message"] = "new_raw_message"  
    payload: NewRawMessagePayload  
  
  
class KeepaliveMsg(BaseModel):  
    """Ping every 20s to keep MV3 service worker alive."""  
    type: Literal["keepalive"] = "keepalive"  
    payload: core.KeepalivePayload  
  
  
class IncomingOnlineUsersUpdatePayload(BaseModel):  
    """Presence heartbeat from Agent (optional extra type)."""  
    user_ids: list[int] = Field(  
        ...,  
        description="List of currently online user IDs",  
        example=[123, 456, 789]  
    )  
    timestamp: str = Field(  
        ...,  
        description="ISO8601 timestamp of the presence update",  
        example="2025-11-08T12:34:56Z"  
    )  
  
  
class IncomingOnlineUsersUpdateMsg(BaseModel):  
    """Optional: Agent reports currently online user IDs."""  
    type: Literal["online_users_update"] = "online_users_update"  
    payload: IncomingOnlineUsersUpdatePayload  
  
  
IncomingWssMessage = Annotated[  
    Union[  
        CacheUpdateMsg,  
        NewRawMessageMsg,  
        KeepaliveMsg,  
        IncomingOnlineUsersUpdateMsg,  # optional extra  
    ],  
    Field(discriminator="type")  
]  
  
# ============================================================  
# Outgoing WS messages (Server ➔ Client)  
# ============================================================  
  
class ConnectionAckMsg(BaseModel):  
    """Sent immediately upon WS connection acceptance."""  
    type: Literal["connection_ack"] = "connection_ack"  
    payload: core.ConnectionInfo  
  
  
class SystemStatusMsg(BaseModel):  
    """Broadcasts current backend status."""  
    type: Literal["system_status"] = "system_status"  
    payload: core.SystemStatus  
  
  
class SystemErrorMsg(BaseModel):  
    """Reports server-side processing/parsing error."""  
    type: Literal["system_error"] = "system_error"  
    payload: core.WssError  
  
  
class FullSyncResponseMsg(BaseModel):  
    """Snapshot: complete graph + analytics."""  
    type: Literal["full_sync_response"] = "full_sync_response"  
    payload: insights.FullSyncResponse  
  
  
class AppendMessageMsg(BaseModel):  
    """  
    Delta: single new/updated conversation node.  
    Uses ExtendedConversationNode (superset of ConversationNode).  
    """  
    type: Literal["append_message"] = "append_message"  
    payload: graph.ExtendedConversationNode  
  
  
class AnalyticsUpdateMsg(BaseModel):  
    """Delta: granular analytics metric update."""  
    type: Literal["analytics_update"] = "analytics_update"  
    payload: insights.AnalyticsUpdate  
  
  
class CommandToExecuteMsg(BaseModel):  
    """AI-generated command for Agent execution."""  
    type: Literal["command_to_execute"] = "command_to_execute"  
    payload: SendMessageCommand  
  
  
class EnrichmentResultMsg(BaseModel):  
    """  
    Optional extra: enrichment results streamed separately  
    from append_message/analytics_update.  
    """  
    type: Literal["enrichment_result"] = "enrichment_result"  
    payload: graph.EnrichmentResultPayload  
  
  
class OutgoingOnlineUsersUpdatePayload(BaseModel):  
    """Presence heartbeat from Brain to Bridge."""  
    user_ids: list[int] = Field(  
        ...,  
        description="List of currently online user IDs",  
        example=[123, 456, 789]  
    )  
    timestamp: str = Field(  
        ...,  
        description="ISO8601 timestamp of the presence update",  
        example="2025-11-08T12:34:56Z"  
    )  
  
  
class OutgoingOnlineUsersUpdateMsg(BaseModel):  
    """Broadcasted presence update to Bridge."""  
    type: Literal["online_users_update"] = "online_users_update"  
    payload: OutgoingOnlineUsersUpdatePayload  
  
  
OutgoingWssMessage = Annotated[  
    Union[  
        ConnectionAckMsg,  
        SystemStatusMsg,  
        SystemErrorMsg,  
        FullSyncResponseMsg,  
        AppendMessageMsg,  
        AnalyticsUpdateMsg,  
        CommandToExecuteMsg,  
        EnrichmentResultMsg,           # optional extra  
        OutgoingOnlineUsersUpdateMsg,  # presence update  
    ],  
    Field(discriminator="type")  
]  