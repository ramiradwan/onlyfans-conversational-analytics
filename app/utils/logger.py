import json  
import os  
from typing import Any  
  
from app.utils.time import timestamp_str  
  
  
def log_json(payload: Any, endpoint_name: str, log_dir: str = "logs") -> str:  
    """  
    Save a JSON-serializable payload to a timestamped file in log_dir.  
  
    Args:  
        payload: The Python object to log (must be JSON serializable).  
        endpoint_name: Short name of the endpoint or action.  
        log_dir: Directory where log files are stored.  
  
    Returns:  
        The file path of the saved log.  
    """  
    os.makedirs(log_dir, exist_ok=True)  
    filename = f"{endpoint_name}_{timestamp_str()}.json"  
    filepath = os.path.join(log_dir, filename)  
  
    try:  
        with open(filepath, "w", encoding="utf-8") as f:  
            json.dump(payload, f, ensure_ascii=False, indent=2)  
        print(f"[LOG] Saved raw response to {filepath}")  
    except Exception as e:  
        print(f"[LOG ERROR] Could not save log file {filepath}: {e}")  
  
    return filepath  