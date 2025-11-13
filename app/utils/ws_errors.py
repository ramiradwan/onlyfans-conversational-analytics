"""  
Utility for broadcasting system_error messages to frontend clients.  
Ensures consistent payload structure and channel naming across the Brain.  
"""  
  
from app.models.wss import SystemErrorMsg  
from app.models.core import WssError  
from app.core.broadcast import broadcast  
from app.utils.logger import logger  
  
  
async def broadcast_system_error(user_id: str, code: str, message: str, detail: str | None = None) -> None:  
    """  
    Broadcast a system_error message to the frontend channel for the given user.  
  
    :param user_id: The user ID to target.  
    :param code: Short error code string (snake_case).  
    :param message: Human-readable error message.  
    :param detail: Optional detailed string for debugging.  
    """  
    try:  
        err_payload = WssError(  
            code=code,  
            errorMessage=message,  
            detail=detail  
        )  
        err_msg = SystemErrorMsg(  
            type="system_error",  
            payload=err_payload  
        )  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=err_msg.model_dump_json()  
        )  
    except Exception as e:  
        logger.exception(f"[WS_ERRORS] Failed to broadcast system_error for {user_id}: {e}")  