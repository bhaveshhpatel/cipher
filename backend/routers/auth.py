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


def _get_supabase_admin() -> Client | None:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        return None
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


def _get_supabase_client() -> Client | None:
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_KEY,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


def _get_user_from_supabase(email: str):
    client = _get_supabase_admin()
    if not client:
        return None
    try:
        users = client.auth.admin.list_users()
        for user in users:
            if getattr(user, 'email', None) == email:
                return user
    except Exception:
        return None
    return None


# ── Explicit OPTIONS handlers so CORS preflight never gets a 400 ──────────
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

    client = _get_supabase_admin()
    if client:
        existing = _get_user_from_supabase(str(body.email))
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")
        try:
            client.auth.admin.create_user({
                "email": str(body.email),
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {"source": "cipher"},
            })
            log.info("Registered user via Supabase: %s", body.email)
            return {"message": "Account created successfully"}
        except Exception as e:
            log.error("Supabase register error: %s", e)
            raise HTTPException(status_code=500, detail=f"Registration failed: {e}")

    # In-memory fallback (no Supabase configured)
    if body.email in _users:
        raise HTTPException(status_code=409, detail="Email already registered")
    _users[str(body.email)] = hash_password(body.password)
    log.info("Registered user in-memory: %s", body.email)
    return {"message": "Account created successfully"}


@router.post("/token", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    supabase = _get_supabase_client()
    if supabase:
        try:
            auth_res = supabase.auth.sign_in_with_password(
                {"email": form.username, "password": form.password}
            )
            if getattr(auth_res, 'user', None):
                token = create_access_token({"sub": form.username})
                log.info("Login via Supabase: %s", form.username)
                return {"access_token": token, "token_type": "bearer"}
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except HTTPException:
            raise
        except Exception as e:
            err = str(e).lower()
            if "invalid" in err or "credentials" in err or "not found" in err or "email" in err:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            log.error("Supabase login error: %s", e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable. Please try again.",
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
    return {"email": current_user.email}
