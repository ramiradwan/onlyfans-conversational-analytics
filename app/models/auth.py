from pydantic import BaseModel  
from typing import Optional  
  
class AuthData(BaseModel):  
    """  
    Authentication data for OnlyFans API.  
    For extension-based ingestion, auth_cookie may be omitted.  
    """  
    auth_cookie: Optional[str] = None  