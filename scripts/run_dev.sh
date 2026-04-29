#!/bin/bash
# 开发模式启动 FastAPI
cd "$(dirname "$0")/.."
exec python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
