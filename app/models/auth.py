from pydantic import BaseModel  
  
class AuthData(BaseModel):  
    """Authentication data for OnlyFans API requests."""  
    auth_cookie: str  