# -*- coding: utf-8 -*-
"""RBAC 权限体系：密码哈希 / 会话 / 权限聚合 / 行级过滤 / 列级剥离 / 种子数据。

设计原则（与《权限体系开发规格书》一致）：
- 登录与权限完全独立：登录只校验 sys_user 的账号密码 + status；
  菜单/数据权限只决定"进来后看到什么"，绝不决定"能不能进来"。
- 三层判定：第1层登录态(Session) -> 第2层菜单权限 -> 第3层数据权限(行级+列级)。
- 行级权限：角色内 AND、角色间 OR；值域支持 "*" 通配表达全选。
- 字段权限合并用 OR + DISTINCT（不是 AND）。
- password 永不返回前端（保持 db_manager 现有安全基线）。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from typing import Any, Optional

from . import db_manager

# 组合环境 key 分隔符，与 settings.ENV_KEY_SEP 保持一致。
ENV_KEY_SEP = "||"

# pbkdf2 迭代次数（与 DDL 注释 pbkdf2_sha256$260000$盐$哈希 一致）。
PBKDF2_ITERATIONS = 260000

# 会话有效期（小时）。
SESSION_EXPIRE_HOURS = 8

# 无论字段权限如何，这些字段始终可见（id 与审计时间列）。
ALWAYS_VISIBLE_FIELDS = {"id", "create_time", "update_time"}

# 行级权限仅作用于三张基础配置表（其余表无 project_name/env 列）。
SCOPE_TABLES = ("env_config", "db_config", "xxl_config")

# 菜单清单（code 严格对齐前端 index.html 的 data-tab 值）。
MENU_GROUPS = [
    {"code": "basic_config", "name": "基础配置"},
    {"code": "system", "name": "系统管理"},
]

MENU_OPTIONS = [
    {"code": "env_config", "name": "环境配置", "parent_code": "basic_config", "is_write": True},
    {"code": "db_config", "name": "数据库配置", "parent_code": "basic_config", "is_write": True},
    {"code": "xxl_config", "name": "XXL-Job配置", "parent_code": "basic_config", "is_write": True},
    {"code": "order_copy", "name": "平台订单中间表复制", "parent_code": "", "is_write": True},
    {"code": "stock_adjust", "name": "调整库存", "parent_code": "", "is_write": True},
    {"code": "platform_order", "name": "公域平台正向造单", "parent_code": "", "is_write": True},
    {"code": "reverse_order", "name": "公域平台逆向造单", "parent_code": "", "is_write": True},
    {"code": "history", "name": "历史记录", "parent_code": "", "is_write": False},
    {"code": "message_data", "name": "报文主数据", "parent_code": "", "is_write": True},
    {"code": "perm_user", "name": "用户管理", "parent_code": "system", "is_write": True},
    {"code": "perm_role", "name": "角色管理", "parent_code": "system", "is_write": True},
    {"code": "perm_menu", "name": "菜单权限", "parent_code": "system", "is_write": True},
    {"code": "perm_data", "name": "数据权限", "parent_code": "system", "is_write": True},
]

# 受控表 -> 字段白名单（代码内维护，绝不从 information_schema 动态取，防漂移/防注入）。
TABLE_FIELDS = {
    "env_config": ["id", "project_name", "env", "page_url", "account", "password", "create_time", "update_time"],
    "db_config": ["id", "project_name", "env", "db_address", "port", "db_name", "username", "password", "create_time", "update_time"],
    "xxl_config": ["id", "project_name", "env", "address", "account", "password", "create_time", "update_time"],
    "message_main_data": ["id", "platform", "message_name", "message_detail", "type", "create_time", "update_time"],
    "api_call_history": ["id", "request_method", "request_url", "request_headers", "request_body",
                         "response_code", "response_body", "cost_ms", "error_msg", "create_time"],
}

TABLE_NAMES = {
    "env_config": "环境配置",
    "db_config": "数据库配置",
    "xxl_config": "XXL-Job配置",
    "message_main_data": "报文主数据",
    "api_call_history": "调用历史",
}

# 受控字段 -> 界面展示名（数据库列备注）。仅用于数据权限页展示，字段白名单仍以 TABLE_FIELDS 为准。
FIELD_COMMENTS = {
    "env_config": {
        "id": "主键ID", "project_name": "项目名称", "env": "环境", "page_url": "页面URL",
        "account": "账号", "password": "密码", "create_time": "创建时间", "update_time": "更新时间",
    },
    "db_config": {
        "id": "主键ID", "project_name": "项目名称", "env": "环境", "db_address": "数据库地址",
        "port": "端口", "db_name": "数据库名", "username": "用户名", "password": "密码",
        "create_time": "创建时间", "update_time": "更新时间",
    },
    "xxl_config": {
        "id": "主键ID", "project_name": "项目名称", "env": "环境", "address": "XXL-Job调度中心地址",
        "account": "账号", "password": "密码", "create_time": "创建时间", "update_time": "更新时间",
    },
    "message_main_data": {
        "id": "主键ID", "platform": "平台", "message_name": "报文名称", "message_detail": "报文详情",
        "type": "类型(正向/逆向)", "create_time": "创建时间", "update_time": "更新时间",
    },
    "api_call_history": {
        "id": "主键ID", "request_method": "请求方法", "request_url": "请求URL",
        "request_headers": "请求头", "request_body": "请求体", "response_code": "响应状态码",
        "response_body": "响应体", "cost_ms": "耗时(毫秒)", "error_msg": "错误信息", "create_time": "创建时间",
    },
}

# 内置超管初始账号（首次登录后应提示修改密码）。
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin@123456"

# root 系统管理员：无需配置任何功能/数据权限，直接拥有全部最高权限（is_super=1）。
ROOT_USERNAME = "root"
ROOT_PASSWORD = "123456"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 基础 DB 访问（复用 db_manager 的连接与 init_db）
# ---------------------------------------------------------------------------
def query(sql: str, params: Any = None, fetch: str = "all"):
    """执行查询。fetch: "all" -> list[dict]，"one" -> dict|None。"""
    db_manager.init_db()
    with _lock:
        conn = db_manager._connect()
        try:
            cur = conn.execute(sql, params)
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row is not None else None
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def execute(sql: str, params: Any = None) -> int:
    """执行写操作并提交，返回 lastrowid。"""
    db_manager.init_db()
    with _lock:
        conn = db_manager._connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return int(cur.lastrowid or 0)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 密码哈希（手写 hashlib，零编译依赖）
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _algo, iterations, salt, digest = password_hash.split("$")
        iterations = int(iterations)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        )
        return secrets.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def public_user_info(user: dict) -> dict:
    """剔除敏感字段，返回可返回前端的用户信息。"""
    return {
        "id": user.get("id"),
        "username": user.get("username", ""),
        "real_name": user.get("real_name", ""),
        "email": user.get("email", ""),
        "phone": user.get("phone", ""),
        "status": user.get("status", 1),
        "is_super": user.get("is_super", 0),
        "last_login_time": user.get("last_login_time"),
        "remark": user.get("remark", ""),
    }


# ---------------------------------------------------------------------------
# 会话管理（服务端 Session，落库 sys_session）
# ---------------------------------------------------------------------------
def create_session(user_id: int, request) -> str:
    session_id = secrets.token_hex(32)
    login_ip = request.client.host if request.client else ""
    user_agent = (request.headers.get("user-agent") or "")[:255]
    execute(
        """
        INSERT INTO sys_session (session_id, user_id, login_ip, user_agent, expire_time)
        VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL %s HOUR))
        """,
        (session_id, user_id, login_ip, user_agent, SESSION_EXPIRE_HOURS),
    )
    execute(
        "UPDATE sys_user SET last_login_time = NOW(), last_login_ip = %s WHERE id = %s",
        (login_ip, user_id),
    )
    cleanup_expired_sessions()
    return session_id


def destroy_session(session_id: str) -> None:
    if session_id:
        execute("DELETE FROM sys_session WHERE session_id = %s", (session_id,))


def destroy_user_sessions(user_id: int) -> None:
    execute("DELETE FROM sys_session WHERE user_id = %s", (user_id,))


def cleanup_expired_sessions() -> None:
    execute("DELETE FROM sys_session WHERE expire_time < NOW()")


def get_current_user(request) -> Optional[dict]:
    """根据 Cookie 中的 session_id 解析当前登录用户；未登录/过期/停用返回 None。"""
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    row = query(
        """
        SELECT u.id, u.username, u.real_name, u.email, u.phone, u.status, u.is_super,
               u.last_login_time, u.remark
        FROM sys_session s
        JOIN sys_user u ON u.id = s.user_id
        WHERE s.session_id = %s AND s.expire_time > NOW() AND u.status = 1
        """,
        (session_id,),
        fetch="one",
    )
    return row


def get_user_by_username(username: str) -> Optional[dict]:
    return query("SELECT * FROM sys_user WHERE username = %s", (username,), fetch="one")


def is_super_user(user_id: int) -> bool:
    row = query("SELECT is_super FROM sys_user WHERE id = %s", (user_id,), fetch="one")
    return bool(row and row.get("is_super"))


# ---------------------------------------------------------------------------
# 权限聚合（多角色 OR 合并）
# ---------------------------------------------------------------------------
def get_user_roles(user_id: int) -> list:
    rows = query(
        """
        SELECT r.* FROM sys_role r
        JOIN sys_user_role ur ON ur.role_id = r.id
        WHERE ur.user_id = %s AND r.status = 1
        ORDER BY r.sort_no ASC, r.id ASC
        """,
        (user_id,),
    )
    return rows


def get_user_menus(user_id: int) -> dict:
    """返回 {menu_code: {can_view, can_write}}，多角色 OR 合并；超管返回全菜单。"""
    if is_super_user(user_id):
        return {m["code"]: {"can_view": True, "can_write": True} for m in MENU_OPTIONS}
    rows = query(
        """
        SELECT mp.menu_code, mp.can_view, mp.can_write
        FROM sys_menu_perm mp
        JOIN sys_user_role ur ON ur.role_id = mp.role_id
        JOIN sys_role r ON r.id = mp.role_id
        WHERE ur.user_id = %s AND r.status = 1
        """,
        (user_id,),
    )
    menus: dict = {}
    for r in rows:
        code = r["menu_code"]
        item = menus.setdefault(code, {"can_view": False, "can_write": False})
        item["can_view"] = item["can_view"] or bool(r.get("can_view"))
        item["can_write"] = item["can_write"] or bool(r.get("can_write"))
    return menus


def get_user_visible_fields(user_id: int, table_code: str) -> set:
    """返回用户可见字段集合；is_super 返回该表全部字段。"""
    allowed = set(TABLE_FIELDS.get(table_code, []))
    if is_super_user(user_id):
        return allowed
    rows = query(
        """
        SELECT DISTINCT dp.field_code
        FROM sys_data_perm dp
        JOIN sys_user_role ur ON ur.role_id = dp.role_id
        JOIN sys_role r ON r.id = dp.role_id
        WHERE ur.user_id = %s AND dp.table_code = %s AND dp.can_view = 1 AND r.status = 1
        """,
        (user_id, table_code),
    )
    fields = {r["field_code"] for r in rows}
    fields |= ALWAYS_VISIBLE_FIELDS
    return fields & allowed


def get_role_scope_matrix(role_id: int, table_code: str) -> dict:
    """按角色返回该角色在该表上的值域 {'project_name': set, 'env': set}。"""
    rows = query(
        "SELECT scope_type, scope_value FROM sys_scope_value WHERE role_id = %s AND table_code = %s",
        (role_id, table_code),
    )
    matrix = {"project_name": set(), "env": set()}
    for r in rows:
        t = r["scope_type"]
        if t in matrix:
            matrix[t].add(r["scope_value"])
    return matrix


def get_user_scope_values(user_id: int, table_code: str) -> dict:
    """返回用户在该表上的值域并集 {'project_name': set, 'env': set}。"""
    if is_super_user(user_id):
        return {"project_name": {"*"}, "env": {"*"}}
    merged = {"project_name": set(), "env": set()}
    for role in get_user_roles(user_id):
        sc = get_role_scope_matrix(role["id"], table_code)
        merged["project_name"] |= sc["project_name"]
        merged["env"] |= sc["env"]
    return merged


# ---------------------------------------------------------------------------
# 行级过滤（§2.4：角色内 AND、角色间 OR；支持 "*" 通配）
# ---------------------------------------------------------------------------
def _scope_match(value: Any, allowed: set) -> bool:
    if "*" in allowed:
        return True
    return value in allowed


def filter_rows_by_scope(table_code: str, rows: list, user_id: int) -> list:
    """按项目/环境值域过滤行；非基础配置三表原样返回。"""
    if table_code not in SCOPE_TABLES:
        return rows
    if is_super_user(user_id):
        return rows
    roles = get_user_roles(user_id)
    if not roles:
        return []
    role_scopes = [get_role_scope_matrix(r["id"], table_code) for r in roles]
    out = []
    for row in rows:
        visible = False
        for sc in role_scopes:
            p = sc.get("project_name") or set()
            e = sc.get("env") or set()
            if _scope_match(row.get("project_name"), p) and _scope_match(row.get("env"), e):
                visible = True
                break
        if visible:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# 列级剥离（§2.5：后端序列化前剥离无权限字段）
# ---------------------------------------------------------------------------
def strip_fields_by_perm(table_code: str, records: list, user_id: int) -> list:
    if not records:
        return records
    visible = get_user_visible_fields(user_id, table_code)
    visible.discard("password")  # password 永不返回
    visible &= set(TABLE_FIELDS.get(table_code, []))
    return [{k: v for k, v in dict(rec).items() if k in visible} for rec in records]


# ---------------------------------------------------------------------------
# 环境二次校验（造单类接口）
# ---------------------------------------------------------------------------
def _parse_env_label(env_label: str) -> tuple:
    label = (env_label or "").strip()
    if ENV_KEY_SEP in label:
        project, env = label.split(ENV_KEY_SEP, 1)
        return project.strip(), env.strip()
    return None, label


def check_env_access(user_id: int, env_label: str) -> bool:
    """校验用户是否可访问某环境。

    组合 key（项目||环境）：按角色内 AND（project + env）判定，跨三张基础配置表 OR。
    纯环境名（如 TEST）：任一角色在任一基础配置表拥有该 env 值域即放行。
    """
    if is_super_user(user_id):
        return True
    project, env = _parse_env_label(env_label)
    if not env:
        return False
    roles = get_user_roles(user_id)
    if not roles:
        return False
    for table_code in SCOPE_TABLES:
        for role in roles:
            sc = get_role_scope_matrix(role["id"], table_code)
            p_set = sc.get("project_name") or set()
            e_set = sc.get("env") or set()
            if project:
                if _scope_match(project, p_set) and _scope_match(env, e_set):
                    return True
            elif _scope_match(env, e_set):
                return True
    return False


# ---------------------------------------------------------------------------
# 种子数据（幂等）
# ---------------------------------------------------------------------------
def _ensure_role(role_code: str, role_name: str, description: str, sort_no: int) -> int:
    row = query("SELECT id FROM sys_role WHERE role_code = %s", (role_code,), fetch="one")
    if row:
        return row["id"]
    execute(
        "INSERT INTO sys_role (role_code, role_name, status, is_builtin, sort_no, description) "
        "VALUES (%s, %s, 1, 1, %s, %s)",
        (role_code, role_name, sort_no, description),
    )
    return query("SELECT id FROM sys_role WHERE role_code = %s", (role_code,), fetch="one")["id"]


def _ensure_menu_perm(role_id: int, menu_code: str, view: int = 1, write: int = 0) -> None:
    row = query(
        "SELECT id FROM sys_menu_perm WHERE role_id = %s AND menu_code = %s",
        (role_id, menu_code), fetch="one",
    )
    if row:
        return
    execute(
        "INSERT INTO sys_menu_perm (role_id, menu_code, can_view, can_write) VALUES (%s, %s, %s, %s)",
        (role_id, menu_code, view, write),
    )


def _ensure_data_perm(role_id: int, table_code: str, field_code: str, view: int = 1, edit: int = 0) -> None:
    row = query(
        "SELECT id FROM sys_data_perm WHERE role_id = %s AND table_code = %s AND field_code = %s",
        (role_id, table_code, field_code), fetch="one",
    )
    if row:
        return
    execute(
        "INSERT INTO sys_data_perm (role_id, table_code, field_code, can_view, can_edit) "
        "VALUES (%s, %s, %s, %s, %s)",
        (role_id, table_code, field_code, view, edit),
    )


def _ensure_scope_value(role_id: int, table_code: str, scope_type: str, scope_value: str) -> None:
    row = query(
        "SELECT id FROM sys_scope_value WHERE role_id = %s AND table_code = %s "
        "AND scope_type = %s AND scope_value = %s",
        (role_id, table_code, scope_type, scope_value), fetch="one",
    )
    if row:
        return
    execute(
        "INSERT INTO sys_scope_value (role_id, table_code, scope_type, scope_value) "
        "VALUES (%s, %s, %s, %s)",
        (role_id, table_code, scope_type, scope_value),
    )


def _ensure_super_admin() -> None:
    builtin = [
        (ADMIN_USERNAME, ADMIN_PASSWORD, "系统管理员", "内置超级管理员，首次登录后请修改密码"),
        (ROOT_USERNAME, ROOT_PASSWORD, "root", "root 系统管理员，拥有全部最高权限"),
    ]
    for username, pwd, real_name, remark in builtin:
        if query("SELECT id FROM sys_user WHERE username = %s", (username,), fetch="one"):
            continue
        execute(
            "INSERT INTO sys_user (username, password_hash, real_name, email, phone, status, is_super, remark) "
            "VALUES (%s, %s, %s, %s, %s, 1, 1, %s)",
            (username, hash_password(pwd), real_name, "", "", remark),
        )


def _seed_full_read_perms() -> None:
    rid = _ensure_role("full_read", "全量只读", "内置角色：默认拥有全部菜单与全部字段可见性", 10)
    for m in MENU_OPTIONS:
        _ensure_menu_perm(rid, m["code"], view=1, write=0)
    for table, fields in TABLE_FIELDS.items():
        for f in fields:
            if f == "password":
                continue
            _ensure_data_perm(rid, table, f, view=1, edit=0)
    for table in SCOPE_TABLES:
        _ensure_scope_value(rid, table, "project_name", "*")
        _ensure_scope_value(rid, table, "env", "*")


def _seed_tester_perms() -> None:
    rid = _ensure_role("tester", "测试执行", "内置角色：造单四表 + 报文主数据", 20)
    for code in ("order_copy", "stock_adjust", "platform_order", "reverse_order", "message_data"):
        _ensure_menu_perm(rid, code, view=1, write=1)
    for f in TABLE_FIELDS.get("message_main_data", []):
        if f == "password":
            continue
        _ensure_data_perm(rid, "message_main_data", f, view=1, edit=1)
    # 值域不预置：项目/环境值运行时才可知，由管理员在数据权限页配置。


def init_seed_data() -> None:
    """首次启动幂等初始化：超管 + 内置角色 + 内置权限。"""
    db_manager.init_db()
    _ensure_super_admin()
    _seed_full_read_perms()
    _seed_tester_perms()
