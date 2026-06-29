#!/usr/bin/env bash
# Chạy nhanh bằng SQLite (không cần Postgres) để thử nghiệm
export DATABASE_URL="sqlite+aiosqlite:///./ksk.db"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-Admin@2026}"
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
