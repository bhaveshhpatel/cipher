from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from core.auth import hash_password, verify_password, create_access_token, get_current_user, TokenData
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# In-memory user store (replace with Supabase in production)
# ---------------------------------------------------------------------------
_users: dict[str, str] = {}   # email → hashed_password

class RegisterRequest(BaseModel):
    email:    EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"

class MessageResponse(BaseModel):
    message: str

@router.post("/register", response_model=MessageResponse, status_code=201)
async def register(body: RegisterRequest):
    if body.email in _users:
        raise HTTPException(status_code=409, detail="Email already registered")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    _users[body.email] = hash_password(body.password)
    return {"message": "Account created successfully"}

@router.post("/token", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    hashed = _users.get(form.username)
    if not hashed or not verify_password(form.password, hashed):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token({"sub": form.username})
    return {"access_token": token, "token_type": "bearer"}

@router.get("/me")
async def me(current_user: TokenData = Depends(get_current_user)):
    return {"email": current_user.email}
