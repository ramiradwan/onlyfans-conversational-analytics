"""  
WebSocket message models with Pydantic discriminated unions.  
  
This file is the single source-of-truth schema for all WS payloads,  
enabling auto-generation of frontend TypeScript types via /api/v1/schemas/wss.  
"""  
  
from typing import Union, Annotated, Literal  
from pydantic import BaseModel, Field  
  
from app.models import core, insights, graph  
from app.models.ingest import CacheUpdatePayload, NewRawMessagePayload  
from app.models.commands import SendMessageCommand  
  
# ------------------------  
# Incoming WS messages (Client ➔ Server)  
# ------------------------  
  
class CacheUpdateMsg(BaseModel):  
    type: Literal["cache_update"] = "cache_update"  
    payload: CacheUpdatePayload  
  
class NewRawMessageMsg(BaseModel):  
    type: Literal["new_raw_message"] = "new_raw_message"  
    payload: NewRawMessagePayload  
  
class KeepaliveMsg(BaseModel):  
    type: Literal["keepalive"] = "keepalive"  
    payload: core.KeepalivePayload  
  
IncomingWssMessage = Annotated[  
    Union[CacheUpdateMsg, NewRawMessageMsg, KeepaliveMsg],  
    Field(discriminator="type")  
]  
  
# ------------------------  
# Outgoing WS messages (Server ➔ Client)  
# ------------------------  
  
class ConnectionAckMsg(BaseModel):  
    type: Literal["connection_ack"] = "connection_ack"  
    payload: core.ConnectionInfo  
  
class SystemStatusMsg(BaseModel):  
    type: Literal["system_status"] = "system_status"  
    payload: core.SystemStatus  
  
class SystemErrorMsg(BaseModel):  
    type: Literal["system_error"] = "system_error"  
    payload: core.WssError  
  
class FullSyncResponseMsg(BaseModel):  
    type: Literal["full_sync_response"] = "full_sync_response"  
    payload: insights.FullSyncResponse  
  
class AppendMessageMsg(BaseModel):  
    type: Literal["append_message"] = "append_message"  
    payload: graph.ConversationNode  
  
class AnalyticsUpdateMsg(BaseModel):  
    type: Literal["analytics_update"] = "analytics_update"  
    payload: insights.AnalyticsUpdate  
  
class CommandToExecuteMsg(BaseModel):  
    type: Literal["command_to_execute"] = "command_to_execute"  
    payload: SendMessageCommand  
  
class EnrichmentResultMsg(BaseModel):  
    type: Literal["enrichment_result"] = "enrichment_result"  
    payload: graph.EnrichmentResultPayload  
  
OutgoingWssMessage = Annotated[  
    Union[  
        ConnectionAckMsg,  
        SystemStatusMsg,  
        SystemErrorMsg,  
        FullSyncResponseMsg,  
        AppendMessageMsg,  
        AnalyticsUpdateMsg,  
        CommandToExecuteMsg,  
        EnrichmentResultMsg,  
    ],  
    Field(discriminator="type")  
]  