from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


# ---------- Auth ----------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    full_name: str
    username: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str
    role: str
    perms: str = ""
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    username: str
    full_name: str = ""
    password: str
    role: str = "user"
    perms: Optional[list[str]] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    perms: Optional[list[str]] = None


# ---------- Group ----------
class GroupBase(BaseModel):
    ma_doan: str
    ten_doan: str
    thoi_gian_kham: str = ""
    dia_diem: str = ""


class GroupUpdate(BaseModel):
    ten_doan: Optional[str] = None
    thoi_gian_kham: Optional[str] = None
    dia_diem: Optional[str] = None


class GroupOut(GroupBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    record_count: int = 0
    expected_count: int = 0


# ---------- Record ----------
class RecordBase(BaseModel):
    cccd: str = ""
    ma_bhyt: str = ""
    ho_ten: str
    ngay_sinh: str = ""
    gioi_tinh: str = ""
    nghe_nghiep: str = ""
    so_nha: str = ""
    khu_pho: str = ""
    phuong: str = ""
    tinh: str = ""
    nhan_ho_so: str = ""
    so_dien_thoai: str = ""


class RecordOut(RecordBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str


# ---------- Expected ----------
class ExpectedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[int] = None
    cccd: str = ""
    ma_bhyt: str = ""
    ho_ten: str
    ngay_sinh: str = ""
    gioi_tinh: str = ""
    so_nha: str = ""
    khu_pho: str = ""
    phuong: str = ""
    tinh: str = ""
    dia_chi: str = ""
    so_dien_thoai: str = ""


# ---------- Logs ----------
class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    username: str
    role: str
    action: str
    entity: str
    entity_id: str
    detail: str
    ip: str
