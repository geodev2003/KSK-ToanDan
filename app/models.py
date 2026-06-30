from datetime import datetime
from sqlalchemy import (
    String, Integer, DateTime, ForeignKey, Boolean, Text, Index, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # 'admin' | 'user'
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ma_doan: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    ten_doan: Mapped[str] = mapped_column(String(255))
    thoi_gian_kham: Mapped[str] = mapped_column(String(64), default="")
    dia_diem: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(128), default="")

    records: Mapped[list["Record"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    expected: Mapped[list["Expected"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class Record(Base):
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    cccd: Mapped[str] = mapped_column(String(20), default="")
    ma_bhyt: Mapped[str] = mapped_column(String(20), default="")
    ho_ten: Mapped[str] = mapped_column(String(128), default="")
    ngay_sinh: Mapped[str] = mapped_column(String(12), default="")     # dd/mm/yyyy
    gioi_tinh: Mapped[str] = mapped_column(String(8), default="")
    nghe_nghiep: Mapped[str] = mapped_column(String(128), default="")
    so_nha: Mapped[str] = mapped_column(String(64), default="")
    khu_pho: Mapped[str] = mapped_column(String(128), default="")
    phuong: Mapped[str] = mapped_column(String(128), default="")
    tinh: Mapped[str] = mapped_column(String(128), default="")
    nhan_ho_so: Mapped[str] = mapped_column(String(32), default="")
    so_dien_thoai: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(128), default="")
    updated_by: Mapped[str] = mapped_column(String(128), default="")

    group: Mapped["Group"] = relationship(back_populates="records")

    __table_args__ = (
        Index("ix_record_dup", "group_id", "ho_ten", "ngay_sinh"),
        Index("ix_record_cccd", "group_id", "cccd"),
    )


class Expected(Base):
    __tablename__ = "expected"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    cccd: Mapped[str] = mapped_column(String(20), default="")
    ma_bhyt: Mapped[str] = mapped_column(String(20), default="")
    ho_ten: Mapped[str] = mapped_column(String(128), default="")
    ngay_sinh: Mapped[str] = mapped_column(String(12), default="")
    gioi_tinh: Mapped[str] = mapped_column(String(8), default="")
    so_nha: Mapped[str] = mapped_column(String(64), default="")
    khu_pho: Mapped[str] = mapped_column(String(128), default="")
    phuong: Mapped[str] = mapped_column(String(128), default="")
    tinh: Mapped[str] = mapped_column(String(128), default="")
    dia_chi: Mapped[str] = mapped_column(String(255), default="")
    so_dien_thoai: Mapped[str] = mapped_column(String(20), default="")

    group: Mapped["Group"] = relationship(back_populates="expected")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    username: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(48), default="", index=True)  # LOGIN, CREATE_RECORD...
    entity: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
