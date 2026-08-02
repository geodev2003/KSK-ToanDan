"""
Tích hợp HIS (noitru.bvhongduc.vn:8080) — đăng ký gói khám sức khỏe.

Luồng đã bắt được từ HIS:
  POST /api/out_patient_package_register/save   -> tạo hồ sơ + đăng ký gói
Trả về: data.patient_code, data.patient_id, data.his[0].ticket_id, data.his[0].code

Cấu hình lưu trong bảng settings (key='his_config') dạng JSON. appkey/userkey là token
phiên đăng nhập HIS — sẽ hết hạn, khi đó admin dán lại từ công cụ sniffer.
"""
import json
import unicodedata
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


def _build_list_order(pkg):
    return [{
        "created": "", "s_name": pkg.get("s_name", ""), "enum_examination_type": "",
        "mobile_code_order": pkg.get("mobile_code_order", ""), "mobile_name_order": "",
        "mobile_id": "", "valid": "", "normal_price": pkg.get("normal_price", 0),
        "insurance_price": 0, "overtime_price": 0, "other_price": 0, "insurance_remuneration": 0,
        "item_id": pkg["service_id"], "item_type": "consultation_package",
        "key": 0, "name": "", "allow_choose_doctor": False, "paid": 1,
    }]


def _no_accent(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def _extract_data_rows(data) -> list:
    d = data.get("data") if isinstance(data, dict) else data
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ["data_list", "list", "items", "rows", "data"]:
            sub = d.get(k)
            if isinstance(sub, list):
                return sub
    return []


async def search_packages(cfg: dict, q: str = "") -> list:
    """Lấy danh sách gói khám trên HIS (duyệt các trang) rồi lọc theo từ khóa (mã code hoặc tên,
    không phân biệt dấu) ngay tại app — không phụ thuộc bộ lọc phía HIS."""
    collected = {}
    page, max_pages = 1, 15
    while page <= max_pages:
        data = await _post(cfg, "service_package/find", {"page": page})
        rows = _extract_data_rows(data)
        for r in rows:
            if isinstance(r, dict):
                sid = r.get("service_id") or r.get("id") or r.get("code")
                if sid:
                    collected[sid] = r

        d = data.get("data") if isinstance(data, dict) else {}
        paging = d.get("paging") if isinstance(d, dict) else {}
        total_page = 1
        if isinstance(paging, dict):
            try:
                total_page = int(paging.get("total_page") or 1)
            except (TypeError, ValueError):
                total_page = 1
        if page >= total_page or not rows:
            break
        page += 1

    out = []
    for r in collected.values():
        code_val = str(r.get("code") or r.get("service_package_code") or "").strip()
        name_val = str(r.get("vi_name") or r.get("name") or r.get("service_package_name") or "").strip()
        disabled_val = 0
        try:
            disabled_val = int(r.get("disabled") or 0)
        except (TypeError, ValueError):
            disabled_val = 0
        out.append({
            "service_id": r.get("service_id") or r.get("id"),
            "code": code_val,
            "name": name_val,
            "normal_price": r.get("normal_price") or r.get("price") or 0,
            "disabled": disabled_val,
        })
    qn = _no_accent(q)
    if qn:
        out = [p for p in out if qn in _no_accent(p["code"]) or qn in _no_accent(p["name"])]
    out.sort(key=lambda p: (int(p.get("disabled") or 0), p["name"]))
    return out


async def resolve_package_by_code(cfg: dict, code: str) -> dict:
    """Tìm gói theo mã code, trả về gói khớp (ưu tiên gói đang mở). None nếu không có."""
    code = (code or "").strip()
    if not code:
        return None
    pkgs = await search_packages(cfg, code)
    exact = [p for p in pkgs if (p["code"] or "").strip().lower() == code.lower()]
    pool = exact or pkgs
    if not pool:
        return None
    pool.sort(key=lambda p: (p.get("disabled", 0)))  # disabled=0 lên trước
    return pool[0]


async def prepare_group_package(db: AsyncSession, cfg: dict, group) -> tuple:
    """Chuẩn bị (pkg, services) cho một đoàn. Đoàn có mã code gói -> tìm gói theo code;
    không có -> dùng gói mặc định. Áp cờ bật/tắt dịch vụ riêng của đoàn."""
    code = (getattr(group, "his_package_code", "") or "").strip() if group is not None else ""
    if code:
        meta = await resolve_package_by_code(cfg, code)
        if not meta:
            raise HisError(f"Không tìm thấy gói khám có mã code '{code}' trên HIS. Kiểm tra lại mã code gói của đoàn.")
        pkg = {
            "service_id": meta["service_id"],
            "code": meta["code"],
            "s_name": (meta["name"] or "").lower(),
            "mobile_code_order": (meta["code"] or "").lower(),
            "normal_price": meta["normal_price"],
        }
    else:
        pkg_cfg = cfg.get("package")
        if not pkg_cfg or not isinstance(pkg_cfg, dict) or not pkg_cfg.get("service_id"):
            raise HisError("Đoàn chưa chọn gói khám HIS riêng và hệ thống cũng chưa cài đặt gói khám mặc định trong 'Cấu hình HIS'. Mở sửa đoàn để chọn gói khám.")
        pkg = dict(pkg_cfg)
    # dịch vụ của gói
    base = await services_for_package(db, cfg, pkg["service_id"])
    # cờ bật/tắt riêng của đoàn (JSON {service_id: bool})
    flags = {}
    raw = getattr(group, "his_service_flags", "") if group is not None else ""
    if raw:
        try:
            flags = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            flags = {}
    services = []
    for s in base:
        s = dict(s)
        sid = str(s.get("service_id"))
        if sid in flags:
            s["register"] = bool(flags[sid])
        services.append(s)
    return pkg, services


# ---------------- config storage ----------------
async def get_config(db: AsyncSession) -> dict:
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CONFIG_KEY))).scalar_one_or_none()
    cfg = dict(DEFAULT_CONFIG)
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except json.JSONDecodeError:
            pass
    return cfg


