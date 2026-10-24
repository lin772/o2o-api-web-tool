# -*- coding: utf-8 -*-
"""O2O API Web MVP —— FastAPI 入口。

本轮范围：
- 页面渲染（GET /）
- 读取本地报文模板目录（GET /api/get_template）
- JSON 格式化（POST /api/format_json）

不实现发送请求 / OMS 业务逻辑（第三轮）；db_manager / logger 为占位。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db_manager, oms_client, permission
from .logger import get_logger


# MySQL 的 DATETIME 字段在 pymysql 中会返回 datetime 对象，直接交给 JSONResponse
# 会导致 `Object of type datetime is not JSON serializable`。这里用 jsonable_encoder
# 统一做一次序列化前转换，保证所有返回记录里的日期字段都能正常输出。
class _JSONResponse(JSONResponse):
    def __init__(self, content=None, status_code=200, *args, **kwargs):
        super().__init__(
            jsonable_encoder(content), status_code=status_code, *args, **kwargs
        )


JSONResponse = _JSONResponse

from api_tool import (
    OrderCopyManager,
    StockAdjustToolManager,
    advance_retail_order_to_target,
    auto_adjust_stock_for_retail_order,
    query_platform_order_by_tid,
    query_platform_order_source_by_tid,
    query_retail_order_by_tid,
    query_retail_order_status,
    run_ip_common_transfer,
    run_platform_order_convert,
    run_source_batch_transfer,
    xxl_fetch_page,
)
from ip_order_service import insert_jingdong_order, insert_taobao_order

from order_service import (
    allocate_and_regenerate,
    finalize_build_for_insert,
    format_db_error,
    insert_source,
    query_shop_by_seller_nick,
    query_shops,
)
from reverse_order_service import (
    build_reverse_message,
    insert_platform_return_order,
    run_reverse_transfer,
)
from message_service import (
    BuildResult,
    apply_seller_nick,
    apply_sku_values,
    build_preview_message,
    detect_message_family,
    detect_sku_slots,
    extract_seller_nick,
    extract_tid,
    format_message_json,
    load_template,
    parse_message_text,
)
from reverse_message_service import format_reverse_json, parse_reverse_editor
from settings import (
    get_env_by_label,
    get_platform_meta,
    list_platform_folders,
    list_reverse_scenarios,
    list_scenarios,
    load_config,
    platform_display_name,
)

# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # 项目根目录
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config" / "app.yaml"
MESSAGE_ROOT = BASE_DIR / "公域报文汇总"                     # 报文模板目录

logger = get_logger(__name__)


def _load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:  # 配置读取失败不阻塞启动
            logger.warning("读取配置失败: %s", exc)
    return {}


CONFIG = _load_config()

app = FastAPI(title="O2O API Web MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态目录挂载（目录不存在时自动创建）
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ---------------------------------------------------------------------------
# 模板目录
# ---------------------------------------------------------------------------
def list_template_files() -> list:
    """返回 公域报文汇总 下所有模板文件的相对路径（如 京东/京东拒收.txt）。"""
    if not MESSAGE_ROOT.is_dir():
        return []
    files: list = []
    for platform_dir in sorted(MESSAGE_ROOT.iterdir()):
        if not platform_dir.is_dir():
            continue
        for f in sorted(platform_dir.glob("*.txt")):
            files.append(f.relative_to(MESSAGE_ROOT).as_posix())
    return files


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    template_list = list_template_files()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"template_list": template_list},
    )


@app.get("/api/get_template")
def get_template(filename: str):
    """读取指定模板文件内容，返回其 JSON 文本。"""
    if not filename:
        return JSONResponse({"ok": False, "error": "缺少 filename 参数"}, status_code=400)

    root = MESSAGE_ROOT.resolve()
    target = (MESSAGE_ROOT / filename).resolve()

    # 防路径穿越：目标必须落在 公域报文汇总 目录内
    if not str(target).startswith(str(root) + os.sep):
        return JSONResponse({"ok": False, "error": "非法的文件名"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": f"模板文件不存在: {filename}"}, status_code=404)

    content = target.read_text(encoding="utf-8")
    return JSONResponse({"ok": True, "filename": filename, "content": content})


@app.post("/api/format_json")
async def format_json(request: Request):
    """接收 JSON 字符串，后端格式化；解析失败返回错误信息。"""
    raw = None
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
            if isinstance(payload, str):
                raw = payload
            elif isinstance(payload, dict) and "content" in payload:
                raw = str(payload["content"])
            else:
                raw = json.dumps(payload, ensure_ascii=False)
        else:
            body = await request.body()
            raw = body.decode("utf-8", errors="replace")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"读取请求体失败: {exc}"}, status_code=400)

    if raw is None or not raw.strip():
        return JSONResponse({"ok": False, "error": "内容为空"}, status_code=400)

    try:
        obj = json.loads(raw)
        formatted = json.dumps(obj, ensure_ascii=False, indent=4)
        return JSONResponse({"ok": True, "formatted": formatted})
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "error": f"JSON 解析失败: {exc}"}, status_code=400)


# ---------------------------------------------------------------------------
# 第三轮：请求执行 + 历史记录
# ---------------------------------------------------------------------------
class RunRequestIn(BaseModel):
    method: str = "GET"
    url: str
    headers: dict = {}
    query_params: dict = {}
    body: str = ""
    body_type: str = "json"
    timeout: float | None = None
    ignore_ssl: bool = True


def _get_default_timeout() -> float:
    try:
        return float(CONFIG.get("request_timeout", 15))
    except Exception:
        return 15.0


@app.post("/api/run_request")
async def run_request(payload: RunRequestIn):
    """执行一次 HTTP 请求，并落库历史记录。"""
    timeout = payload.timeout or _get_default_timeout()

    # oms_client.run_request 已捕获全量异常，不会抛栈
    result = oms_client.run_request(
        method=payload.method,
        url=payload.url,
        headers=payload.headers,
        query=payload.query_params,
        body=payload.body,
        body_type=payload.body_type,
        timeout=timeout,
        ignore_ssl=payload.ignore_ssl,
    )

    # 无论成功失败均落库
    try:
        record_id = db_manager.insert_call_record(
            request_method=payload.method.upper(),
            request_url=payload.url,
            request_headers=payload.headers,
            request_body=payload.body,
            response_code=result.get("status_code"),
            response_body=result.get("response_body"),
            cost_ms=result.get("cost_ms"),
            error_msg=result.get("error_msg", ""),
        )
    except Exception as exc:  # 落库失败不影响接口返回
        logger.warning("写入调用历史失败: %s", exc)
        record_id = None

    result["record_id"] = record_id
    return JSONResponse(result)


@app.get("/api/history/{record_id}")
def get_history(record_id: int, request: Request):
    """按 id 读取单条调用历史（列级剥离）。"""
    denied = _menu_gate(request, "history")
    if denied:
        return denied
    user = request.state.user
    record = db_manager.query_record_by_id(record_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    record = permission.strip_fields_by_perm("api_call_history", [record], user["id"])[0]
    return JSONResponse({"ok": True, "record": record})


# ---------------------------------------------------------------------------
# 历史记录：分页列表 + 删除单条 + 历史页面渲染
# ---------------------------------------------------------------------------
@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """渲染调用历史记录页面。"""
    return templates.TemplateResponse(request, "history.html", {})


@app.get("/api/history")
def list_history(request: Request, page: int = 1, page_size: int = 20):
    """分页查询调用历史记录（列级剥离）。"""
    denied = _menu_gate(request, "history")
    if denied:
        return denied
    user = request.state.user
    records, total = db_manager.query_history_list(page, page_size)
    records = permission.strip_fields_by_perm("api_call_history", records, user["id"])
    return JSONResponse(
        {
            "success": True,
            "error_msg": "",
            "page": page,
            "page_size": page_size,
            "total": total,
            "records": records,
        }
    )


@app.delete("/api/history/{record_id}")
def delete_history(record_id: int, request: Request):
    """删除单条调用历史记录（校验菜单写权限）。"""
    denied = _menu_gate(request, "history", write=True)
    if denied:
        return denied
    ok = db_manager.delete_history_by_id(record_id)
    if ok:
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": "记录不存在"}, status_code=404
    )


# ---------------------------------------------------------------------------
# 报文主数据：页面 + 增删改查接口
# ---------------------------------------------------------------------------
class MessageAddIn(BaseModel):
    platform: str = ""
    message_name: str = ""
    message_detail: str = ""
    type: str = "正向"


class MessageUpdateIn(BaseModel):
    message_name: str = ""
    message_detail: str = ""
    type: str = "正向"


class MessageBatchDeleteIn(BaseModel):
    ids: list = []


@app.get("/message-data", response_class=HTMLResponse)
def message_data_page(request: Request):
    """渲染报文主数据页面。"""
    return templates.TemplateResponse(request, "message_data.html", {})


@app.get("/api/message-data/list")
def list_message_data(request: Request, platform: str = ""):
    """根据平台查询报文列表（列级剥离，无行级）。"""
    denied = _menu_gate(request, "message_data")
    if denied:
        return denied
    user = request.state.user
    if not platform:
        return JSONResponse(
            {"success": False, "error_msg": "缺少 platform 参数"}, status_code=400
        )
    records = db_manager.get_message_list(platform)
    records = permission.strip_fields_by_perm("message_main_data", records, user["id"])
    return JSONResponse({"success": True, "error_msg": "", "records": records})


@app.post("/api/message-data/add")
def add_message_data(payload: MessageAddIn, request: Request):
    """新增报文主数据（校验菜单写权限）。"""
    denied = _menu_gate(request, "message_data", write=True)
    if denied:
        return denied
    result = db_manager.add_message_data(
        payload.platform, payload.message_name, payload.message_detail, payload.type
    )
    if result.get("success"):
        return JSONResponse(
            {"success": True, "error_msg": "", "id": result.get("id")}
        )
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "新增失败")},
        status_code=400,
    )


@app.get("/api/message-data/{message_id}")
def get_message_data(message_id: int, request: Request):
    """根据 id 查询单条报文详情（列级剥离）。"""
    denied = _menu_gate(request, "message_data")
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_message_by_id(message_id)
    if record is None:
        return JSONResponse(
            {"success": False, "error_msg": "记录不存在"}, status_code=404
        )
    record = permission.strip_fields_by_perm("message_main_data", [record], user["id"])[0]
    return JSONResponse({"success": True, "error_msg": "", "record": record})


@app.put("/api/message-data/{message_id}")
def update_message_data(message_id: int, payload: MessageUpdateIn, request: Request):
    """修改报文主数据（校验菜单写权限）。"""
    denied = _menu_gate(request, "message_data", write=True)
    if denied:
        return denied
    result = db_manager.update_message_data(
        message_id, payload.message_name, payload.message_detail, payload.type
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "修改失败")},
        status_code=400,
    )


@app.delete("/api/message-data/batch")
def batch_delete_message(payload: MessageBatchDeleteIn, request: Request):
    """批量删除报文主数据（校验菜单写权限）。"""
    denied = _menu_gate(request, "message_data", write=True)
    if denied:
        return denied
    result = db_manager.batch_delete_message(payload.ids)
    if result.get("success"):
        return JSONResponse(
            {"success": True, "error_msg": "", "count": result.get("count", 0)}
        )
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "删除失败")},
        status_code=400,
    )


# ---------------------------------------------------------------------------
# 环境配置（env_config）/ 数据库配置（db_config）CRUD 接口
# 注意：列表与详情接口均不返回 password 字段，页面永不暴露明文密码。
# ---------------------------------------------------------------------------
class EnvConfigIn(BaseModel):
    project_name: str = ""
    env: str = ""
    page_url: str = ""
    account: str = ""
    password: str = ""


class DbConfigIn(BaseModel):
    project_name: str = ""
    env: str = ""
    db_address: str = ""
    port: str = ""
    db_name: str = ""
    username: str = ""
    password: str = ""


class XxlConfigIn(BaseModel):
    project_name: str = ""
    env: str = ""
    address: str = ""
    account: str = ""
    password: str = ""


def _menu_gate(request: Request, menu_code: str, write: bool = False):
    """菜单权限门控：无权限返回 403 响应，否则返回 None。"""
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse({"success": False, "error_msg": "未登录或会话已过期"}, status_code=401)
    item = permission.get_user_menus(user["id"]).get(menu_code, {})
    need = "can_write" if write else "can_view"
    if not item.get(need):
        return JSONResponse({"success": False, "error_msg": "无权访问该功能"}, status_code=403)
    return None


def _env_gate(request: Request, env_label: str):
    """环境行级权限门控：越权返回 403 响应，否则返回 None。"""
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse({"success": False, "error_msg": "未登录或会话已过期"}, status_code=401)
    if not permission.check_env_access(user["id"], env_label):
        return JSONResponse({"success": False, "error_msg": "无权访问该项目/环境"}, status_code=403)
    return None


@app.get("/api/env-config/list")
def list_env_config(request: Request):
    """查询环境配置列表（行级过滤 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "env_config")
    if denied:
        return denied
    user = request.state.user
    records = db_manager.get_env_config_list()
    records = permission.filter_rows_by_scope("env_config", records, user["id"])
    records = permission.strip_fields_by_perm("env_config", records, user["id"])
    return JSONResponse({"success": True, "error_msg": "", "records": records})


