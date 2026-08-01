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
    his_package_id: str = ""
    his_package_code: str = ""
    his_package_name: str = ""
    his_package_price: str = ""
    his_service_flags: str = ""


class GroupUpdate(BaseModel):
    ten_doan: Optional[str] = None
    thoi_gian_kham: Optional[str] = None
    dia_diem: Optional[str] = None
    his_package_id: Optional[str] = None
    his_package_code: Optional[str] = None
    his_package_name: Optional[str] = None
    his_package_price: Optional[str] = None
    his_service_flags: Optional[str] = None


class GroupOut(GroupBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    record_count: int = 0
    expected_count: int = 0


# ---------- Record ----------
class RecordBase(BaseModel):
    cccd: str = ""
    ma_bhyt: str = ""
    ho_ten: str = ""
    ngay_sinh: str = ""
    gioi_tinh: str = ""
    nghe_nghiep: str = ""
    career_id: str = ""
    dan_toc: str = ""
    ethnic_group_id: str = ""
    quoc_tich: str = ""
    nationality_id: str = ""
    province_id: str = ""
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
    his_status: str = ""
    his_patient_code: str = ""
    his_patient_id: str = ""
    his_ticket_id: str = ""
    his_message: str = ""
    his_registered_at: str = ""


class RecordBulkImport(BaseModel):
    records: list[RecordBase]
    force: bool = False           # true = vẫn thêm dù trùng họ tên+ngày sinh
    his_package_code: str = ""    # nếu đoàn chưa có mã gói HIS, sẽ tự điền từ đây


class HisBulkRegister(BaseModel):
    record_ids: list[int]
    force: bool = False


class HisRegisterOne(BaseModel):
    force: bool = False


class RecordBulkDelete(BaseModel):
    record_ids: list[int]


class RecordBulkExport(BaseModel):
    record_ids: list[int]


# ---------- Expected ----------
class ExpectedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[int] = None
    cccd: str = ""
    ma_bhyt: str = ""
    ho_ten: str
    ngay_sinh: str = ""
    gioi_tinh: str = ""
    dan_toc: str = ""
    quoc_tich: str = ""
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
