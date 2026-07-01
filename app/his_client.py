"""
Tích hợp HIS (noitru.bvhongduc.vn:8080) — đăng ký gói khám sức khỏe.

Luồng đã bắt được từ HIS:
  POST /api/out_patient_package_register/save   -> tạo hồ sơ + đăng ký gói
Trả về: data.patient_code, data.patient_id, data.his[0].ticket_id, data.his[0].code

Cấu hình lưu trong bảng settings (key='his_config') dạng JSON. appkey/userkey là token
phiên đăng nhập HIS — sẽ hết hạn, khi đó admin dán lại từ công cụ sniffer.
"""
import json
from datetime import datetime, date, timezone, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models as m

VN = timezone(timedelta(hours=7))
CONFIG_KEY = "his_config"


class HisError(Exception):
    """Lỗi nghiệp vụ khi gọi HIS (hiển thị cho người dùng)."""


# ------- cấu hình mặc định (seed) từ dữ liệu đã bắt được -------
DEFAULT_CONFIG = {
    "base_url": "http://noitru.bvhongduc.vn:8080",
    "appkey": "",
    "userkey": "",
    "ethnic_group_id": 1,      # Kinh
    "nationality": 1,          # Việt Nam
    "ticket_prefix": "PDV",
    "address": {"province_id": 701, "district_id": 70101},  # mặc định TP.HCM / Quận 1
    "package": {
        "service_id": 65995477,
        "code": "KSK-TD-2026",
        "s_name": "ksk toàn dân 2026",
        "mobile_code_order": "ksk-td-2026",
        "normal_price": 616000,
    },
    # service_id -> có đăng ký hay không (khớp thao tác tay: XQ ngực thẳng = false)
    "register_map": {
        "10387": True, "10453": True, "10577": True, "10383": True,
        "42279152": True, "42279272": True, "10565": True, "8709587": False,
    },
    # listServices sẽ được tự động nạp từ HIS (findByServicePackage) ở lần dùng đầu, rồi cache lại
    "list_services": [],
}


def _build_list_order(cfg):
    p = cfg["package"]
    return [{
        "created": "", "s_name": p.get("s_name", ""), "enum_examination_type": "",
        "mobile_code_order": p.get("mobile_code_order", ""), "mobile_name_order": "",
        "mobile_id": "", "valid": "", "normal_price": p.get("normal_price", 0),
        "insurance_price": 0, "overtime_price": 0, "other_price": 0, "insurance_remuneration": 0,
        "item_id": p["service_id"], "item_type": "consultation_package",
        "key": 0, "name": "", "allow_choose_doctor": False, "paid": 1,
    }]


# ---------------- config storage ----------------
async def get_config(db: AsyncSession) -> dict:
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CONFIG_KEY))).scalar_one_or_none()
    cfg = dict(DEFAULT_CONFIG)
    if row and row.value:
        try:
            saved = json.loads(row.value)
            cfg.update(saved)
            # gộp sâu cho address/package/register_map
            for k in ("address", "package", "register_map"):
                if isinstance(DEFAULT_CONFIG[k], dict):
                    merged = dict(DEFAULT_CONFIG[k]); merged.update(saved.get(k, {})); cfg[k] = merged
        except json.JSONDecodeError:
            pass
    return cfg


async def save_config(db: AsyncSession, patch: dict) -> dict:
    cfg = await get_config(db)
    for k, v in patch.items():
        if k in ("address", "package", "register_map") and isinstance(v, dict):
            d = dict(cfg.get(k, {})); d.update(v); cfg[k] = d
        else:
            cfg[k] = v
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CONFIG_KEY))).scalar_one_or_none()
    if row is None:
        row = m.Setting(key=CONFIG_KEY, value=json.dumps(cfg, ensure_ascii=False))
        db.add(row)
    else:
        row.value = json.dumps(cfg, ensure_ascii=False)
    await db.commit()
    return cfg


def _headers(cfg):
    if not cfg.get("appkey") or not cfg.get("userkey"):
        raise HisError("Chưa cấu hình appkey/userkey của HIS. Vào 'Cấu hình HIS' để dán token.")
    return {
        "Content-Type": "application/json",
        "appkey": cfg["appkey"],
        "userkey": cfg["userkey"],
        "Accept": "application/json, text/plain, */*",
    }


async def _post(cfg, path, body):
    url = cfg["base_url"].rstrip("/") + "/api/" + path
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, json=body, headers=_headers(cfg))
    except httpx.RequestError as e:
        raise HisError(f"Không kết nối được HIS ({url}). Kiểm tra máy có vào được noitru.bvhongduc.vn:8080 không. Chi tiết: {e}")
    if r.status_code in (401, 403):
        raise HisError("HIS từ chối (token hết hạn hoặc sai). Hãy đăng nhập lại HIS, bắt token mới bằng sniffer rồi cập nhật trong 'Cấu hình HIS'.")
    if r.status_code >= 400:
        raise HisError(f"HIS trả lỗi HTTP {r.status_code}.")
    try:
        data = r.json()
    except ValueError:
        raise HisError("HIS trả về dữ liệu không phải JSON.")
    if not data.get("success", False):
        msg = (data.get("error") or {}).get("message") or "HIS báo lỗi không rõ."
        raise HisError(f"HIS: {msg}")
    return data


