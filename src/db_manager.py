# -*- coding: utf-8 -*-
"""MySQL 数据访问层：调用历史 / 报文主数据 / 各类配置的读写。

本模块把原 SQLite 访问全部迁移到 MySQL（o2o_api_db），对外保持原有函数签名
与返回结构不变，业务接口、前端页面均无需改动。

- 自动建库建表
- insert_call_record() 写调用记录
- 多线程兼容（适配 FastAPI --reload / 多 worker 场景）
- MySQL 连接地址运行时通过 WSL 获取 docker-desktop 的 eth0 IP
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

# 手动兜底：当 WSL 自动获取 IP 失败时，可在此写死 Docker 宿主机 IP（留空则友好退出）。
FALLBACK_MYSQL_HOST = ""

_port = 3306
_user = "root"
_password = os.environ.get("DB_PASSWORD", "123456")
_db_name = "o2o_api_db"

_lock = threading.Lock()
_initialized = False


def get_wsl_docker_ip() -> str:
    """运行时执行 WSL 命令获取 docker-desktop 的 eth0 inet IP。

    执行命令：wsl -d docker-desktop -- ip addr show eth0
    依赖仅使用内置 subprocess + re，不新增第三方包。
    """
    cmd = ["wsl", "-d", "docker-desktop", "--", "ip", "addr", "show", "eth0"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception as exc:  # WSL 未启动 / docker-desktop 未运行 / 超时等
        raise RuntimeError(
            f"无法执行 WSL 命令获取 Docker IP：{exc}\n"
            "请确认 WSL 与 Docker Desktop 已启动。"
        ) from exc

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise RuntimeError(
            f"WSL 命令执行失败（退出码 {proc.returncode}）：{err}"
        )

    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout or "")
    if not match:
        raise RuntimeError(
            f"未在 WSL 输出中匹配到 eth0 的 inet IP，原始输出：{proc.stdout}"
        )
    return match.group(1)


def _resolve_mysql_host() -> str:
    """解析 MySQL host。

    优先级：
    1. 环境变量 DB_HOST 有值 → 直接使用，不再执行任何探测逻辑；
    2. 非 Windows（Linux 服务器）→ 固定返回 "local-mysql"（容器内服务名），绝不调用 wsl；
    3. Windows 本地开发 → 保留原有 wsl 自动获取 Docker IP 的逻辑。
    """
    # 1. 优先读取环境变量 DB_HOST
    env_host = (os.environ.get("DB_HOST") or "").strip()
    if env_host:
        print(f"使用环境变量 DB_HOST：{env_host}")
        return env_host

    # 2. 非 Windows：直接使用容器内服务名，绝不调用 wsl
    if os.name != "nt":
        print("当前为非 Windows 环境，使用默认 MySQL host：local-mysql")
        return "local-mysql"

    # 3. Windows：保留原有 WSL 探测逻辑，失败时友好退出或使用手动 fallback
    try:
        host = get_wsl_docker_ip()
    except Exception as exc:
        print(f"\033[91m[错误] {exc}\033[0m")
        if FALLBACK_MYSQL_HOST:
            print(f"[提示] 使用手动 fallback IP：{FALLBACK_MYSQL_HOST}")
            return FALLBACK_MYSQL_HOST
        raise SystemExit(f"\033[91m无法获取 WSL Docker IP，程序退出。"
                         f"请在 FALLBACK_MYSQL_HOST 中写死 IP 后重试。\033[0m")
    print(f"当前WSL Docker IP：{host}")
    return host


_DB_CONFIG = {
    "host": _resolve_mysql_host(),
    "port": _port,
    "user": _user,
    "password": _password,
    "database": _db_name,
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
}


class _MySQLConnection:
    """pymysql 连接的轻量适配器，暴露与 sqlite3 类似的 execute() 接口。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: Any = None):
        """执行 SQL 并返回游标（DictCursor，结果行为 dict）。"""
        cur = self._conn.cursor()
        try:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur
        except Exception:
            cur.close()
            raise

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _connect() -> _MySQLConnection:
    conn = pymysql.connect(**_DB_CONFIG)
    return _MySQLConnection(conn)


