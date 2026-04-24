from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from core.auth import hash_password, verify_password, create_access_token, get_current_user, TokenData
from config import settings
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
import logging

log = logging.getLogger("auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])
_users: dict[str, str] = {}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


def _supabase_admin() -> Client | None:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        log.warning("Supabase admin creds not configured — falling back to in-memory store")
        return None
    try:
        return create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
            options=ClientOptions(auto_refresh_token=False, persist_session=False),
        )
    except Exception as exc:
        log.error("Failed to create Supabase admin client: %s", exc)
        return None


def _supabase_client() -> Client | None:
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        log.warning("Supabase anon creds not configured — falling back to in-memory store")
        return None
    try:
        return create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_KEY,
            options=ClientOptions(auto_refresh_token=False, persist_session=False),
        )
    except Exception as exc:
        log.error("Failed to create Supabase anon client: %s", exc)
        return None


def _find_user_by_email(email: str):
    client = _supabase_admin()
    if not client:
        return None
    try:
        users = client.auth.admin.list_users()
        for user in users:
            if getattr(user, "email", None) == email:
                return user
    except Exception as exc:
        log.warning("list_users failed: %s", exc)
    return None


@router.options("/register")
async def options_register():
    return Response(status_code=200)


@router.options("/token")
async def options_token():
    return Response(status_code=200)


@router.post("/register", response_model=MessageResponse, status_code=201)
async def register(body: RegisterRequest):
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    client = _supabase_admin()

    if client:
        if _find_user_by_email(str(body.email)):
            raise HTTPException(status_code=409, detail="Email already registered")
        try:
            result = client.auth.admin.create_user({
                "email": str(body.email),
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {"source": "cipher"},
            })
            if not getattr(result, "user", None):
                raise HTTPException(status_code=500, detail="Registration failed: Supabase did not return a user")
            log.info("Registered via Supabase: %s", body.email)
            return {"message": "Account created successfully"}
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Supabase register error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Registration failed: {exc}")

    if str(body.email) in _users:
        raise HTTPException(status_code=409, detail="Email already registered")
    _users[str(body.email)] = hash_password(body.password)
    log.info("Registered in-memory: %s", body.email)
    return {"message": "Account created successfully"}


@router.post("/token", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    supabase = _supabase_client()

    if supabase:
        try:
            auth_res = supabase.auth.sign_in_with_password(
                {"email": form.username, "password": form.password}
            )
            if not getattr(auth_res, "user", None):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = create_access_token({"sub": form.username})
            log.info("Login via Supabase: %s", form.username)
            return {"access_token": token, "token_type": "bearer"}
        except HTTPException:
            raise
        except Exception as exc:
            err_lower = str(exc).lower()
            if any(k in err_lower for k in ("invalid", "credentials", "not found", "email", "password", "user")):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            log.error("Supabase login error: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again in a moment.",
            )

    hashed = _users.get(form.username)
    if not hashed or not verify_password(form.password, hashed):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token({"sub": form.username})
    log.info("Login via in-memory fallback: %s", form.username)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
async def me(current_user: TokenData = Depends(get_current_user)):
    """Returns email + role. Frontend uses this to gate admin UI."""
    return {"email": current_user.email, "role": current_user.role}
