# Time handling utilities

from datetime import datetime, timezone  
  
  
def utcnow() -> datetime:  
    """  
    Get the current UTC datetime with timezone info.  
    """  
    return datetime.now(timezone.utc)  
  
  
def timestamp_str(fmt: str = "%Y%m%d_%H%M%S") -> str:  
    """  
    Get a UTC timestamp string in the given format.  
    Default: YYYYMMDD_HHMMSS  
    """  
    return utcnow().strftime(fmt)  
  
  
def iso_timestamp() -> str:  
    """  
    Get the current UTC time as an ISO 8601 string (e.g., 2024-06-05T15:30:45Z).  
    """  
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")  