async def save_config(db: AsyncSession, patch: dict) -> dict:
    cfg = await get_config(db)
    cfg.update(patch)
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CONFIG_KEY))).scalar_one_or_none()
    if row is None:
        db.add(m.Setting(key=CONFIG_KEY, value=json.dumps(cfg, ensure_ascii=False)))
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
        raise HisError(f"Không kết nối được HIS ({url}). Kiểm tra kết nối mạng/máy chủ HIS. Chi tiết: {e}")
    if r.status_code in (401, 403):
        raise HisError("HIS từ chối (token hết hạn hoặc sai). Hãy đăng nhập lại HIS, lấy token mới dán vào 'Cấu hình HIS'.")
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
    """Nạp danh sách dịch vụ của gói MẶC ĐỊNH từ HIS nếu chưa có, gắn cờ register theo register_map."""
    if cfg.get("list_services"):
        return cfg
    pkg_id = cfg.get("package", {}).get("service_id")
    if not pkg_id:
        return cfg
    cfg["list_services"] = await services_for_package(db, cfg, pkg_id)
    await save_config(db, {"list_services": cfg["list_services"]})
    return cfg


async def services_for_package(db: AsyncSession, cfg: dict, pkg_id) -> list:
    """Lấy danh sách dịch vụ của MỘT gói (theo service_package_id). Cache riêng theo từng mã gói."""
    key = f"his_svc_{pkg_id}"
    row = (await db.execute(select(m.Setting).where(m.Setting.key == key))).scalar_one_or_none()
    if row and row.value:
        try:
            cached = json.loads(row.value)
            if cached:
                return cached
        except json.JSONDecodeError:
            pass
    data = await _post(cfg, "service_package/findByServicePackage", {"servicePackageId": int(pkg_id)})
    services = _extract_data_rows(data)
    if not services:
        raise HisError(f"Không lấy được dịch vụ của gói {pkg_id} từ HIS (rỗng). Kiểm tra lại mã gói.")
    reg_map = cfg.get("register_map", {})
    out = []
    for s in services:
        s = dict(s)
        sid = str(s.get("service_id"))
        s["register"] = bool(reg_map.get(sid, True))
        s["diff_sex_type"] = False
        out.append(s)
    if row is None:
        db.add(m.Setting(key=key, value=json.dumps(out, ensure_ascii=False)))
    else:
        row.value = json.dumps(out, ensure_ascii=False)
    await db.commit()
    return out


