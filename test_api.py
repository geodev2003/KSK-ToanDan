import os, tempfile
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.gettempdir(), "ksk_test.db")
os.environ["ADMIN_PASSWORD"] = "Admin@2026"
# fresh db
dbpath = os.environ["DATABASE_URL"].replace("sqlite+aiosqlite:///", "")
if os.path.exists(dbpath): os.remove(dbpath)

from fastapi.testclient import TestClient
from app.main import app

results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)

with TestClient(app) as c:  # triggers lifespan -> create tables + seed admin
    # 1. login as admin
    r = c.post("/api/auth/login", data={"username": "admin", "password": "Admin@2026"})
    check("admin login", r.status_code == 200 and r.json()["role"] == "admin")
    atok = r.json()["access_token"]
    AH = {"Authorization": f"Bearer {atok}"}

    # 2. wrong password rejected
    check("wrong pw rejected", c.post("/api/auth/login", data={"username":"admin","password":"x"}).status_code == 401)

    # 3. create a user
    r = c.post("/api/users", json={"username":"nhanvien1","full_name":"Nhân Viên 1","password":"pass123","role":"user"}, headers=AH)
    check("create user", r.status_code == 200)

    # 4. user login
    r = c.post("/api/auth/login", data={"username":"nhanvien1","password":"pass123"})
    check("user login", r.status_code == 200 and r.json()["role"] == "user")
    utok = r.json()["access_token"]; UH = {"Authorization": f"Bearer {utok}"}

    # 5. user CANNOT create group (admin only)
    check("user blocked from create group", c.post("/api/groups", json={"ma_doan":"D1","ten_doan":"X"}, headers=UH).status_code == 403)

    # 6. user CANNOT view logs
    check("user blocked from logs", c.get("/api/logs", headers=UH).status_code == 403)

    # 7. admin creates group
    r = c.post("/api/groups", json={"ma_doan":"D001","ten_doan":"Công ty ABC","thoi_gian_kham":"15/07/2026","dia_diem":"Hội trường A"}, headers=AH)
    check("admin create group", r.status_code == 200)
    gid = r.json()["id"]

    # 8. duplicate group code rejected
    check("dup group code rejected", c.post("/api/groups", json={"ma_doan":"D001","ten_doan":"Y"}, headers=AH).status_code == 409)

    # 9. user adds records (concurrent entry simulation)
    r = c.post(f"/api/groups/{gid}/records", json={"ho_ten":"Nguyễn Văn A","ngay_sinh":"01/02/1990","gioi_tinh":"Nam","cccd":"012345678901","nhan_ho_so":"Sáng"}, headers=UH)
    check("user create record", r.status_code == 200 and r.json()["created_by"] == "Nhân Viên 1")
    rid = r.json()["id"]
    c.post(f"/api/groups/{gid}/records", json={"ho_ten":"Trần Thị B","ngay_sinh":"03/04/1985","gioi_tinh":"Nữ"}, headers=AH)

    # 10. duplicate record warns (409) then force works
    dup_payload = {"ho_ten":"Nguyễn Văn A","ngay_sinh":"01/02/1990","gioi_tinh":"Nam"}
    check("dup record warns", c.post(f"/api/groups/{gid}/records", json=dup_payload, headers=UH).status_code == 409)
    check("dup record force ok", c.post(f"/api/groups/{gid}/records?force=true", json=dup_payload, headers=UH).status_code == 200)

    # 11. list records + meta
    r = c.get(f"/api/groups/{gid}/records", headers=UH)
    check("list records count=3", r.status_code == 200 and len(r.json()) == 3)
    check("meta count", c.get(f"/api/groups/{gid}/meta", headers=UH).json()["count"] == 3)

    # 12. update + delete record
    check("update record", c.put(f"/api/records/{rid}", json={**dup_payload,"nghe_nghiep":"Kỹ sư"}, headers=UH).status_code == 200)
    check("delete record", c.delete(f"/api/records/{rid}", headers=UH).status_code == 200)

    # 13. expected import (admin) + compare
    check("set expected", c.put(f"/api/groups/{gid}/expected", json=[
        {"ho_ten":"Trần Thị B","ngay_sinh":"03/04/1985"},
        {"ho_ten":"Lê Văn C","ngay_sinh":"05/05/1970"},
    ], headers=AH).status_code == 200)
    r = c.get(f"/api/groups/{gid}/compare", headers=UH)
    cj = r.json()
    check("compare came=1 absent=1", len(cj["came"]) == 1 and len(cj["absent"]) == 1)

    # 14. group counts reflected
    g = [x for x in c.get("/api/groups", headers=UH).json() if x["id"]==gid][0]
    check("group record_count=2", g["record_count"] == 2 and g["expected_count"] == 2)

    # 15. admin sees logs, entries exist
    logs = c.get("/api/logs", headers=AH).json()
    actions = {l["action"] for l in logs}
    check("logs captured actions", {"LOGIN","CREATE_USER","CREATE_GROUP","CREATE_RECORD","DELETE_RECORD","IMPORT_EXPECTED"} <= actions)

    # 16. change password
    check("change password", c.post("/api/me/password", json={"old_password":"pass123","new_password":"newpass"}, headers=UH).status_code == 200)
    check("login with new password", c.post("/api/auth/login", data={"username":"nhanvien1","password":"newpass"}).status_code == 200)

print("\n==== %d/%d passed ====" % (sum(1 for _,c in results if c), len(results)))
if not all(c for _,c in results):
    raise SystemExit(1)