@app.get("/api/env-config/options")
def env_config_options(request: Request):
    """返回用户有权限的项目/环境组合，供环境下拉与数据库配置联动。"""
    denied = _menu_gate(request, "env_config")
    if denied:
        return denied
    user = request.state.user
    options = db_manager.get_env_config_options()
    options = permission.filter_rows_by_scope("env_config", options, user["id"])
    return JSONResponse({"success": True, "error_msg": "", "options": options})


@app.post("/api/env-config/add")
def add_env_config(payload: EnvConfigIn, request: Request):
    """新增环境配置（校验菜单写权限）。"""
    denied = _menu_gate(request, "env_config", write=True)
    if denied:
        return denied
    result = db_manager.add_env_config(
        payload.project_name, payload.env, payload.page_url, payload.account, payload.password
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": "", "id": result.get("id")})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "新增失败")}, status_code=400
    )


@app.get("/api/env-config/{config_id}")
def get_env_config(config_id: int, request: Request):
    """查询单条环境配置（行级可见性 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "env_config")
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_env_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("env_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    record = permission.strip_fields_by_perm("env_config", [record], user["id"])[0]
    return JSONResponse({"success": True, "error_msg": "", "record": record})


@app.put("/api/env-config/{config_id}")
def update_env_config(config_id: int, payload: EnvConfigIn, request: Request):
    """修改环境配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "env_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_env_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("env_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.update_env_config(
        config_id, payload.project_name, payload.env, payload.page_url, payload.account, payload.password
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "修改失败")}, status_code=400
    )


@app.delete("/api/env-config/{config_id}")
def delete_env_config(config_id: int, request: Request):
    """删除单条环境配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "env_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_env_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("env_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.delete_env_config(config_id)
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "删除失败")}, status_code=400
    )


@app.get("/api/db-config/list")
def list_db_config(request: Request):
    """查询数据库配置列表（行级过滤 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "db_config")
    if denied:
        return denied
    user = request.state.user
    records = db_manager.get_db_config_list()
    records = permission.filter_rows_by_scope("db_config", records, user["id"])
    records = permission.strip_fields_by_perm("db_config", records, user["id"])
    return JSONResponse({"success": True, "error_msg": "", "records": records})


@app.post("/api/db-config/add")
def add_db_config(payload: DbConfigIn, request: Request):
    """新增数据库配置（校验菜单写权限）。"""
    denied = _menu_gate(request, "db_config", write=True)
    if denied:
        return denied
    result = db_manager.add_db_config(
        payload.project_name, payload.env, payload.db_address, payload.port,
        payload.db_name, payload.username, payload.password,
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": "", "id": result.get("id")})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "新增失败")}, status_code=400
    )


@app.get("/api/db-config/{config_id}")
def get_db_config(config_id: int, request: Request):
    """查询单条数据库配置（行级可见性 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "db_config")
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_db_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("db_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    record = permission.strip_fields_by_perm("db_config", [record], user["id"])[0]
    return JSONResponse({"success": True, "error_msg": "", "record": record})


@app.put("/api/db-config/{config_id}")
def update_db_config(config_id: int, payload: DbConfigIn, request: Request):
    """修改数据库配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "db_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_db_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("db_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.update_db_config(
        config_id, payload.project_name, payload.env, payload.db_address, payload.port,
        payload.db_name, payload.username, payload.password,
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "修改失败")}, status_code=400
    )


@app.delete("/api/db-config/{config_id}")
def delete_db_config(config_id: int, request: Request):
    """删除单条数据库配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "db_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_db_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("db_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.delete_db_config(config_id)
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "删除失败")}, status_code=400
    )


# ---------------------------------------------------------------------------
# XXL-Job 配置（xxl_config）CRUD 接口
# 注意：列表与详情接口均不返回 password 字段，页面永不暴露明文密码。
# ---------------------------------------------------------------------------
@app.get("/api/xxl-config/list")
def list_xxl_config(request: Request):
    """查询 XXL-Job 配置列表（行级过滤 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "xxl_config")
    if denied:
        return denied
    user = request.state.user
    records = db_manager.get_xxl_config_list()
    records = permission.filter_rows_by_scope("xxl_config", records, user["id"])
    records = permission.strip_fields_by_perm("xxl_config", records, user["id"])
    return JSONResponse({"success": True, "error_msg": "", "records": records})


@app.post("/api/xxl-config/add")
def add_xxl_config(payload: XxlConfigIn, request: Request):
    """新增 XXL-Job 配置（校验菜单写权限）。"""
    denied = _menu_gate(request, "xxl_config", write=True)
    if denied:
        return denied
    result = db_manager.add_xxl_config(
        payload.project_name, payload.env, payload.address, payload.account, payload.password
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": "", "id": result.get("id")})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "新增失败")}, status_code=400
    )