# ---------------- services template ----------------
async def ensure_services(db: AsyncSession, cfg: dict) -> dict:
    """Nạp danh sách dịch vụ của gói từ HIS nếu chưa có, gắn cờ register theo register_map."""
    if cfg.get("list_services"):
        return cfg
    pkg_id = cfg["package"]["service_id"]
    data = await _post(cfg, "service_package/findByServicePackage", {"servicePackageId": pkg_id})
    services = data.get("data") or []
    if not services:
        raise HisError("Không lấy được danh sách dịch vụ của gói khám từ HIS (rỗng).")
    reg_map = cfg.get("register_map", {})
    out = []
    for s in services:
        s = dict(s)
        sid = str(s.get("service_id"))
        s["register"] = bool(reg_map.get(sid, True))   # mặc định đăng ký, trừ dịch vụ bị tắt trong register_map
        s["diff_sex_type"] = False
        out.append(s)
    cfg["list_services"] = out
    await save_config(db, {"list_services": out})
    return cfg


# ---------------- payload builder ----------------
def _map_gender(gioi_tinh):
    if gioi_tinh == "Nam":
        return "male"
    if gioi_tinh == "Nữ":
        return "female"
    return "male"  # mặc định, HIS bắt buộc có giới tính


def _parse_dob(ngay_sinh):
    parts = (ngay_sinh or "").strip().split("/")
    if len(parts) != 3:
        raise HisError("Ngày sinh không hợp lệ (cần dd/mm/yyyy).")
    try:
        d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
        date(y, mo, d)
    except ValueError:
        raise HisError("Ngày sinh không hợp lệ (cần dd/mm/yyyy).")
    return f"{y:04d}/{mo:02d}/{d:02d} 00:00", mo


def build_payload(rec: m.Record, cfg: dict) -> dict:
    if not (rec.ho_ten or "").strip():
        raise HisError("Thiếu họ tên.")
    dob, mob = _parse_dob(rec.ngay_sinh)
    addr = cfg.get("address", {})
    ticket_name = f"{cfg.get('ticket_prefix', 'PDV')} {datetime.now(VN).date().isoformat()}"
    street = " ".join(x for x in [rec.so_nha, rec.khu_pho] if x).strip()
    full_address = ", ".join(x for x in [rec.so_nha, rec.khu_pho, rec.phuong, rec.tinh] if x).strip()
    return {
        "data_patient": {"relative_number": ""},
        "data_person": {
            "ethnic_group_id": cfg.get("ethnic_group_id", 1),
            "gender": _map_gender(rec.gioi_tinh),
            "name": rec.ho_ten.strip(),
            "month_of_birth": mob,
            "date_of_birth": dob,
            "marital_status": 0,
            "phone_number": rec.so_dien_thoai or "",
            "nationality": cfg.get("nationality", 1),
        },
        "data_ticket": {
            "name": ticket_name, "enum_examination_type": 1, "examination_type_id": 1,
            "discount_type_id": 0, "enum_introduction_outpatient": 2,
            "introduction_outpatient_diagnose": "", "yeu_cau": 0, "coupon_code": "",
        },
        "data_ticket_item": {"diagnosis": ""},
        "data_address": {
            "street": street, "ward_name": rec.phuong or "",
            "district_id": addr.get("district_id", 70101),
            "province_id": addr.get("province_id", 701),
            "full_address": full_address or street,
        },
        "data_plus": {"patient_code": ""},
        "data_insurance": {},
        "list_order": _build_list_order(cfg),
        "listServices": cfg["list_services"],
    }


async def register_one(db: AsyncSession, rec: m.Record, cfg: dict) -> dict:
    """Đăng ký 1 người lên HIS. Trả về dict kết quả (patient_code, patient_id, ticket_id, code)."""
    await ensure_services(db, cfg)
    body = build_payload(rec, cfg)
    data = await _post(cfg, "out_patient_package_register/save", body)
    d = data.get("data") or {}
    his0 = (d.get("his") or [{}])[0]
    return {
        "patient_code": str(d.get("patient_code") or ""),
        "patient_id": str(d.get("patient_id") or ""),
        "ticket_id": str(his0.get("ticket_id") or ""),
        "code": his0.get("code") or "",
    }


async def test_connection(db: AsyncSession, cfg: dict) -> str:
    """Gọi thử 1 endpoint nhẹ để kiểm tra token còn sống. Trả về thông báo."""
    data = await _post(cfg, "ethnic_group/find", {})
    n = len(data.get("data") or [])
    return f"Kết nối HIS OK — token hợp lệ (đọc được {n} dân tộc)."
