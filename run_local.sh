#!/usr/bin/env bash
# 本地一键启动（容器内已预装 Python/Node/PostgreSQL 时使用）
set -e
cd "$(dirname "$0")/backend"

# 1. 虚拟环境与依赖
if [ ! -d "../.venv" ]; then
  /opt/python-3.12/bin/python3 -m venv ../.venv
  ../.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
fi

# 2. 建库（如不存在）
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='irrigation'" | grep -q 1 || \
  PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres \
  -c "CREATE DATABASE irrigation OWNER ccdev;"

# 3. 写入样例数据（含四类治理场景 + 计费双时间对账五组样例）
../.venv/bin/python -m scripts.seed
../.venv/bin/python -m scripts.seed_billing

# 4. 启动后端（同时托管 ../frontend/dist 若已构建）
IRR_WEB_DIST=../frontend/dist exec ../.venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000
