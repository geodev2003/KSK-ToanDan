import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as _date

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, delete, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Base, engine, get_db
from . import models as m
from . import schemas as s
from .security import (
    hash_password, verify_password, create_token,
    get_current_user, require_admin, log_action,
    require_perm, user_can, ALL_PERMS, DEFAULT_USER_PERMS,
)
from . import his_client

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def _migrate_columns(sync_conn):
    """Tự thêm cột mới vào bảng cũ (không mất dữ liệu). Chạy cho cả SQLite lẫn Postgres."""
    insp = inspect(sync_conn)
    tables = insp.get_table_names()
    plan = {
        "expected": {
            "gioi_tinh": "VARCHAR(8)", "so_nha": "VARCHAR(64)", "khu_pho": "VARCHAR(128)",
            "phuong": "VARCHAR(128)", "tinh": "VARCHAR(128)", "dia_chi": "VARCHAR(255)",
            "so_dien_thoai": "VARCHAR(20)",
        },
        "records": {
            "so_dien_thoai": "VARCHAR(20)",
            "his_status": "VARCHAR(16)", "his_patient_code": "VARCHAR(32)",
            "his_patient_id": "VARCHAR(32)", "his_ticket_id": "VARCHAR(32)",
            "his_message": "VARCHAR(255)", "his_registered_at": "VARCHAR(32)",
        },
        "users": {"perms": "VARCHAR(255)"},
    }
    for table, cols in plan.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, typ in cols.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT ''"))
                # user cũ (trước khi có phân quyền) được cấp bộ quyền mặc định để vẫn dùng được
                if table == "users" and name == "perms":
                    sync_conn.execute(text(
                        "UPDATE users SET perms = :p WHERE role <> 'admin' AND (perms IS NULL OR perms = '')"
                    ), {"p": ",".join(DEFAULT_USER_PERMS)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_columns)
    # seed default admin if none exists
    from .database import SessionLocal
    async with SessionLocal() as db:
        res = await db.execute(select(func.count()).select_from(m.User).where(m.User.role == "admin"))
        if res.scalar() == 0:
            au = os.getenv("ADMIN_USERNAME", "admin")
            ap = os.getenv("ADMIN_PASSWORD", "Admin@2026")
            db.add(m.User(username=au, full_name="Quản trị viên",
                          hashed_password=hash_password(ap), role="admin", is_active=True))
            await db.commit()
            print(f"[seed] Created admin account: {au} / {ap}  (đổi mật khẩu ngay sau khi đăng nhập)")
    yield


app = FastAPI(title="KSK - Nhập liệu Đoàn Khám Sức Khỏe", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)


# =================== AUTH ===================
@app.post("/api/auth/login", response_model=s.Token)
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends(),
                db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.User).where(m.User.username == form.username))
    user = res.scalar_one_or_none()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Sai tài khoản hoặc mật khẩu")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Tài khoản đã bị khóa")
    await log_action(db, request, user, "LOGIN")
    await db.commit()
    return s.Token(access_token=create_token(user.username, user.role),
                   role=user.role, full_name=user.full_name or user.username, username=user.username)


@app.get("/api/me", response_model=s.UserOut)
async def me(user: m.User = Depends(get_current_user)):
    return user


@app.post("/api/me/password")
async def change_my_password(request: Request, payload: dict,
                             user: m.User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    old = (payload.get("old_password") or "")
    new = (payload.get("new_password") or "")
    if not verify_password(old, user.hashed_password):
        raise HTTPException(status_code=400, detail="Mật khẩu cũ không đúng")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="Mật khẩu mới quá ngắn")
    user.hashed_password = hash_password(new)
    await log_action(db, request, user, "CHANGE_PASSWORD")
    await db.commit()
    return {"ok": True}


# =================== USERS (admin) ===================
@app.get("/api/users", response_model=list[s.UserOut])
async def list_users(db: AsyncSession = Depends(get_db), _: m.User = Depends(require_admin)):
    res = await db.execute(select(m.User).order_by(m.User.id))
    return res.scalars().all()


