import json  
import os  
import logging  
from typing import Any  
  
from app.utils.time import timestamp_str  
  
# -----------------------------------------------------------------------------  
# Configure application-wide logger  
# -----------------------------------------------------------------------------  
logger = logging.getLogger("onlyfans_analytics")  
logger.setLevel(logging.DEBUG)  # Change to INFO in production  
  
# Avoid duplicate handlers if this file is imported multiple times  
if not logger.handlers:  
    console_handler = logging.StreamHandler()  
    console_handler.setFormatter(  
        logging.Formatter(  
            "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",  
            datefmt="%Y-%m-%d %H:%M:%S",  
        )  
    )  
    logger.addHandler(console_handler)  
  
# -----------------------------------------------------------------------------  
# JSON logging helper for saving payloads to file  
# -----------------------------------------------------------------------------  
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
        logger.info(f"[LOG] Saved raw response to {filepath}")  
    except Exception as e:  
        logger.error(f"[LOG ERROR] Could not save log file {filepath}: {e}")  
  
    return filepath  