# ---------------- payload builder ----------------
def _map_gender(gioi_tinh):
    if gioi_tinh == "Nữ":
        return "female"
    return "male"  # mặc định, HIS bắt buộc có giới tính


def _parse_dob(ngay_sinh):
    s = (ngay_sinh or "").strip().replace("-", "/")
    parts = s.split("/")
    if len(parts) != 3:
        raise HisError(f"Ngày sinh '{ngay_sinh}' không đúng định dạng (cần dd/mm/yyyy).")
    try:
        p1, p2, p3 = int(parts[0]), int(parts[1]), int(parts[2])
        if p1 > 1000:  # YYYY/MM/DD
            y, mo, d = p1, p2, p3
        else:  # DD/MM/YYYY
            d, mo, y = p1, p2, p3
        date(y, mo, d)
    except ValueError:
        raise HisError("Ngày sinh không hợp lệ (cần dd/mm/yyyy).")
    return f"{y:04d}/{mo:02d}/{d:02d} 00:00", mo


def build_payload(rec: m.Record, cfg: dict, pkg: dict = None, services: list = None) -> dict:
    if not (rec.ho_ten or "").strip():
        raise HisError("Thiếu họ tên.")
    pkg = pkg or cfg["package"]
    services = services if services is not None else cfg.get("list_services", [])
    dob, mob = _parse_dob(rec.ngay_sinh)
    addr = cfg.get("address", {})
    ticket_name = f"{cfg.get('ticket_prefix', 'PDV')} {datetime.now(VN).date().isoformat()}"
    street = " ".join(x for x in [rec.so_nha, rec.khu_pho] if x).strip()
    full_address = ", ".join(x for x in [rec.so_nha, rec.khu_pho, rec.phuong, rec.tinh] if x).strip()
    # dân tộc / quốc tịch: ưu tiên theo record, không có thì lấy mặc định cấu hình
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    ethnic_id = _int(getattr(rec, "ethnic_group_id", ""), cfg.get("ethnic_group_id", 1))
    nat_id = _int(getattr(rec, "nationality_id", ""), cfg.get("nationality", 1))
    province_id = _int(getattr(rec, "province_id", ""), addr.get("province_id", 701))
    return {
        "data_patient": {"relative_number": ""},
        "data_person": {
            "ethnic_group_id": ethnic_id,
            "gender": _map_gender(rec.gioi_tinh),
            "name": rec.ho_ten.strip(),
            "month_of_birth": mob,
            "date_of_birth": dob,
            "marital_status": 0,
            "phone_number": rec.so_dien_thoai or "",
            "nationality": nat_id,
            "identity_card_number": (rec.cccd or "").strip(),
            "cmnd": (rec.cccd or "").strip(),
            "cccd": (rec.cccd or "").strip(),
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
            "province_id": province_id,
            "full_address": full_address or street,
        },
        "data_plus": {"patient_code": ""},
        "data_insurance": {},
        "list_order": _build_list_order(pkg),
        "listServices": services,
    }


async def register_one(cfg: dict, rec: m.Record, pkg: dict, services: list) -> dict:
    """Đăng ký 1 người lên HIS với gói + dịch vụ đã chuẩn bị sẵn."""
    body = build_payload(rec, cfg, pkg=pkg, services=services)
    data = await _post(cfg, "out_patient_package_register/save", body)
    d = data.get("data") or {}
    his0 = (d.get("his") or [{}])[0]
    return {
        "patient_code": str(d.get("patient_code") or ""),
        "patient_id": str(d.get("patient_id") or ""),
        "ticket_id": str(his0.get("ticket_id") or ""),
        "code": his0.get("code") or "",
    }