@app.post("/api/users", response_model=s.UserOut)
async def create_user(request: Request, payload: s.UserCreate,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    exists = await db.execute(select(m.User).where(m.User.username == payload.username))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tên đăng nhập đã tồn tại")
    if payload.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Vai trò không hợp lệ")
    perms = payload.perms if payload.perms is not None else DEFAULT_USER_PERMS
    perms_csv = ",".join(p for p in perms if p in ALL_PERMS)
    user = m.User(username=payload.username.strip(), full_name=payload.full_name.strip(),
                  hashed_password=hash_password(payload.password), role=payload.role, perms=perms_csv)
    db.add(user)
    await log_action(db, request, admin, "CREATE_USER", "user", payload.username,
                     f"role={payload.role}")
    await db.commit()
    await db.refresh(user)
    return user


@app.put("/api/users/{uid}", response_model=s.UserOut)
async def update_user(request: Request, uid: int, payload: s.UserUpdate,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.User).where(m.User.id == uid))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    if payload.full_name is not None: user.full_name = payload.full_name.strip()
    if payload.role in ("admin", "user"): user.role = payload.role
    if payload.is_active is not None: user.is_active = payload.is_active
    if payload.password: user.hashed_password = hash_password(payload.password)
    if payload.perms is not None: user.perms = ",".join(p for p in payload.perms if p in ALL_PERMS)
    await log_action(db, request, admin, "UPDATE_USER", "user", user.username)
    await db.commit()
    await db.refresh(user)
    return user


@app.delete("/api/users/{uid}")
async def delete_user(request: Request, uid: int,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.User).where(m.User.id == uid))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Không thể tự xóa tài khoản đang đăng nhập")
    uname = user.username
    await db.delete(user)
    await log_action(db, request, admin, "DELETE_USER", "user", uname)
    await db.commit()
    return {"ok": True}


# =================== GROUPS ===================
async def _group_out(db, g: m.Group) -> s.GroupOut:
    rc = await db.scalar(select(func.count()).select_from(m.Record).where(m.Record.group_id == g.id))
    ec = await db.scalar(select(func.count()).select_from(m.Expected).where(m.Expected.group_id == g.id))
    return s.GroupOut(id=g.id, ma_doan=g.ma_doan, ten_doan=g.ten_doan,
                      thoi_gian_kham=g.thoi_gian_kham, dia_diem=g.dia_diem,
                      record_count=rc or 0, expected_count=ec or 0)


@app.get("/api/groups", response_model=list[s.GroupOut])
async def list_groups(db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    res = await db.execute(select(m.Group).order_by(m.Group.created_at.desc()))
    return [await _group_out(db, g) for g in res.scalars().all()]


@app.post("/api/groups", response_model=s.GroupOut)
async def create_group(request: Request, payload: s.GroupBase,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    exists = await db.execute(select(m.Group).where(m.Group.ma_doan == payload.ma_doan))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Mã đoàn đã tồn tại")
    g = m.Group(ma_doan=payload.ma_doan.strip(), ten_doan=payload.ten_doan.strip(),
                thoi_gian_kham=payload.thoi_gian_kham.strip(), dia_diem=payload.dia_diem.strip(),
                created_by=admin.username)
    db.add(g)
    await log_action(db, request, admin, "CREATE_GROUP", "group", payload.ma_doan, payload.ten_doan)
    await db.commit()
    await db.refresh(g)
    return await _group_out(db, g)


@app.put("/api/groups/{gid}", response_model=s.GroupOut)
async def update_group(request: Request, gid: int, payload: s.GroupUpdate,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.Group).where(m.Group.id == gid))
    g = res.scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    if payload.ten_doan is not None: g.ten_doan = payload.ten_doan.strip()
    if payload.thoi_gian_kham is not None: g.thoi_gian_kham = payload.thoi_gian_kham.strip()
    if payload.dia_diem is not None: g.dia_diem = payload.dia_diem.strip()
    await log_action(db, request, admin, "UPDATE_GROUP", "group", g.ma_doan)
    await db.commit()
    await db.refresh(g)
    return await _group_out(db, g)


@app.delete("/api/groups/{gid}")
async def delete_group(request: Request, gid: int,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.Group).where(m.Group.id == gid))
    g = res.scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    ma = g.ma_doan
    await db.delete(g)
    await log_action(db, request, admin, "DELETE_GROUP", "group", ma)
    await db.commit()
    return {"ok": True}


