"""
Core authentication utilities: password hashing, JWT creation/verification.
Uses bcrypt directly (compatible with bcrypt 4.x) rather than passlib.

Phase 5B: TokenData now includes `role` field.
get_current_user looks up user_profiles table in Supabase to fetch role.
Falls back to role='user' gracefully if table lookup fails.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import logging
import bcrypt
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from config import settings

log = logging.getLogger("auth")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


class TokenData(BaseModel):
    email: Optional[str] = None
    role:  str = "user"   # 'user' | 'admin'  — loaded from user_profiles


class UserInDB(BaseModel):
    email:           str
    hashed_password: str
    is_active:       bool = True


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def _fetch_role(email: str) -> str:
    """
    Look up role from user_profiles table.
    Returns 'user' as default if not found or on any error.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        return "user"
    try:
        from supabase import create_client
        from supabase.lib.client_options import ClientOptions
        client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
            options=ClientOptions(auto_refresh_token=False, persist_session=False),
        )
        result = (
            client.table("user_profiles")
            .select("role")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("role", "user")
    except Exception as e:
        log.warning(f"[auth] user_profiles lookup failed for {email}: {e}")
    return "user"


async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenData:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        role = await _fetch_role(email)
        return TokenData(email=email, role=role)
    except JWTError:
        raise credentials_exception
