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


# JSON login schema — used by the /login endpoint tests expect
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Module-level helpers — exposed so tests can patch.object them
# ---------------------------------------------------------------------------

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
    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        log.warning("Supabase anon creds not configured — falling back to in-memory store")
        return None
    try:
        return create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_ANON_KEY,
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


async def _authenticate_user(email: str, password: str) -> dict | None:
    """
    Verify credentials. Returns user dict on success, None on failure.
    Module-level so tests can patch.object(auth_mod, '_authenticate_user', ...).
    """
    # In-memory fallback path
    hashed = _users.get(email)
    if hashed and verify_password(password, hashed):
        return {"email": email, "role": "user"}

    # Supabase path
    supabase = _supabase_client()
    if supabase:
        try:
            auth_res = supabase.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            if getattr(auth_res, "user", None):
                log.info("[auth] Supabase sign-in OK: %s", email)
                return {"email": email, "role": "user"}
        except Exception as exc:
            log.warning("[auth] Supabase sign-in error for %s: %s", email, exc)

    # Check Supabase user_profiles table as secondary lookup
    try:
        from services.universe_store import _client as _sb
        sb = _sb()
        if sb:
            res = (
                sb.table("user_profiles")
                .select("email,role")
                .eq("email", email)
                .maybe_single()
                .execute()
            )
            data = getattr(res, "data", None)
            if data and data.get("email") == email:
                stored_hash = data.get("password_hash", "")
                if verify_password(password, stored_hash):
                    return {"email": email, "role": data.get("role", "user")}
    except Exception as exc:
        log.warning("[auth] user_profiles lookup failed for %s: %s", email, exc)

    return None


def _create_access_token(data: dict) -> str:
    """
    Thin wrapper around core.auth.create_access_token.
    Module-level so tests can patch.object(auth_mod, '_create_access_token', ...).
    """
    return create_access_token(data)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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


@router.post("/login", response_model=TokenResponse)
async def login_json(body: LoginRequest):
    """
    JSON body login endpoint — used by the frontend React app and tests.
    Accepts {"email": "...", "password": "..."} application/json.
    """
    user = await _authenticate_user(str(body.email), body.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = _create_access_token({"sub": user["email"]})
    log.info("Login via /login: %s", body.email)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/token", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    """
    OAuth2 form-data login — kept for OAuth2 compatibility / Swagger UI.
    """
    user = await _authenticate_user(form.username, form.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = _create_access_token({"sub": form.username})
    log.info("Login via /token: %s", form.username)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
async def me(current_user: TokenData = Depends(get_current_user)):
    """Returns email + role. Frontend uses this to gate admin UI."""
    return {"email": current_user.email, "role": current_user.role}