# =================== RECORDS ===================
@app.get("/api/groups/{gid}/meta")
async def group_meta(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    """Nhẹ — dùng cho polling: chỉ trả số lượng + mốc cập nhật cuối."""
    count = await db.scalar(select(func.count()).select_from(m.Record).where(m.Record.group_id == gid))
    last = await db.scalar(select(func.max(m.Record.updated_at)).where(m.Record.group_id == gid))
    return {"count": count or 0, "last_update": last.isoformat() if last else None}


@app.get("/api/groups/{gid}/records", response_model=list[s.RecordOut])
async def list_records(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    res = await db.execute(select(m.Record).where(m.Record.group_id == gid).order_by(m.Record.id))
    return res.scalars().all()


@app.post("/api/groups/{gid}/records", response_model=s.RecordOut)
async def create_record(request: Request, gid: int, payload: s.RecordBase,
                        force: bool = Query(False),
                        user: m.User = Depends(require_perm("create_record")), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    if not payload.ho_ten.strip():
        raise HTTPException(status_code=400, detail="Họ tên là bắt buộc")
    # duplicate check (same name + dob) — cảnh báo, cho phép ép thêm bằng force=true
    if not force and payload.ho_ten.strip():
        dup = await db.scalar(select(func.count()).select_from(m.Record).where(
            m.Record.group_id == gid,
            func.lower(m.Record.ho_ten) == payload.ho_ten.strip().lower(),
            m.Record.ngay_sinh == payload.ngay_sinh.strip(),
        ))
        if dup and dup > 0:
            raise HTTPException(status_code=409,
                detail=f"Đã có người trùng Họ tên + Ngày sinh ({dup}). Gửi lại với force=true để vẫn thêm.")
    rec = m.Record(group_id=gid, created_by=user.full_name or user.username,
                   updated_by=user.full_name or user.username,
                   **{k: (v or "").strip() for k, v in payload.model_dump().items()})
    db.add(rec)
    await log_action(db, request, user, "CREATE_RECORD", "record", "", f"{rec.ho_ten} | {g.ma_doan}")
    await db.commit()
    await db.refresh(rec)
    return rec


@app.put("/api/records/{rid}", response_model=s.RecordOut)
async def update_record(request: Request, rid: int, payload: s.RecordBase,
                        user: m.User = Depends(require_perm("edit_record")), db: AsyncSession = Depends(get_db)):
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    for k, v in payload.model_dump().items():
        setattr(rec, k, (v or "").strip())
    rec.updated_by = user.full_name or user.username
    await log_action(db, request, user, "UPDATE_RECORD", "record", str(rid), rec.ho_ten)
    await db.commit()
    await db.refresh(rec)
    return rec


@app.delete("/api/records/{rid}")
async def delete_record(request: Request, rid: int,
                        user: m.User = Depends(require_perm("delete_record")), db: AsyncSession = Depends(get_db)):
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    name = rec.ho_ten
    await db.delete(rec)
    await log_action(db, request, user, "DELETE_RECORD", "record", str(rid), name)
    await db.commit()
    return {"ok": True}


# =================== EXPECTED + COMPARE ===================
@app.get("/api/groups/{gid}/expected", response_model=list[s.ExpectedItem])
async def get_expected(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    res = await db.execute(select(m.Expected).where(m.Expected.group_id == gid))
    return res.scalars().all()


EXP_FIELDS = ["cccd", "ma_bhyt", "ho_ten", "ngay_sinh", "gioi_tinh",
              "so_nha", "khu_pho", "phuong", "tinh", "dia_chi", "so_dien_thoai"]


@app.put("/api/groups/{gid}/expected")
async def set_expected(request: Request, gid: int, items: list[s.ExpectedItem],
                       admin: m.User = Depends(require_perm("manage_expected")), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    await db.execute(delete(m.Expected).where(m.Expected.group_id == gid))
    for it in items:
        if it.ho_ten.strip():
            db.add(m.Expected(group_id=gid, **{k: (getattr(it, k) or "").strip() for k in EXP_FIELDS}))
    await log_action(db, request, admin, "IMPORT_EXPECTED", "group", g.ma_doan, f"{len(items)} người")
    await db.commit()
    return {"ok": True, "count": len(items)}


@app.post("/api/groups/{gid}/expected/item", response_model=s.ExpectedItem)
async def add_expected_item(request: Request, gid: int, payload: s.ExpectedItem,
                            admin: m.User = Depends(require_perm("manage_expected")), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    if not payload.ho_ten.strip():
        raise HTTPException(status_code=400, detail="Họ tên là bắt buộc")
    e = m.Expected(group_id=gid, **{k: (getattr(payload, k) or "").strip() for k in EXP_FIELDS})
    db.add(e)
    await log_action(db, request, admin, "CREATE_EXPECTED", "expected", "", f"{e.ho_ten} | {g.ma_doan}")
    await db.commit()
    await db.refresh(e)
    return e


@app.put("/api/expected/{eid}", response_model=s.ExpectedItem)
async def update_expected_item(request: Request, eid: int, payload: s.ExpectedItem,
                               admin: m.User = Depends(require_perm("manage_expected")), db: AsyncSession = Depends(get_db)):
    e = await db.scalar(select(m.Expected).where(m.Expected.id == eid))
    if not e:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi dự kiến")
    if not payload.ho_ten.strip():
        raise HTTPException(status_code=400, detail="Họ tên là bắt buộc")
    for k in EXP_FIELDS:
        setattr(e, k, (getattr(payload, k) or "").strip())
    await log_action(db, request, admin, "UPDATE_EXPECTED", "expected", str(eid), e.ho_ten)
    await db.commit()
    await db.refresh(e)
    return e


@app.delete("/api/expected/{eid}")
async def delete_expected_item(request: Request, eid: int,
                               admin: m.User = Depends(require_perm("manage_expected")), db: AsyncSession = Depends(get_db)):
    e = await db.scalar(select(m.Expected).where(m.Expected.id == eid))
    if not e:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi dự kiến")
    name = e.ho_ten
    await db.delete(e)
    await log_action(db, request, admin, "DELETE_EXPECTED", "expected", str(eid), name)
    await db.commit()
    return {"ok": True}


@app.post("/api/expected/bulk_delete")
async def bulk_delete_expected(request: Request, payload: dict,
                               admin: m.User = Depends(require_perm("manage_expected")), db: AsyncSession = Depends(get_db)):
    ids = [int(x) for x in (payload.get("ids") or [])]
    if not ids:
        return {"ok": True, "deleted": 0}
    await db.execute(delete(m.Expected).where(m.Expected.id.in_(ids)))
    await log_action(db, request, admin, "DELETE_EXPECTED", "expected", "", f"{len(ids)} người")
    await db.commit()
    return {"ok": True, "deleted": len(ids)}


def _match_key(cccd: str, ho_ten: str, ngay_sinh: str) -> str:
    if cccd and len(cccd) >= 9:
        return "cccd:" + cccd
    return "nb:" + (ho_ten or "").strip().lower() + "|" + (ngay_sinh or "").strip()


@app.get("/api/groups/{gid}/compare")
async def compare(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    exp = (await db.execute(select(m.Expected).where(m.Expected.group_id == gid))).scalars().all()
    act = (await db.execute(select(m.Record).where(m.Record.group_id == gid))).scalars().all()
    act_keys = {_match_key(r.cccd, r.ho_ten, r.ngay_sinh) for r in act}
    exp_keys = {_match_key(e.cccd, e.ho_ten, e.ngay_sinh) for e in exp}
    came = [e for e in exp if _match_key(e.cccd, e.ho_ten, e.ngay_sinh) in act_keys]
    absent = [e for e in exp if _match_key(e.cccd, e.ho_ten, e.ngay_sinh) not in act_keys]
    extra = [r for r in act if _match_key(r.cccd, r.ho_ten, r.ngay_sinh) not in exp_keys]
    fmt = lambda x: {"cccd": x.cccd, "ho_ten": x.ho_ten, "ngay_sinh": x.ngay_sinh}
    return {
        "expected_total": len(exp), "actual_total": len(act),
        "came": [fmt(x) for x in came],
        "absent": [fmt(x) for x in absent],
        "extra": [fmt(x) for x in extra],
    }


# =================== LOGS (admin) ===================
@app.get("/api/logs", response_model=list[s.LogOut])
async def get_logs(limit: int = Query(200, le=1000), offset: int = 0,
                   username: str = "", action: str = "",
                   _: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    q = select(m.AuditLog).order_by(m.AuditLog.ts.desc())
    if username:
        q = q.where(m.AuditLog.username == username)
    if action:
        q = q.where(m.AuditLog.action == action)
    q = q.limit(limit).offset(offset)
    res = await db.execute(q)
    return res.scalars().all()


# =================== HIS INTEGRATION ===================
def _now_vn_iso():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).replace(microsecond=0).isoformat()


@app.get("/api/his/config")
async def his_get_config(_: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cfg = await his_client.get_config(db)
    # không trả list_services (rất dài) — chỉ báo đã nạp hay chưa
    return {
        "base_url": cfg["base_url"], "appkey": cfg["appkey"], "userkey": cfg["userkey"],
        "ethnic_group_id": cfg["ethnic_group_id"], "nationality": cfg["nationality"],
        "ticket_prefix": cfg["ticket_prefix"], "address": cfg["address"], "package": cfg["package"],
        "services_loaded": len(cfg.get("list_services") or []),
    }


@app.put("/api/his/config")
async def his_put_config(request: Request, payload: dict,
                         admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    allowed = {"base_url", "appkey", "userkey", "ethnic_group_id", "nationality",
               "ticket_prefix", "address", "package"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    # nếu đổi gói khám -> xóa cache dịch vụ để nạp lại
    if "package" in patch:
        patch["list_services"] = []
    cfg = await his_client.save_config(db, patch)
    await log_action(db, request, admin, "HIS_CONFIG", "his", "", "cập nhật cấu hình HIS")
    await db.commit()
    return {"ok": True, "services_loaded": len(cfg.get("list_services") or [])}


@app.post("/api/his/test")
async def his_test(_: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cfg = await his_client.get_config(db)
    try:
        msg = await his_client.test_connection(db, cfg)
        return {"ok": True, "message": msg}
    except his_client.HisError as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/his/refresh-services")
async def his_refresh_services(_: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cfg = await his_client.get_config(db)
    cfg["list_services"] = []
    try:
        cfg = await his_client.ensure_services(db, cfg)
        return {"ok": True, "count": len(cfg.get("list_services") or [])}
    except his_client.HisError as e:
        return {"ok": False, "message": str(e)}


async def _register_record(db, request, actor, rec, cfg, force):
    """Đăng ký 1 record; cập nhật trạng thái vào DB. Trả về (ok, message, info)."""
    if rec.his_status == "registered" and rec.his_ticket_id and not force:
        return True, "Đã đăng ký trước đó (bỏ qua).", {
            "patient_code": rec.his_patient_code, "ticket_id": rec.his_ticket_id}
    try:
        res = await his_client.register_one(db, rec, cfg)
    except his_client.HisError as e:
        rec.his_status = "error"; rec.his_message = str(e)[:255]
        await db.commit()
        return False, str(e), None
    except Exception as e:  # noqa
        rec.his_status = "error"; rec.his_message = f"Lỗi hệ thống: {e}"[:255]
        await db.commit()
        return False, str(e), None
    rec.his_status = "registered"
    rec.his_patient_code = res["patient_code"]; rec.his_patient_id = res["patient_id"]
    rec.his_ticket_id = res["ticket_id"]; rec.his_message = ""
    rec.his_registered_at = _now_vn_iso()
    await db.commit()
    await log_action(db, request, actor, "HIS_REGISTER", "record", str(rec.id),
                     f"patient_code={res['patient_code']} ticket={res['ticket_id']}")
    await db.commit()
    return True, "Đăng ký thành công.", res


@app.post("/api/records/{rid}/his-register")
async def his_register_record(request: Request, rid: int, payload: s.HisRegisterOne = None,
                              user: m.User = Depends(require_perm("his_register")),
                              db: AsyncSession = Depends(get_db)):
    rec = (await db.execute(select(m.Record).where(m.Record.id == rid))).scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    force = bool(payload.force) if payload else False
    cfg = await his_client.get_config(db)
    ok, msg, res = await _register_record(db, request, user, rec, cfg, force)
    await db.refresh(rec)
    return {"ok": ok, "message": msg, "record": s.RecordOut.model_validate(rec)}


@app.post("/api/groups/{gid}/his-register-bulk")
async def his_register_bulk(request: Request, gid: int, payload: s.HisBulkRegister,
                            user: m.User = Depends(require_perm("his_register")),
                            db: AsyncSession = Depends(get_db)):
    cfg = await his_client.get_config(db)
    # kiểm tra cấu hình sớm để báo lỗi gọn thay vì lặp
    if not cfg.get("appkey") or not cfg.get("userkey"):
        raise HTTPException(status_code=400, detail="Chưa cấu hình appkey/userkey của HIS.")
    results = []
    ok_count = 0
    for rid in payload.record_ids:
        rec = (await db.execute(select(m.Record).where(m.Record.id == rid, m.Record.group_id == gid))).scalar_one_or_none()
        if not rec:
            results.append({"id": rid, "ho_ten": "", "ok": False, "message": "Không tìm thấy"})
            continue
        ok, msg, res = await _register_record(db, request, user, rec, cfg, payload.force)
        ok_count += 1 if ok else 0
        results.append({"id": rid, "ho_ten": rec.ho_ten, "ok": ok, "message": msg,
                        "patient_code": rec.his_patient_code, "ticket_id": rec.his_ticket_id})
    return {"total": len(payload.record_ids), "success": ok_count,
            "failed": len(payload.record_ids) - ok_count, "results": results}


# =================== REPORTS ===================
VN_OFFSET = timedelta(hours=7)  # Asia/Ho_Chi_Minh


def _to_local(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt + VN_OFFSET


def _bucket(local_dt, period):
    d = local_dt.date()
    if period == "month":
        return f"{d.year}-{d.month:02d}", f"Tháng {d.month:02d}/{d.year}"
    if period == "week":
        iso = d.isocalendar()
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat(), f"Tuần {iso[1]:02d}/{iso[0]} (từ {monday.strftime('%d/%m')})"
    return d.isoformat(), d.strftime("%d/%m/%Y")  # day


def _age_from(ngay_sinh: str, today: _date):
    """Tính tuổi từ chuỗi dd/mm/yyyy. Trả None nếu không hợp lệ."""
    try:
        parts = (ngay_sinh or "").strip().split("/")
        if len(parts) != 3:
            return None
        d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
        bd = _date(y, mo, d)
    except (ValueError, TypeError):
        return None
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age if 0 <= age < 150 else None


# nhóm tuổi: (key, nhãn, hàm kiểm tra)
AGE_GROUPS = [
    ("child",   "Trẻ em (<16)",        lambda a: a is not None and a < 16),
    ("adult",   "Người lớn (16–59)",   lambda a: a is not None and 16 <= a < 60),
    ("elderly", "Người cao tuổi (≥60)", lambda a: a is not None and a >= 60),
    ("unknown", "Không rõ tuổi",        lambda a: a is None),
]


@app.get("/api/reports")
async def reports(period: str = "day", date_from: str = "", date_to: str = "",
                  group_id: int | None = None,
                  user: m.User = Depends(require_perm("view_reports")),
                  db: AsyncSession = Depends(get_db)):
    if period not in ("day", "week", "month"):
        period = "day"
    today = (datetime.now(timezone.utc) + VN_OFFSET).date()
    try:
        to_d = _date.fromisoformat(date_to) if date_to else today
    except ValueError:
        to_d = today
    try:
        from_d = _date.fromisoformat(date_from) if date_from else (to_d - timedelta(days=29))
    except ValueError:
        from_d = to_d - timedelta(days=29)

    q = select(m.Record.created_at, m.Record.gioi_tinh, m.Record.group_id, m.Record.ngay_sinh)
    if group_id:
        q = q.where(m.Record.group_id == group_id)
    rows = (await db.execute(q)).all()

    groups = {g.id: g for g in (await db.execute(select(m.Group))).scalars().all()}

    series = {}
    by_group = {}
    age_count = {k: {"key": k, "label": lab, "count": 0, "male": 0, "female": 0} for k, lab, _ in AGE_GROUPS}
    total = male = female = 0
    for created_at, gt, gid, ns in rows:
        loc = _to_local(created_at)
        if loc is None:
            continue
        d = loc.date()
        if d < from_d or d > to_d:
            continue
        total += 1
        is_m = gt == "Nam"; is_f = gt == "Nữ"
        male += is_m; female += is_f
        key, label = _bucket(loc, period)
        b = series.setdefault(key, {"key": key, "label": label, "count": 0, "male": 0, "female": 0})
        b["count"] += 1; b["male"] += is_m; b["female"] += is_f
        g = groups.get(gid)
        gk = by_group.setdefault(gid, {
            "ma_doan": g.ma_doan if g else "?", "ten_doan": g.ten_doan if g else "(đã xóa)",
            "count": 0, "male": 0, "female": 0})
        gk["count"] += 1; gk["male"] += is_m; gk["female"] += is_f
        # nhóm tuổi
        age = _age_from(ns, today)
        for k, _lab, test in AGE_GROUPS:
            if test(age):
                age_count[k]["count"] += 1; age_count[k]["male"] += is_m; age_count[k]["female"] += is_f
                break

    return {
        "period": period, "from": from_d.isoformat(), "to": to_d.isoformat(),
        "total": total, "male": male, "female": female, "other": total - male - female,
        "series": sorted(series.values(), key=lambda x: x["key"]),
        "by_group": sorted(by_group.values(), key=lambda x: -x["count"]),
        "by_age": [age_count[k] for k, _, _ in AGE_GROUPS],
    }


# =================== STATIC FRONTEND ===================
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
