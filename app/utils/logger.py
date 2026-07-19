import logging  
  
# -----------------------------------------------------------------------------  
# Configure application-wide logger  
# -----------------------------------------------------------------------------  
logger = logging.getLogger("onlyfans_analytics")  
logger.setLevel(logging.INFO)
  
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
