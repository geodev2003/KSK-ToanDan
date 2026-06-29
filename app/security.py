import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .models import User, AuditLog

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-in-production-2026")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "12"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


# ---------- Password ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------- JWT ----------
def create_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Phiên đăng nhập không hợp lệ hoặc đã hết hạn",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise cred_exc
    except jwt.PyJWTError:
        raise cred_exc

    res = await db.execute(select(User).where(User.username == username))
    user = res.scalar_one_or_none()
    if not user or not user.is_active:
        raise cred_exc
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Chỉ admin mới có quyền thực hiện thao tác này")
    return user


# ---------- Audit log ----------
async def log_action(
    db: AsyncSession,
    request: Request,
    user: User,
    action: str,
    entity: str = "",
    entity_id: str = "",
    detail: str = "",
):
    ip = request.client.host if request.client else ""
    # honor reverse-proxy / Cloudflare header
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if fwd:
        ip = fwd.split(",")[0].strip()
    db.add(AuditLog(
        username=user.username, role=user.role, action=action,
        entity=entity, entity_id=str(entity_id), detail=detail, ip=ip,
    ))
    # committed by caller
