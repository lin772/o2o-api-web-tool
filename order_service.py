# -*- coding: utf-8 -*-
"""数据库查询、订单写入与 OMS 转单。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymysql
except ImportError as e:
    raise ImportError("请先安装 pymysql") from e

from message_service import BuildResult, extract_tid, regenerate_order_numbers
from oms_client import OmsClient
from settings import db_connect_options, derive_env_tag, get_env_config, load_config

_TRANSIENT_DB_ERR = {2006, 2013}


def db_connect(db_cfg: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None):
    """连接 OceanBase/MySQL，带超时与重试（应对 2013 连接中断）。"""
    cfg = cfg or load_config()
    opts = db_connect_options(cfg)
    kwargs = {
        "host": db_cfg["host"],
        "port": int(db_cfg.get("port", 3306)),
        "user": db_cfg["user"],
        "password": db_cfg["password"],
        "database": db_cfg["database"],
        "charset": db_cfg.get("charset", "utf8mb4"),
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
        "connect_timeout": opts["connect_timeout"],
        "read_timeout": opts["read_timeout"],
        "write_timeout": opts["write_timeout"],
    }
    last_err: Optional[Exception] = None
    for attempt in range(1, opts["retry_times"] + 1):
        try:
            return pymysql.connect(**kwargs)
        except pymysql.MySQLError as e:
            last_err = e
            if attempt < opts["retry_times"]:
                time.sleep(opts["retry_delay_seconds"])
    assert last_err is not None
    raise last_err


def _with_db_retry(env_key: str, db_key: str, runner):
    """执行数据库操作，连接/查询中断时自动重试。"""
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    db_cfg = env[db_key]
    opts = db_connect_options(cfg)
    last_err: Optional[Exception] = None
    for attempt in range(1, opts["retry_times"] + 1):
        conn = None
        try:
            conn = db_connect(db_cfg, cfg)
            return runner(conn)
        except pymysql.MySQLError as e:
            last_err = e
            code = e.args[0] if e.args else None
            if code in _TRANSIENT_DB_ERR and attempt < opts["retry_times"]:
                time.sleep(opts["retry_delay_seconds"])
                continue
            raise
        finally:
            if conn is not None:
                conn.close()
    assert last_err is not None
    raise last_err


def format_db_error(err: Exception) -> str:
    """将 pymysql 错误转为界面友好提示。"""
    if isinstance(err, pymysql.MySQLError) and err.args:
        code = err.args[0]
        if code in _TRANSIENT_DB_ERR:
            return (
                f"数据库连接中断（{code}），请检查网络/VPN 或切换 TEST 环境后重试。"
                f" 原始信息: {err.args[1] if len(err.args) > 1 else err}"
            )
    return str(err)


def resolve_source_defaults(shop: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并入库默认字段；优先使用店铺档案的 ad_client_id / ad_org_id。"""
    cfg = cfg or load_config()
    defaults = dict(cfg.get("source_defaults") or {})
    source_cfg = cfg.get("source_insert") or {}
    if source_cfg.get("derive_client_from_shop", True):
        if shop.get("ad_client_id") is not None:
            defaults["ad_client_id"] = shop["ad_client_id"]
        if shop.get("ad_org_id") is not None:
            defaults["ad_org_id"] = shop["ad_org_id"]
    return defaults


def query_shops(
    env_key: str,
    keyword: str = "",
    *,
    platform_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    def run(conn):
        with conn.cursor() as cur:
            sql = (
                "SELECT id, ecode, seller_nick, cp_c_shop_title, cp_c_platform_id, "
                "ad_client_id, ad_org_id "
                "FROM cp_c_shop WHERE isactive = 'Y'"
            )
            params: List[Any] = []
            if platform_id is not None:
                sql += " AND cp_c_platform_id = %s"
                params.append(int(platform_id))
            if keyword:
                sql += " AND (seller_nick LIKE %s OR cp_c_shop_title LIKE %s OR ecode LIKE %s)"
                kw = f"%{keyword}%"
                params.extend([kw, kw, kw])
            sql += " ORDER BY id LIMIT 500"
            cur.execute(sql, params)
            return cur.fetchall()

    return _with_db_retry(env_key, "base_db", run)


def query_shop_by_seller_nick(env_key: str, seller_nick: str) -> Optional[Dict[str, Any]]:
    """按卖家昵称精确匹配店铺档案，用于从报文反查店铺。"""

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ecode, seller_nick, cp_c_shop_title, cp_c_platform_id, "
                "ad_client_id, ad_org_id "
                "FROM cp_c_shop WHERE isactive = 'Y' AND seller_nick = %s LIMIT 1",
                (seller_nick,),
            )
            return cur.fetchone()

    return _with_db_retry(env_key, "base_db", run)