def init_db() -> None:
    """建库建表（幂等，线程安全）。"""
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_call_history (
                    id INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    request_method TEXT COMMENT '请求方法',
                    request_url TEXT COMMENT '请求URL',
                    request_headers TEXT COMMENT '请求头',
                    request_body TEXT COMMENT '请求体',
                    response_code INT COMMENT '响应状态码',
                    response_body TEXT COMMENT '响应体',
                    cost_ms INT COMMENT '耗时(毫秒)',
                    error_msg TEXT COMMENT '错误信息',
                    create_time TEXT COMMENT '创建时间',
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调用历史记录表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_main_data (
                    id INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    platform TEXT NOT NULL COMMENT '平台',
                    message_name TEXT NOT NULL COMMENT '报文名称',
                    message_detail TEXT NOT NULL COMMENT '报文详情',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
                    type VARCHAR(255) NOT NULL DEFAULT '正向' COMMENT '类型(正向/逆向)',
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报文主数据表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS env_config (
                    id INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    project_name TEXT NOT NULL COMMENT '项目名称',
                    env TEXT NOT NULL COMMENT '环境',
                    page_url TEXT COMMENT '页面URL',
                    account TEXT COMMENT '账号',
                    password TEXT COMMENT '密码',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='环境配置表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS db_config (
                    id INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    project_name TEXT NOT NULL COMMENT '项目名称',
                    env TEXT NOT NULL COMMENT '环境',
                    db_address TEXT COMMENT '数据库地址',
                    port TEXT COMMENT '端口',
                    db_name TEXT COMMENT '数据库名',
                    username TEXT COMMENT '用户名',
                    password TEXT COMMENT '密码',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据库配置表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xxl_config (
                    id INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    project_name TEXT NOT NULL COMMENT '项目名称',
                    env TEXT NOT NULL COMMENT '环境',
                    address TEXT COMMENT 'XXL-Job调度中心地址',
                    account TEXT COMMENT '账号',
                    password TEXT COMMENT '密码',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='XXL-Job配置表'
                """
            )
            # ------------------------------------------------------------------
            # 权限体系 7 张表（RBAC）：用户 / 角色 / 关联 / 菜单权限 / 数据权限(字段) / 值域(行级) / 会话
            # 与 permission_schema.sql 保持一致，幂等可重复执行。
            # ------------------------------------------------------------------
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_user (
                    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    username        VARCHAR(64) NOT NULL COMMENT '登录账号',
                    password_hash   VARCHAR(255) NOT NULL COMMENT '密码哈希(pbkdf2_sha256$迭代$盐$哈希)',
                    real_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '姓名',
                    email           VARCHAR(128) NOT NULL DEFAULT '' COMMENT '邮箱',
                    phone           VARCHAR(32) NOT NULL DEFAULT '' COMMENT '手机号',
                    status          TINYINT NOT NULL DEFAULT 1 COMMENT '状态 1启用 0停用',
                    is_super        TINYINT NOT NULL DEFAULT 0 COMMENT '是否超级管理员 1是 0否(跳过菜单/数据权限校验)',
                    last_login_time DATETIME NULL COMMENT '最后登录时间',
                    last_login_ip   VARCHAR(64) NOT NULL DEFAULT '' COMMENT '最后登录IP',
                    remark          VARCHAR(255) NOT NULL DEFAULT '' COMMENT '备注',
                    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_user_username (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户档案表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_role (
                    id            INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    role_code     VARCHAR(64) NOT NULL COMMENT '角色编码(唯一,程序内标识)',
                    role_name     VARCHAR(64) NOT NULL COMMENT '角色名称(界面展示)',
                    status        TINYINT NOT NULL DEFAULT 1 COMMENT '状态 1启用 0停用',
                    is_builtin    TINYINT NOT NULL DEFAULT 0 COMMENT '是否内置角色 1是 0否(内置不可删除)',
                    sort_no       INT NOT NULL DEFAULT 0 COMMENT '排序号(越小越靠前)',
                    description   VARCHAR(255) NOT NULL DEFAULT '' COMMENT '角色描述',
                    create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_role_code (role_code),
                    KEY idx_sys_role_sort (sort_no)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色档案表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_user_role (
                    id          INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    user_id     INT NOT NULL COMMENT '用户ID',
                    role_id     INT NOT NULL COMMENT '角色ID',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_user_role (user_id, role_id),
                    KEY idx_sys_ur_role (role_id),
                    KEY idx_sys_ur_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户角色关联表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_menu_perm (
                    id          INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    role_id     INT NOT NULL COMMENT '角色ID',
                    menu_code   VARCHAR(64) NOT NULL COMMENT '菜单编码(对齐前端data-tab)',
                    can_view    TINYINT NOT NULL DEFAULT 1 COMMENT '是否可见 1是 0否',
                    can_write   TINYINT NOT NULL DEFAULT 0 COMMENT '是否可增删改 1是 0否',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_menu_role_code (role_id, menu_code),
                    KEY idx_sys_menu_role (role_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='菜单权限表'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_data_perm (
                    id          INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    role_id     INT NOT NULL COMMENT '角色ID',
                    table_code  VARCHAR(64) NOT NULL COMMENT '受控表编码',
                    field_code  VARCHAR(64) NOT NULL COMMENT '受控字段编码(列名)',
                    can_view    TINYINT NOT NULL DEFAULT 1 COMMENT '是否可见 1是 0否(不可见则后端剥离该列)',
                    can_edit    TINYINT NOT NULL DEFAULT 0 COMMENT '是否可编辑 1是 0否',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_data_role_t_f (role_id, table_code, field_code),
                    KEY idx_sys_data_role (role_id),
                    KEY idx_sys_data_table (table_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据权限表(字段级)'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_scope_value (
                    id          INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    role_id     INT NOT NULL COMMENT '角色ID',
                    table_code  VARCHAR(64) NOT NULL COMMENT '受控表编码(仅基础配置三表)',
                    scope_type  VARCHAR(32) NOT NULL COMMENT '值域类型 project_name(项目名称) / env(环境)',
                    scope_value VARCHAR(128) NOT NULL COMMENT '允许访问的值(与该表列值精确匹配)',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_scope_role_t_v (role_id, table_code, scope_type, scope_value),
                    KEY idx_sys_scope_role (role_id),
                    KEY idx_sys_scope_lookup (table_code, scope_type, scope_value)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据权限值域表(行级:项目名称/环境)'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sys_session (
                    id          INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    session_id  VARCHAR(128) NOT NULL COMMENT '会话随机串(存Cookie)',
                    user_id     INT NOT NULL COMMENT '用户ID',
                    login_ip    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '登录IP',
                    user_agent  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '浏览器UA',
                    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    expire_time DATETIME NOT NULL COMMENT '过期时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sys_session_sid (session_id),
                    KEY idx_sys_session_user (user_id),
                    KEY idx_sys_session_expire (expire_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='登录会话表'
                """
            )
            # ------------------------------------------------------------------
            # 日期 + 5 位自增流水号生成器底层表：一行代表一个"取号隔离维度"。
            #   隔离维度 = env_tag + 截取前缀 + yyyyMMdd
            # 主键 seq_key 即为该维度的复合键；current_no 记录该维度已分配到的当前流水号。
            # 取号时通过 INSERT ... ON DUPLICATE KEY UPDATE 原子推进，天生并发安全。
            # 表建在公共库 o2o_api_db，多环境 / 多实例共享，不依赖进程内内存缓存。
            # ------------------------------------------------------------------
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_seq_generator (
                    seq_key VARCHAR(128) NOT NULL COMMENT '隔离维度: env_tag + 前缀 + yyyyMMdd',
                    current_no BIGINT NOT NULL DEFAULT 0 COMMENT '当前已分配到的最大流水号',
                    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (seq_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日期流水号生成器表'
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def _json_dump(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def insert_call_record(
    request_method: str,
    request_url: str,
    request_headers: Any = None,
    request_body: Any = None,
    response_code: Optional[int] = None,
    response_body: Any = None,
    cost_ms: Optional[int] = None,
    error_msg: str = "",
) -> int:
    """写入一条调用记录，返回自增主键 id。"""
    init_db()
    create_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO api_call_history
                    (request_method, request_url, request_headers, request_body,
                     response_code, response_body, cost_ms, error_msg, create_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    request_method,
                    request_url,
                    _json_dump(request_headers),
                    _json_dump(request_body),
                    response_code,
                    _json_dump(response_body),
                    cost_ms,
                    error_msg,
                    create_time,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def query_record_by_id(record_id: int) -> Optional[dict]:
    """按 id 查询单条调用记录，不存在返回 None。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM api_call_history WHERE id = %s", (record_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def query_history_list(page: int = 1, page_size: int = 20) -> tuple[list, int]:
    """分页查询调用历史记录，返回 (记录列表, 总条数)。"""
    init_db()
    # 页码从 1 开始，并限制单页最大 100 条，避免极端请求拖垮查询
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    offset = (page - 1) * page_size

    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS cnt FROM api_call_history"
            ).fetchone()["cnt"]
            rows = conn.execute(
                "SELECT * FROM api_call_history ORDER BY id DESC LIMIT %s OFFSET %s",
                (page_size, offset),
            ).fetchall()
        finally:
            conn.close()

    return [dict(r) for r in rows], total


def delete_history_by_id(record_id: int) -> bool:
    """根据 id 删除单条调用记录，返回是否真的删除成功。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM api_call_history WHERE id = %s", (record_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 报文主数据（message_main_data）CRUD
# ---------------------------------------------------------------------------
# 已知平台清单：除这些平台外的其余平台，统一归类为「通用」展示与存储。
KNOWN_MESSAGE_PLATFORMS = ["淘宝", "京东", "抖音", "拼多多", "快手", "小红书", "视频号"]


def add_message_data(platform: str, message_name: str, message_detail: str, type: str = "正向") -> dict:
    """新增一条报文主数据，校验名称与详情、类型。返回 {success, error_msg, id}。"""
    init_db()
    if not message_name or not message_name.strip():
        return {"success": False, "error_msg": "报文名称不能为空", "id": None}
    if not message_detail or not message_detail.strip():
        return {"success": False, "error_msg": "报文详情不能为空", "id": None}
    if type not in ("正向", "逆向"):
        return {"success": False, "error_msg": "类型必须为正向或逆向", "id": None}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    platform = (platform or "").strip()
    # 落库归类：非已知平台（含空值）统一归入「通用」
    if platform not in KNOWN_MESSAGE_PLATFORMS:
        platform = "通用"
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO message_main_data"
                " (platform, message_name, message_detail, type, create_time, update_time)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (platform, message_name.strip(), message_detail, type, now, now),
            )
            conn.commit()
            return {"success": True, "error_msg": "", "id": int(cur.lastrowid)}
        finally:
            conn.close()


def get_message_list(platform: str) -> list:
    """根据平台查询报文列表（id、名称、平台、类型、创建时间）。
    - platform 为「通用」时，返回所有非已知平台的记录；
    - 结果按类型归类：正向在前、逆向在后，同类按创建时间倒序（最新在前）。
    """
    init_db()
    order_sql = (
        " ORDER BY CASE WHEN type = '正向' THEN 0 ELSE 1 END ASC,"
        " create_time DESC, id DESC"
    )
    with _lock:
        conn = _connect()
        try:
            if platform == "通用":
                placeholders = ",".join(["%s"] * len(KNOWN_MESSAGE_PLATFORMS))
                rows = conn.execute(
                    "SELECT id, message_name, platform, type, create_time"
                    " FROM message_main_data WHERE platform NOT IN"
                    f" ({placeholders}){order_sql}",
                    KNOWN_MESSAGE_PLATFORMS,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, message_name, platform, type, create_time"
                    f" FROM message_main_data WHERE platform = %s{order_sql}",
                    (platform,),
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_message_by_id(message_id: int) -> Optional[dict]:
    """根据 id 查询单条报文详情，不存在返回 None。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM message_main_data WHERE id = %s", (message_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def update_message_data(message_id: int, message_name: str, message_detail: str, type: str = "正向") -> dict:
    """修改报文信息，校验名称、详情、类型非空。返回 {success, error_msg}。"""
    init_db()
    if not message_name or not message_name.strip():
        return {"success": False, "error_msg": "报文名称不能为空"}
    if not message_detail or not message_detail.strip():
        return {"success": False, "error_msg": "报文详情不能为空"}
    if type not in ("正向", "逆向"):
        return {"success": False, "error_msg": "类型必须为正向或逆向"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE message_main_data"
                " SET message_name = %s, message_detail = %s, type = %s, update_time = %s"
                " WHERE id = %s",
                (message_name.strip(), message_detail, type, now, message_id),
            )
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


def batch_delete_message(ids: list) -> dict:
    """批量删除报文记录。返回 {success, error_msg, count}。"""
    init_db()
    if not ids:
        return {"success": False, "error_msg": "未选择要删除的记录", "count": 0}
    try:
        id_list = [int(i) for i in ids]
    except (TypeError, ValueError):
        return {"success": False, "error_msg": "ids 参数不合法", "count": 0}

    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join(["%s"] * len(id_list))
            cur = conn.execute(
                f"DELETE FROM message_main_data WHERE id IN ({placeholders})",
                id_list,
            )
            conn.commit()
            return {"success": True, "error_msg": "", "count": cur.rowcount}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 环境配置（env_config）CRUD
# 注意：list / get 均不返回 password 字段，页面永不暴露明文密码；
# 数据库明文存储，编辑时密码留空则保留原密码。
# ---------------------------------------------------------------------------
def add_env_config(
    project_name: str,
    env: str,
    page_url: str = "",
    account: str = "",
    password: str = "",
) -> dict:
    """新增一条环境配置，校验项目名称与环境非空。返回 {success, error_msg, id}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空", "id": None}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空", "id": None}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO env_config"
                " (project_name, env, page_url, account, password, create_time, update_time)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    project_name.strip(),
                    env.strip(),
                    (page_url or "").strip(),
                    (account or "").strip(),
                    password or "",
                    now,
                    now,
                ),
            )
            conn.commit()
            return {"success": True, "error_msg": "", "id": int(cur.lastrowid)}
        finally:
            conn.close()


def get_env_config_list() -> list:
    """查询环境配置列表，不包含 password 字段。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, project_name, env, page_url, account, create_time, update_time"
                " FROM env_config ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_env_config_by_id(config_id: int) -> Optional[dict]:
    """按 id 查询单条环境配置（不含 password），不存在返回 None。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, project_name, env, page_url, account, create_time, update_time"
                " FROM env_config WHERE id = %s",
                (config_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def update_env_config(
    config_id: int,
    project_name: str,
    env: str,
    page_url: str = "",
    account: str = "",
    password: str = "",
) -> dict:
    """修改环境配置，密码留空表示保持原密码不变。返回 {success, error_msg}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空"}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT password FROM env_config WHERE id = %s", (config_id,)
            ).fetchone()
            if existing is None:
                return {"success": False, "error_msg": "记录不存在"}
            # 页面永不解密，编辑提交的密码为空则保留数据库中的原密码
            final_password = password if password else existing["password"]

            cur = conn.execute(
                "UPDATE env_config"
                " SET project_name = %s, env = %s, page_url = %s, account = %s,"
                " password = %s, update_time = %s"
                " WHERE id = %s",
                (
                    project_name.strip(),
                    env.strip(),
                    (page_url or "").strip(),
                    (account or "").strip(),
                    final_password or "",
                    now,
                    config_id,
                ),
            )
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


def delete_env_config(config_id: int) -> dict:
    """删除单条环境配置。返回 {success, error_msg}。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM env_config WHERE id = %s", (config_id,))
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


def get_env_config_options() -> list:
    """返回去重后的 (project_name, env) 组合，供数据库配置的项目/环境联动下拉使用。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT project_name, env FROM env_config"
                " ORDER BY project_name, env"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 数据库配置（db_config）CRUD
# 注意：list / get 均不返回 password 字段，页面永不暴露明文密码。
# ---------------------------------------------------------------------------
def add_db_config(
    project_name: str,
    env: str,
    db_address: str = "",
    port: str = "",
    db_name: str = "",
    username: str = "",
    password: str = "",
) -> dict:
    """新增一条数据库配置，校验项目名称与环境非空。返回 {success, error_msg, id}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空", "id": None}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空", "id": None}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO db_config"
                " (project_name, env, db_address, port, db_name, username, password,"
                " create_time, update_time)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_name.strip(),
                    env.strip(),
                    (db_address or "").strip(),
                    (port or "").strip(),
                    (db_name or "").strip(),
                    (username or "").strip(),
                    password or "",
                    now,
                    now,
                ),
            )
            conn.commit()
            return {"success": True, "error_msg": "", "id": int(cur.lastrowid)}
        finally:
            conn.close()


def get_db_config_list() -> list:
    """查询数据库配置列表，不包含 password 字段。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, project_name, env, db_address, port, db_name, username,"
                " create_time, update_time"
                " FROM db_config ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_db_config_by_id(config_id: int) -> Optional[dict]:
    """按 id 查询单条数据库配置（不含 password），不存在返回 None。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, project_name, env, db_address, port, db_name, username,"
                " create_time, update_time"
                " FROM db_config WHERE id = %s",
                (config_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def update_db_config(
    config_id: int,
    project_name: str,
    env: str,
    db_address: str = "",
    port: str = "",
    db_name: str = "",
    username: str = "",
    password: str = "",
) -> dict:
    """修改数据库配置，密码留空表示保持原密码不变。返回 {success, error_msg}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空"}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT password FROM db_config WHERE id = %s", (config_id,)
            ).fetchone()
            if existing is None:
                return {"success": False, "error_msg": "记录不存在"}
            final_password = password if password else existing["password"]

            cur = conn.execute(
                "UPDATE db_config"
                " SET project_name = %s, env = %s, db_address = %s, port = %s,"
                " db_name = %s, username = %s, password = %s, update_time = %s"
                " WHERE id = %s",
                (
                    project_name.strip(),
                    env.strip(),
                    (db_address or "").strip(),
                    (port or "").strip(),
                    (db_name or "").strip(),
                    (username or "").strip(),
                    final_password or "",
                    now,
                    config_id,
                ),
            )
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


def delete_db_config(config_id: int) -> dict:
    """删除单条数据库配置。返回 {success, error_msg}。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM db_config WHERE id = %s", (config_id,))
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# XXL-Job 配置（xxl_config）CRUD
# 注意：list / get 均不返回 password 字段，页面永不暴露明文密码；
# 数据库明文存储，编辑时密码留空则保留原密码。
# ---------------------------------------------------------------------------
def add_xxl_config(
    project_name: str,
    env: str,
    address: str = "",
    account: str = "",
    password: str = "",
) -> dict:
    """新增一条 XXL-Job 配置，校验项目名称与环境非空。返回 {success, error_msg, id}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空", "id": None}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空", "id": None}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO xxl_config"
                " (project_name, env, address, account, password, create_time, update_time)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    project_name.strip(),
                    env.strip(),
                    (address or "").strip(),
                    (account or "").strip(),
                    password or "",
                    now,
                    now,
                ),
            )
            conn.commit()
            return {"success": True, "error_msg": "", "id": int(cur.lastrowid)}
        finally:
            conn.close()


def get_xxl_config_list() -> list:
    """查询 XXL-Job 配置列表，不包含 password 字段。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, project_name, env, address, account, create_time, update_time"
                " FROM xxl_config ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_xxl_config_by_id(config_id: int) -> Optional[dict]:
    """按 id 查询单条 XXL-Job 配置（不含 password），不存在返回 None。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, project_name, env, address, account, create_time, update_time"
                " FROM xxl_config WHERE id = %s",
                (config_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def update_xxl_config(
    config_id: int,
    project_name: str,
    env: str,
    address: str = "",
    account: str = "",
    password: str = "",
) -> dict:
    """修改 XXL-Job 配置，密码留空表示保持原密码不变。返回 {success, error_msg}。"""
    init_db()
    if not project_name or not project_name.strip():
        return {"success": False, "error_msg": "项目名称不能为空"}
    if not env or not env.strip():
        return {"success": False, "error_msg": "环境不能为空"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT password FROM xxl_config WHERE id = %s", (config_id,)
            ).fetchone()
            if existing is None:
                return {"success": False, "error_msg": "记录不存在"}
            final_password = password if password else existing["password"]

            cur = conn.execute(
                "UPDATE xxl_config"
                " SET project_name = %s, env = %s, address = %s, account = %s,"
                " password = %s, update_time = %s"
                " WHERE id = %s",
                (
                    project_name.strip(),
                    env.strip(),
                    (address or "").strip(),
                    (account or "").strip(),
                    final_password or "",
                    now,
                    config_id,
                ),
            )
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


def delete_xxl_config(config_id: int) -> dict:
    """删除单条 XXL-Job 配置。返回 {success, error_msg}。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM xxl_config WHERE id = %s", (config_id,))
            conn.commit()
            if cur.rowcount > 0:
                return {"success": True, "error_msg": ""}
            return {"success": False, "error_msg": "记录不存在"}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 内部读取函数：供业务逻辑取明文连接配置（含 password），仅服务端使用。
# ---------------------------------------------------------------------------
def get_env_configs_with_password() -> list:
    """返回全部环境配置（含 password），仅供服务端业务逻辑使用。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM env_config ORDER BY id DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_db_configs_with_password() -> list:
    """返回全部数据库配置（含 password），仅供服务端业务逻辑使用。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM db_config ORDER BY id DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_xxl_configs_with_password() -> list:
    """返回全部 XXL-Job 配置（含 password），仅供服务端业务逻辑使用。"""
    init_db()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM xxl_config ORDER BY id DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# auto_shard_key_tid 生成器：【固定前缀 + yyyyMMdd + 5 位补零自增流水号】
# ---------------------------------------------------------------------------
# 需求约束：
#   1. 禁止雪花算法、禁止 UUID、也废弃上一版「纯号段」预取方案。
#   2. 前缀 = 原始单号前 6 位（固定 6 位、最少 4 位，永远不变）。
#   3. 隔离维度 = env_tag + 前缀 + yyyyMMdd：同环境、同前缀、同一天内流水原子自增；
#      跨天（seq_key 中的日期变化）自动从 00001 重新开始。
#   4. 复用本项目已有 DB 会话（本模块 _connect()，连公共库 o2o_api_db），不新建连接。
#   5. 并发安全：INSERT ... ON DUPLICATE KEY UPDATE current_no = LAST_INSERT_ID(current_no+1)
#      由 InnoDB 行锁保证同一 seq_key 串行推进；不同 seq_key（环境/前缀/日期不同）互不阻塞。
#      因此 A 库内部唯一、B 库内部唯一，A/B 之间允许重复（各自 seq_key 不同）。
# ---------------------------------------------------------------------------

# 5 位流水号上限；达到该值即视为「当日该前缀序号耗尽」。
_SEQ_MAX = 99999


class IdGeneratorError(RuntimeError):
    """流水号生成器专属异常，便于上层对取号失败做清晰、可识别的区分。"""


def _build_seq_key(env_tag: str, prefix: str, ymd: str) -> str:
    """拼装隔离维度 seq_key（主键），各段用短横线分隔，保证跨维度不会互相覆盖。"""
    return f"{env_tag}-{prefix}-{ymd}"


def get_next_tid(origin_tid: str, env_tag: str) -> str:
    """生成下一个 auto_shard_key_tid，格式【固定前缀 + yyyyMMdd + 5 位补零流水号】。

    业务规则（与需求逐条对应）：
      - 入参 origin_tid：原始单号字符串，统一截取前 6 位作为固定前缀（永远不变）；
      - 入参 env_tag：环境标识，如 env_A / env_B，作为取号隔离维度的第一级；
      - 尾部 = 当天 yyyyMMdd + 5 位流水号（从 00001 开始，不足 5 位左侧补 0）；
      - 同 env_tag + 同前缀 + 同一天，流水号原子自增；跨天自动重置为 00001；
      - 当流水号达到 99999 时抛出 IdGeneratorError，提示当日该前缀序号耗尽。

    返回字符串：避免 19 位以上的长数字在 JSON / JavaScript 层丢失精度。

    底层表 id_seq_generator（公共库 o2o_api_db）：
        seq_key    主键 = env_tag + 前缀 + yyyyMMdd（唯一索引兜底）
        current_no 该维度当前已分配到的最大流水号

    原子取号实现（MySQL 经典技巧）：
        INSERT INTO id_seq_generator (seq_key, current_no)
        VALUES (%s, LAST_INSERT_ID(1))
        ON DUPLICATE KEY UPDATE current_no = LAST_INSERT_ID(current_no + 1);
        SELECT LAST_INSERT_ID();
      - 首次插入：current_no = LAST_INSERT_ID(1)，LAST_INSERT_ID() 返回 1；
      - 后续：ON DUPLICATE KEY UPDATE 把 current_no+1 喂给 LAST_INSERT_ID()，
        原子自增的同时把新值暂存到会话变量，随后 SELECT 取回。

    并发说明：LAST_INSERT_ID() 是连接级会话变量；每次调用独立 _connect() 拿到独立连接，
    天然隔离。同一 seq_key 的并发由 InnoDB 对主键行的排他锁串行化，不会重复取号。

    异常：
      - IdGeneratorError：入参非法 / 序号耗尽 / 数据库异常（含主键重复兜底）。

    调用示例：
        from src import db_manager

        # 同一原始单号、同一天内连续取号，流水号严格递增
        tid1 = db_manager.get_next_tid("888246666621927407", "env_A")
        # -> "8882462026102300001"
        tid2 = db_manager.get_next_tid("888246666621927407", "env_A")
        # -> "8882462026102300002"

        # 不同前缀起各自独立的 5 位流水
        tid3 = db_manager.get_next_tid("3379215005", "env_B")
        # -> "3379212026102300001"
    """
    # ---- 入参校验 ---------------------------------------------------------
    if origin_tid is None:
        raise IdGeneratorError("origin_tid 不能为 None。")
    if not isinstance(origin_tid, str):
        raise IdGeneratorError(
            f"origin_tid 必须为字符串，实际类型：{type(origin_tid).__name__}。"
        )
    origin_tid = origin_tid.strip()
    if not origin_tid:
        raise IdGeneratorError("origin_tid 不能为空字符串。")

    if env_tag is None:
        raise IdGeneratorError("env_tag 不能为 None。")
    if not isinstance(env_tag, str):
        raise IdGeneratorError(
            f"env_tag 必须为字符串，实际类型：{type(env_tag).__name__}。"
        )
    env_tag = env_tag.strip()
    if not env_tag:
        raise IdGeneratorError("env_tag 不能为空字符串。")

    # ---- 截取固定前缀（前 6 位；原始单号不足 6 位时按实际长度截取） -------
    # 业务上原始单号均为 6 位以上的纯数字；这里保留 len>=4 的最低约束。
    if len(origin_tid) < 4:
        raise IdGeneratorError(
            f"origin_tid 长度不足 4 位，无法截取有效前缀：{origin_tid!r}。"
        )
    prefix = origin_tid[:6]

    # ---- 当天日期 yyyyMMdd ------------------------------------------------
    ymd = datetime.now().strftime("%Y%m%d")

    # 确保公共库 id_seq_generator 表已就绪（幂等）。
    init_db()

    seq_key = _build_seq_key(env_tag, prefix, ymd)

    # ---- 原子取号 ---------------------------------------------------------
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO id_seq_generator (seq_key, current_no) "
            "VALUES (%s, LAST_INSERT_ID(1)) "
            "ON DUPLICATE KEY UPDATE current_no = LAST_INSERT_ID(current_no + 1)",
            (seq_key,),
        )
        cur = conn.execute("SELECT LAST_INSERT_ID() AS seq_no")
        row = cur.fetchone()
        if row is None or row.get("seq_no") is None:
            raise IdGeneratorError(f"取号失败：无法读取流水号，seq_key={seq_key!r}。")
        seq = int(row["seq_no"])

        # 限制判断：达到/超过 5 位流水上限即视为当日该前缀序号耗尽。
        if seq >= _SEQ_MAX:
            raise IdGeneratorError(
                f"当日该前缀序号已耗尽（seq_key={seq_key!r}，流水号已到 {seq}）。"
            )

        conn.commit()
    except IdGeneratorError:
        conn.rollback()
        raise
    except pymysql.err.IntegrityError as exc:
        # 数据库唯一索引（seq_key 主键）兜底：极端并发下若仍发生重复，转为清晰异常。
        conn.rollback()
        raise IdGeneratorError(
            f"流水号主键冲突（唯一索引兜底触发），seq_key={seq_key!r}：{exc}"
        ) from exc
    except Exception as exc:
        conn.rollback()
        raise IdGeneratorError(f"生成 auto_shard_key_tid 失败（seq_key={seq_key!r}）：{exc}") from exc
    finally:
        conn.close()

    # 拼接最终格式：前缀 + yyyyMMdd + 5 位补零流水号。
    # 例：前缀 888246 + 20261023 + 流水 1 -> "8882462026102300001"
    return f"{prefix}{ymd}{seq:05d}"