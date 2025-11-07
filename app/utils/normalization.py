"""  
Normalization utilities:  
Provides stateless helper functions to clean, coerce, and standardize  
raw message/chat payloads from the OnlyFans extension or API.  
Now aligned with unified Message & ChatThread models in app/models/core.py.  
"""  

import re
from typing import Any, Dict, Optional, Union, List  
from datetime import datetime  
from app.utils.time import utcnow  
from app.utils.logger import logger  
from app.config import settings 

def strip_html(text: str) -> str:  
    """Remove HTML tags from a string."""  
    return re.sub(r"</?[^>]+>", "", text) if text else text  
  
def normalize_str(value: Any) -> Optional[str]:  
    """Convert a value to a stripped string, or None if empty/invalid."""  
    if value is None:  
        return None  
    try:  
        s = str(value).strip()  
        return s if s else None  
    except Exception as e:  
        logger.debug(f"normalize_str error: {e}")  
        return None  
  
  
def normalize_int(value: Any) -> Optional[int]:  
    """Convert a value to int, or None if conversion fails."""  
    try:  
        return int(value)  
    except (TypeError, ValueError):  
        return None  
  
  
def normalize_bool(value: Any) -> Optional[bool]:  
    """Convert value to bool, handling string representations."""  
    if isinstance(value, bool):  
        return value  
    if isinstance(value, str):  
        v = value.strip().lower()  
        if v in ("true", "1", "yes"):  
            return True  
        if v in ("false", "0", "no"):  
            return False  
    return None  
  
  
def normalize_datetime(value: Any) -> Optional[datetime]:  
    """Convert value to a timezone-aware datetime, or None if invalid."""  
    if value is None:  
        return None  
    if isinstance(value, datetime):  
        return value  
    if isinstance(value, (int, float)):  
        try:  
            return datetime.fromtimestamp(value, tz=utcnow().tzinfo)  
        except Exception as e:  
            logger.debug(f"normalize_datetime timestamp error: {e}")  
            return None  
    if isinstance(value, str):  
        try:  
            return datetime.fromisoformat(value.replace("Z", "+00:00"))  
        except Exception as e:  
            logger.debug(f"normalize_datetime str error: {e}")  
            return None  
    return None  
  
  
def normalize_participants(raw: Any) -> List[str]:  
    """Normalize participants list to a list of string IDs."""  
    if not raw:  
        return []  
    if isinstance(raw, list):  
        return [normalize_str(p) for p in raw if normalize_str(p)]  
    return [normalize_str(raw)] if normalize_str(raw) else []  
  
  
def normalize_message_payload(raw: Dict[str, Any]) -> Dict[str, Any]:  
    """  
    Normalize a raw message dictionary from extension/API into a clean dict  
    for unified Message model.  
  
    Adds:  
      - HTML-stripped `text`  
      - Consistent string `chat_id`  
      - `is_creator` boolean (from settings.onlyfans_creator_id)  
      - `sender_name` (from fromUser, withUser, or fallback)  
    """  
    normalized: Dict[str, Any] = dict(raw)  # shallow copy  
  
    # --- Clean text ---  
    if "text" in normalized:  
        normalized["text"] = strip_html(normalized.get("text"))  
    elif "body" in normalized:  
        normalized["text"] = strip_html(normalized.get("body"))  
  
    # --- Ensure chat_id is string ---  
    chat_id_val = raw.get("chat_id") or raw.get("chatId")  
    if chat_id_val is not None:  
        normalized["chat_id"] = str(chat_id_val)  
  
    # --- Ensure both datetime fields ---  
    created_at_val = raw.get("created_at") or raw.get("createdAt")  
    normalized["created_at"] = normalize_datetime(created_at_val)  
    normalized["createdAt"] = normalize_datetime(created_at_val)  
  
    if "changedAt" in raw:  
        normalized["changedAt"] = normalize_datetime(raw.get("changedAt"))  
  
    # --- Normalize fromUser ---  
    if "fromUser" in raw and isinstance(raw["fromUser"], dict):  
        fu = dict(raw["fromUser"])  
        if "_view" in fu and not fu.get("view"):  
            fu["view"] = fu["_view"]  
        normalized["fromUser"] = fu  
        # Prefer fromUser name/username as sender_name  
        if not normalized.get("sender_name"):  
            normalized["sender_name"] = fu.get("name") or fu.get("username")  
  
    # --- Fallback: try withUser for sender_name if fromUser missing ---  
    if not normalized.get("sender_name") and "withUser" in raw and isinstance(raw["withUser"], dict):  
        wu = raw["withUser"]  
        normalized["sender_name"] = wu.get("name") or wu.get("username")  
  
    # --- Set is_creator ---  
    creator_id = settings.onlyfans_creator_id  
    if creator_id:  
        normalized["is_creator"] = str(raw.get("sender_id")) == str(creator_id)  
    else:  
        # Fallback: try direction field  
        if "direction" in raw:  
            normalized["is_creator"] = str(raw.get("direction")).lower() in ("outbound", "creator")  
        else:  
            normalized["is_creator"] = None  
  
    # --- Fallback sender_name if still missing ---  
    if not normalized.get("sender_name"):  
        if normalized.get("is_creator") is True:  
            normalized["sender_name"] = "Creator"  
        elif normalized.get("is_creator") is False:  
            normalized["sender_name"] = "Fan"  
        else:  
            normalized["sender_name"] = "Unknown"  
  
    # --- Normalize previews ---  
    if "previews" in raw and isinstance(raw["previews"], list):  
        fixed_previews = []  
        for p in raw["previews"]:  
            if isinstance(p, dict):  
                fixed_previews.append(p)  
            else:  
                fixed_previews.append({"id": p})  
        normalized["previews"] = fixed_previews  
  
    return normalized  
  
  
def normalize_chat_payload(raw: Dict[str, Any]) -> Dict[str, Any]:  
    """  
    Normalize a raw chat dictionary from extension/API into a clean dict for unified ChatThread model.  
    Minimal transformation — preserves extension field names.  
    """  
    normalized: Dict[str, Any] = dict(raw)  # shallow copy  
  
    # Normalize participants  
    normalized["participants"] = normalize_participants(raw.get("participants"))  
  
    # Ensure last_message_time normalized  
    if "last_message_time" in raw:  
        normalized["last_message_time"] = normalize_datetime(raw.get("last_message_time"))  
  
    return normalized  