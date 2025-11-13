from __future__ import annotations  
from pydantic import BaseModel, Field  
from typing import Optional, List, Dict, Any  
from datetime import datetime  
from typing import Literal  
  
# ---- Nested support models ----  
class AvatarThumbs(BaseModel):  
    c50: str | None = None  
    c144: str | None = None  
  
class HeaderThumbs(BaseModel):  
    w480: str | None = None  
    w760: str | None = None  
  
class HeaderSize(BaseModel):  
    width: int | None = None  
    height: int | None = None  
  
class MediaFile(BaseModel):  
    height: int | None = None  
    size: int | None = None  
    sources: List[str] | None = None  
    url: str | None = None  
    width: int | None = None  
  
class MediaPreview(BaseModel):  
    height: int | None = None  
    size: int | None = None  
    url: str | None = None  
    width: int | None = None  
  
class MediaItem(BaseModel):  
    canView: bool | None = None  
    convertedToVideo: bool | None = None  
    createdAt: datetime | None = None  
    duration: float | None = None  
    files: Dict[str, MediaFile] | None = None  
    preview: MediaPreview | None = None  
    squarePreview: Dict[str, Any] | None = None  
    thumb: Dict[str, Any] | None = None  
    hasCustomPreview: bool | None = None  
    hasError: bool | None = None  
    id: int | str | None = None  
    isReady: bool | None = None  
    type: str | None = None  
    videoSources: Dict[str, str | None] | None = None  
  
class UserRef(BaseModel):  
    """Minimal OnlyFans user reference with optional extended profile fields."""  
    id: int | str | None = None  
    view: str | None = None  
    _view: str | None = None  
    name: str | None = None  
    username: str | None = None  
    displayName: str | None = None  
    avatar: str | None = None  
    avatarThumbs: AvatarThumbs | None = None  
    header: str | None = None  
    headerSize: HeaderSize | None = None  
    headerThumbs: HeaderThumbs | None = None  
    lastSeen: datetime | None = None  
    notice: str | None = None  
  
    # Subscription-related fields  
    canAddSubscriber: bool | None = None  
    canCommentStory: bool | None = None  
    canEarn: bool | None = None  
    canLookStory: bool | None = None  
    canPayInternal: bool | None = None  
    canRestrict: bool | None = None  
    currentSubscribePrice: float | None = None  
    hasNotViewedStory: bool | None = None  
    hasScheduledStream: bool | None = None  
    hasStories: bool | None = None  
    hasStream: bool | None = None  
    isPaywallRequired: bool | None = None  
    isRestricted: bool | None = None  
    isVerified: bool | None = None  
    listsStates: List[Dict[str, Any]] | None = None  
    showMediaCount: bool | None = None  
    subscribePrice: float | None = None  
    subscribedBy: bool | None = None  
    subscribedByAutoprolong: bool | None = None  
    subscribedByExpire: bool | None = None  
    subscribedByExpireDate: datetime | None = None  
    subscribedIsExpiredNow: bool | None = None  
    subscribedOn: bool | None = None  
    subscribedOnDuration: str | None = None  
    subscribedOnExpiredNow: bool | None = None  
    subscriptionBundles: List[Dict[str, Any]] | None = None  
    tipsEnabled: bool | None = None  
    tipsMax: int | None = None  
    tipsMin: int | None = None  
    tipsMinInternal: int | None = None  
    tipsTextEnabled: bool | None = None  
  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
# ---- Core message and conversation models ----  
class Message(BaseModel):  
    """Represents a single OnlyFans chat message, optionally enriched."""  
    id: int | str  
    chat_id: int | str | None = None  
    text: str | None = None  
  
    createdAt: datetime | None = None  
    created_at: datetime | None = None  
    changedAt: datetime | None = None  
  
    fromUser: UserRef | None = None  
    is_creator: bool | None = None  
  
    # --- Enrichment fields ---  
    sentimentScore: float | None = Field(None, description="Sentiment score between 0 and 1")  
    topics: List[str] | None = Field(None, description="List of NLP-extracted topics")  
  
    is_inbound: bool | None = None  
    isPinned: bool | None = None  
    isTip: bool | None = None  
    isLiked: bool | None = None  
    isFree: bool | None = None  
    isCouplePeopleMedia: bool | None = None  
    isMarkdownDisabled: bool | None = None  
    isMediaReady: bool | None = None  
    isNew: bool | None = None  
    isOpened: bool | None = None  
    isReportedByMe: bool | None = None  
    isFromQueue: bool | None = None  
  
    canBePinned: bool | None = None  
    canPurchase: bool | None = None  
    canPurchaseReason: str | None = None  
    canReport: bool | None = None  
    canUnsendQueue: bool | None = None  
  
    mediaCount: int | None = None  
    price: float | None = None  
    cancelSeconds: int | None = None  
    queueId: int | str | None = None  
    unsendSecondsQueue: int | None = None  
  
    giphyId: str | None = None  
    lockedText: bool | None = None  
    responseType: str | None = None  
  
    media: List[MediaItem] | None = None  
    previews: List[Dict[str, Any]] | None = None  
    releaseForms: List[Dict[str, Any]] | None = None  
  
    replyToMessage: Message | None = None  
  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
class ChatThread(BaseModel):  
    """Represents a chat thread with a fan, including optional recent messages."""  
    id: int | str  
    withUser: UserRef | None = None  
    last_message: Message | None = None  
    unread_count: int | None = None  
    unreadMessagesCount: int | None = None  
    messages: List[Message] | None = None  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
class SyncResponse(BaseModel):  
    """Full sync response for REST or internal use."""  
    chats: List[ChatThread]  
    messages: List[Message]  
  
Message.model_rebuild()  
  
# ---- Misc core models ----  
class KeepalivePayload(BaseModel):  
    """Payload for keepalive messages from Agent to Brain."""  
    timestamp: datetime | None = None  
  
class ConnectionInfo(BaseModel):  
    """Sent immediately on WSS connect — confirms connection and provides system version."""  
    version: str  
    clientType: str | None = None  
    userId: str | None = None  
    statusMessage: str | None = None  
  
class SystemStatus(BaseModel):  
    """Represents the current operational status of the Brain."""  
    status: Literal["PROCESSING_SNAPSHOT", "REALTIME", "ERROR"]  
    detail: str | None = None  
  
class WssError(BaseModel):  
    """Represents an error sent over WS to the Bridge."""  
    errorMessage: str  # main error message  
    code: str | None = None  
    detail: str | None = None  