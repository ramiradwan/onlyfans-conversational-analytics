from pydantic import BaseModel, Field  
from typing import Optional, List, Union, Dict, Any, Literal  
from datetime import datetime  
  
# ---- Nested support models ----  
  
class AvatarThumbs(BaseModel):  
    c50: Optional[str] = None  
    c144: Optional[str] = None  
  
class HeaderThumbs(BaseModel):  
    w480: Optional[str] = None  
    w760: Optional[str] = None  
  
class HeaderSize(BaseModel):  
    width: Optional[int] = None  
    height: Optional[int] = None  
  
class MediaFile(BaseModel):  
    height: Optional[int] = None  
    size: Optional[int] = None  
    sources: Optional[List[str]] = None  
    url: Optional[str] = None  
    width: Optional[int] = None  
  
class MediaPreview(BaseModel):  
    height: Optional[int] = None  
    size: Optional[int] = None  
    url: Optional[str] = None  
    width: Optional[int] = None  
  
class MediaItem(BaseModel):  
    canView: Optional[bool] = None  
    convertedToVideo: Optional[bool] = None  
    createdAt: Optional[datetime] = None  
    duration: Optional[float] = None  
    files: Optional[Dict[str, MediaFile]] = None  
    preview: Optional[MediaPreview] = None  
    squarePreview: Optional[Dict[str, Any]] = None  
    thumb: Optional[Dict[str, Any]] = None  
    hasCustomPreview: Optional[bool] = None  
    hasError: Optional[bool] = None  
    id: Optional[Union[int, str]] = None  
    isReady: Optional[bool] = None  
    type: Optional[str] = None  
    videoSources: Optional[Dict[str, Optional[str]]] = None  
  
class UserRef(BaseModel):  
    id: Optional[Union[int, str]] = None  
    view: Optional[str] = None  
    _view: Optional[str] = None  
    name: Optional[str] = None  
    username: Optional[str] = None  
    displayName: Optional[str] = None  
    avatar: Optional[str] = None  
    avatarThumbs: Optional[AvatarThumbs] = None  
    header: Optional[str] = None  
    headerSize: Optional[HeaderSize] = None  
    headerThumbs: Optional[HeaderThumbs] = None  
    lastSeen: Optional[datetime] = None  
    notice: Optional[str] = None  
  
    # Subscription-related fields  
    canAddSubscriber: Optional[bool] = None  
    canCommentStory: Optional[bool] = None  
    canEarn: Optional[bool] = None  
    canLookStory: Optional[bool] = None  
    canPayInternal: Optional[bool] = None  
    canRestrict: Optional[bool] = None  
    currentSubscribePrice: Optional[float] = None  
    hasNotViewedStory: Optional[bool] = None  
    hasScheduledStream: Optional[bool] = None  
    hasStories: Optional[bool] = None  
    hasStream: Optional[bool] = None  
    isPaywallRequired: Optional[bool] = None  
    isRestricted: Optional[bool] = None  
    isVerified: Optional[bool] = None  
    listsStates: Optional[List[Dict[str, Any]]] = None  
    showMediaCount: Optional[bool] = None  
    subscribePrice: Optional[float] = None  
    subscribedBy: Optional[bool] = None  
    subscribedByAutoprolong: Optional[bool] = None  
    subscribedByExpire: Optional[bool] = None  
    subscribedByExpireDate: Optional[datetime] = None  
    subscribedIsExpiredNow: Optional[bool] = None  
    subscribedOn: Optional[bool] = None  
    subscribedOnDuration: Optional[str] = None  
    subscribedOnExpiredNow: Optional[bool] = None  
    subscriptionBundles: Optional[List[Dict[str, Any]]] = None  
    tipsEnabled: Optional[bool] = None  
    tipsMax: Optional[int] = None  
    tipsMin: Optional[int] = None  
    tipsMinInternal: Optional[int] = None  
    tipsTextEnabled: Optional[bool] = None  
  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
# ---- Core message and conversation models ----  
  
class Message(BaseModel):  
    id: Union[int, str]  
    chat_id: Optional[Union[int, str]] = None  
    text: Optional[str] = None  
  
    createdAt: Optional[datetime] = None  
    created_at: Optional[datetime] = None  
    changedAt: Optional[datetime] = None  
  
    fromUser: Optional[UserRef] = None  
    is_creator: Optional[bool] = None
  
    is_inbound: Optional[bool] = None  
    isPinned: Optional[bool] = None  
    isTip: Optional[bool] = None  
    isLiked: Optional[bool] = None  
    isFree: Optional[bool] = None  
    isCouplePeopleMedia: Optional[bool] = None  
    isMarkdownDisabled: Optional[bool] = None  
    isMediaReady: Optional[bool] = None  
    isNew: Optional[bool] = None  
    isOpened: Optional[bool] = None  
    isReportedByMe: Optional[bool] = None  
    isFromQueue: Optional[bool] = None  
  
    canBePinned: Optional[bool] = None  
    canPurchase: Optional[bool] = None  
    canPurchaseReason: Optional[str] = None  
    canReport: Optional[bool] = None  
    canUnsendQueue: Optional[bool] = None  
  
    mediaCount: Optional[int] = None  
    price: Optional[float] = None  
    cancelSeconds: Optional[int] = None  
    queueId: Optional[Union[int, str]] = None  
    unsendSecondsQueue: Optional[int] = None  
  
    giphyId: Optional[str] = None  
    lockedText: Optional[bool] = None  
    responseType: Optional[str] = None  
  
    media: Optional[List[MediaItem]] = None  
    previews: Optional[List[Dict[str, Any]]] = None  
    releaseForms: Optional[List[Dict[str, Any]]] = None  
  
    replyToMessage: Optional["Message"] = None  
  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
class ChatThread(BaseModel):  
    id: Union[int, str]  
    withUser: Optional[UserRef] = None  
    last_message: Optional[Message] = None  
    unread_count: Optional[int] = None  
    unreadMessagesCount: Optional[int] = None  
    messages: Optional[List[Message]] = None  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
# ---- Unified sync response ----  
  
class SyncResponse(BaseModel):  
    chats: List[ChatThread]  
    messages: List[Message]  
  
# ---- Resolve recursion ----  
Message.model_rebuild()  

class KeepalivePayload(BaseModel):  
    """Payload for keepalive messages from Agent to Brain."""  
    timestamp: Optional[datetime] = None  

class ConnectionInfo(BaseModel):  
    """Sent immediately on WSS connect — confirms connection and provides system version."""  
    version: str  
    message: Optional[str] = None  

class SystemStatus(BaseModel):  
    """Represents the current operational status of the Brain."""  
    status: Literal["PROCESSING_SNAPSHOT", "REALTIME", "ERROR"]  
    detail: str | None = None  

class WssError(BaseModel):  
    """Represents an error sent over WS to the Bridge."""  
    code: str  
    message: str  
    detail: str | None = None  