"""演示用轻量登录（非生产级鉴权，仅用于平台内账号演示）。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["认证"])

DEMO_ACCOUNTS = {
    "admin": {"password": "admin123", "name": "灌区管理员", "role": "admin"},
    "viewer": {"password": "viewer123", "name": "运行查看员", "role": "viewer"},
}


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(data: LoginIn):
    acc = DEMO_ACCOUNTS.get(data.username)
    if not acc or acc["password"] != data.password:
        raise HTTPException(401, "账号或密码错误")
    token = f"demo-token-{data.username}"
    return {"access_token": token, "token_type": "bearer",
            "username": data.username, "name": acc["name"], "role": acc["role"]}
