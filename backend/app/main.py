import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import Base, engine
from .routers import auth, contracts, facts, ingestion, master, metrics, billing

# 导入模型以确保元数据注册
from . import models  # noqa: F401
from . import models_billing  # noqa: F401

app = FastAPI(title=settings.app_name, version="1.1.0",
              description="多源用水事实数据与指标发布平台 + 用水计费事实双时间对账："
                          "追加式事实账、可回放指标版本、账期快照与差异桥（不含任何支付/退款动作）")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(contracts.router)
app.include_router(ingestion.router)
app.include_router(master.router)
app.include_router(facts.router)
app.include_router(metrics.router)
app.include_router(billing.router)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok", "app": settings.app_name}


# 生产环境托管 React 构建产物（存在才挂载，纯后端开发不受影响）
_DIST = os.environ.get("IRR_WEB_DIST", "../frontend/dist")
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(_DIST, "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # API 路径未命中时返回 404，其余路径回退到 SPA 入口
        if full_path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(404)
        index_file = os.path.join(_DIST, "index.html")
        return FileResponse(index_file) if os.path.isfile(index_file) else {"detail": "frontend not built"}