def query_skus(
    env_key: str,
    keyword: str = "",
    page: int = 1,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """分页查询 SKU 档案，采用 LIMIT+1 判断是否有下一页，避免全表 COUNT。"""
    from settings import sku_query_page_size

    cfg = load_config()
    page = max(1, int(page or 1))
    size = max(10, int(page_size or sku_query_page_size(cfg)))
    offset = (page - 1) * size
    fetch_limit = size + 1

    def run(conn):
        with conn.cursor() as cur:
            sql = "SELECT id, ecode, ename FROM ps_c_sku WHERE isactive = 'Y'"
            params: List[Any] = []
            if keyword:
                sql += " AND (ecode LIKE %s OR ename LIKE %s)"
                kw = f"%{keyword}%"
                params.extend([kw, kw])
            # LIMIT/OFFSET 使用字面量：部分 OceanBase/MySQL 驱动对占位符绑定会返回空结果
            sql += f" ORDER BY id LIMIT {int(fetch_limit)} OFFSET {int(offset)}"
            cur.execute(sql, params)
            rows = cur.fetchall()
            has_next = len(rows) > size
            items = rows[:size]
            return {
                "items": items,
                "page": page,
                "page_size": size,
                "count": len(items),
                "has_prev": page > 1,
                "has_next": has_next,
                "keyword": keyword,
            }

    return _with_db_retry(env_key, "base_db", run)


def allocate_and_regenerate(env_key: str, editor_mq: Any) -> Tuple[Any, str]:
    """分配【固定前缀 + yyyyMMdd + 5 位流水】新单号，并按平台替换原单号/子单号。

    流程：
      1. 从编辑区报文提取原单号 origin_tid（淘宝=trede tid、京东=orderId、
         standplat=data[].tid）；
      2. 由 env_key 派生取号隔离维度 env_tag（A 库/B 库隔离）；
      3. 复用公共库 session 的 get_next_tid 原子取号（返回字符串，避免精度丢失）；
      4. 按平台把「=原单号」的单号/子单号/赠品挂靠全部替换为新单号。

    返回 (替换后的报文, 新主单号 new_tid)。
    """
    from src import db_manager

    origin_tid = extract_tid(editor_mq)
    if not origin_tid:
        raise ValueError("无法从报文中识别 tid，无法分配新单号")
    env_tag = derive_env_tag(env_key)
    new_tid = db_manager.get_next_tid(origin_tid, env_tag)
    mq_final = regenerate_order_numbers(editor_mq, origin_tid, new_tid)
    return mq_final, new_tid


def alloc_source_id(order_conn) -> int:
    with order_conn.cursor() as cur:
        cur.execute("SELECT MAX(id) AS m FROM IP_B_PLATFORM_ORDER_SOURCE")
        return int((cur.fetchone() or {}).get("m") or 0) + 1


def insert_source(
    env_key: str,
    build: BuildResult,
    db_message: str,
    tid: str,
) -> int:
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    shop = build.shop
    defaults = resolve_source_defaults(shop, cfg)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    order_conn = db_connect(env["order_db"])
    try:
        source_id = alloc_source_id(order_conn)
        row = {
            "id": source_id,
            "tid": tid,
            "platform_id": build.platform_id,
            "seller_nick": build.seller_nick,
            "cp_c_shop_id": shop["id"],
            "cp_c_shop_ecode": shop["ecode"],
            "cp_c_shop_title": shop["cp_c_shop_title"],
            "message": db_message,
            "method": build.method,
            "trans_date": now,
            "creationdate": now,
            "modifieddate": now,
            **defaults,
        }
        with order_conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM IP_B_PLATFORM_ORDER_SOURCE")
            cols = {r["Field"] for r in cur.fetchall()}
            data = {k: v for k, v in row.items() if k in cols}
            cur.execute(
                f"INSERT INTO IP_B_PLATFORM_ORDER_SOURCE ({', '.join(data)}) "
                f"VALUES ({', '.join(['%s'] * len(data))})",
                tuple(data.values()),
            )
        order_conn.commit()
        return source_id
    finally:
        order_conn.close()


def run_transfer(env_key: str, source_id: int, tid: str) -> Dict[str, Any]:
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    transfer_cfg = cfg.get("transfer") or {}
    oms_cfg = env["oms"]
    login = oms_cfg.get("login", {})
    timeout = int(transfer_cfg.get("poll_timeout_seconds", 45))
    result: Dict[str, Any] = {"source_id": source_id, "tid": tid, "steps": []}

    if not transfer_cfg.get("auto_source_transfer", True):
        result["steps"].append("已跳过报文转平台订单（config.transfer.auto_source_transfer=false）")
        return result

    client = OmsClient(oms_cfg["base_url"], login.get("username", ""), login.get("password", ""))
    client.login()
    code, body = client.batch_transfer_source(source_id)
    result["steps"].append({"batchTransfer": {"http": code, "body": body}})

    order_conn = db_connect(env["order_db"])
    try:
        platform_id = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with order_conn.cursor() as cur:
                cur.execute("SELECT id, trans_status FROM IP_B_PLATFORM_ORDER WHERE tid = %s", (tid,))
                po = cur.fetchone()
                if po:
                    platform_id = po["id"]
                    result["platform_order"] = po
                    break
            time.sleep(2)

        if not platform_id:
            result["warning"] = "平台订单未在超时内生成"
            return result

        if transfer_cfg.get("auto_retail_transfer", True):
            code2, body2 = client.transfer_platform_order(platform_id)
            result["steps"].append({"orderTransfer": {"http": code2, "body": body2}})
            while time.time() < deadline:
                with order_conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, bill_no, order_status FROM oc_b_order WHERE tid = %s ORDER BY id DESC LIMIT 1",
                        (tid,),
                    )
                    retail = cur.fetchone()
                    if retail:
                        result["retail_order"] = retail
                        break
                time.sleep(3)
    finally:
        order_conn.close()

    return result


def finalize_build_for_insert(
    env_key: str,
    build: BuildResult,
    editor_mq: Any,
) -> Tuple[str, str]:
    """分配新单号 + 按平台替换子单号，返回 (db_message, 新主单号)。"""
    mq_final, new_tid = allocate_and_regenerate(env_key, editor_mq)
    family = "standplat" if isinstance(mq_final, dict) and "data" in mq_final else "other"
    if family == "standplat":
        from message_service import build_standplat_db_message

        db_msg = build_standplat_db_message(mq_final, build.seller_nick, build.platform_id)
    else:
        db_msg = json.dumps(mq_final, ensure_ascii=False)
    actual_tid = extract_tid(mq_final) or new_tid
    return db_msg, actual_tid