@app.get("/api/xxl-config/{config_id}")
def get_xxl_config(config_id: int, request: Request):
    """查询单条 XXL-Job 配置（行级可见性 + 列级剥离，不含密码）。"""
    denied = _menu_gate(request, "xxl_config")
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_xxl_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("xxl_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    record = permission.strip_fields_by_perm("xxl_config", [record], user["id"])[0]
    return JSONResponse({"success": True, "error_msg": "", "record": record})


@app.put("/api/xxl-config/{config_id}")
def update_xxl_config(config_id: int, payload: XxlConfigIn, request: Request):
    """修改 XXL-Job 配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "xxl_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_xxl_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("xxl_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.update_xxl_config(
        config_id, payload.project_name, payload.env, payload.address, payload.account, payload.password
    )
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "修改失败")}, status_code=400
    )


@app.delete("/api/xxl-config/{config_id}")
def delete_xxl_config(config_id: int, request: Request):
    """删除单条 XXL-Job 配置（校验菜单写权限 + 目标行可见性）。"""
    denied = _menu_gate(request, "xxl_config", write=True)
    if denied:
        return denied
    user = request.state.user
    record = db_manager.get_xxl_config_by_id(config_id)
    if record is None:
        return JSONResponse({"success": False, "error_msg": "记录不存在"}, status_code=404)
    if not permission.filter_rows_by_scope("xxl_config", [record], user["id"]):
        return JSONResponse({"success": False, "error_msg": "无权访问该记录"}, status_code=403)
    result = db_manager.delete_xxl_config(config_id)
    if result.get("success"):
        return JSONResponse({"success": True, "error_msg": ""})
    return JSONResponse(
        {"success": False, "error_msg": result.get("error_msg", "删除失败")}, status_code=400
    )


@app.get("/api/xxl/open-page")
def open_xxl_page(request: Request, env: str = "", path: str = ""):
    """带 OMS sessionCookie 访问 XXL-Job 网页（服务端代理，返回页面内容）。"""
    denied = _menu_gate(request, "xxl_config")
    if denied:
        return denied
    if not env:
        return JSONResponse({"success": False, "error_msg": "缺少 env 参数"}, status_code=400)
    try:
        result = xxl_fetch_page(env, path)
        return JSONResponse({
            "success": True,
            "error_msg": "",
            "http": result["http"],
            "content_type": result["content_type"],
            "text": result["text"],
        })
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# 店铺 / 逻辑仓 / 配销仓 / 库存调整（复用 api_tool 中的 Manager）
# ---------------------------------------------------------------------------
class StockAdjustIn(BaseModel):
    logic_store_id: int = 0
    sa_store_id: int = 0
    sku_ecode: str = ""
    qty: str = "1"
    env: str = "TEST"


@app.get("/api/shops")
def list_shops(request: Request, env: str = "TEST"):
    """刷新店铺：读取 cp_c_shop 表，返回店铺列表。"""
    gate = _menu_gate(request, "order_copy")
    if gate:
        return gate
    gate = _env_gate(request, env)
    if gate:
        return gate
    env_key = _resolve_env_key(env)
    manager = None
    try:
        manager = OrderCopyManager(env_key=env_key)
        if not manager.base_conn and not manager.connect_base_db():
            raise RuntimeError(f"基础库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")
        shops = manager.get_all_shops()
        return JSONResponse({"success": True, "error_msg": "", "shops": shops})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)
    finally:
        if manager:
            manager.close()


@app.get("/api/logic-stores")
def list_logic_stores(request: Request, keyword: str = "", env: str = "TEST"):
    """查询逻辑仓档案，返回可下拉选择的逻辑仓（支持按编码/名称模糊搜索）。"""
    gate = _menu_gate(request, "stock_adjust")
    if gate:
        return gate
    gate = _env_gate(request, env)
    if gate:
        return gate
    env_key = _resolve_env_key(env)
    manager = None
    try:
        manager = StockAdjustToolManager(env_key=env_key)
        if not manager.base_conn and not manager.connect_base_db():
            raise RuntimeError(f"基础库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")
        stores = manager.get_logic_stores(keyword)
        return JSONResponse({"success": True, "error_msg": "", "stores": stores})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)
    finally:
        if manager:
            manager.close()


@app.get("/api/sa-stores")
def list_sa_stores(request: Request, logic_store_id: int, env: str = "TEST"):
    """根据逻辑仓查询聚合仓与配销仓。"""
    gate = _menu_gate(request, "stock_adjust")
    if gate:
        return gate
    gate = _env_gate(request, env)
    if gate:
        return gate
    env_key = _resolve_env_key(env)
    manager = None
    try:
        manager = StockAdjustToolManager(env_key=env_key)
        if not manager.base_conn and not manager.connect_base_db():
            raise RuntimeError(f"基础库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")
        if not manager.sg_conn and not manager.connect_sg_db():
            raise RuntimeError(f"SG库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")
        share_store, sa_stores = manager.get_sa_stores_by_logic_store(logic_store_id)
        return JSONResponse(
            {"success": True, "error_msg": "", "share_store": share_store, "sa_stores": sa_stores}
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)
    finally:
        if manager:
            manager.close()


@app.post("/api/stock-adjust/execute")
def execute_stock_adjust(request: Request, payload: StockAdjustIn):
    """执行库存调整：调用 OMS 库存调整单 + 分货单接口。"""
    gate = _menu_gate(request, "stock_adjust", write=True)
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    env_key = _resolve_env_key(payload.env)
    if not payload.logic_store_id:
        return JSONResponse({"success": False, "error_msg": "请选择逻辑仓"}, status_code=400)
    if not payload.sa_store_id:
        return JSONResponse({"success": False, "error_msg": "请选择配销仓"}, status_code=400)

    sku_ecode = payload.sku_ecode.strip()
    if not sku_ecode:
        return JSONResponse({"success": False, "error_msg": "请输入 SKU"}, status_code=400)

    qty = payload.qty.strip()
    try:
        float(qty)
    except Exception:
        return JSONResponse({"success": False, "error_msg": "调整数量必须是数字"}, status_code=400)

    manager = None
    try:
        manager = StockAdjustToolManager(env_key=env_key)
        if not manager.base_conn and not manager.connect_base_db():
            raise RuntimeError(f"基础库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")
        if not manager.sg_conn and not manager.connect_sg_db():
            raise RuntimeError(f"SG库连接失败: {manager.last_error or '请检查数据库地址/VPN/网络'}")

        logic_store = manager.get_logic_store_by_id(payload.logic_store_id)
        if not logic_store:
            return JSONResponse({"success": False, "error_msg": "逻辑仓不存在"}, status_code=400)

        share_store, sa_stores = manager.get_sa_stores_by_logic_store(payload.logic_store_id)
        if not share_store:
            return JSONResponse({"success": False, "error_msg": "当前逻辑仓未找到聚合仓"}, status_code=400)

        sa_store = next((s for s in sa_stores if s.get("id") == payload.sa_store_id), None)
        if not sa_store:
            return JSONResponse({"success": False, "error_msg": "配销仓不存在或不属于当前逻辑仓"}, status_code=400)

        result = manager.execute_flow(logic_store, share_store, sa_store, sku_ecode, qty)

        if not result.get("success"):
            reasons = []
            for step in result.get("steps", []):
                if not step.get("success") and step.get("reason"):
                    reasons.append(f"{step.get('step')}: {step['reason']}")
            return JSONResponse(
                {"success": False, "error_msg": "；".join(reasons) or "库存调整执行未成功", "data": result}
            )
        return JSONResponse({"success": True, "error_msg": "", "data": result})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)
    finally:
        if manager:
            manager.close()


# ---------------------------------------------------------------------------
# 公域平台正向造单 / 逆向造单（复用桌面 GUI 的 service 层逻辑）
# ---------------------------------------------------------------------------
class PlatformOrderPreviewIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    scenario: str = ""
    shop_id: int | None = None
    sku_values: list = []


class PlatformOrderGenerateIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    scenario: str = ""
    shop_id: int | None = None
    editor_text: str = ""
    chain: str = ""
    auto_adjust_stock: bool = False


class ReplaceSellerIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    seller_nick: str = ""
    editor_text: str = ""


class ReplaceSkuIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    scenario: str = ""
    editor_text: str = ""
    sku_values: list = []


class ReverseOrderPreviewIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    scenario: str = ""
    tid: str = ""


class ReverseOrderGenerateIn(BaseModel):
    env: str = "TEST"
    platform: str = ""
    scenario: str = ""
    tid: str = ""
    editor_text: str = ""


def _resolve_env_key(label: str) -> str:
    """把界面环境标签（S4/TEST）解析为配置里的 env key，失败回退 test。"""
    try:
        return get_env_by_label(label or "TEST", load_config())
    except Exception:
        return "test"


def _resolve_shop(env_key: str, platform: str, shop_id) -> dict | None:
    """按店铺 id 从 cp_c_shop 中查找店铺；按平台过滤，保证入库字段完整。"""
    if shop_id is None:
        return None
    cfg = load_config()
    pid = get_platform_meta(platform, cfg).get("platform_id") if platform else None
    try:
        shops = query_shops(env_key, platform_id=int(pid) if pid is not None else None)
    except Exception:
        return None
    for shop in shops:
        if str(shop.get("id")) == str(shop_id):
            return shop
    return None


def _resolve_seller_and_shop(env_key: str, platform: str, shop_id, msg_seller_nick: str):
    """解析卖家昵称与店铺档案。

    优先级：
    1. 用户手动选择店铺（仅用于替换报文中的卖家昵称）；
    2. 否则用报文提取到的卖家昵称反查 cp_c_shop 店铺档案。
    """
    if shop_id is not None:
        shop = _resolve_shop(env_key, platform, shop_id)
        if shop and shop.get("seller_nick"):
            return shop["seller_nick"], shop
    seller = (msg_seller_nick or "").strip()
    if not seller:
        raise ValueError("无法从报文中提取卖家昵称，且未选择店铺，请补充卖家昵称或选择店铺")
    shop = query_shop_by_seller_nick(env_key, seller)
    if not shop:
        raise ValueError(f"店铺档案 cp_c_shop 中未找到卖家昵称 [{seller}] 对应的店铺，请检查或先选择店铺")
    return seller, shop


@app.get("/api/platforms")
def api_platforms():
    """返回 公域报文汇总 目录下的平台文件夹列表（作为平台下拉数据源）。"""
    try:
        return JSONResponse({"success": True, "error_msg": "", "platforms": list_platform_folders()})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)


@app.get("/api/scenarios")
def api_scenarios(platform: str, kind: str = "forward"):
    """返回指定平台下的场景报文文件列表（forward 正向 / reverse 逆向）。"""
    if not platform:
        return JSONResponse({"success": False, "error_msg": "缺少 platform 参数"}, status_code=400)
    try:
        if kind == "reverse":
            scenarios = list_reverse_scenarios(platform)
        else:
            # 正向：文件模板 + 报文主数据中手工新增的正向报文（与文件模板同名的跳过，避免重复）
            scenarios = list_scenarios(platform)
            file_bases = {os.path.splitext(os.path.basename(s))[0] for s in scenarios}
            for record in db_manager.get_message_list(platform):
                if (record.get("type") or "正向") != "正向":
                    continue
                name = (record.get("message_name") or "").strip()
                if name and os.path.splitext(name)[0] in file_bases:
                    continue
                scenarios.append(
                    {
                        "value": f"db:{record['id']}",
                        "label": f"{record['message_name']}（主数据）",
                        "source": "db",
                    }
                )
        return JSONResponse({"success": True, "error_msg": "", "scenarios": scenarios})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=500)


@app.get("/api/platform-order/shops")
def platform_order_shops(request: Request, env: str = "TEST", platform: str = ""):
    """按平台读取店铺档案，供正向造单选择（写入需要 id/ecode/cp_c_shop_title）。"""
    gate = _menu_gate(request, "platform_order")
    if gate:
        return gate
    gate = _env_gate(request, env)
    if gate:
        return gate
    env_key = _resolve_env_key(env)
    cfg = load_config()
    pid = get_platform_meta(platform, cfg).get("platform_id") if platform else None
    try:
        shops = query_shops(env_key, platform_id=int(pid) if pid is not None else None)
        return JSONResponse({"success": True, "error_msg": "", "shops": shops})
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": format_db_error(exc)}, status_code=500)


def _load_scenario_message(platform: str, scenario: str):
    """加载场景报文内容，支持文件模板与报文主数据（db:{id}）。"""
    if scenario.startswith("db:"):
        try:
            record_id = int(scenario.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"非法的报文主数据标识: {scenario}")
        record = db_manager.get_message_by_id(record_id)
        if not record:
            raise FileNotFoundError(f"报文主数据不存在: id={record_id}")
        return json.loads(record["message_detail"])
    return load_template(platform, scenario)


@app.post("/api/platform-order/preview")
def platform_order_preview(request: Request, payload: PlatformOrderPreviewIn):
    """正向仅预览：直接加载所选场景报文模板并格式化，不查库。"""
    gate = _menu_gate(request, "platform_order")
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    if not payload.platform or not payload.scenario:
        return JSONResponse({"success": False, "error_msg": "请选择平台与场景报文"}, status_code=400)
    try:
        raw = _load_scenario_message(payload.platform, payload.scenario)
        seller = extract_seller_nick(raw) or ""
        build = build_preview_message(
            payload.platform, payload.scenario, seller, {}, payload.sku_values or [], base_raw=raw
        )
        return JSONResponse(
            {
                "success": True,
                "error_msg": "",
                "preview": format_message_json(build.mq_message),
                "tid": build.tid,
                "warnings": build.warnings,
            }
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=400)


# 订单节点串联：节点名 -> 目标
#   None             : 接口节点，仅将报文解析落库到首个中间表，不再转单
#   "platform_order" : 其他平台专用，batchTransfer 转单到平台订单即停
#   1/2/4/5/6        : 推进零售发货单 oc_b_order 到目标 order_status
_TB_JD_CHAIN_TARGET = {
    "淘宝订单接口": None,
    "淘宝订单接口-待寻源": 2,
    "淘宝订单接口-待寻源-待审核": 1,
    "淘宝订单接口-待寻源-待审核-配货中": 4,
    "淘宝订单接口-待寻源-待审核-配货中-仓库发货": 5,
    "淘宝订单接口-待寻源-待审核-配货中-仓库发货-平台发货": 6,
    "京东订单接口": None,
    "京东订单接口-待寻源": 2,
    "京东订单接口-待寻源-待审核": 1,
    "京东订单接口-待寻源-待审核-配货中": 4,
    "京东订单接口-待寻源-待审核-配货中-仓库发货": 5,
    "京东订单接口-待寻源-待审核-配货中-仓库发货-平台发货": 6,
}

_STANDPLAT_CHAIN_TARGET = {
    "平台订单报文": None,
    "平台订单报文-平台订单": "platform_order",
    "平台订单报文-平台订单-待寻源": 2,
    "平台订单报文-平台订单-待寻源-待审核": 1,
    "平台订单报文-平台订单-待寻源-待审核-配货中": 4,
    "平台订单报文-平台订单-待寻源-待审核-配货中-仓库发货": 5,
    "平台订单报文-平台订单-待寻源-待审核-配货中-仓库发货-平台发货": 6,
}

_NODE_LABEL = {2: "待寻源", 1: "待审核", 4: "配货中", 5: "仓库发货", 6: "平台发货"}


class _ChainFailure(Exception):
    """记录节点串联失败时所在的节点与失败原因。"""

    def __init__(self, node: str, reason: str):
        super().__init__(reason)
        self.node = node
        self.reason = reason


def _log_advance_result(adv: dict, log) -> None:
    label = _NODE_LABEL.get(adv.get("status"), f"状态{adv.get('status')}")
    log(f"▸ 已推进到{label}")


def _warehouse_ship(env_key: str, retail_order_id: int, progress=None):
    """仓库发货接口：配货中 -> 仓库发货（order_status 4 -> 5）。接口待实现，先占位。"""
    if progress is not None:
        progress("▸ 调用发货接口中。。")
    raise NotImplementedError("发货接口待实现：仓库发货(order_status 4→5)暂未接入")


def _poll_to_platform_ship(env_key: str, retail_order_id: int, log, rounds: int = 10, interval: int = 5):
    """仓库发货(5) -> 平台发货(6)：每 interval 秒查询一次 order_status，rounds 次内到 6 即成功。"""
    for i in range(rounds):
        status = query_retail_order_status(env_key, retail_order_id)
        if status == 6:
            log(f"▸ 零售发货单已进入平台发货状态(6)（第 {i + 1} 轮查询）")
            return
        log(f"▸ 等待平台发货(6)，当前 order_status={status}（第 {i + 1} 轮查询）")
        time.sleep(interval)
    raise RuntimeError("平台发货失败：仓库发货后连续查询未进入 order_status=6")


def _advance_retail_to_node(env_key: str, retail_id: int, target: int, log) -> None:
    """推进零售发货单到目标节点。

    1/2/4 走原推进逻辑；5 需先到配货中(4)再调发货接口；6 需发货接口后再轮询到平台发货。
    """
    if target in (1, 2, 4):
        _log_advance_result(advance_retail_order_to_target(env_key, retail_id, target_status=target, progress=log), log)
        return

    # 仓库发货 / 平台发货：先推进到配货中(4)
    _log_advance_result(advance_retail_order_to_target(env_key, retail_id, target_status=4, progress=log), log)

    # 仓库发货：调用发货接口（占位，待实现后替换）
    _warehouse_ship(env_key, retail_id, progress=log)

    if target == 6:
        _poll_to_platform_ship(env_key, retail_id, log)


def _poll_source_trans_status(env_key: str, tid: str, rounds: int = 10, interval: int = 2):
    """轮询平台订单报文 trans_status，返回最新记录（可能为 None）。"""
    last = None
    for _ in range(rounds):
        last = query_platform_order_source_by_tid(env_key, tid)
        if last and last.get("trans_status") in (2, 4, 5):
            break
        time.sleep(interval)
    return last


def _run_standplat_chain(env_key: str, source_id: int, tid: str, chain: str, progress: list, auto_adjust_stock: bool = False):
    """standplat 平台完整节点串联：SOURCE -> 平台订单 -> 零售发货单 -> 按需推进。"""
    def log(msg: str) -> None:
        progress.append(msg)

    target = _STANDPLAT_CHAIN_TARGET.get(chain)
    stage = "平台订单报文"

    try:
        log(f"▶ 开始执行（节点：{chain}）")

        # 平台订单报文：仅落库（generate 已写 SOURCE），不再转单
        if target is None:
            log("▸ 报文已解析落库到平台订单报文中间表，接口节点完成")
            return

        log("▸ 订单报文下发中。。")
        run_source_batch_transfer(env_key, source_id)

        stage = "平台订单"
        source = _poll_source_trans_status(env_key, tid)
        trans_status = source.get("trans_status") if source else None
        if trans_status in (4, 5):
            raise RuntimeError(f"订单报文下发异常: trans_status={trans_status}")
        if trans_status != 2:
            raise RuntimeError(f"订单报文下发未完成: trans_status={trans_status}")
        log("▸ 订单报文下发成功，正在执行转单")

        # 平台订单：转单到平台订单即停
        if target == "platform_order":
            log("▸ 节点串联完成（平台订单已生成）")
            return

        stage = _NODE_LABEL.get(target, "平台订单")
        po = None
        for _ in range(8):
            po = query_platform_order_by_tid(env_key, tid)
            if po:
                break
            time.sleep(2)
        if not po:
            raise RuntimeError(f"未找到平台订单 tid={tid}")

        log("▸ 转单中（平台订单转零售发货单）。。")
        run_platform_order_convert(env_key, po["id"])

        retail = None
        for _ in range(10):
            retail = query_retail_order_by_tid(env_key, tid)
            if retail:
                break
            time.sleep(2)
        if not retail:
            raise RuntimeError(f"未找到零售发货单 tid={tid}")
        log("▸ 转单成功")

        if retail.get("order_status") == 7:
            log("▸ 零售发货单状态=已取消，结束制单流程")
        else:
            _advance_retail_to_node(env_key, retail["id"], target, log)
            if auto_adjust_stock and query_retail_order_status(env_key, retail["id"]) == 2:
                log("▸ 零售发货单 order_status=2 且已勾选自动调整库存，开始自动调整库存")
                auto_adjust_stock_for_retail_order(
                    env_key, retail["id"], retail.get("cp_c_shop_id"), log=log
                )

        log("▸ 节点串联完成")
    except Exception as exc:
        raise _ChainFailure(stage, str(exc)) from exc


# 淘宝/京东中间表经通用转单后直接生成零售发货单 oc_b_order（不经过 IP_B_PLATFORM_ORDER）
_TAOBAO_TRANSFER = {"table": "IP_B_TAOBAO_ORDER", "tableid": 24421, "menu": "adtable_24421"}
_JINGDONG_TRANSFER = {"table": "IP_B_JINGDONG_ORDER", "tableid": 24482, "menu": "adtable_24482"}


def _run_tb_jd_chain(env_key: str, transfer: dict, master_id: int, tid: str, chain: str, progress: list, auto_adjust_stock: bool = False):
    """淘宝/京东中间表串连接口：通用转单 -> 查询零售发货单 oc_b_order -> 按需推进。"""
    def log(msg: str) -> None:
        progress.append(msg)

    target = _TB_JD_CHAIN_TARGET.get(chain)
    stage = "接口"

    try:
        log(f"▶ 开始执行（节点：{chain}）")

        # 淘宝/京东订单接口：仅落库（generate 已写中间表），不再转单
        if target is None:
            log("▸ 报文已解析落库到中间表，接口节点完成")
            return

        stage = "中间表转单"
        log("▸ 转单中（通用转单 → 零售发货单）。。")
        run_ip_common_transfer(env_key, transfer["table"], transfer["tableid"], [master_id], transfer["menu"])

        stage = "查询零售发货单"
        retail = None
        for _ in range(10):
            retail = query_retail_order_by_tid(env_key, tid)
            if retail:
                break
            time.sleep(2)
        if not retail:
            raise RuntimeError(f"未找到零售发货单 tid={tid}")
        log("▸ 转单成功")

        if retail.get("order_status") == 7:
            log("▸ 零售发货单状态=已取消，结束制单流程")
        else:
            stage = _NODE_LABEL.get(target, "节点")
            _advance_retail_to_node(env_key, retail["id"], target, log)
            if auto_adjust_stock and query_retail_order_status(env_key, retail["id"]) == 2:
                log("▸ 零售发货单 order_status=2 且已勾选自动调整库存，开始自动调整库存")
                auto_adjust_stock_for_retail_order(
                    env_key, retail["id"], retail.get("cp_c_shop_id"), log=log
                )

        log("▸ 节点串联完成")
    except Exception as exc:
        raise _ChainFailure(stage, str(exc)) from exc


@app.post("/api/platform-order/generate")
def platform_order_generate(request: Request, payload: PlatformOrderGenerateIn):
    """正向生成订单：按平台路由写首个中间表；standplat 走订单节点串联。"""
    gate = _menu_gate(request, "platform_order", write=True)
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate

    def respond(result: dict, status_code: int = 200):
        """生成订单无论成功失败均写入调用历史。"""
        try:
            db_manager.insert_call_record(
                request_method="POST",
                request_url="/api/platform-order/generate",
                request_headers={
                    "platform": payload.platform,
                    "scenario": payload.scenario,
                    "chain": payload.chain,
                },
                request_body=payload.editor_text,
                response_code=status_code,
                response_body=result,
                cost_ms=None,
                error_msg=result.get("error_msg", "") if not result.get("success") else "",
            )
        except Exception as exc:  # 落库失败不影响接口返回
            logger.warning("写入生成订单历史失败: %s", exc)
        return JSONResponse(result, status_code=status_code)

    env_key = _resolve_env_key(payload.env)
    if not payload.platform or not payload.scenario:
        return respond({"success": False, "error_msg": "请选择平台与场景报文"}, status_code=400)
    if not payload.editor_text.strip():
        return respond({"success": False, "error_msg": "报文为空，请先点击「仅预览」"}, status_code=400)

    progress: list = []
    try:
        editor_mq = parse_message_text(payload.editor_text)
        msg_seller = extract_seller_nick(editor_mq) or ""
        seller, shop = _resolve_seller_and_shop(env_key, payload.platform, payload.shop_id, msg_seller)

        # 优化点1：首个节点按报文族路由到对应中间表
        family = detect_message_family(editor_mq)

        # 淘宝：写淘宝订单中间表，随后走通用转单串联 -> 零售发货单 oc_b_order
        if family in ("jdp_array", "jdp_object"):
            editor_mq, _new_tid = allocate_and_regenerate(env_key, editor_mq)
            mid, tid = insert_taobao_order(env_key, editor_mq, shop)
            progress.append(f"▸ 写入淘宝订单中间表 ip_b_taobao_order 成功（id={mid}, tid={tid}）")
            chain = payload.chain or "淘宝订单接口"
            try:
                _run_tb_jd_chain(env_key, _TAOBAO_TRANSFER, mid, tid, chain, progress, payload.auto_adjust_stock)
            except _ChainFailure as exc:
                return respond(
                    {
                        "success": False,
                        "error_msg": f"{exc.node}节点失败：{exc.reason}",
                        "tid": tid,
                        "middle_table": "ip_b_taobao_order",
                        "master_id": mid,
                        "chain": chain,
                        "progress": progress,
                        "failed_node": exc.node,
                        "failure_reason": exc.reason,
                    },
                    status_code=500,
                )
            return respond(
                {
                    "success": True,
                    "error_msg": "",
                    "tid": tid,
                    "middle_table": "ip_b_taobao_order",
                    "master_id": mid,
                    "chain": chain,
                    "progress": progress,
                    "failed_node": None,
                    "failure_reason": None,
                }
            )

        # 京东：写京东订单中间表，随后走通用转单串联 -> 零售发货单 oc_b_order
        if family == "jd_array":
            editor_mq, _order_id = allocate_and_regenerate(env_key, editor_mq)
            mid, order_id = insert_jingdong_order(env_key, editor_mq, shop)
            progress.append(f"▸ 写入京东订单中间表 ip_b_jingdong_order 成功（id={mid}, orderId={order_id}）")
            chain = payload.chain or "京东订单接口"
            try:
                _run_tb_jd_chain(env_key, _JINGDONG_TRANSFER, mid, order_id, chain, progress, payload.auto_adjust_stock)
            except _ChainFailure as exc:
                return respond(
                    {
                        "success": False,
                        "error_msg": f"{exc.node}节点失败：{exc.reason}",
                        "tid": order_id,
                        "middle_table": "ip_b_jingdong_order",
                        "master_id": mid,
                        "chain": chain,
                        "progress": progress,
                        "failed_node": exc.node,
                        "failure_reason": exc.reason,
                    },
                    status_code=500,
                )
            return respond(
                {
                    "success": True,
                    "error_msg": "",
                    "tid": order_id,
                    "middle_table": "ip_b_jingdong_order",
                    "master_id": mid,
                    "chain": chain,
                    "progress": progress,
                    "failed_node": None,
                    "failure_reason": None,
                }
            )

        # 其他平台：写平台订单报文中间表，走订单节点串联
        meta = get_platform_meta(payload.platform, load_config())
        if meta.get("platform_id") is None:
            return respond(
                {"success": False, "error_msg": f"平台「{payload.platform}」缺少 platform_id 配置"},
                status_code=400,
            )

        preview_tid = extract_tid(editor_mq)
        if not preview_tid:
            return respond({"success": False, "error_msg": "无法从报文中识别 tid"}, status_code=400)

        build = BuildResult(
            mq_message=editor_mq,
            db_message="",
            tid=preview_tid,
            platform_id=int(meta["platform_id"]),
            method=meta.get("method") or "",
            seller_nick=seller,
            shop=shop,
        )
        db_msg, tid = finalize_build_for_insert(env_key, build, editor_mq)
        source_id = insert_source(env_key, build, db_msg, tid)
        progress.append(f"▸ 写入平台订单报文中间表(SOURCE)成功（id={source_id}, tid={tid}）")

        chain = payload.chain or "平台订单报文"
        try:
            _run_standplat_chain(env_key, source_id, tid, chain, progress, payload.auto_adjust_stock)
        except _ChainFailure as exc:
            return respond(
                {
                    "success": False,
                    "error_msg": f"{exc.node}节点失败：{exc.reason}",
                    "source_id": source_id,
                    "tid": tid,
                    "progress": progress,
                    "failed_node": exc.node,
                    "failure_reason": exc.reason,
                },
                status_code=500,
            )

        return respond(
            {
                "success": True,
                "error_msg": "",
                "source_id": source_id,
                "tid": tid,
                "chain": chain,
                "progress": progress,
                "failed_node": None,
                "failure_reason": None,
            }
        )
    except ValueError as exc:
        return respond({"success": False, "error_msg": str(exc)}, status_code=400)
    except Exception as exc:
        return respond({"success": False, "error_msg": format_db_error(exc)}, status_code=500)


@app.post("/api/platform-order/replace-seller")
def platform_order_replace_seller(request: Request, payload: ReplaceSellerIn):
    """校验卖家昵称是否存在于店铺档案，存在则替换预览报文中所有卖家昵称字段。"""
    gate = _menu_gate(request, "platform_order")
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    env_key = _resolve_env_key(payload.env)
    platform = (payload.platform or "").strip()
    seller_nick = (payload.seller_nick or "").strip()
    if not platform:
        return JSONResponse({"success": False, "error_msg": "请选择平台"}, status_code=400)
    if not seller_nick:
        return JSONResponse({"success": False, "error_msg": "请输入卖家昵称"}, status_code=400)
    if not payload.editor_text.strip():
        return JSONResponse({"success": False, "error_msg": "报文为空，请先点击「仅预览」"}, status_code=400)
    try:
        shop = query_shop_by_seller_nick(env_key, seller_nick)
        if not shop:
            return JSONResponse(
                {
                    "success": False,
                    "error_msg": f"店铺档案 cp_c_shop 中未找到卖家昵称 [{seller_nick}]，请检查后重试",
                },
                status_code=400,
            )

        # 校验店铺档案对应平台与所选平台是否一致
        meta = get_platform_meta(platform, load_config())
        if meta.get("platform_id") is not None:
            shop_platform_id = shop.get("cp_c_platform_id")
            try:
                shop_platform_id = int(shop_platform_id) if shop_platform_id is not None else None
            except (TypeError, ValueError):
                shop_platform_id = None
            if shop_platform_id is not None and shop_platform_id != int(meta["platform_id"]):
                expected = platform_display_name(int(meta["platform_id"]), load_config())
                actual = platform_display_name(shop_platform_id, load_config())
                return JSONResponse(
                    {
                        "success": False,
                        "error_msg": (
                            f"卖家昵称 [{seller_nick}] 对应的平台店铺档案属于「{actual}」，"
                            f"与所选平台「{expected}」不一致，请检查后重试"
                        ),
                    },
                    status_code=400,
                )

        raw = parse_message_text(payload.editor_text)
        apply_seller_nick(raw, seller_nick, detect_message_family(raw))
        return JSONResponse({"success": True, "error_msg": "", "preview": format_message_json(raw)})
    except ValueError as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": format_db_error(exc)}, status_code=500)


@app.post("/api/platform-order/replace-sku")
def platform_order_replace_sku(request: Request, payload: ReplaceSkuIn):
    """按序号替换预览报文中的 SKU（SKU1->第一段，SKU2->第二段…），未填写的保持原值。"""
    gate = _menu_gate(request, "platform_order")
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    platform = (payload.platform or "").strip()
    scenario = (payload.scenario or "").strip()
    if not platform or not scenario:
        return JSONResponse({"success": False, "error_msg": "请选择平台与场景报文"}, status_code=400)
    if not payload.editor_text.strip():
        return JSONResponse({"success": False, "error_msg": "报文为空，请先点击「仅预览」"}, status_code=400)
    sku_values = [str(v).strip() if v is not None else "" for v in (payload.sku_values or [])]
    if not any(sku_values):
        return JSONResponse({"success": False, "error_msg": "请至少填写一个 SKU"}, status_code=400)
    try:
        raw = parse_message_text(payload.editor_text)
        slots = detect_sku_slots(raw)
        if not slots:
            return JSONResponse({"success": False, "error_msg": "未识别到 SKU 槽位"}, status_code=400)
        apply_sku_values(raw, slots, sku_values)
        return JSONResponse(
            {"success": True, "error_msg": "", "preview": format_message_json(raw), "slot_count": len(slots)}
        )
    except ValueError as exc:
        return JSONResponse({"success": False, "error_msg": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": format_db_error(exc)}, status_code=500)


@app.post("/api/reverse-order/preview")
def reverse_order_preview(request: Request, payload: ReverseOrderPreviewIn):
    """逆向仅预览：查零售发货单并回填模板，返回预览文本。"""
    gate = _menu_gate(request, "reverse_order")
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    env_key = _resolve_env_key(payload.env)
    platform = (payload.platform or "").strip()
    scenario = (payload.scenario or "").strip()
    tid = (payload.tid or "").strip()
    if not platform or not scenario:
        return JSONResponse({"success": False, "error_msg": "请选择平台与逆向场景报文"}, status_code=400)
    if not tid:
        return JSONResponse({"success": False, "error_msg": "请填写平台单号（零售发货单 tid）"}, status_code=400)
    try:
        build = build_reverse_message(env_key, platform, scenario, tid)
        return JSONResponse(
            {
                "success": True,
                "error_msg": "",
                "preview": format_reverse_json(build.envelope),
                "return_no": build.return_no,
                "tid": build.tid,
                "message_family": build.message_family,
                "warnings": build.warnings,
            }
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error_msg": format_db_error(exc)}, status_code=400)


@app.post("/api/reverse-order/generate")
def reverse_order_generate(request: Request, payload: ReverseOrderGenerateIn):
    """逆向生成售后申请：解析预览报文 -> 写入 ip_b_platform_return_order -> 售后转换。"""
    gate = _menu_gate(request, "reverse_order", write=True)
    if gate:
        return gate
    gate = _env_gate(request, payload.env)
    if gate:
        return gate
    env_key = _resolve_env_key(payload.env)
    platform = (payload.platform or "").strip()
    scenario = (payload.scenario or "").strip()
    tid = (payload.tid or "").strip()
    if not platform or not scenario:
        return JSONResponse({"success": False, "error_msg": "请选择平台与逆向场景报文"}, status_code=400)
    if not tid:
        return JSONResponse({"success": False, "error_msg": "请填写平台单号（零售发货单 tid）"}, status_code=400)
    progress: list = []
    try:
        def log(msg: str) -> None:
            progress.append(msg)

        log("▶ 开始执行逆向造单")
        log("▸ 构建逆向售后报文。。")
        base = build_reverse_message(env_key, platform, scenario, tid)
        build = parse_reverse_editor(payload.editor_text, base) if payload.editor_text.strip() else base
        log(f"▸ 逆向售后报文构建完成（return_no={build.return_no}）")

        log("▸ 写入平台售后申请中间表(ip_b_platform_return_order)中。。")
        row_id = insert_platform_return_order(env_key, build)
        log(f"▸ 写入完成（id={row_id}）")

        log("▸ 执行售后转换(batchConvert)中。。")
        transfer_result = run_reverse_transfer(env_key, row_id, on_step=log)
        log("▸ 逆向造单完成")
        return JSONResponse(
            {
                "success": True,
                "error_msg": "",
                "return_order_id": row_id,
                "return_no": build.return_no,
                "tid": build.tid,
                "transfer": transfer_result,
                "progress": progress,
            }
        )
    except Exception as exc:
        log(f"✖ 逆向造单失败：{format_db_error(exc)}")
        return JSONResponse(
            {"success": False, "error_msg": format_db_error(exc), "progress": progress},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# 权限体系：登录 / 会话 / 权限管理 API / auth 中间件
# 登录与权限完全独立：登录只校验 sys_user 的账号密码 + status；
# 菜单/数据权限只决定"进来后看到什么"，绝不决定"能不能进来"。
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class UserIn(BaseModel):
    username: str = ""
    password: str = ""
    real_name: str = ""
    email: str = ""
    phone: str = ""
    status: int = 1
    remark: str = ""


class UserRolesIn(BaseModel):
    role_ids: list = []


class RoleIn(BaseModel):
    role_code: str = ""
    role_name: str = ""
    status: int = 1
    sort_no: int = 0
    description: str = ""


class MenuPermIn(BaseModel):
    items: list = []


class DataPermIn(BaseModel):
    items: list = []


class ScopeValueIn(BaseModel):
    items: list = []


# 免登录路径：登录页、登录接口、静态资源、文档。
_PUBLIC_PREFIXES = ("/static/", "/docs", "/redoc", "/openapi.json")
_PUBLIC_EXACT = ("/login", "/api/auth/login", "/favicon.ico")


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _require_super(request: Request):
    """权限管理 API 门控：仅超级管理员可操作。返回 (user, error_response)。"""
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_super"):
        return user, JSONResponse(
            {"success": False, "error_msg": "无权访问，需要超级管理员权限"}, status_code=403
        )
    return user, None


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """登录态中间件：未登录的页面跳转 /login，未登录的 /api 返回 401。

    仅负责第 1 层（登录态），不做菜单/数据权限判定（那属于第 2/3 层，
    由各资源接口自行校验并返回 403）。"""
    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    user = permission.get_current_user(request)
    request.state.user = user

    if user is None:
        if path.startswith("/api/"):
            return JSONResponse(
                {"success": False, "error_msg": "未登录或会话已过期"}, status_code=401
            )
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)


@app.on_event("startup")
def startup_seed():
    """启动时幂等初始化权限表 + 内置超管 + 内置角色/权限种子数据。"""
    try:
        permission.init_seed_data()
    except Exception as exc:  # 种子失败不阻塞启动，但记录日志便于排查
        logger.error("初始化权限种子数据失败: %s", exc)


# ---------------------------------------------------------------------------
# 登录 / 登出 / 当前用户
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if permission.get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/api/auth/login")
def auth_login(payload: LoginIn, request: Request):
    """登录：只查 sys_user 一张表，绝不校验角色/菜单/数据权限。"""
    username = (payload.username or "").strip()
    user = permission.get_user_by_username(username) if username else None
    if not user or not permission.verify_password(payload.password or "", user["password_hash"]):
        return JSONResponse({"success": False, "error_msg": "账号或密码错误"}, status_code=401)
    if user["status"] != 1:
        return JSONResponse({"success": False, "error_msg": "账号已停用，请联系管理员"}, status_code=401)

    session_id = permission.create_session(user["id"], request)
    response = JSONResponse({"success": True, "error_msg": "", "user": permission.public_user_info(user)})
    response.set_cookie("session_id", session_id, httponly=True, samesite="lax", max_age=8 * 3600)
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    session_id = request.cookies.get("session_id")
    permission.destroy_session(session_id)
    response = JSONResponse({"success": True, "error_msg": ""})
    response.delete_cookie("session_id")
    return response


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse({"success": False, "error_msg": "未登录"}, status_code=401)
    menus = permission.get_user_menus(user["id"])
    fields = {}
    for table_code in permission.TABLE_FIELDS:
        fields[table_code] = sorted(permission.get_user_visible_fields(user["id"], table_code) - {"password"})
    return JSONResponse(
        {
            "success": True,
            "error_msg": "",
            "user": permission.public_user_info(user),
            "menus": menus,
            "fields": fields,
            "roles": [r["role_name"] for r in permission.get_user_roles(user["id"])],
        }
    )


# ---------------------------------------------------------------------------
# 用户档案管理（is_super 门控）
# ---------------------------------------------------------------------------
@app.get("/api/users")
def api_users_list(request: Request):
    _, err = _require_super(request)
    if err:
        return err
    real_name = (request.query_params.get("real_name") or "").strip()
    status = request.query_params.get("status")
    where = ["username NOT IN ('root', 'admin')"]
    args = []
    if real_name:
        where.append("real_name LIKE %s")
        args.append(f"%{real_name}%")
    if status in ("0", "1"):
        where.append("status = %s")
        args.append(int(status))
    users = permission.query(
        "SELECT id, username, real_name, email, phone, status, is_super, "
        "last_login_time, last_login_ip, remark, create_time FROM sys_user "
        "WHERE " + " AND ".join(where) + " ORDER BY id",
        args,
    )
    for u in users:
        roles = permission.query(
            "SELECT r.role_name FROM sys_role r "
            "JOIN sys_user_role ur ON ur.role_id = r.id "
            "WHERE ur.user_id = %s ORDER BY r.sort_no, r.id",
            (u["id"],),
        )
        u["roles"] = [r["role_name"] for r in roles]
    return JSONResponse({"success": True, "error_msg": "", "data": users})


@app.post("/api/users")
def api_users_create(payload: UserIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    username = (payload.username or "").strip()
    if not username:
        return JSONResponse({"success": False, "error_msg": "登录账号不能为空"}, status_code=400)
    if not payload.password:
        return JSONResponse({"success": False, "error_msg": "密码不能为空"}, status_code=400)
    if permission.get_user_by_username(username):
        return JSONResponse({"success": False, "error_msg": "登录账号已存在"}, status_code=400)
    permission.execute(
        "INSERT INTO sys_user (username, password_hash, real_name, email, phone, status, is_super, remark) "
        "VALUES (%s, %s, %s, %s, %s, %s, 0, %s)",
        (
            username,
            permission.hash_password(payload.password),
            payload.real_name,
            payload.email,
            payload.phone,
            payload.status,
            payload.remark,
        ),
    )
    return JSONResponse({"success": True, "error_msg": ""})


@app.put("/api/users/{user_id}")
def api_users_update(user_id: int, payload: UserIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    existing = permission.query("SELECT * FROM sys_user WHERE id = %s", (user_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "用户不存在"}, status_code=404)
    permission.execute(
        "UPDATE sys_user SET real_name = %s, email = %s, phone = %s, status = %s, remark = %s WHERE id = %s",
        (payload.real_name, payload.email, payload.phone, payload.status, payload.remark, user_id),
    )
    if payload.password:
        permission.execute(
            "UPDATE sys_user SET password_hash = %s WHERE id = %s",
            (permission.hash_password(payload.password), user_id),
        )
        permission.destroy_user_sessions(user_id)  # 改密码立即踢下线
    if payload.status != 1:
        permission.destroy_user_sessions(user_id)  # 停用立即踢下线
    return JSONResponse({"success": True, "error_msg": ""})


@app.post("/api/users/{user_id}/reset-password")
def api_users_reset_password(user_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    existing = permission.query("SELECT * FROM sys_user WHERE id = %s", (user_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "用户不存在"}, status_code=404)
    if existing.get("is_super"):
        return JSONResponse({"success": False, "error_msg": "不能重置超级管理员密码"}, status_code=400)
    permission.execute(
        "UPDATE sys_user SET password_hash = %s WHERE id = %s",
        (permission.hash_password("123456"), user_id),
    )
    permission.destroy_user_sessions(user_id)  # 重置密码立即踢下线
    return JSONResponse({"success": True, "error_msg": ""})


@app.delete("/api/users/{user_id}")
def api_users_delete(user_id: int, request: Request):
    current, err = _require_super(request)
    if err:
        return err
    if current and current["id"] == user_id:
        return JSONResponse({"success": False, "error_msg": "不能删除当前登录账号"}, status_code=400)
    existing = permission.query("SELECT * FROM sys_user WHERE id = %s", (user_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "用户不存在"}, status_code=404)
    if existing.get("is_super"):
        return JSONResponse({"success": False, "error_msg": "不能删除超级管理员账号"}, status_code=400)
    permission.execute("DELETE FROM sys_user_role WHERE user_id = %s", (user_id,))
    permission.execute("DELETE FROM sys_session WHERE user_id = %s", (user_id,))
    permission.execute("DELETE FROM sys_user WHERE id = %s", (user_id,))
    return JSONResponse({"success": True, "error_msg": ""})


@app.get("/api/users/{user_id}/roles")
def api_user_roles_get(user_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    rows = permission.query("SELECT role_id FROM sys_user_role WHERE user_id = %s", (user_id,))
    return JSONResponse({"success": True, "error_msg": "", "data": [r["role_id"] for r in rows]})


@app.post("/api/users/{user_id}/roles")
def api_user_roles_set(user_id: int, payload: UserRolesIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    existing = permission.query("SELECT id FROM sys_user WHERE id = %s", (user_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "用户不存在"}, status_code=404)
    permission.execute("DELETE FROM sys_user_role WHERE user_id = %s", (user_id,))
    for rid in payload.role_ids:
        permission.execute(
            "INSERT INTO sys_user_role (user_id, role_id) VALUES (%s, %s)", (user_id, rid)
        )
    return JSONResponse({"success": True, "error_msg": ""})


# ---------------------------------------------------------------------------
# 角色档案管理（is_super 门控）
# ---------------------------------------------------------------------------
@app.get("/api/roles")
def api_roles_list(request: Request):
    _, err = _require_super(request)
    if err:
        return err
    roles = permission.query("SELECT * FROM sys_role ORDER BY sort_no, id")
    for r in roles:
        cnt = permission.query(
            "SELECT COUNT(*) AS c FROM sys_user_role WHERE role_id = %s", (r["id"],), fetch="one"
        )
        r["user_count"] = (cnt or {}).get("c", 0)
    return JSONResponse({"success": True, "error_msg": "", "data": roles})


@app.post("/api/roles")
def api_roles_create(payload: RoleIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    code = (payload.role_code or "").strip()
    name = (payload.role_name or "").strip()
    if not name:
        return JSONResponse({"success": False, "error_msg": "角色名称不能为空"}, status_code=400)
    if not code:
        code = "role_" + str(int(time.time() * 1000))
    if permission.query("SELECT id FROM sys_role WHERE role_code = %s", (code,), fetch="one"):
        return JSONResponse({"success": False, "error_msg": "角色编码已存在"}, status_code=400)
    permission.execute(
        "INSERT INTO sys_role (role_code, role_name, status, is_builtin, sort_no, description) "
        "VALUES (%s, %s, %s, 0, %s, %s)",
        (code, name, payload.status, payload.sort_no, payload.description),
    )
    return JSONResponse({"success": True, "error_msg": ""})


@app.put("/api/roles/{role_id}")
def api_roles_update(role_id: int, payload: RoleIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    existing = permission.query("SELECT * FROM sys_role WHERE id = %s", (role_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "角色不存在"}, status_code=404)
    permission.execute(
        "UPDATE sys_role SET role_name = %s, status = %s, sort_no = %s, description = %s WHERE id = %s",
        (payload.role_name, payload.status, payload.sort_no, payload.description, role_id),
    )
    return JSONResponse({"success": True, "error_msg": ""})


@app.delete("/api/roles/{role_id}")
def api_roles_delete(role_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    existing = permission.query("SELECT * FROM sys_role WHERE id = %s", (role_id,), fetch="one")
    if not existing:
        return JSONResponse({"success": False, "error_msg": "角色不存在"}, status_code=404)
    if existing.get("is_builtin"):
        return JSONResponse({"success": False, "error_msg": "内置角色不可删除"}, status_code=400)
    permission.execute("DELETE FROM sys_user_role WHERE role_id = %s", (role_id,))
    permission.execute("DELETE FROM sys_menu_perm WHERE role_id = %s", (role_id,))
    permission.execute("DELETE FROM sys_data_perm WHERE role_id = %s", (role_id,))
    permission.execute("DELETE FROM sys_scope_value WHERE role_id = %s", (role_id,))
    permission.execute("DELETE FROM sys_role WHERE id = %s", (role_id,))
    return JSONResponse({"success": True, "error_msg": ""})


@app.get("/api/roles/{role_id}/users")
def api_role_users(role_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    users = permission.query(
        "SELECT u.id, u.username, u.real_name, u.status FROM sys_user u "
        "JOIN sys_user_role ur ON ur.user_id = u.id WHERE ur.role_id = %s ORDER BY u.id",
        (role_id,),
    )
    return JSONResponse({"success": True, "error_msg": "", "data": users})


@app.get("/api/roles/{role_id}/menus")
def api_role_menus_get(role_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    rows = permission.query(
        "SELECT menu_code, can_view, can_write FROM sys_menu_perm WHERE role_id = %s", (role_id,)
    )
    return JSONResponse({"success": True, "error_msg": "", "data": rows})


@app.put("/api/roles/{role_id}/menus")
def api_role_menus_put(role_id: int, payload: MenuPermIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    permission.execute("DELETE FROM sys_menu_perm WHERE role_id = %s", (role_id,))
    for it in payload.items:
        permission.execute(
            "INSERT INTO sys_menu_perm (role_id, menu_code, can_view, can_write) VALUES (%s, %s, %s, %s)",
            (role_id, it.get("menu_code"), 1 if it.get("can_view") else 0, 1 if it.get("can_write") else 0),
        )
    return JSONResponse({"success": True, "error_msg": ""})


@app.get("/api/roles/{role_id}/data-perms")
def api_role_data_get(role_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    rows = permission.query(
        "SELECT table_code, field_code, can_view, can_edit FROM sys_data_perm WHERE role_id = %s",
        (role_id,),
    )
    return JSONResponse({"success": True, "error_msg": "", "data": rows})


@app.put("/api/roles/{role_id}/data-perms")
def api_role_data_put(role_id: int, payload: DataPermIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    permission.execute("DELETE FROM sys_data_perm WHERE role_id = %s", (role_id,))
    for it in payload.items:
        permission.execute(
            "INSERT INTO sys_data_perm (role_id, table_code, field_code, can_view, can_edit) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                role_id,
                it.get("table_code"),
                it.get("field_code"),
                1 if it.get("can_view") else 0,
                1 if it.get("can_edit") else 0,
            ),
        )
    return JSONResponse({"success": True, "error_msg": ""})


@app.get("/api/roles/{role_id}/scope-values")
def api_role_scope_get(role_id: int, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    rows = permission.query(
        "SELECT table_code, scope_type, scope_value FROM sys_scope_value WHERE role_id = %s",
        (role_id,),
    )
    return JSONResponse({"success": True, "error_msg": "", "data": rows})


@app.put("/api/roles/{role_id}/scope-values")
def api_role_scope_put(role_id: int, payload: ScopeValueIn, request: Request):
    _, err = _require_super(request)
    if err:
        return err
    permission.execute("DELETE FROM sys_scope_value WHERE role_id = %s", (role_id,))
    for it in payload.items:
        permission.execute(
            "INSERT INTO sys_scope_value (role_id, table_code, scope_type, scope_value) "
            "VALUES (%s, %s, %s, %s)",
            (role_id, it.get("table_code"), it.get("scope_type"), it.get("scope_value")),
        )
    return JSONResponse({"success": True, "error_msg": ""})


# ---------------------------------------------------------------------------
# 权限配置元数据（供前端勾选界面渲染）
# ---------------------------------------------------------------------------
@app.get("/api/perm/menus")
def api_perm_menus(request: Request):
    _, err = _require_super(request)
    if err:
        return err
    return JSONResponse(
        {"success": True, "error_msg": "", "groups": permission.MENU_GROUPS, "options": permission.MENU_OPTIONS}
    )


@app.get("/api/perm/table-fields")
def api_perm_table_fields(request: Request):
    _, err = _require_super(request)
    if err:
        return err
    return JSONResponse(
        {
            "success": True,
            "error_msg": "",
            "tables": permission.TABLE_FIELDS,
            "names": permission.TABLE_NAMES,
            "field_comments": permission.FIELD_COMMENTS,
        }
    )


@app.get("/api/perm/scope-options")
def api_perm_scope_options(request: Request):
    _, err = _require_super(request)
    if err:
        return err
    projects, envs = set(), set()
    for table_code in permission.SCOPE_TABLES:
        for r in permission.query(f"SELECT DISTINCT project_name, env FROM {table_code}"):
            if r.get("project_name"):
                projects.add(r["project_name"])
            if r.get("env"):
                envs.add(r["env"])
    return JSONResponse({"success": True, "error_msg": "", "project_name": sorted(projects), "env": sorted(envs)})


# ---------------------------------------------------------------------------
# 权限配置页面路由（登录即可访问；数据接口由 is_super 门控）
# ---------------------------------------------------------------------------
@app.get("/perm/user", response_class=HTMLResponse)
def perm_user_page(request: Request):
    return templates.TemplateResponse(request, "perm_user.html", {})


@app.get("/perm/role", response_class=HTMLResponse)
def perm_role_page(request: Request):
    return templates.TemplateResponse(request, "perm_role.html", {})


@app.get("/perm/menu", response_class=HTMLResponse)
def perm_menu_page(request: Request):
    return templates.TemplateResponse(request, "perm_menu.html", {})


@app.get("/perm/data", response_class=HTMLResponse)
def perm_data_page(request: Request):
    return templates.TemplateResponse(request, "perm_data.html", {})


# ---------------------------------------------------------------------------
# 请求日志中间件：记录每个请求的 method / url / 状态码 / 耗时 / 错误
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录请求耗时与错误信息到控制台，异常时打印完整栈再重新抛出。"""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        cost_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "请求异常 method=%s url=%s cost_ms=%d error=%s",
            request.method,
            request.url,
            cost_ms,
            exc,
        )
        raise
    cost_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "请求完成 method=%s url=%s status=%d cost_ms=%d",
        request.method,
        request.url,
        response.status_code,
        cost_ms,
    )
    return response


# ---------------------------------------------------------------------------
# 全局异常处理器：捕获更多异常，控制台打印完整日志，前端返回友好提示
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """HTTP 异常统一返回 success/error_msg 结构体，状态码透传。"""
    logger.warning(
        "HTTP 异常 status=%d detail=%s url=%s",
        exc.status_code,
        exc.detail,
        request.url,
    )
    return JSONResponse(
        {
            "success": False,
            "error_msg": str(exc.detail),
            "status_code": exc.status_code,
        },
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """请求参数校验失败时返回可读的提示信息。"""
    logger.warning("参数校验失败 url=%s error=%s", request.url, exc.errors())
    return JSONResponse(
        {"success": False, "error_msg": "请求参数不合法", "detail": exc.errors()},
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理：完整打印错误栈，前端只返回友好提示。"""
    logger.error(
        "未处理异常 method=%s url=%s",
        request.method,
        request.url,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        {"success": False, "error_msg": "服务器内部错误，请稍后重试", "status_code": 500},
        status_code=500,
    )
