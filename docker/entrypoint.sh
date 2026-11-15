#!/bin/sh
set -e

echo "[entrypoint] 等待数据库 ${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432} 就绪..."
python - <<'PY'
import os, time, psycopg2
url = os.environ.get("IRR_DATABASE_URL")
for i in range(60):
    try:
        psycopg2.connect(url).close()
        print("[entrypoint] 数据库连接成功")
        break
    except Exception as exc:
        print(f"[entrypoint] 数据库未就绪({i+1}/60): {exc}")
        time.sleep(2)
else:
    raise SystemExit("数据库等待超时")
PY

# 建表（应用 startup 也会执行，此处提前保证 seed 前表已存在；含计量与计费两套模型）
python -c "from app.db import Base, engine; from app import models, models_billing; Base.metadata.create_all(bind=engine)"

if [ "${IRR_SEED:-true}" = "true" ]; then
  echo "[entrypoint] 写入样例数据（迟到补传/换表缺证/渠道失衡/证据冲突场景）..."
  python -m scripts.seed || echo "[entrypoint] 基础种子已存在或写入失败，跳过"
  echo "[entrypoint] 写入计费双时间对账样例（跨阶梯/跨租约/重复回执/冻结后补传/人工证据冲突/跨价目待证）..."
  python -m scripts.seed_billing || echo "[entrypoint] 计费种子已存在或写入失败，跳过"
fi

exec "$@"