# ---------------- phường/xã (ward) ----------------
async def search_ward(cfg: dict, province_id, q: str = "", limit: int = 15) -> list:
    """Tìm phường/xã theo tỉnh + từ khóa. Trả về [{ward_id, name, ma_phuong}]."""
    try:
        pid = int(province_id)
    except (TypeError, ValueError):
        return []
    data = await _post(cfg, "ward/find", {"vi_name": q or "", "province_id": pid, "limit": limit})
    out = []
    for r in data.get("data") or []:
        out.append({
            "ward_id": r.get("ward_id"),
            "name": (r.get("vi_name") or "").strip(),
            "ma_phuong": r.get("ma_phuong") or "",
        })
    return out


# ---------------- hủy đăng ký (xóa gói khám của bệnh nhân) ----------------
async def unregister(cfg: dict, patient_code: str) -> dict:
    """Hủy toàn bộ dịch vụ gói đã đăng ký của 1 bệnh nhân (theo patient_code)."""
    found = await _post(cfg, "out_patient_package_register/find", {"patient_code": str(patient_code)})
    rows = found.get("data") or []
    if not rows:
        raise HisError(f"Không tìm thấy bệnh nhân mã {patient_code} trên HIS.")
    patient_id = rows[0].get("person_id") or rows[0].get("patient_id")
    hist = await _post(cfg, "out_patient_package_register/get_history", {"patient_id": patient_id})
    items = hist.get("data") or []
    if not items:
        return {"deleted": 0, "message": "Bệnh nhân chưa có dịch vụ gói nào để hủy."}
    by_ticket = {}
    for it in items:
        tid = it.get("ticket_item_id")
        by_ticket.setdefault(tid, []).append(it.get("ticket_item_service_package_id"))
    total = 0
    for tid, ids in by_ticket.items():
        ids = [i for i in ids if i is not None]
        if not ids:
            continue
        await _post(cfg, "out_patient_package_register/delete_package", {"list": ids, "ticket_item_id": tid})
        total += len(ids)
    return {"deleted": total, "message": f"Đã hủy {total} dịch vụ của bệnh nhân {patient_code}."}


async def test_connection(db: AsyncSession, cfg: dict) -> str:
    """Gọi thử 1 endpoint nhẹ để kiểm tra token còn sống. Trả về thông báo."""
    data = await _post(cfg, "ethnic_group/find", {})
    n = len(data.get("data") or [])
    return f"Kết nối HIS OK — token hợp lệ (đọc được {n} dân tộc)."


# ---------------- danh mục (dân tộc / quốc tịch / nghề nghiệp) ----------------
CATALOG_KEY = "his_catalog"


def _simplify(rows, id_field):
    out = []
    for r in rows or []:
        if r.get("disable") in (1, "1") or r.get("disabled") in (1, "1"):
            continue
        out.append({"id": r.get(id_field), "name": (r.get("vi_name") or "").strip()})
    return out


async def fetch_catalog(db: AsyncSession, cfg: dict) -> dict:
    ethnic = _simplify((await _post(cfg, "ethnic_group/find", {})).get("data"), "ethnic_group_id")
    nationality = _simplify((await _post(cfg, "nationality/find", {})).get("data"), "nationality_id")
    career = _simplify((await _post(cfg, "career/find", {})).get("data"), "career_id")
    province = _simplify((await _post(cfg, "province/find", {})).get("data"), "province_id")
    cat = {"ethnic": ethnic, "nationality": nationality, "career": career, "province": province}
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CATALOG_KEY))).scalar_one_or_none()
    if row is None:
        db.add(m.Setting(key=CATALOG_KEY, value=json.dumps(cat, ensure_ascii=False)))
    else:
        row.value = json.dumps(cat, ensure_ascii=False)
    await db.commit()
    return cat


async def get_catalog(db: AsyncSession) -> dict:
    base = {"ethnic": [], "nationality": [], "career": [], "province": []}
    row = (await db.execute(select(m.Setting).where(m.Setting.key == CATALOG_KEY))).scalar_one_or_none()
    if row and row.value:
        try:
            base.update(json.loads(row.value))
        except json.JSONDecodeError:
            pass
    return base
