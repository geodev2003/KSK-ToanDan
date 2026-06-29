# Hệ thống Nhập liệu Đoàn Khám Sức Khỏe (KSK)

Web nhập liệu nhiều người dùng đồng thời cho đoàn khám sức khỏe, lưu vào database, có
phân quyền **admin / user** riêng biệt và ghi **nhật ký (audit log)** mọi thao tác.

Stack: **FastAPI (async) + SQLAlchemy + PostgreSQL** · Frontend vanilla JS + SheetJS · JWT auth.

---

## Tính năng

**Người dùng (user)**
- Đăng nhập bằng tài khoản + mật khẩu
- Chọn đoàn khám → nhập liệu nhanh (Tab chuyển trường, Enter lưu, F2 mở form)
- Thống kê tức thì: tổng, nam, nữ, số trùng (Họ tên + Ngày sinh)
- Sửa/xóa bản ghi, tìm kiếm
- So sánh danh sách dự kiến ↔ thực tế (ai đến / chưa đến / phát sinh)
- Xuất Excel theo cấu trúc import HIS
- Danh sách tự đồng bộ ~5 giây/lần khi nhiều người cùng nhập

**Quản trị (admin)** — có thêm:
- Tạo/sửa/xóa đoàn khám
- Quản lý người dùng (tạo/sửa/khóa/xóa, đặt vai trò)
- Import danh sách dự kiến (Excel)
- **Xem nhật ký thao tác** của tất cả người dùng (chỉ admin)

**Đảm bảo đồng thời & chính xác**
- PostgreSQL transaction → nhiều người ghi cùng lúc không phá dữ liệu của nhau
- STT đánh lại lúc hiển thị/xuất Excel (không lưu cứng) → không đụng số khi chèn song song
- Chống trùng: cảnh báo khi trùng Họ tên + Ngày sinh (cho phép ép thêm nếu thật sự khác người)
- CCCD/BHYT xuất Excel ở định dạng text (không bị về dạng khoa học)

---

## Chạy nhanh (thử nghiệm, SQLite — không cần Postgres)

```bash
cd ksk-app
./run_local.sh
```
Mở http://localhost:8000 · đăng nhập `admin` / `Admin@2026`.

## Chạy production (Docker + PostgreSQL)

```bash
cd ksk-app
cp .env.example .env          # rồi sửa mật khẩu DB, SECRET_KEY, ADMIN_PASSWORD
docker compose up -d --build
```
Truy cập http://localhost:8000 (hoặc đưa qua Cloudflare Tunnel như các tool khác của bạn).

> **Quan trọng:** đăng nhập lần đầu bằng `ADMIN_USERNAME/ADMIN_PASSWORD`, rồi **đổi mật khẩu ngay**
> (nút "Đổi MK" góc phải), và đặt `SECRET_KEY` ngẫu nhiên dài trong `.env`.

## Cấu hình (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `DATABASE_URL` | postgres nội bộ | `postgresql+asyncpg://...` hoặc `sqlite+aiosqlite:///./ksk.db` |
| `SECRET_KEY` | (đổi ngay) | khóa ký JWT |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | admin / Admin@2026 | tài khoản admin tạo tự động lần đầu |
| `TOKEN_EXPIRE_HOURS` | 12 | thời hạn phiên đăng nhập |

---

## Cấu trúc

```
ksk-app/
├─ app/
│  ├─ main.py        # FastAPI app + toàn bộ API routes
│  ├─ database.py    # engine async (Postgres / SQLite)
│  ├─ models.py      # User, Group, Record, Expected, AuditLog
│  ├─ schemas.py     # Pydantic
│  └─ security.py    # bcrypt, JWT, phân quyền, ghi log
├─ static/index.html # giao diện (gọi API)
├─ Dockerfile · docker-compose.yml · requirements.txt
├─ run_local.sh      # chạy nhanh bằng SQLite
└─ test_api.py       # 21 test end-to-end
```

## API chính

```
POST /api/auth/login            đăng nhập (form) → JWT
GET  /api/me                    thông tin tài khoản
POST /api/me/password           đổi mật khẩu

GET  /api/groups                danh sách đoàn (kèm số đã nhập / dự kiến)
POST /api/groups                tạo đoàn            [admin]
PUT  /api/groups/{id}           sửa đoàn            [admin]
DELETE /api/groups/{id}         xóa đoàn            [admin]

GET  /api/groups/{id}/records   danh sách bản ghi
GET  /api/groups/{id}/meta      số lượng + mốc cập nhật (polling nhẹ)
POST /api/groups/{id}/records   thêm (409 nếu trùng; ?force=true để ép)
PUT  /api/records/{id}          sửa
DELETE /api/records/{id}        xóa

PUT  /api/groups/{id}/expected  import DS dự kiến   [admin]
GET  /api/groups/{id}/compare   so sánh dự kiến ↔ thực tế

GET  /api/users · POST · PUT · DELETE   quản lý người dùng   [admin]
GET  /api/logs                  nhật ký thao tác    [admin]
```

## Kiểm thử

```bash
pip install -r requirements.txt aiosqlite httpx
python test_api.py     # 21/21 passed
```

---

## Ghi chú HIS

Hàm xuất Excel đang dùng đúng thứ tự cột theo template bạn gửi
(STT, CCCD, Mã BHYT, Họ tên, Ngày sinh, Giới tính, Nghề nghiệp, Số nhà, Khu phố,
Phường, Tỉnh, Nhận hồ sơ, Thời gian, Người nhập, Đoàn KSK). Nếu PM HIS cần thứ tự
hoặc mã danh mục khác (mã giới tính, mã phường/tỉnh…), chỉ cần sửa hàm `exportExcel()`
trong `static/index.html` cho khớp file import mẫu của HIS.
