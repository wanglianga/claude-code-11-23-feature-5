# ---- 阶段1：构建 React 前端 ----
FROM node:22-alpine AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY frontend/ ./
RUN npm run build

# ---- 阶段2：Python 后端运行时（同时托管前端构建产物）----
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    IRR_WEB_DIST=/app/frontend/dist

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /web/dist ./frontend/dist
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app/backend
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
