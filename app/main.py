import os
import json
import re as _re
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as _date

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.responses import Response
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
from . import docx_forms

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def _migrate_columns(sync_conn):
    """Tự thêm cột mới vào bảng cũ (không mất dữ liệu). Chạy cho cả SQLite lẫn Postgres."""
    insp = inspect(sync_conn)
    tables = insp.get_table_names()
    plan = {
        "expected": {
            "gioi_tinh": "VARCHAR(8)", "dan_toc": "VARCHAR(64)", "quoc_tich": "VARCHAR(64)",
            "so_nha": "VARCHAR(64)", "khu_pho": "VARCHAR(128)",
            "phuong": "VARCHAR(128)", "tinh": "VARCHAR(128)", "dia_chi": "VARCHAR(255)",
            "so_dien_thoai": "VARCHAR(20)",
        },
        "groups": {
            "his_package_id": "VARCHAR(32)", "his_package_code": "VARCHAR(64)",
            "his_package_name": "VARCHAR(255)", "his_package_price": "VARCHAR(32)",
            "his_service_flags": "TEXT",
        },
        "records": {
            "so_dien_thoai": "VARCHAR(20)",
            "career_id": "VARCHAR(16)", "dan_toc": "VARCHAR(64)", "ethnic_group_id": "VARCHAR(16)",
            "quoc_tich": "VARCHAR(64)", "nationality_id": "VARCHAR(16)", "province_id": "VARCHAR(16)",
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

    # Mở rộng độ dài các cột địa chỉ / thông tin dài trên Postgres để tránh văng lỗi truncation
    if "postgresql" in sync_conn.dialect.name:
        for tbl, cols in [("records", ["so_nha", "khu_pho", "phuong", "tinh", "nghe_nghiep", "his_message"]),
                          ("expected", ["so_nha", "khu_pho", "phuong", "tinh", "dia_chi"])]:
            if tbl in tables:
                existing = {c["name"] for c in insp.get_columns(tbl)}
                for col_name in cols:
                    if col_name in existing:
                        try:
                            sync_conn.execute(text(f"ALTER TABLE {tbl} ALTER COLUMN {col_name} TYPE VARCHAR(500)"))
                            sync_conn.commit()
                        except Exception:
                            pass


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
    token = create_token(user.username, user.role)
    await log_action(db, request, user, "LOGIN", "user", str(user.id), f"Role: {user.role}")
    await db.commit()
    return s.Token(access_token=token, role=user.role, full_name=user.full_name, username=user.username)


@app.get("/api/me")
async def get_me(user: m.User = Depends(get_current_user)):
    return {
        "id": user.id, "username": user.username, "full_name": user.full_name,
        "role": user.role, "perms": user.perms or "",
        "all_perms": ALL_PERMS if user.role == "admin" else [p for p in (user.perms or "").split(",") if p],
    }


@app.post("/api/me/password")
async def change_my_password(request: Request, payload: dict,
                             user: m.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    old_pw = payload.get("old_password", "")
    new_pw = payload.get("new_password", "")
    if not verify_password(old_pw, user.hashed_password):
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không đúng")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu mới phải từ 6 ký tự")
    user.hashed_password = hash_password(new_pw)
    await log_action(db, request, user, "CHANGE_PASSWORD", "user", str(user.id), "Đổi mật khẩu cá nhân")
    await db.commit()
    return {"ok": True}


# =================== GROUPS ===================
@app.get("/api/groups", response_model=list[s.GroupOut])
async def list_groups(db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    stmt = (
        select(
            m.Group,
            func.count(func.distinct(m.Record.id)).label("record_count"),
            func.count(func.distinct(m.Expected.id)).label("expected_count"),
        )
        .outerjoin(m.Record, m.Record.group_id == m.Group.id)
        .outerjoin(m.Expected, m.Expected.group_id == m.Group.id)
        .group_by(m.Group.id)
        .order_by(m.Group.created_at.desc())
    )
    res = await db.execute(stmt)
    out = []
    for g, rc, ec in res.all():
        item = s.GroupOut.model_validate(g)
        item.record_count = rc or 0
        item.expected_count = ec or 0
        out.append(item)
    return out


@app.post("/api/groups", response_model=s.GroupOut)
async def create_group(request: Request, payload: s.GroupBase,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    ex = await db.execute(select(m.Group).where(m.Group.ma_doan == payload.ma_doan.strip()))
    if ex.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Mã đoàn đã tồn tại")
    g = m.Group(**payload.model_dump(), created_by=admin.full_name or admin.username)
    db.add(g)
    await log_action(db, request, admin, "CREATE_GROUP", "group", "", f"{g.ma_doan} - {g.ten_doan}")
    await db.commit()
    await db.refresh(g)
    return s.GroupOut.model_validate(g)


@app.put("/api/groups/{gid}", response_model=s.GroupOut)
async def update_group(request: Request, gid: int, payload: s.GroupUpdate,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    for k, v in payload.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(g, k, v)
    await log_action(db, request, admin, "UPDATE_GROUP", "group", str(gid), g.ten_doan)
    await db.commit()
    await db.refresh(g)
    return s.GroupOut.model_validate(g)


@app.delete("/api/groups/{gid}")
async def delete_group(request: Request, gid: int,
                       admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    name = g.ten_doan
    await db.delete(g)
    await log_action(db, request, admin, "DELETE_GROUP", "group", str(gid), name)
    await db.commit()
    return {"ok": True}


# =================== RECORDS ===================
@app.get("/api/groups/{gid}/records", response_model=list[s.RecordOut])
async def list_records(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    res = await db.execute(select(m.Record).where(m.Record.group_id == gid).order_by(m.Record.created_at.asc()))
    return res.scalars().all()


@app.get("/api/groups/{gid}/meta")
async def group_meta(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    cnt = await db.scalar(select(func.count()).select_from(m.Record).where(m.Record.group_id == gid))
    max_ts = await db.scalar(select(func.max(m.Record.updated_at)).where(m.Record.group_id == gid))
    return {"count": cnt or 0, "last_updated": max_ts.isoformat() if max_ts else ""}


@app.post("/api/groups/{gid}/records", response_model=s.RecordOut)
async def create_record(request: Request, gid: int, payload: s.RecordBase, force: bool = False,
                        user: m.User = Depends(require_perm("create_record")), db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    if not payload.ho_ten.strip():
        raise HTTPException(status_code=400, detail="Họ tên là bắt buộc")

    # Cảnh báo trùng Họ tên + Ngày sinh trong cùng đoàn (trừ khi force=true)
    if not force and payload.ho_ten.strip() and payload.ngay_sinh.strip():
        dup = await db.scalar(
            select(m.Record).where(
                m.Record.group_id == gid,
                func.lower(m.Record.ho_ten) == payload.ho_ten.strip().lower(),
                m.Record.ngay_sinh == payload.ngay_sinh.strip()
            )
        )
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"Trùng tên & ngày sinh với bản ghi hiện có ({dup.ho_ten} - {dup.ngay_sinh})"
            )

    rec = m.Record(
        group_id=gid, created_by=user.full_name or user.username,
        updated_by=user.full_name or user.username,
        **{k: (v or "").strip() for k, v in payload.model_dump().items()}
    )
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


@app.post("/api/groups/{gid}/records/bulk-import")
async def bulk_import_records(request: Request, gid: int, payload: s.RecordBulkImport,
                              user: m.User = Depends(require_perm("create_record")),
                              db: AsyncSession = Depends(get_db)):
    """Nhập hàng loạt bản ghi từ Excel (khác với Expected — đây là DS để đăng ký HIS thật).
    Trùng họ tên + ngày sinh sẽ bị bỏ qua trừ khi force=true (force=true sẽ cập nhật dữ liệu mới).
    Nếu đoàn chưa có mã gói HIS và payload có kèm his_package_code, sẽ tự điền vào đoàn."""
    def _str(val, max_len=500):
        if val is None:
            return ""
        s = str(val).strip()
        return s[:max_len] if max_len else s

    try:
        g = await db.scalar(select(m.Group).where(m.Group.id == gid))
        if not g:
            raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")

        code = _str(payload.his_package_code)
        if code and not _str(g.his_package_code):
            g.his_package_code = code

        res = await db.execute(select(m.Record).where(m.Record.group_id == gid))
        existing_records = res.scalars().all()
        existing_map = {(_str(r.ho_ten).lower(), _str(r.ngay_sinh)): r for r in existing_records}

        created, updated, skipped = 0, 0, []
        for item in payload.records:
            name = _str(item.ho_ten)
            if not name:
                skipped.append({"ho_ten": "", "reason": "Thiếu họ tên"})
                continue
            key = (name.lower(), _str(item.ngay_sinh))

            if key in existing_map:
                if payload.force:
                    rec = existing_map[key]
                    for k, v in item.model_dump().items():
                        val = _str(v)
                        if val and hasattr(rec, k):
                            setattr(rec, k, val)
                    rec.updated_by = user.full_name or user.username
                    updated += 1
                else:
                    skipped.append({"ho_ten": name, "reason": "Trùng họ tên + ngày sinh"})
                continue

            rec_data = {k: _str(v) for k, v in item.model_dump().items()}
            rec = m.Record(group_id=gid, created_by=user.full_name or user.username,
                           updated_by=user.full_name or user.username, **rec_data)
            db.add(rec)
            existing_map[key] = rec
            created += 1

        await log_action(db, request, user, "IMPORT_RECORDS", "group", g.ma_doan,
                         f"{created} tạo mới, {updated} cập nhật, {len(skipped)} bỏ qua")
        await db.commit()
        return {"created": created, "updated": updated, "skipped": skipped, "his_package_code": g.his_package_code}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Lỗi nhập danh sách: {str(e)}")


# =================== EXPECTED + COMPARE ===================
@app.get("/api/groups/{gid}/expected", response_model=list[s.ExpectedItem])
async def get_expected(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    res = await db.execute(select(m.Expected).where(m.Expected.group_id == gid))
    return res.scalars().all()


EXP_FIELDS = ["cccd", "ma_bhyt", "ho_ten", "ngay_sinh", "gioi_tinh", "dan_toc", "quoc_tich",
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
            db.add(m.Expected(group_id=gid, **{k: (getattr(it, k, "") or "").strip() for k in EXP_FIELDS}))
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
    e = m.Expected(group_id=gid, **{k: (getattr(payload, k, "") or "").strip() for k in EXP_FIELDS})
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
        setattr(e, k, (getattr(payload, k, "") or "").strip())
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
        return "c:" + cccd.strip()
    return f"n:{ho_ten.strip().lower()}|{ngay_sinh.strip()}"


@app.get("/api/groups/{gid}/compare")
async def compare_expected_records(gid: int, db: AsyncSession = Depends(get_db), _: m.User = Depends(get_current_user)):
    exp_res = await db.execute(select(m.Expected).where(m.Expected.group_id == gid))
    exp = exp_res.scalars().all()
    act_res = await db.execute(select(m.Record).where(m.Record.group_id == gid))
    act = act_res.scalars().all()

    exp_map = {_match_key(e.cccd, e.ho_ten, e.ngay_sinh): e for e in exp}
    act_map = {_match_key(r.cccd, r.ho_ten, r.ngay_sinh): r for r in act}

    came = []
    absent = []
    extra = []

    for key, e in exp_map.items():
        if key in act_map:
            came.append(s.ExpectedItem.model_validate(e))
        else:
            absent.append(s.ExpectedItem.model_validate(e))

    for key, r in act_map.items():
        if key not in exp_map:
            extra.append(s.RecordOut.model_validate(r))

    return {
        "expected_total": len(exp), "actual_total": len(act),
        "came_count": len(came), "absent_count": len(absent), "extra_count": len(extra),
        "came": came, "absent": absent, "extra": extra,
    }


# =================== USERS & PERMS ===================
@app.get("/api/users", response_model=list[s.UserOut])
async def list_users(admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.User).order_by(m.User.created_at.asc()))
    return res.scalars().all()


@app.post("/api/users", response_model=s.UserOut)
async def create_user(request: Request, payload: s.UserCreate,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    ex = await db.execute(select(m.User).where(m.User.username == payload.username.strip()))
    if ex.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Tên tài khoản đã tồn tại")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải từ 6 ký tự")
    perms_str = ",".join(payload.perms) if payload.perms is not None else ",".join(DEFAULT_USER_PERMS)
    u = m.User(
        username=payload.username.strip(),
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role if payload.role in ("admin", "user") else "user",
        perms=perms_str,
    )
    db.add(u)
    await log_action(db, request, admin, "CREATE_USER", "user", "", f"{u.username} ({u.role})")
    await db.commit()
    await db.refresh(u)
    return u


@app.put("/api/users/{uid}", response_model=s.UserOut)
async def update_user(request: Request, uid: int, payload: s.UserUpdate,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    u = await db.scalar(select(m.User).where(m.User.id == uid))
    if not u:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    if payload.full_name is not None:
        u.full_name = payload.full_name.strip()
    if payload.password:
        if len(payload.password) < 6:
            raise HTTPException(status_code=400, detail="Mật khẩu phải từ 6 ký tự")
        u.hashed_password = hash_password(payload.password)
    if payload.role in ("admin", "user"):
        u.role = payload.role
    if payload.is_active is not None:
        u.is_active = payload.is_active
    if payload.perms is not None:
        u.perms = ",".join(payload.perms)
    await log_action(db, request, admin, "UPDATE_USER", "user", str(uid), u.username)
    await db.commit()
    await db.refresh(u)
    return u


@app.delete("/api/users/{uid}")
async def delete_user(request: Request, uid: int,
                      admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if uid == admin.id:
        raise HTTPException(status_code=400, detail="Không thể tự xóa tài khoản của chính mình")
    u = await db.scalar(select(m.User).where(m.User.id == uid))
    if not u:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    username = u.username
    await db.delete(u)
    await log_action(db, request, admin, "DELETE_USER", "user", str(uid), username)
    await db.commit()
    return {"ok": True}


# =================== HIS CONFIG & REGISTER ===================
@app.get("/api/his/config")
async def get_his_config(admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await his_client.get_config(db)


@app.post("/api/his/config")
@app.put("/api/his/config")
async def update_his_config(request: Request, payload: dict,
                            admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cfg = await his_client.save_config(db, payload)
    await log_action(db, request, admin, "UPDATE_HIS_CONFIG", "his", "", "Cập nhật cấu hình HIS")
    return cfg


# =================== APP CONFIG ===================
@app.get("/api/app-config")
async def get_app_config(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(m.Setting).where(m.Setting.key == "app_config"))).scalar_one_or_none()
    cfg = {"session_cutoff": "13:00"}
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except json.JSONDecodeError:
            pass
    return cfg


@app.put("/api/app-config")
async def update_app_config(request: Request, payload: dict,
                              admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(m.Setting).where(m.Setting.key == "app_config"))).scalar_one_or_none()
    cfg = {"session_cutoff": "13:00"}
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except json.JSONDecodeError:
            pass
    cfg.update(payload)
    if row is None:
        db.add(m.Setting(key="app_config", value=json.dumps(cfg, ensure_ascii=False)))
    else:
        row.value = json.dumps(cfg, ensure_ascii=False)
    await log_action(db, request, admin, "UPDATE_APP_CONFIG", "setting", "", "Cập nhật cấu hình ứng dụng")
    await db.commit()
    return cfg


@app.post("/api/his/test")
async def test_his_connection(admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cfg = await his_client.get_config(db)
    try:
        msg = await his_client.test_connection(db, cfg)
        return {"ok": True, "message": msg}
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/his/packages")
@app.post("/api/his/packages")
@app.post("/api/his/package-search")
async def search_his_packages(payload: dict = None, q: str = "", _: m.User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """Tìm danh sách gói khám trên HIS theo mã code hoặc tên gói."""
    if payload and isinstance(payload, dict):
        q = payload.get("q") or payload.get("code") or payload.get("vi_name") or q
    cfg = await his_client.get_config(db)
    try:
        pkgs = await his_client.search_packages(cfg, q or "")
        return {"ok": True, "packages": pkgs}
    except his_client.HisError as e:
        return {"ok": False, "message": str(e), "packages": []}
    except Exception as e:
        return {"ok": False, "message": f"Lỗi tìm gói khám: {e}", "packages": []}


@app.post("/api/his/ward-search")
async def search_his_ward(payload: dict, _: m.User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Tìm phường/xã theo tỉnh + từ khóa."""
    cfg = await his_client.get_config(db)
    pid = payload.get("province_id") or 0
    q = payload.get("q") or ""
    try:
        return await his_client.search_ward(cfg, pid, q)
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/his/catalog")
async def get_his_catalog(_: m.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Lấy danh mục Dân tộc / Quốc tịch / Nghề nghiệp / Tỉnh thành để hiển thị trên form."""
    return await his_client.get_catalog(db)


@app.post("/api/his/catalog/refresh")
@app.post("/api/his/refresh-catalog")
async def refresh_his_catalog(admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Tải lại danh mục từ HIS và lưu cache."""
    cfg = await his_client.get_config(db)
    try:
        cat = await his_client.fetch_catalog(db, cfg)
        return {
            "ok": True,
            "ethnic": cat.get("ethnic", []),
            "nationality": cat.get("nationality", []),
            "career": cat.get("career", []),
            "province": cat.get("province", []),
            "counts": {k: len(v) for k, v in cat.items()}
        }
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/his/refresh-services")
async def refresh_his_services(admin: m.User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Tải lại danh sách dịch vụ của gói mặc định từ HIS."""
    cfg = await his_client.get_config(db)
    pkg_id = cfg.get("package", {}).get("service_id")
    if not pkg_id:
        raise HTTPException(status_code=400, detail="Chưa chọn gói khám mặc định trong Cấu hình HIS")
    try:
        key = f"his_svc_{pkg_id}"
        row = (await db.execute(select(m.Setting).where(m.Setting.key == key))).scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.commit()
        services = await his_client.services_for_package(db, cfg, pkg_id)
        cfg["list_services"] = services
        await his_client.save_config(db, {"list_services": services})
        return {"ok": True, "count": len(services)}
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/groups/{gid}/his-services")
async def get_group_his_services(gid: int, user: m.User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """Lấy danh sách dịch vụ thuộc gói khám của đoàn này (kèm trạng thái bật/tắt từng món)."""
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    cfg = await his_client.get_config(db)
    try:
        pkg, services = await his_client.prepare_group_package(db, cfg, g)
        return {"package": pkg, "services": services}
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/records/{rid}/his-register")
async def register_one_record_his(request: Request, rid: int, payload: s.HisRegisterOne = s.HisRegisterOne(),
                                   user: m.User = Depends(require_perm("his_register")),
                                   db: AsyncSession = Depends(get_db)):
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    if rec.his_status == "registered" and not payload.force:
        raise HTTPException(status_code=400, detail=f"Bản ghi đã đăng ký thành công (mã BN {rec.his_patient_code}). Thêm force=true nếu muốn đăng ký lại.")

    g = await db.scalar(select(m.Group).where(m.Group.id == rec.group_id))
    cfg = await his_client.get_config(db)
    try:
        pkg, services = await his_client.prepare_group_package(db, cfg, g)
        res = await his_client.register_one(cfg, rec, pkg, services)
        rec.his_status = "registered"
        rec.his_patient_code = res["patient_code"]
        rec.his_patient_id = res["patient_id"]
        rec.his_ticket_id = res["ticket_id"]
        rec.his_message = f"Thành công ({res['code']})"
        rec.his_registered_at = datetime.now(his_client.VN).isoformat()
        await log_action(db, request, user, "HIS_REGISTER_ONE", "record", str(rid),
                         f"{rec.ho_ten} -> {res['patient_code']} ({pkg['code']})")
        await db.commit()
        await db.refresh(rec)
        return rec
    except his_client.HisError as e:
        rec.his_status = "error"
        rec.his_message = str(e)[:250]
        await db.commit()
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/groups/{gid}/his-bulk-register")
@app.post("/api/groups/{gid}/his-register-bulk")
async def register_bulk_his(request: Request, gid: int, payload: s.HisBulkRegister,
                            user: m.User = Depends(require_perm("his_register")),
                            db: AsyncSession = Depends(get_db)):
    g = await db.scalar(select(m.Group).where(m.Group.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoàn khám")
    if not payload.record_ids:
        raise HTTPException(status_code=400, detail="Danh sách bản ghi rỗng")

    res = await db.execute(
        select(m.Record).where(m.Record.group_id == gid, m.Record.id.in_(payload.record_ids))
    )
    records = res.scalars().all()
    cfg = await his_client.get_config(db)

    try:
        pkg, services = await his_client.prepare_group_package(db, cfg, g)
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=f"Lỗi gói khám đoàn: {e}")

    success_cnt = 0
    fail_cnt = 0
    errors = []

    for r in records:
        if r.his_status == "registered" and not payload.force:
            continue  # bỏ qua người đã ĐK thành công ngoại trừ khi ép ĐK lại
        try:
            out = await his_client.register_one(cfg, r, pkg, services)
            r.his_status = "registered"
            r.his_patient_code = out["patient_code"]
            r.his_patient_id = out["patient_id"]
            r.his_ticket_id = out["ticket_id"]
            r.his_message = f"Thành công ({out['code']})"
            r.his_registered_at = datetime.now(his_client.VN).isoformat()
            success_cnt += 1
        except his_client.HisError as e:
            r.his_status = "error"
            r.his_message = str(e)[:250]
            fail_cnt += 1
            errors.append({"id": r.id, "ho_ten": r.ho_ten, "error": str(e)})

    await log_action(db, request, user, "HIS_REGISTER_BULK", "group", g.ma_doan,
                     f"Thành công {success_cnt}/{len(records)}, thất bại {fail_cnt} (Gói: {pkg['code']})")
    await db.commit()
    return {"total": len(records), "success": success_cnt, "fail": fail_cnt, "errors": errors, "package": pkg["code"]}


@app.post("/api/records/{rid}/his-unregister")
async def unregister_record_his(request: Request, rid: int,
                                 user: m.User = Depends(require_perm("his_register")),
                                 db: AsyncSession = Depends(get_db)):
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    if not rec.his_patient_code:
        raise HTTPException(status_code=400, detail="Bản ghi chưa có Mã bệnh nhân HIS")

    cfg = await his_client.get_config(db)
    try:
        res = await his_client.unregister(cfg, rec.his_patient_code)
        rec.his_status = ""
        rec.his_message = f"Đã hủy gói ({res['deleted']} DV)"
        await log_action(db, request, user, "HIS_UNREGISTER", "record", str(rid),
                         f"{rec.ho_ten} ({rec.his_patient_code})")
        await db.commit()
        await db.refresh(rec)
        return {"ok": True, "message": res["message"], "record": s.RecordOut.model_validate(rec)}
    except his_client.HisError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =================== BULK DELETE & EXPORT MAU 02 ===================
@app.post("/api/records/bulk-delete")
async def bulk_delete_records(request: Request, payload: s.RecordBulkDelete,
                              user: m.User = Depends(require_perm("delete_record")),
                              db: AsyncSession = Depends(get_db)):
    """Xóa hàng loạt các bản ghi theo danh sách record_ids."""
    if not payload.record_ids:
        raise HTTPException(status_code=400, detail="Danh sách bản ghi rỗng")

    res = await db.execute(select(m.Record).where(m.Record.id.in_(payload.record_ids)))
    records = res.scalars().all()
    count = len(records)
    for r in records:
        await db.delete(r)
    await log_action(db, request, user, "BULK_DELETE_RECORDS", "record", "", f"Đã xóa {count} bản ghi")
    await db.commit()
    return {"deleted": count}


@app.get("/api/records/{rid}/export/docx")
async def export_record_docx(rid: int, db: AsyncSession = Depends(get_db),
                              user: m.User = Depends(get_current_user)):
    """Xuất Mẫu 02 (.docx edit được) cho 1 bệnh nhân."""
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    g = await db.scalar(select(m.Group).where(m.Group.id == rec.group_id))
    rec_dict = s.RecordOut.model_validate(rec).model_dump()
    rec_dict["id"] = rec.id
    group_dict = s.GroupOut.model_validate(g).model_dump() if g else {}

    doc = docx_forms.build_patient_form(rec_dict, group_dict)
    docx_bytes = docx_forms.save_docx(doc)
    fname = quote(f"Mau02_{(rec.ho_ten or 'khach_hang').replace(' ', '_')}.docx")
    return Response(content=docx_bytes, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=utf-8''{fname}"})


@app.get("/api/records/{rid}/export/pdf")
async def export_record_pdf(rid: int, db: AsyncSession = Depends(get_db),
                             user: m.User = Depends(get_current_user)):
    """Xuất Mẫu 02 (.pdf) xem trước hoặc in cho 1 bệnh nhân."""
    rec = await db.scalar(select(m.Record).where(m.Record.id == rid))
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    g = await db.scalar(select(m.Group).where(m.Group.id == rec.group_id))
    rec_dict = s.RecordOut.model_validate(rec).model_dump()
    rec_dict["id"] = rec.id
    group_dict = s.GroupOut.model_validate(g).model_dump() if g else {}

    doc = docx_forms.build_patient_form(rec_dict, group_dict)
    docx_bytes = docx_forms.save_docx(doc)
    pdf_bytes = docx_forms.docx_to_pdf(docx_bytes, rec=rec_dict, group=group_dict)
    fname = quote(f"Mau02_{(rec.ho_ten or 'khach_hang').replace(' ', '_')}.pdf")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=utf-8''{fname}"})


@app.post("/api/records/export/docx")
async def export_bulk_docx(payload: s.RecordBulkExport, db: AsyncSession = Depends(get_db),
                            user: m.User = Depends(get_current_user)):
    """Xuất gộp Mẫu 02 (.docx) cho danh sách bệnh nhân được chọn."""
    if not payload.record_ids:
        raise HTTPException(status_code=400, detail="Chưa chọn khách hàng nào")
    res = await db.execute(select(m.Record).where(m.Record.id.in_(payload.record_ids)))
    records = res.scalars().all()
    if not records:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu")

    groups_res = await db.execute(select(m.Group))
    group_map = {g.id: s.GroupOut.model_validate(g).model_dump() for g in groups_res.scalars().all()}

    docs = []
    for r in records:
        rec_dict = s.RecordOut.model_validate(r).model_dump()
        rec_dict["id"] = r.id
        g_dict = group_map.get(r.group_id, {})
        docs.append(docx_forms.build_patient_form(rec_dict, g_dict))

    merged_doc = docx_forms.merge_docs(docs)
    docx_bytes = docx_forms.save_docx(merged_doc)
    return Response(content=docx_bytes, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": 'attachment; filename="Mau02_DanhSach.docx"'})


@app.post("/api/records/export/pdf")
async def export_bulk_pdf(payload: s.RecordBulkExport, db: AsyncSession = Depends(get_db),
                           user: m.User = Depends(get_current_user)):
    """Xuất gộp Mẫu 02 (.pdf) xem trước hoặc in cho danh sách bệnh nhân được chọn."""
    if not payload.record_ids:
        raise HTTPException(status_code=400, detail="Chưa chọn khách hàng nào")
    res = await db.execute(select(m.Record).where(m.Record.id.in_(payload.record_ids)))
    records = res.scalars().all()
    if not records:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu")

    groups_res = await db.execute(select(m.Group))
    group_map = {g.id: s.GroupOut.model_validate(g).model_dump() for g in groups_res.scalars().all()}

    pdf_list = []
    for r in records:
        rec_dict = s.RecordOut.model_validate(r).model_dump()
        rec_dict["id"] = r.id
        g_dict = group_map.get(r.group_id, {})
        doc = docx_forms.build_patient_form(rec_dict, g_dict)
        docx_bytes = docx_forms.save_docx(doc)
        pdf_bytes = docx_forms.docx_to_pdf(docx_bytes, rec=rec_dict, group=g_dict)
        pdf_list.append(pdf_bytes)

    merged_pdf = docx_forms.merge_pdfs(pdf_list)
    return Response(content=merged_pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="Mau02_DanhSach.pdf"'})


# =================== LOGS ===================
@app.get("/api/logs", response_model=list[s.LogOut])
async def list_logs(limit: int = 200, admin: m.User = Depends(require_admin),
                    db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(m.AuditLog).order_by(m.AuditLog.ts.desc()).limit(limit))
    return res.scalars().all()


# =================== REPORTS ===================
@app.get("/api/reports/summary")
async def report_summary(start: str = Query(""), end: str = Query(""), gid: int = Query(0),
                         user: m.User = Depends(require_perm("view_reports")), db: AsyncSession = Depends(get_db)):
    stmt = select(m.Record)
    if gid > 0:
        stmt = stmt.where(m.Record.group_id == gid)
    if start:
        try:
            st_dt = datetime.strptime(start + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            stmt = stmt.where(m.Record.created_at >= st_dt)
        except ValueError:
            pass
    if end:
        try:
            en_dt = datetime.strptime(end + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            stmt = stmt.where(m.Record.created_at <= en_dt)
        except ValueError:
            pass

    records = (await db.execute(stmt)).scalars().all()
    groups_res = await db.execute(select(m.Group))
    group_map = {g.id: g.ten_doan for g in groups_res.scalars().all()}

    total = len(records)
    nam = sum(1 for r in records if r.gioi_tinh == "Nam")
    nu = sum(1 for r in records if r.gioi_tinh == "Nữ")

    by_date = {}
    by_group = {}

    for r in records:
        vn_time = r.created_at.astimezone(timezone(timedelta(hours=7)))
        dt_str = vn_time.strftime("%Y-%m-%d")

        if dt_str not in by_date:
            by_date[dt_str] = {"date": dt_str, "total": 0, "nam": 0, "nu": 0}
        by_date[dt_str]["total"] += 1
        if r.gioi_tinh == "Nam":
            by_date[dt_str]["nam"] += 1
        elif r.gioi_tinh == "Nữ":
            by_date[dt_str]["nu"] += 1

        gname = group_map.get(r.group_id, f"Đoàn #{r.group_id}")
        if gname not in by_group:
            by_group[gname] = {"group_name": gname, "total": 0, "nam": 0, "nu": 0}
        by_group[gname]["total"] += 1
        if r.gioi_tinh == "Nam":
            by_group[gname]["nam"] += 1
        elif r.gioi_tinh == "Nữ":
            by_group[gname]["nu"] += 1

    timeline = sorted(by_group_by_date_list(by_date), key=lambda x: x["date"], reverse=True)
    group_summary = sorted(list(by_group.values()), key=lambda x: x["total"], reverse=True)

    return {
        "total": total, "nam": nam, "nu": nu,
        "timeline": timeline,
        "by_group": group_summary,
    }


def by_group_by_date_list(by_date_dict):
    return list(by_date_dict.values())


# Serve frontend static files
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
