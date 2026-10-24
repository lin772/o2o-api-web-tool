# -*- coding: utf-8 -*-
"""
O2O API 测试工具（集成版）

集成来源：
- VF_O2O_AoTo_Interface/O2O_API工具_分发包: 基础 API 测试功能、S4/预发环境
- VF_O2O_AoTo_UI/O2O_API工具_分发包: UI版本环境(18980)、数据库订单复制功能

功能特性：
1. 配置优先从 config/config.yaml 读取（S4 / TEST 环境）
2. 平台订单中间表复制
3. 逻辑仓 / 逻辑仓库存基础数据查询
4. 调整库存
5. 公域平台正向造单 / 逆向造单
"""
from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Callable

try:
    import requests
    import urllib3
except ImportError as e:
    print(f"错误: 缺少依赖: {e}")
    print("请执行: pip install requests urllib3")
    input("按 Enter 键退出...")
    raise SystemExit(1)

try:
    import pymysql
except ImportError:
    print("警告: 未安装 pymysql，数据库功能将不可用")
    pymysql = None


def _default_env_key() -> str:
    """默认环境 key：优先取配置表中第一个环境，回退 test。"""
    try:
        from settings import list_env_keys
        keys = list_env_keys()
        if keys:
            return keys[0]
    except Exception:
        pass
    return "test"


DEFAULT_ENV = _default_env_key()





# ==================== 订单复制相关 ====================

def get_db_config_by_env(env_key: str = None) -> Dict[str, Any]:
    """按环境获取订单库/基础库连接配置（从 db_config 配置表读取）。"""
    from settings import get_db_config_view

    view = get_db_config_view(env_key or DEFAULT_ENV)
    if "order_db" not in view or "base_db" not in view:
        raise RuntimeError("数据库配置缺失，请在「数据库配置」中补充 base/order 等库配置")
    return view


# ==================== 订单节点串联 ====================

def _load_tool_config() -> Dict[str, Any]:
    """读取 config/config.yaml，失败返回空 dict。"""
    try:
        from settings import load_config
        return load_config()
    except Exception as e:
        print(f"加载 config.yaml 失败: {e}")
        return {}


def get_oms_config(env_key: str = None) -> Dict[str, Any]:
    """从 env_config 配置表读取 OMS 配置，返回扁平 {base_url, username, password}。"""
    from settings import get_oms_env_view

    view = get_oms_env_view(env_key or DEFAULT_ENV)
    if not view or not view.get("base_url") or not (view.get("login") or {}).get("username"):
        raise RuntimeError("OMS 配置缺失，请在「环境配置」中补充地址与账号")
    login = view["login"]
    return {
        "base_url": view["base_url"],
        "username": login["username"],
        "password": login.get("password", ""),
    }


def get_xxl_job_configs() -> Dict[str, Any]:
    """按环境返回 XXL-Job 连接配置（从 xxl_config 配置表读取）。"""
    from settings import get_xxl_job_map

    result = get_xxl_job_map()
    if not result:
        raise RuntimeError("XXL-Job 配置缺失，请在「XXL-Job配置」中补充地址")
    return result


def get_xxl_status_tasks() -> List[Dict[str, Any]]:
    """按当前零售发货单状态返回动作定义（从 config.yaml 读取）。"""
    tasks = _load_tool_config().get("xxl_status_tasks")
    if isinstance(tasks, list) and tasks:
        return tasks
    raise RuntimeError("XXL 状态任务配置缺失，请检查 config/config.yaml 的 xxl_status_tasks")


def _order_db_connect(env_key: str = None):
    """连接当前环境订单库。"""
    cfg = get_db_config_by_env(env_key)["order_db"]
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def query_platform_order_by_tid(env_key: str, tid: str) -> Optional[Dict[str, Any]]:
    """按平台单号（tid=storeCode+docNum）查询平台订单。"""
    conn = _order_db_connect(env_key)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tid, trans_status, pt_status FROM IP_B_PLATFORM_ORDER WHERE tid = %s",
                (tid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def query_platform_order_trans_remark(env_key: str, platform_order_id: int) -> Optional[Dict[str, Any]]:
    """查询平台订单转单状态与失败备注（TRANS_REMARK 记录单据转换失败原因）。"""
    conn = _order_db_connect(env_key)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trans_status, trans_remark FROM IP_B_PLATFORM_ORDER WHERE id = %s",
                (platform_order_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _get_platform_address_config() -> Dict[str, Any]:
    """读取 config.yaml 中 platform_address 配置块。"""
    return _load_tool_config().get("platform_address") or {}


def _build_platform_address_payload(tid: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """组装 saveOrderAddress 请求体，对齐 H5 台湾页 RecipientFormTW 的字段语义。

    - receiver_address：仅详细地址（form.address），不放城市/区
    - receiver_delivery_address：城市名 + 区名 + 详细地址
    - receiver_json.city / district：使用城市/区编码（如 710100 / 710101），后端据此匹配省市区
    """
    city = defaults.get("receiver_city") or "臺北市"
    district = defaults.get("receiver_district") or "中正區"
    city_code = defaults.get("receiver_city_code") or city
    district_code = defaults.get("receiver_district_code") or district
    detail = defaults.get("receiver_address") or ""
    delivery_address = f"{city}{district}{detail}"
    other = {
        "country": "台灣",
        "city": city_code,
        "district": district_code,
        "zipcode": defaults.get("receiver_zip") or "",
        "address": detail,
        "countryCode": "+886",
    }
    payload: Dict[str, Any] = {
        "tid": tid,
        "receiver_name": defaults.get("receiver_name") or "張三",
        "receiver_mobile": defaults.get("receiver_mobile") or "0912345678",
        "receiver_zip": defaults.get("receiver_zip") or "100",
        "receiver_address": detail,
        "receiver_delivery_address": delivery_address,
        "receiver_json": json.dumps(other, ensure_ascii=False),
        "buyer_message": defaults.get("buyer_message") or "工具录入地址",
    }
    if defaults.get("receiver_phone"):
        payload["receiver_phone"] = defaults["receiver_phone"]
    return payload


def _oms_api_ok(http_code: int, body: Any) -> bool:
    """OMS 业务接口成功判断：HTTP 2xx 且业务 ok=true / code=0（ValueHolderV14/ValueHolder 框架）。"""
    if not (200 <= http_code < 300):
        return False
    if not isinstance(body, dict):
        return True
    if "ok" in body:
        return bool(body.get("ok"))
    code = body.get("code")
    if code is not None and str(code) not in ("0", "200", "success", "SUCCESS"):
        return False
    if body.get("success") is False:
        return False
    return True


def _oms_fail_message(body: Any) -> str:
    """从 OMS 业务响应中提取失败原因。"""
    if isinstance(body, dict):
        for key in ("message", "msg", "error", "errorMsg", "errMsg"):
            if body.get(key):
                return str(body[key])
        return json.dumps(body, ensure_ascii=False)[:300]
    return str(body)[:300]


def generate_and_submit_platform_address(env_key: str, tid: str) -> Dict[str, Any]:
    """平台订单地址链路：构造 H5 地址录入页 URL -> RSA 签名 -> 提交收货地址（替代 SQL 直更）。"""
    from oms_client import OmsClient, sign_address_payload

    cfg = _get_platform_address_config()
    address_base = (cfg.get("address_page_req_url") or "").rstrip("/")
    if not address_base:
        raise RuntimeError("config.yaml 缺少 platform_address.address_page_req_url")
    public_key = (cfg.get("rsa_public_key") or "").strip()
    if not public_key:
        raise RuntimeError("config.yaml 缺少 platform_address.rsa_public_key")

    oms_cfg = get_oms_config(env_key)
    client = OmsClient(oms_cfg["base_url"], oms_cfg["username"], oms_cfg["password"])
    login = client.login()

    # 不再调用 orderAddressQRGenerate，直接按 H5 链接模板拼出地址录入页 URL（tid + 当前时间戳）
    h5_url = f"{address_base}/?country=CN&locale=cht&tid={tid}&timestamp={int(time.time() * 1000)}"

    payload = _build_platform_address_payload(tid, cfg.get("default") or {})
    signed = sign_address_payload(payload, public_key)
    code_save, body_save = client.platform_order_save_address(signed)
    if not _oms_api_ok(code_save, body_save):
        raise RuntimeError(f"提交收货地址失败: {_oms_fail_message(body_save)}")

    return {
        "login_code": login.get("code") if isinstance(login, dict) else login,
        "address_h5_url": h5_url,
        "saveOrderAddress": {"http": code_save, "code": body_save.get("code") if isinstance(body_save, dict) else None, "body": body_save},
    }


def query_retail_order_by_tid(env_key: str, tid: str) -> Optional[Dict[str, Any]]:
    """按平台单号查询零售发货单。"""
    conn = _order_db_connect(env_key)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, order_status, cp_c_shop_id FROM oc_b_order WHERE tid = %s ORDER BY id DESC LIMIT 1",
                (tid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def query_retail_order_lost_items(env_key: str, retail_order_id: int) -> List[Dict[str, Any]]:
    """查询零售发货单明细中缺货数量(qty_lost)不等于 0 的明细行。"""
    conn = _order_db_connect(env_key)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ps_c_sku_ecode, qty_lost, qty FROM oc_b_order_item "
                "WHERE oc_b_order_id = %s AND qty_lost <> 0 ORDER BY id",
                (retail_order_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _query_retail_status_with_conn(conn, retail_order_id: int) -> Optional[int]:
    """复用连接查询零售发货单当前 order_status。"""
    with conn.cursor() as cur:
        cur.execute("SELECT order_status FROM oc_b_order WHERE id = %s", (retail_order_id,))
        row = cur.fetchone()
        return row["order_status"] if row else None


def query_retail_order_status(env_key: str, retail_order_id: int, conn=None) -> Optional[int]:
    """查询零售发货单当前 order_status（可传入已建连接复用，避免反复建拆）。"""
    if conn is not None:
        return _query_retail_status_with_conn(conn, retail_order_id)
    conn = _order_db_connect(env_key)
    try:
        return _query_retail_status_with_conn(conn, retail_order_id)
    finally:
        conn.close()


class OperationCancelled(Exception):
    """用户点击“终止”按钮后中断后台流程的专用异常。"""


def _poll_retail_status_change(
    conn,
    retail_order_id: int,
    prev_status: Optional[int],
    timeout: int = 20,
    interval: int = 3,
    cancel_event: Optional[Any] = None,
) -> Optional[int]:
    """触发定时任务后短轮询，状态变化即返回（避免固定长 sleep）。

    传入 threading.Event 作为 cancel_event 时，轮询期间被置位则抛出 OperationCancelled。
    """
    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cancelled():
            raise OperationCancelled("用户终止")
        cur = _query_retail_status_with_conn(conn, retail_order_id)
        if cur != prev_status:
            return cur
        time.sleep(interval)
    return _query_retail_status_with_conn(conn, retail_order_id)


def run_platform_order_convert(env_key: str, platform_order_id: int) -> Dict[str, Any]:
    """平台订单标记未转换，再执行单据转换。"""
    from oms_client import OmsClient

    cfg = get_oms_config(env_key)
    client = OmsClient(cfg["base_url"], cfg["username"], cfg["password"])
    login = client.login()
    code1, body1 = client.set_un_transfer(platform_order_id)
    if not _oms_api_ok(code1, body1):
        raise RuntimeError(f"标记未转换失败: {_oms_fail_message(body1)}")
    code2, body2 = client.transfer_platform_order(platform_order_id)
    if not _oms_api_ok(code2, body2):
        remark = query_platform_order_trans_remark(env_key, platform_order_id)
        extra = ""
        if remark:
            extra = f"；DB转单状态={remark.get('trans_status')}，失败原因={remark.get('trans_remark')}"
        raise RuntimeError(f"单据转换失败: {_oms_fail_message(body2)}{extra}")
    return {
        "login_code": login.get("code") if isinstance(login, dict) else login,
        "setUnTransfer": {"http": code1, "code": body1.get("code") if isinstance(body1, dict) else None, "body": body1},
        "orderTransfer": {"http": code2, "code": body2.get("code") if isinstance(body2, dict) else None, "body": body2},
    }


def run_ip_common_transfer(env_key: str, table: str, tableid: int, ids, menu: str) -> Dict[str, Any]:
    """淘宝/京东中间表通用转单 -> 零售发货单 oc_b_order。"""
    from oms_client import OmsClient

    cfg = get_oms_config(env_key)
    client = OmsClient(cfg["base_url"], cfg["username"], cfg["password"])
    login = client.login()
    code, body = client.ip_common_transfer(table, tableid, ids, menu)
    if not _oms_api_ok(code, body):
        raise RuntimeError(f"单据转单失败: {_oms_fail_message(body)}")
    return {
        "login_code": login.get("code") if isinstance(login, dict) else login,
        "ipCommonTransfer": {"http": code, "code": body.get("code") if isinstance(body, dict) else None, "body": body},
    }


def _oms_session_cookie(env_key: str) -> str:
    """登录对应环境 OMS，返回 sessionCookie 值（跨域携带到 XXL-Job 请求头）。"""
    client = _oms_login(env_key)
    return client.session.cookies.get("sessionCookie") or ""


def _xxl_login(env_key: str, oms_cookie: Optional[str] = None) -> requests.Session:
    """登录 XXL-Job，返回已登录的 session。

    不同环境的 XXL-Job 版本不一：
    - 标准版(官方 2.x)：登录接口为 POST /login，返回 {"code":200,"content":"<token>"}，
      需把 token 写入 cookie XXL_JOB_LOGIN_IDENTITY 供 /jobinfo/trigger 鉴权。
    - 定制版：登录接口可能为 POST /auth/doLogin。
    此外 /xxl-job-admin 挂在 OMS 同域名下时，由 SSO 网关按 OMS 的 sessionCookie 鉴权，
    缺少/无效 cookie 会被 tengine 直接 403，因此必须先注入 OMS 的 sessionCookie。
    """
    configs = get_xxl_job_configs()
    cfg = configs.get(env_key) or configs.get(DEFAULT_ENV) or next(iter(configs.values()), {})
    if oms_cookie is None:
        try:
            oms_cookie = _oms_session_cookie(env_key)
        except Exception:
            oms_cookie = ""
    session = requests.Session()
    session.trust_env = False
    from urllib.parse import urlparse

    host = urlparse(cfg["url"]).hostname or ""
    # 先注入 OMS sessionCookie，保证后续所有请求(含登录)都能通过 SSO 网关鉴权
    if oms_cookie and host:
        session.cookies.set("sessionCookie", oms_cookie, domain=host, path="/")

    base = cfg["url"].rstrip("/")
    username = cfg["username"]
    password = cfg["password"]

    # 登录接口候选：优先标准版 /login，其次定制版 /auth/doLogin
    last_error: Any = None
    for path in ("/login", "/auth/doLogin"):
        try:
            resp = session.post(
                base + path,
                data={"userName": username, "password": password, "ifRemember": "on"},
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        try:
            login_data = resp.json()
        except Exception:
            login_data = {"text": resp.text[:200]}
        if isinstance(login_data, dict) and login_data.get("code") == 200:
            # 标准版 SSO 模式：把返回的 token 写入登录态 cookie
            token = login_data.get("content") or login_data.get("data") or ""
            if token and host:
                session.cookies.set("XXL_JOB_LOGIN_IDENTITY", token, domain=host, path="/")
            session._xxl_url = cfg["url"]
            session._oms_cookie = oms_cookie
            return session
        last_error = login_data

    raise RuntimeError(f"XXL-Job 登录失败: {last_error}")


def _xxl_do_login_and_trigger(env_key: str, jobs: List[Dict[str, Any]], session=None) -> List[Dict[str, Any]]:
    """触发指定定时任务（复用已登录 session，避免每轮重复登录）。"""
    if session is None:
        session = _xxl_login(env_key)
    base = getattr(session, "_xxl_url", None)
    if not base:
        configs = get_xxl_job_configs()
        cfg = configs.get(env_key) or configs.get(DEFAULT_ENV) or next(iter(configs.values()), {})
        base = cfg["url"]

    trigger_url = base.rstrip("/") + "/jobinfo/trigger"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    results: List[Dict[str, Any]] = []
    for job in jobs:
        param = '{"className":"' + job["class_name"] + '","type":"localJob"}'
        r = session.post(
            trigger_url,
            data={"id": job["id"], "executorParam": param, "addressList": ""},
            headers=headers,
            timeout=20,
        )
        try:
            j = r.json()
            ok = j.get("code") == 200
            msg = j.get("msg", "")
        except Exception:
            ok = True
            msg = r.text[:200]
        results.append({"name": job["name"], "id": job["id"], "ok": ok, "msg": msg})
    return results


def _oms_login(env_key: str):
    """登录 OMS，返回已登录的 OmsClient。"""
    from oms_client import OmsClient

    cfg = get_oms_config(env_key)
    client = OmsClient(cfg["base_url"], cfg["username"], cfg["password"])
    client.login()
    return client


def xxl_fetch_page(env_key: str, path: str = "", session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """带 OMS sessionCookie 访问 XXL-Job 网页，返回 {http, content_type, text}。"""
    if session is None:
        session = _xxl_login(env_key)
    base = getattr(session, "_xxl_url", None)
    if not base:
        configs = get_xxl_job_configs()
        cfg = configs.get(env_key) or configs.get(DEFAULT_ENV) or next(iter(configs.values()), {})
        base = cfg["url"]
    url = base.rstrip("/") + ("/" + path.lstrip("/") if path else "/")
    resp = session.get(url, timeout=20)
    return {
        "http": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "text": resp.text,
    }


def run_retail_audit(env_key: str, retail_order_id: int, client=None) -> Dict[str, Any]:
    """零售发货单手工审核（审核按钮逻辑）。"""
    if client is None:
        client = _oms_login(env_key)
    code, body = client.audit_order(retail_order_id)
    # OMS 业务接口成功判断：HTTP 2xx 且业务 code=0
    business_ok = isinstance(body, dict) and body.get("code") == 0
    http_ok = 200 <= code < 300
    return {
        "ok": http_ok and business_ok,
        "http": code,
        "body": body,
    }


# 零售发货单 order_status -> 推进动作的业务文案（用于逐步日志）
_STATUS_TASK_LABEL = {
    50: "待分配任务",
    2: "寻源任务",
    1: "审核",
    3: "配货任务",
}


def advance_retail_order_to_target(
    env_key: str,
    retail_order_id: int,
    target_status: int = 4,
    max_wait_rounds: int = 50,
    progress: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """按当前 order_status 选择对应动作推进，直到零售发货单进入目标状态(target_status)。

    - status=50 → 触发 TobeConfirmedSendMqTask
    - status=2  → 触发 OmsOccupyServiceTask
    - status=1  → 执行零售发货单手工审核（OMS 接口）
    - status=3  → 触发 AutoOrderWmsTask
    - status=22 → 处理中，等待异步任务完成后重查
    - status=21 → 缺货等异步场景，短等待后重查
    每个任务最多触发 5 次，超限即终止并返回报错。
    复用 DB 连接与 XXL/OMS 会话；`progress` 用于回传进度文案（可选）。
    传入 threading.Event 作为 cancel_event 时，等待/轮询期间被置位则抛出 OperationCancelled。
    """
    def _log(msg: str) -> None:
        if progress is not None:
            progress(msg)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _sleep(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if _cancelled():
                raise OperationCancelled("用户终止")
            time.sleep(min(0.5, deadline - time.time()))

    status_tasks = get_xxl_status_tasks()
    task_by_status: Dict[int, Dict[str, Any]] = {t.get("status"): t for t in status_tasks}

    executed: List[Dict[str, Any]] = []
    run_counts: Dict[str, int] = {}
    triggered_names: set = set()
    xxl_session = None
    oms_client = None
    status22_rounds = 0
    status21_rounds = 0
    last_status: Optional[int] = None
    stuck_count = 0
    cancelled_flag = False

    conn = _order_db_connect(env_key)
    try:
        for _ in range(max_wait_rounds):
            if _cancelled():
                raise OperationCancelled("用户终止")
            status = _query_retail_status_with_conn(conn, retail_order_id)
            if status == target_status:
                _log(f"零售发货单已进入目标状态 {target_status}")
                break
            if status is None:
                raise RuntimeError(f"零售发货单不存在: id={retail_order_id}")

            if status == 7:
                _log("零售发货单状态=已取消，结束制单流程")
                cancelled_flag = True
                break

            # 卡住检测：连续同状态记录轮数，用于决定是否重复触发同一任务
            if status == last_status:
                stuck_count += 1
            else:
                stuck_count = 0
                last_status = status

            # 处理中：异步任务尚未完成，延长等待
            if status == 22:
                status22_rounds += 1
                if status22_rounds > 8:
                    raise RuntimeError(f"零售发货单持续 status=22 处理中，请人工检查: id={retail_order_id}")
                _log(f"status=22 处理中，等待 20s 后重查（第 {status22_rounds} 轮）")
                _sleep(20)
                continue

            # 缺货等异步场景：短等待后重查
            if status == 21:
                status21_rounds += 1
                if status21_rounds > 8:
                    raise RuntimeError(f"零售发货单持续 status=21 缺货，请人工检查: id={retail_order_id}")
                _log(f"status=21 缺货/异步处理中，等待 10s 后重查（第 {status21_rounds} 轮）")
                _sleep(10)
                continue

            task = task_by_status.get(status)
            if task is None:
                _log(f"status={status} 未匹配到定时任务，等待 5s 后重查")
                _sleep(5)
                continue

            name = task.get("name", f"status_{status}")
            label = _STATUS_TASK_LABEL.get(status, name)

            # 该状态对应任务已触发过，且状态未持续卡住：仅等待状态变化，不重复触发
            if name in triggered_names and stuck_count < 3:
                _log(f"{label}已触发，等待状态变化（第 {stuck_count} 轮无变化）")
                _sleep(8)
                continue

            count = run_counts.get(name, 0)
            # 单个任务执行次数超限（达到 5 次）即终止，避免无限重试
            if count >= 5:
                raise RuntimeError(
                    f"[{label}] 已执行 {count} 次仍无进展，"
                    f"当前 order_status={status}，终止流程"
                )
            run_counts[name] = count + 1
            triggered_names.add(name)

            if task.get("action") == "audit":
                _log("正在执行审核")
                if oms_client is None:
                    oms_client = _oms_login(env_key)
                audit = run_retail_audit(env_key, retail_order_id, client=oms_client)
                body = audit.get("body")
                msg = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)[:300]
                executed.append({"name": name, "id": retail_order_id, "ok": audit.get("ok", False), "msg": msg})
                _log("审核成功" if audit.get("ok") else "审核失败")
            else:
                _log(f"正在执行{label}")
                if xxl_session is None:
                    xxl_session = _xxl_login(env_key)
                results = _xxl_do_login_and_trigger(env_key, [task], session=xxl_session)
                for r in results:
                    executed.append(r)
                    _log(f"{label}{'成功' if r.get('ok') else '失败'} {r.get('msg', '')}")

            # 触发后短轮询等待状态变化（最长 20s），发生变化即进入下一轮
            _poll_retail_status_change(conn, retail_order_id, status, timeout=20, interval=3, cancel_event=cancel_event)

        final_status = _query_retail_status_with_conn(conn, retail_order_id)
    finally:
        conn.close()

    if cancelled_flag:
        return {
            "status": 7,
            "executed": executed,
            "message": "零售发货单状态=已取消，结束制单流程",
        }

    status_text = "配货中" if target_status == 4 else f"状态{target_status}"
    return {
        "status": final_status,
        "executed": executed,
        "message": f"零售发货单已推进至{status_text}" if final_status == target_status else f"未推进至{status_text}，当前order_status={final_status}",
    }


# 保留旧名称，兼容现有调用（O2O 门店下单推进到配货中）
def advance_retail_order_to_allocating(
    env_key: str,
    retail_order_id: int,
    max_wait_rounds: int = 30,
    progress: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """推进零售发货单到配货中(status=4)。"""
    return advance_retail_order_to_target(
        env_key, retail_order_id, target_status=4, max_wait_rounds=max_wait_rounds,
        progress=progress, cancel_event=cancel_event,
    )


def query_platform_order_source_by_tid(env_key: str, tid: str) -> Optional[Dict[str, Any]]:
    """按单号查询平台订单报文（IP_B_PLATFORM_ORDER_SOURCE）。"""
    conn = _order_db_connect(env_key)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tid, trans_status FROM IP_B_PLATFORM_ORDER_SOURCE WHERE tid = %s",
                (tid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def run_source_batch_transfer(env_key: str, source_id: int) -> Dict[str, Any]:
    """平台订单报文 -> 平台订单（单据转换按钮逻辑）。"""
    from oms_client import OmsClient

    cfg = get_oms_config(env_key)
    client = OmsClient(cfg["base_url"], cfg["username"], cfg["password"])
    login = client.login()
    code, body = client.batch_transfer_source(source_id)
    return {"login": login, "batchTransfer": {"http": code, "body": body}}


class OrderCopyManager:
    """订单复制管理器"""

    def __init__(self, order_config: Dict[str, Any] = None, base_config: Dict[str, Any] = None, env_key: str = None):
        """
        初始化数据库连接
        
        Args:
            order_config: 订单库配置
            base_config: 基础库配置（包含CP_C_SHOP和PS_C_SKU表）
            env_key: 当前环境标识（s4/test）
        """
        if not pymysql:
            raise ImportError("请先安装 pymysql: pip install pymysql")

        env_config = get_db_config_by_env(env_key)
        self.env_key = (env_key or DEFAULT_ENV).strip().lower()
        self.order_config = order_config or env_config["order_db"]
        self.base_config = base_config or env_config["base_db"]
        self.order_conn = None
        self.base_conn = None
        self.last_error = None

    def connect_order_db(self) -> bool:
        """连接订单库"""
        try:
            self.order_conn = pymysql.connect(
                host=self.order_config['host'],
                user=self.order_config['user'],
                password=self.order_config['password'],
                database=self.order_config['database'],
                port=self.order_config['port'],
                charset=self.order_config.get('charset', 'utf8mb4'),
                cursorclass=pymysql.cursors.DictCursor
            )
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"订单库连接失败: {e}")
            return False

    def connect_base_db(self) -> bool:
        """连接基础库"""
        try:
            self.base_conn = pymysql.connect(
                host=self.base_config['host'],
                user=self.base_config['user'],
                password=self.base_config['password'],
                database=self.base_config['database'],
                port=self.base_config['port'],
                charset=self.base_config.get('charset', 'utf8mb4'),
                cursorclass=pymysql.cursors.DictCursor
            )
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"基础库连接失败: {e}")
            return False

    def close(self):
        """关闭数据库连接"""
        if self.order_conn:
            self.order_conn.close()
            self.order_conn = None
        if self.base_conn:
            self.base_conn.close()
            self.base_conn = None

    def get_all_shops(self) -> List[Dict[str, Any]]:
        """获取所有店铺列表（从CP_C_SHOP）"""
        try:
            if not self.base_conn:
                if not self.connect_base_db():
                    raise RuntimeError(f"基础库连接失败: {self.last_error or '请检查数据库地址/VPN/网络'}")
            with self.base_conn.cursor() as cursor:
                sql = """
                    SELECT id, ecode, seller_nick, cp_c_platform_id, cp_c_shop_title 
                    FROM cp_c_shop 
                    ORDER BY id
                """
                cursor.execute(sql)
                results = cursor.fetchall()
                return results
        except Exception as e:
            print(f"获取店铺列表失败: {e}")
            return []

    def get_sku_by_ecode(self, ecode: str) -> Optional[Dict[str, Any]]:
        """根据SKU ecode查询商品信息（从PS_C_SKU，在base库）"""
        try:
            if not self.base_conn:
                if not self.connect_base_db():
                    return None
            with self.base_conn.cursor() as cursor:
                sql = "SELECT * FROM ps_c_sku WHERE ecode = %s"
                cursor.execute(sql, (ecode,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询SKU信息失败: {e}")
            return None

    def get_max_id(self) -> Optional[int]:
        """获取当前表中最大的 id 值"""
        try:
            if not self.order_conn:
                if not self.connect_order_db():
                    return None
            with self.order_conn.cursor() as cursor:
                sql = "SELECT MAX(id) as max_id FROM IP_B_PLATFORM_ORDER_SOURCE"
                cursor.execute(sql)
                result = cursor.fetchone()
                max_id = result.get('max_id') if result else None
                return max_id if max_id is not None else 0
        except Exception as e:
            print(f"获取最大ID失败: {e}")
            return None

    def get_max_tid(self) -> Optional[int]:
        """获取当前表中最大的数字 tid 值。"""
        try:
            if not self.order_conn:
                if not self.connect_order_db():
                    return None
            with self.order_conn.cursor() as cursor:
                sql = """
                    SELECT MAX(CAST(tid AS UNSIGNED)) AS max_tid
                    FROM IP_B_PLATFORM_ORDER_SOURCE
                    WHERE tid REGEXP '^[0-9]+$'
                """
                cursor.execute(sql)
                result = cursor.fetchone()
                max_tid = result.get('max_tid') if result else None
                return int(max_tid) if max_tid is not None else 0
        except Exception as e:
            print(f"获取最大TID失败: {e}")
            return None

    def get_order_by_tid(self, tid: str) -> Optional[Dict[str, Any]]:
        """根据 tid 查询订单数据"""
        try:
            if not self.order_conn:
                if not self.connect_order_db():
                    return None
            with self.order_conn.cursor() as cursor:
                sql = "SELECT * FROM IP_B_PLATFORM_ORDER_SOURCE WHERE tid = %s"
                cursor.execute(sql, (tid,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询订单数据失败: {e}")
            return None

    def copy_order(self, source_tid: str, shop_id: int = None, sku_ecode: str = None) -> Dict[str, Any]:
        """
        复制订单数据
        
        Args:
            source_tid: 源订单tid
            shop_id: 目标店铺ID（可选）
            sku_ecode: 商品SKU编码（可选）
            
        Returns:
            操作结果字典
        """
        result = {
            'success': False,
            'message': '',
            'data': None
        }

        # 连接数据库
        if not self.connect_order_db():
            result['message'] = '订单库连接失败'
            return result

        if shop_id and not self.connect_base_db():
            result['message'] = '基础库连接失败'
            return result

        try:
            # 查询源订单
            source_order = self.get_order_by_tid(source_tid)
            if not source_order:
                result['message'] = f'未找到源订单: {source_tid}'
                return result

            # 获取当前最大id并+1
            max_id = self.get_max_id()
            if max_id is None:
                result['message'] = '获取最大ID失败'
                return result
            
            new_id = max_id + 1
            
            # 获取当前最大tid并+1（避免与已有数据冲突）
            max_tid = self.get_max_tid()
            if max_tid is None:
                result['message'] = '获取最大TID失败'
                return result
            
            new_tid = str(max_tid + 1)

            # 复制订单数据
            new_order = source_order.copy()
            new_order['id'] = new_id
            new_order['tid'] = new_tid
            # 移除不存在的字段
            if 'create_time' in new_order:
                del new_order['create_time']
            if 'update_time' in new_order:
                del new_order['update_time']

            # 如果指定了店铺，更新店铺相关字段
            if shop_id:
                shop_info = self.get_shop_by_id(shop_id)
                if shop_info:
                    new_order['platform_id'] = shop_info.get('cp_c_platform_id', '')
                    new_order['cp_c_shop_id'] = shop_id
                    new_order['cp_c_shop_ecode'] = shop_info.get('ecode', '')
                    new_order['cp_c_shop_title'] = shop_info.get('cp_c_shop_title', '')
                    new_order['seller_nick'] = shop_info.get('seller_nick', '')

                    # 更新message字段中的platform和sellernick
                    message_str = new_order.get('message', '')
                    if message_str:
                        try:
                            message_json = json.loads(message_str)
                            if 'platform' in message_json:
                                message_json['platform'] = new_order['platform_id']
                            if 'sellernick' in message_json:
                                message_json['sellernick'] = new_order['seller_nick']
                            new_order['message'] = json.dumps(message_json, ensure_ascii=False)
                        except:
                            pass

            # 如果指定了SKU，更新message中的outer_sku_id（先查询PS_C_SKU获取ecode）
            if sku_ecode:
                # 查询PS_C_SKU表获取ecode
                sku_info = self.get_sku_by_ecode(sku_ecode)
                if sku_info:
                    target_ecode = sku_info.get('ecode', sku_ecode)  # 使用查询到的ecode
                else:
                    target_ecode = sku_ecode  # 如果查询不到，使用用户输入的值
                
                message_str = new_order.get('message', '')
                if message_str:
                    try:
                        message_json = json.loads(message_str)
                        if isinstance(message_json, dict):
                            items = message_json.get('items', [])
                            if items:
                                for item in items:
                                    if 'outer_sku_id' in item:
                                        item['outer_sku_id'] = target_ecode
                            new_order['message'] = json.dumps(message_json, ensure_ascii=False)
                    except:
                        pass

            # 更新message字段中与旧tid一致的值为新tid
            message_str = new_order.get('message', '')
            if message_str and source_tid in message_str:
                try:
                    message_json = json.loads(message_str)
                    message_str_new = json.dumps(message_json, ensure_ascii=False).replace(source_tid, new_tid)
                    new_order['message'] = message_str_new
                except:
                    # 如果不是JSON，直接替换字符串
                    new_order['message'] = message_str.replace(source_tid, new_tid)

            # 插入新订单（包含id字段）
            columns = list(new_order.keys())
            placeholders = ', '.join(['%s'] * len(columns))
            sql = f"INSERT INTO IP_B_PLATFORM_ORDER_SOURCE ({', '.join(columns)}) VALUES ({placeholders})"

            with self.order_conn.cursor() as cursor:
                cursor.execute(sql, tuple(new_order[k] for k in columns))
                self.order_conn.commit()

            result['success'] = True
            result['message'] = f'订单复制成功！新订单tid: {new_tid}'
            result['data'] = {
                'new_tid': new_tid,
                'new_id': new_id,
                'source_tid': source_tid
            }

        except Exception as e:
            result['message'] = f'复制订单失败: {str(e)}'
            if self.order_conn:
                self.order_conn.rollback()
        finally:
            self.close()

        return result

    def get_shop_by_id(self, shop_id: int) -> Optional[Dict[str, Any]]:
        """根据店铺ID查询店铺信息（从CP_C_SHOP）"""
        try:
            if not self.base_conn:
                if not self.connect_base_db():
                    return None
            with self.base_conn.cursor() as cursor:
                sql = """
                    SELECT id, ecode, seller_nick, cp_c_platform_id, cp_c_shop_title 
                    FROM cp_c_shop 
                    WHERE id = %s
                """
                cursor.execute(sql, (shop_id,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询店铺信息失败: {e}")
            return None


def test_db_connection(order_config: Dict[str, Any] = None, base_config: Dict[str, Any] = None, env_key: str = None) -> Tuple[bool, str]:
    """测试数据库连接"""
    if not pymysql:
        return False, "未安装 pymysql"
    
    env_config = get_db_config_by_env(env_key)
    order_config = order_config or env_config["order_db"]
    base_config = base_config or env_config["base_db"]
    
    try:
        # 测试订单库连接
        conn = pymysql.connect(
            host=order_config['host'],
            user=order_config['user'],
            password=order_config['password'],
            database=order_config['database'],
            port=order_config['port'],
            charset=order_config.get('charset', 'utf8mb4'),
            connect_timeout=5
        )
        conn.close()
        
        # 测试基础库连接
        conn = pymysql.connect(
            host=base_config['host'],
            user=base_config['user'],
            password=base_config['password'],
            database=base_config['database'],
            port=base_config['port'],
            charset=base_config.get('charset', 'utf8mb4'),
            connect_timeout=5
        )
        conn.close()
        
        return True, "连接成功"
    except Exception as e:
        return False, str(e)


# ==================== 基础数据共享存储 ====================
# 供「基础数据」页写入，O2O 门店下单 / EC 电商下单工具读取，
# 避免逻辑仓查询反复访问数据库。
basic_data_store = {}  # env_key -> {"logic_stores": [...], "logic_store_inventory": [...]}


def _basic_data(env_key: str) -> Dict[str, Any]:
    return basic_data_store.setdefault(env_key, {"logic_stores": [], "logic_store_inventory": []})


def set_basic_data(env_key: str, logic_stores=None, logic_store_inventory=None) -> Dict[str, Any]:
    data = _basic_data(env_key)
    if logic_stores is not None:
        data["logic_stores"] = logic_stores
    if logic_store_inventory is not None:
        data["logic_store_inventory"] = logic_store_inventory
    return data


def get_basic_logic_stores(env_key: str) -> List[Dict[str, Any]]:
    return list(_basic_data(env_key).get("logic_stores", []))


def get_basic_inventory_by_store(env_key: str, store_id) -> List[Dict[str, Any]]:
    rows = _basic_data(env_key).get("logic_store_inventory", [])
    return [r for r in rows if r.get("store_id") == store_id]


def get_basic_available_skus_by_store(env_key: str, store_id, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """从基础数据内存读取逻辑仓可用 SKU，按可用量从大到小排序。"""
    items = []
    for r in get_basic_inventory_by_store(env_key, store_id):
        sku = str(r.get("ps_c_sku_ecode") or "").strip()
        if not sku:
            continue
        try:
            qty = int(r.get("qty_available") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty < 0:
            continue
        items.append({"ps_c_sku_ecode": sku, "qty_available": qty})
    items.sort(key=lambda x: x["qty_available"], reverse=True)
    if limit is not None:
        items = items[:max(0, int(limit))]
    return items


# ==================== 库存调整工具 ====================
class StockAdjustToolManager:
    """库存调整造数：查询档案、调用库存调整单和分货单接口。"""

    def __init__(self, env_key: str = None):
        if not pymysql:
            raise ImportError("请先安装 pymysql: pip install pymysql")
        self.env_key = (env_key or DEFAULT_ENV).strip().lower()
        env_config = get_db_config_by_env(self.env_key)
        self.base_config = env_config["base_db"]
        self.order_config = env_config.get("order_db") or self.base_config
        self.sg_config = dict(env_config.get("sg_db") or self.base_config)
        oms = get_oms_config(self.env_key)
        self.oms_config = {
            "base_url": oms["base_url"],
            "login": {
                "username": oms["username"],
                "password": oms.get("password", ""),
            },
        }
        self.base_conn = None
        self.order_conn = None
        self.sg_conn = None
        self.last_error = None
        self._client = None

    def _connect(self, config: Dict[str, Any]):
        return pymysql.connect(
            host=config["host"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            port=config["port"],
            charset=config.get("charset", "utf8mb4"),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

    def connect_base_db(self) -> bool:
        try:
            self.base_conn = self._connect(self.base_config)
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"基础库连接失败: {e}")
            return False

    def connect_order_db(self) -> bool:
        try:
            self.order_conn = self._connect(self.order_config)
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"订单库连接失败: {e}")
            return False

    def connect_sg_db(self) -> bool:
        try:
            self.sg_conn = self._connect(self.sg_config)
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"SG库连接失败: {e}")
            return False

    def close(self):
        if self.base_conn:
            self.base_conn.close()
            self.base_conn = None
        if self.order_conn:
            self.order_conn.close()
            self.order_conn = None
        if self.sg_conn:
            self.sg_conn.close()
            self.sg_conn = None

    def _query_base(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if not self.base_conn and not self.connect_base_db():
            return []
        with self.base_conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _query_order(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if not self.order_conn and not self.connect_order_db():
            return []
        with self.order_conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _query_sg(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if not self.sg_conn and not self.connect_sg_db():
            return []
        with self.sg_conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _execute_sg(self, sql: str, params: tuple = ()) -> int:
        if not self.sg_conn and not self.connect_sg_db():
            return 0
        with self.sg_conn.cursor() as cursor:
            affected = cursor.execute(sql, params)
        self.sg_conn.commit()
        return affected

    def _query_archive(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        last_error = None
        for query in (self._query_base, self._query_order):
            try:
                return query(sql, params)
            except Exception as e:
                last_error = e
        raise last_error

    def _query_archive_not_empty(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        last_error = None
        for query in (self._query_base, self._query_order):
            try:
                rows = query(sql, params)
                if rows:
                    return rows
            except Exception as e:
                last_error = e
        if last_error:
            raise last_error
        return []

    def get_logic_stores(self, keyword: str = "") -> List[Dict[str, Any]]:
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            sql = """
                SELECT id, ecode, ename
                FROM cp_c_store
                WHERE isactive = 'Y' AND (ecode LIKE %s OR ename LIKE %s)
                ORDER BY id DESC
                LIMIT 200
            """
            return self._query_base(sql, (like, like))
        # 无关键字时走最简单的索引路径，避免 LIKE '%%' 造成的全表扫描
        sql = """
            SELECT id, ecode, ename
            FROM cp_c_store
            WHERE isactive = 'Y'
            ORDER BY id DESC
            LIMIT 200
        """
        return self._query_base(sql)

    def get_logic_store_by_id(self, logic_store_id: int) -> Optional[Dict[str, Any]]:
        """查询单个逻辑仓档案（含 ad_client_id），供库存调整执行使用。"""
        if logic_store_id is None:
            return None
        rows = self._query_base(
            "SELECT id, ecode, ename, sg_c_share_store_id, ad_client_id FROM cp_c_store WHERE id = %s LIMIT 1",
            (logic_store_id,),
        )
        return rows[0] if rows else None

    def get_max_available_skus_by_logic_store(self, logic_store_id: int, limit: int = 1) -> List[Dict[str, Any]]:
        """查询指定逻辑仓库存表中可用量最大的前 N 个 SKU。"""
        limit = max(1, int(limit))
        sql = f"""
            SELECT ps_c_sku_ecode, qty_available
            FROM sg_b_storage
            WHERE cp_c_store_id = %s
              AND isactive = 'Y'
            ORDER BY qty_available DESC, id DESC
            LIMIT {limit}
        """
        return self._query_sg(sql, (logic_store_id,))

    def get_available_skus_by_logic_store(self, logic_store_id: int) -> List[Dict[str, Any]]:
        """查询指定逻辑仓库存表中所有可用量 >= 0 的 SKU，按可用量从大到小排序。"""
        sql = """
            SELECT ps_c_sku_ecode, qty_available
            FROM sg_b_storage
            WHERE cp_c_store_id = %s
              AND isactive = 'Y'
              AND qty_available >= 0
            ORDER BY qty_available DESC, id DESC
        """
        return self._query_sg(sql, (logic_store_id,))

    def query_logic_store_archive(self) -> List[Dict[str, Any]]:
        """查询逻辑仓档案表（cp_c_store）所有激活逻辑仓，返回 编码/名称/品牌。"""
        if not self.base_conn and not self.connect_base_db():
            raise RuntimeError(f"基础库连接失败: {self.last_error or '请检查数据库地址/VPN/网络'}")
        sql = """
            SELECT id, ecode, ename, BRAND_CODE, ps_c_brand_ids
            FROM cp_c_store
            WHERE isactive = 'Y'
            ORDER BY id DESC
        """
        rows = self._query_base(sql)

        # 解析品牌：优先 BRAND_CODE，否则通过 ps_c_brand_ids 关联 ps_c_brand 表
        brand_ids = []
        for r in rows:
            raw_ids = str(r.get("ps_c_brand_ids") or "")
            for part in raw_ids.split(","):
                part = part.strip()
                if part.isdigit():
                    brand_ids.append(int(part))
        brand_lookup = {}
        if brand_ids:
            try:
                placeholders = ",".join(["%s"] * len(brand_ids))
                bsql = f"SELECT id, ecode, ename FROM ps_c_brand WHERE id IN ({placeholders})"
                for b in self._query_base(bsql, tuple(brand_ids)):
                    brand_lookup[str(b.get("id"))] = str(b.get("ecode") or b.get("ename") or "").strip()
            except Exception:
                brand_lookup = {}

        result = []
        for r in rows:
            brand = str(r.get("BRAND_CODE") or "").strip()
            if not brand and r.get("ps_c_brand_ids"):
                parts = [brand_lookup.get(str(p).strip(), "") for p in str(r["ps_c_brand_ids"]).split(",")]
                brand = "/".join([p for p in parts if p])
            result.append({
                "id": r.get("id"),
                "ecode": str(r.get("ecode") or "").strip(),
                "ename": str(r.get("ename") or "").strip(),
                "brand": brand,
            })
        return result

    def query_logic_store_inventory(self) -> List[Dict[str, Any]]:
        """查询逻辑仓档案 + 逻辑仓库存表（sg_b_storage），以逻辑仓维度记录。

        每个逻辑仓返回其库存 SKU 记录；若无库存，仍保留一条空记录。
        """
        stores = self.query_logic_store_archive()
        store_ids = [s["id"] for s in stores if s.get("id") is not None]

        if not self.sg_conn and not self.connect_sg_db():
            raise RuntimeError(f"SG库连接失败: {self.last_error or '请检查数据库地址/VPN/网络'}")

        storage_rows = []
        if store_ids:
            placeholders = ",".join(["%s"] * len(store_ids))
            sql = f"""
                SELECT cp_c_store_id, ps_c_sku_ecode, qty_available
                FROM sg_b_storage
                WHERE cp_c_store_id IN ({placeholders})
                  AND isactive = 'Y'
                ORDER BY cp_c_store_id ASC, qty_available DESC, id DESC
            """
            try:
                storage_rows = self._query_sg(sql, tuple(store_ids))
            except Exception:
                storage_rows = []

        by_store = {}
        for r in storage_rows:
            by_store.setdefault(r.get("cp_c_store_id"), []).append(r)

        result = []
        for s in stores:
            sid = s.get("id")
            rows = by_store.get(sid, [])
            if not rows:
                rows = [{"ps_c_sku_ecode": "", "qty_available": None}]
            for r in rows:
                result.append({
                    "store_id": sid,
                    "ecode": s.get("ecode", ""),
                    "ename": s.get("ename", ""),
                    "ps_c_sku_ecode": str(r.get("ps_c_sku_ecode") or "").strip(),
                    "qty_available": r.get("qty_available"),
                })
        return result

    def _find_share_store_sa_item_table(self) -> Optional[str]:
        sql = """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND lower(column_name) IN ('sg_c_share_store_id', 'sg_c_sa_store_id')
              AND lower(table_name) LIKE '%share%store%'
              AND lower(table_name) NOT LIKE 'sg_b_%'
              AND lower(table_name) NOT LIKE 'v_%'
            GROUP BY table_name
            HAVING COUNT(DISTINCT lower(column_name)) = 2
            ORDER BY
              CASE WHEN lower(table_name) LIKE 'sg_c_share_store%%' THEN 0 ELSE 1 END,
              CASE WHEN lower(table_name) LIKE '%%item%%' OR lower(table_name) LIKE '%%line%%' OR lower(table_name) LIKE '%%detail%%' THEN 0 ELSE 1 END,
              table_name
            LIMIT 1
        """
        try:
            rows = self._query_sg(sql)
            if rows:
                return rows[0].get("table_name")
        except Exception:
            pass
        return None

    def get_sa_stores_by_logic_store(self, logic_store_id: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        logic_rows = self._query_base(
            "SELECT id, ecode, ename, sg_c_share_store_id, ad_client_id FROM cp_c_store WHERE id = %s LIMIT 1",
            (logic_store_id,),
        )
        if not logic_rows:
            return None, []
        logic_store = logic_rows[0]
        share_store_id = logic_store.get("sg_c_share_store_id")
        share_store = None
        if share_store_id:
            share_rows = self._query_sg(
                "SELECT id, ecode, ename FROM sg_c_share_store WHERE id = %s LIMIT 1",
                (share_store_id,),
            )
            share_store = share_rows[0] if share_rows else {"id": share_store_id, "ecode": "", "ename": ""}
        if not share_store_id:
            return share_store, []

        item_table = self._find_share_store_sa_item_table()
        if item_table:
            sa_stores = self._query_sg(
                f"""
                SELECT DISTINCT sa.id, sa.ecode, sa.ename
                FROM {item_table} item
                INNER JOIN sg_c_sa_store sa ON sa.id = item.sg_c_sa_store_id
                WHERE item.sg_c_share_store_id = %s
                  AND sa.isactive = 'Y'
                ORDER BY sa.id DESC
                LIMIT 200
                """,
                (share_store_id,),
            )
        else:
            sa_stores = self._query_sg(
                """
                SELECT id, ecode, ename
                FROM sg_c_sa_store
                WHERE isactive = 'Y' AND sg_c_share_store_id = %s
                ORDER BY id DESC
                LIMIT 200
                """,
                (share_store_id,),
            )
        return share_store, sa_stores

    def get_sku(self, sku_ecode: str) -> Optional[Dict[str, Any]]:
        rows = self._query_base("SELECT * FROM ps_c_sku WHERE ecode = %s LIMIT 1", (sku_ecode,))
        return rows[0] if rows else None

    def derive_adjust_targets_by_shop(
        self, shop_id: Any
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """按下单店铺推导库存调整目标：逻辑仓、聚合仓、配销仓。

        链路：cp_c_shop_id -> sg_c_sa_sourcing_priority_strategy ->
        sg_c_sa_sourcing_priority_strategy_item(取 sg_c_sa_store_orderno 最大) ->
        sg_c_sa_store(所属聚合仓) -> cp_c_store(逻辑仓，多则随机取一)。
        """
        if shop_id is None:
            return None, None, None
        item_rows = self._query_sg(
            """
            SELECT item.sg_c_sa_store_id AS sa_store_id,
                   c.sg_c_share_store_id AS share_store_id
            FROM sg_c_sa_sourcing_priority_strategy_item item
            INNER JOIN sg_c_sa_sourcing_priority_strategy main
                    ON main.id = item.sg_c_sa_sourcing_priority_strategy_id
            INNER JOIN sg_c_sa_store c ON c.id = item.sg_c_sa_store_id
            WHERE main.cp_c_shop_id = %s
              AND main.isactive = 'Y'
              AND item.isactive = 'Y'
              AND c.isactive = 'Y'
            ORDER BY COALESCE(item.sg_c_sa_store_orderno, 0) DESC, item.id DESC
            LIMIT 1
            """,
            (shop_id,),
        )
        if not item_rows:
            return None, None, None
        row = item_rows[0]
        sa_store_id = row.get("sa_store_id")
        share_store_id = row.get("share_store_id")

        sa_store = None
        if sa_store_id:
            sa_rows = self._query_sg(
                "SELECT id, ecode, ename FROM sg_c_sa_store WHERE id = %s AND isactive = 'Y' LIMIT 1",
                (sa_store_id,),
            )
            sa_store = sa_rows[0] if sa_rows else {"id": sa_store_id, "ecode": "", "ename": ""}

        if not share_store_id:
            return None, None, sa_store

        share_rows = self._query_sg(
            "SELECT id, ecode, ename FROM sg_c_share_store WHERE id = %s AND isactive = 'Y' LIMIT 1",
            (share_store_id,),
        )
        share_store = share_rows[0] if share_rows else {"id": share_store_id, "ecode": "", "ename": ""}

        logic_rows = self._query_base(
            "SELECT id, ecode, ename, sg_c_share_store_id, ad_client_id FROM cp_c_store "
            "WHERE sg_c_share_store_id = %s AND isactive = 'Y'",
            (share_store_id,),
        )
        logic_store = random.choice(logic_rows) if logic_rows else None
        return logic_store, share_store, sa_store

    def _oms_username(self) -> str:
        login_cfg = self.oms_config.get("login") or {}
        return (login_cfg.get("username") or "").strip()

    def _project_name(self) -> str:
        from settings import _parse_env_key

        project, _ = _parse_env_key(self.env_key)
        return project or ""

    def _default_ad_client_id(self) -> int:
        """adClientId 默认值：威富O2O 项目取 1，其余取 37。"""
        if "vf_o2o" in self._project_name():
            return 1
        return 37

    def _oms_client(self):
        if self._client is not None:
            return self._client
        from oms_client import OmsClient
        login_cfg = self.oms_config.get("login") or {}
        username = self._oms_username()
        password = login_cfg.get("password") or ""
        if not self.oms_config.get("base_url") or not username:
            raise RuntimeError("OMS 配置缺失，请在「环境配置」中补充地址与账号")
        client = OmsClient(
            self.oms_config.get("base_url"),
            username,
            password,
        )
        client.login()
        self._client = client
        return client

    def _sync_allocation_ad_client_id(self, allocation_id: int, ad_client_id: Any) -> Dict[str, Any]:
        if not allocation_id or not ad_client_id:
            return {"updated": False, "message": "缺少分货单ID或adClientId"}
        head_count = self._execute_sg(
            "UPDATE sg_b_share_allocation SET ad_client_id = %s WHERE id = %s",
            (ad_client_id, allocation_id),
        )
        item_count = self._execute_sg(
            "UPDATE sg_b_share_allocation_item SET ad_client_id = %s WHERE sg_b_share_allocation_id = %s",
            (ad_client_id, allocation_id),
        )
        return {"updated": True, "adClientId": ad_client_id, "head_count": head_count, "item_count": item_count}

    def _next_allocation_bill_no(self) -> str:
        prefix = "STS" + datetime.now().strftime("%y%m%d")
        rows = self._query_sg(
            "SELECT bill_no FROM sg_b_share_allocation WHERE bill_no LIKE %s ORDER BY bill_no DESC LIMIT 1",
            (prefix + "%",),
        )
        seq = 1
        if rows and rows[0].get("bill_no"):
            try:
                seq = int(str(rows[0]["bill_no"])[-8:]) + 1
            except Exception:
                pass
        return f"{prefix}{seq:08d}"

    def _next_sg_id(self, table_name: str) -> int:
        rows = self._query_sg(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM {table_name}")
        return int(rows[0]["next_id"])

    def _create_allocation_direct(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data", {})
        main = data.get("SG_B_SHARE_ALLOCATION") or {}
        items = data.get("SG_B_SHARE_ALLOCATION_ITEM") or []
        if not items:
            return {"status_code": None, "body": {"error": "分货单明细为空"}, "payload": payload}
        item = items[0]
        allocation_id = self._next_sg_id("sg_b_share_allocation")
        item_id = self._next_sg_id("sg_b_share_allocation_item")
        bill_no = self._next_allocation_bill_no()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        owner_name = self._oms_username()
        ad_client_id = (
            main.get("adClientId")
            or payload.get("fixcolumn", {}).get("adClientId")
            or self._default_ad_client_id()
        )
        qty = item.get("qty") or 0
        qty_apply = item.get("qtyApply") if item.get("qtyApply") is not None else qty
        self._execute_sg(
            """
            INSERT INTO sg_b_share_allocation
            (id, ad_client_id, ad_org_id, bill_no, bill_date, status, sg_c_share_store_id, sg_c_share_store_ecode,
             sg_c_share_store_ename, tot_row_num, tot_qty, tot_qty_apply, tot_qty_diff, tot_amt, remark, isactive,
             ownerid, ownername, creationdate, modifierid, modifiername, modifieddate)
            VALUES (%s,%s,27,%s,%s,1,%s,%s,%s,1,%s,%s,%s,0,%s,'Y',2821,%s,%s,2821,%s,%s)
            """,
            (
                allocation_id, ad_client_id, bill_no, main.get("billDate"), main.get("sgCShareStoreId"),
                main.get("sgCShareStoreEcode"), main.get("sgCShareStoreEname"), qty, qty_apply,
                float(qty) - float(qty_apply), main.get("remark"), owner_name, now, owner_name, now,
            ),
        )
        self._execute_sg(
            """
            INSERT INTO sg_b_share_allocation_item
            (id, ad_client_id, ad_org_id, sg_c_sa_store_id, sg_c_sa_store_ecode, sg_c_sa_store_ename,
             sg_b_share_allocation_id, ps_c_sku_id, ps_c_sku_ecode, gbcode, ps_c_brand_id, ps_c_pro_id,
             ps_c_pro_ecode, ps_c_pro_ename, ps_c_spec1_id, ps_c_spec1_ecode, ps_c_spec1_ename,
             ps_c_spec2_id, ps_c_spec2_ecode, ps_c_spec2_ename, qty, qty_apply, qty_diff, amt, isactive,
             ownerid, ownername, creationdate, modifierid, modifiername, modifieddate)
            VALUES (%s,%s,27,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'Y',2821,%s,%s,2821,%s,%s)
            """,
            (
                item_id, ad_client_id, item.get("sgCSaStoreId"), item.get("sgCSaStoreEcode"), item.get("sgCSaStoreEname"),
                allocation_id, item.get("psCSkuId"), item.get("psCSkuEcode"), item.get("gbcode"), item.get("psCBrandId"),
                item.get("psCProId"), item.get("psCProEcode"), item.get("psCProEname"), item.get("psCSpec1Id"),
                item.get("psCSpec1Ecode"), item.get("psCSpec1Ename"), item.get("psCSpec2Id"), item.get("psCSpec2Ecode"),
                item.get("psCSpec2Ename"), qty, qty_apply, float(qty) - float(qty_apply), owner_name, now, owner_name, now,
            ),
        )
        return {"status_code": 200, "body": {"code": 0, "message": "success", "data": {"objid": allocation_id, "billNo": bill_no}}, "payload": payload}

    @staticmethod
    def _post_json(client, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        from urllib.parse import urljoin
        try:
            resp = client.session.post(urljoin(client.base_url + "/", path.lstrip("/")), json=payload, timeout=client.timeout)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:2000]}
            return {"status_code": resp.status_code, "body": body, "url": resp.url, "payload": payload}
        except Exception as e:
            return {"status_code": None, "body": {"error": str(e)}, "url": path, "payload": payload}

    @staticmethod
    def _debug_event(hypothesis_id: str, location: str, msg: str, data: Dict[str, Any]):
        # #region debug-point allocation-submit
        try:
            import urllib.request
            env_path = Path(__file__).resolve().parent / ".dbg" / "allocation-submit.env"
            url = "http://127.0.0.1:7777/event"
            session_id = "allocation-submit"
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if line.startswith("DEBUG_SERVER_URL="):
                        url = line.split("=", 1)[1]
                    elif line.startswith("DEBUG_SESSION_ID="):
                        session_id = line.split("=", 1)[1]
            payload = json.dumps({
                "sessionId": session_id,
                "runId": "pre-fix",
                "hypothesisId": hypothesis_id,
                "location": location,
                "msg": f"[DEBUG] {msg}",
                "data": data,
                "ts": int(time.time() * 1000),
            }, ensure_ascii=False).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=1).read()
        except Exception:
            pass
        # #endregion

    @staticmethod
    def _extract_id(data: Any) -> Optional[int]:
        if isinstance(data, dict):
            for key in ("id", "ID", "objId", "objid", "OBJID", "billId", "billid"):
                if data.get(key):
                    try:
                        return int(data[key])
                    except Exception:
                        pass
            for key in ("data", "dataJo", "result", "value", "body"):
                found = StockAdjustToolManager._extract_id(data.get(key))
                if found:
                    return found
        if isinstance(data, list):
            for item in data:
                found = StockAdjustToolManager._extract_id(item)
                if found:
                    return found
        return None

    @staticmethod
    def _is_success(response: Dict[str, Any]) -> bool:
        if response.get("skipped"):
            return False
        status_code = response.get("status_code")
        if status_code is not None and not (200 <= int(status_code) < 300):
            return False
        body = response.get("body")
        if isinstance(body, dict):
            if body.get("error"):
                return False
            code = body.get("code")
            if code is not None and str(code) not in ("0", "200", "success", "SUCCESS"):
                return False
            success = body.get("success")
            if success is False:
                return False
        return status_code is None or 200 <= int(status_code) < 300

    @staticmethod
    def _failure_reason(response: Dict[str, Any]) -> str:
        if response.get("message"):
            return str(response["message"])
        body = response.get("body")
        if isinstance(body, dict):
            for key in ("message", "msg", "error", "errorMsg", "errMsg"):
                if body.get(key):
                    return str(body[key])
            data = body.get("data")
            if isinstance(data, dict):
                for key in ("message", "msg", "error", "errorMsg", "errMsg"):
                    if data.get(key):
                        return str(data[key])
            if body:
                return json.dumps(body, ensure_ascii=False)
        if response.get("status_code"):
            return f"HTTP {response['status_code']}"
        return "未知失败原因"

    @classmethod
    def _business_step(cls, name: str, response: Dict[str, Any]) -> Dict[str, Any]:
        success = cls._is_success(response)
        item = {"step": name, "success": success, "result": "成功" if success else "失败"}
        if not success:
            item["reason"] = cls._failure_reason(response)
        return item

    def build_payloads(self, logic_store: Dict[str, Any], share_store: Dict[str, Any], sa_store: Dict[str, Any], sku: Optional[Dict[str, Any]], sku_ecode: str, qty: str) -> Dict[str, Dict[str, Any]]:
        qty_num = float(qty) if "." in str(qty) else int(qty)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        adjust_payload = {
            "table": "SG_B_STO_ADJUST",
            "objid": -1,
            "data": {
                "SG_B_STO_ADJUST": {
                    "ID": -1,
                    "BILL_DATE": now_str,
                    "BILL_TYPE": "1",
                    "CP_C_STORE_ID": str(logic_store.get("id") or ""),
                    "SG_B_ADJUST_PROP_ID": "74",
                    "STATUS": "1",
                },
                "SG_B_STO_ADJUST_ITEM": [
                    {"PS_C_SKU_ECODE": sku_ecode, "QTY": qty_num, "ID": -1},
                ],
            },
            "id": -1,
        }
        allocation_main = {
            "BILL_DATE": datetime.now().strftime("%Y%m%d"),
            "TOT_ROW_NUM": 0,
            "TOT_QTY": 0,
            "SG_C_SHARE_STORE_ID": str(share_store.get("id")),
            "ID": -1,
        }
        allocation_items = [{
            "SG_C_SA_STORE_ID": str(sa_store.get("id")),
            "PS_C_SKU_ECODE": sku_ecode,
            "QTY": qty_num,
            "ID": -1,
        }]
        allocation_payload = {
            "table": "SG_B_SHARE_ALLOCATION",
            "objid": -1,
            "data": {
                "SG_B_SHARE_ALLOCATION": allocation_main,
                "SG_B_SHARE_ALLOCATION_ITEM": allocation_items,
            },
            "id": -1,
        }
        return {"adjust_save": adjust_payload, "allocation_save": allocation_payload}

    def execute_flow(self, logic_store: Dict[str, Any], share_store: Dict[str, Any], sa_store: Dict[str, Any], sku_ecode: str, qty: str, on_step: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        sku = self.get_sku(sku_ecode)
        payloads = self.build_payloads(logic_store, share_store, sa_store, sku, sku_ecode, qty)
        client = self._oms_client()
        steps = []
        raw_steps = []

        adjust_save = self._post_json(client, "/api/cs/sg/v2/sgBStoAdjust/save", payloads["adjust_save"])
        raw_steps.append({"step": "库存调整单新增", **adjust_save})
        item = self._business_step("库存调整单新增", adjust_save)
        steps.append(item)
        if on_step:
            on_step(item)
        adjust_id = self._extract_id(adjust_save["body"])
        if adjust_id:
            adjust_submit_payload = {"objId": adjust_id, "id": adjust_id, "ids": [adjust_id]}
            adjust_submit = self._post_json(client, "/api/cs/sg/v2/sgBStoAdjust/submit", adjust_submit_payload)
            raw_steps.append({"step": "库存调整单提交", **adjust_submit})
            item = self._business_step("库存调整单提交", adjust_submit)
            steps.append(item)
            if on_step:
                on_step(item)
            time.sleep(0.2)
        else:
            adjust_submit = {"skipped": True, "message": "保存响应中未识别到单据ID，请查看保存失败原因或原始响应"}
            raw_steps.append({"step": "库存调整单提交", **adjust_submit})
            item = self._business_step("库存调整单提交", adjust_submit)
            steps.append(item)
            if on_step:
                on_step(item)

        # #region debug-point C:allocation-save
        self._debug_event("C", "api_tool.py:allocation-save", "分货单保存接口调用前", {
            "base_url": client.base_url,
            "path": "/api/cs/sg/sgBShareAllocation/save",
            "payload": payloads["allocation_save"],
        })
        # #endregion
        allocation_save = self._post_json(client, "/api/cs/sg/sgBShareAllocation/save", payloads["allocation_save"])
        raw_steps.append({"step": "分货单新增", **allocation_save})
        item = self._business_step("分货单新增", allocation_save)
        steps.append(item)
        if on_step:
            on_step(item)
        allocation_id = self._extract_id(allocation_save["body"])
        # #region debug-point C:allocation-saved
        self._debug_event("C", "api_tool.py:allocation-saved", "分货单保存接口响应", {
            "allocation_id": allocation_id,
            "response": allocation_save.get("body"),
        })
        # #endregion
        if allocation_id:
            allocation_submit_payload = {"table": "SG_B_SHARE_ALLOCATION", "id": str(allocation_id)}
            # #region debug-point A-B-D-E:allocation-submit
            self._debug_event("A-B-D-E", "api_tool.py:allocation-submit", "分货单提交前", {
                "path": "/api/cs/sg/sgBShareAllocation/submit",
                "base_url": client.base_url,
                "payload": allocation_submit_payload,
            })
            # #endregion
            allocation_submit = self._post_json(client, "/api/cs/sg/sgBShareAllocation/submit", allocation_submit_payload)
            # #region debug-point A-B-D-E:allocation-response
            self._debug_event("A-B-D-E", "api_tool.py:allocation-response", "分货单提交响应", {
                "status_code": allocation_submit.get("status_code"),
                "url": allocation_submit.get("url"),
                "response": allocation_submit.get("body"),
            })
            # #endregion
            raw_steps.append({"step": "分货单提交", **allocation_submit})
            item = self._business_step("分货单提交", allocation_submit)
            steps.append(item)
            if on_step:
                on_step(item)
        else:
            allocation_submit = {"skipped": True, "message": "保存响应中未识别到单据ID，请查看保存失败原因或原始响应"}
            raw_steps.append({"step": "分货单提交", **allocation_submit})
            item = self._business_step("分货单提交", allocation_submit)
            steps.append(item)
            if on_step:
                on_step(item)

        return {
            "success": all(step["success"] for step in steps),
            "sku_found": bool(sku),
            "steps": steps,
            "raw_steps": raw_steps,
            "payloads": payloads,
        }


def auto_adjust_stock_for_retail_order(
    env_key: str,
    retail_order_id: int,
    shop_id: Any = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """零售发货单缺货时自动执行调整库存（供正向造单节点串联后调用）。"""
    def _emit(msg: str) -> None:
        if log is not None:
            log(msg)

    manager = None
    try:
        lost_items = query_retail_order_lost_items(env_key, retail_order_id)
        if not lost_items:
            _emit("▸ 无缺货明细(qty_lost 均为 0)，跳过自动调整库存")
            return {"success": True, "skipped": True, "reason": "无缺货明细", "results": []}

        manager = StockAdjustToolManager(env_key)
        logic_store, share_store, sa_store = manager.derive_adjust_targets_by_shop(shop_id)
        if not logic_store:
            _emit("▸ 未推导出逻辑仓，跳过自动调整库存")
            return {"success": False, "skipped": True, "reason": "未推导出逻辑仓", "results": []}
        if not sa_store:
            _emit("▸ 未推导出配销仓，跳过自动调整库存")
            return {"success": False, "skipped": True, "reason": "未推导出配销仓", "results": []}

        _emit(
            f"▸ 自动调整库存：逻辑仓={logic_store.get('ecode')}/{logic_store.get('ename')}(id={logic_store.get('id')})，"
            f"配销仓={sa_store.get('ecode')}/{sa_store.get('ename')}(id={sa_store.get('id')})"
        )

        results = []
        for lost in lost_items:
            sku_ecode = lost.get("ps_c_sku_ecode")
            if not sku_ecode:
                continue
            _emit(f"▸ 缺货 SKU={sku_ecode}，执行调整库存（数量=100）")
            res = manager.execute_flow(
                logic_store,
                share_store,
                sa_store,
                sku_ecode,
                "100",
                on_step=lambda s: _emit(
                    f"   - {s.get('step')}{s.get('result', '')}"
                    + (f"；失败原因：{s['reason']}" if not s.get("success") and s.get("reason") else "")
                ),
            )
            results.append({"sku_ecode": sku_ecode, **res})
        _emit("▸ 自动调整库存完成")
        return {"success": True, "skipped": False, "results": results}
    finally:
        if manager:
            manager.close()


# ==================== GUI 模式 ====================
def _run_gui(initial_tab: Optional[str] = None):
    """桌面窗口：无需浏览器；现代化布局与配色。"""
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    from ui_theme import (
        ACCENT_LIGHT,
        CARD,
        CARD_BORDER,
        ERROR_RED,
        INFO_BLUE,
        LOG_BG,
        LOG_FG,
        MAIN_BG,
        NAV_BG,
        NAV_FG,
        NAV_FG_MUTED,
        SUCCESS_GREEN,
        TEXT_MAIN,
        TEXT_MUTED,
        CONTENT_PADDING,
        SECTION_GAP,
        UI_FONT,
        UI_FONT_HINT,
        UI_FONT_MODULE,
        UI_FONT_NAV,
        apply_theme,
        configure_log_text,
    )

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    from win_dpi import enable_windows_dpi_awareness

    enable_windows_dpi_awareness()

    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    fonts = apply_theme(style, root)
    MONO = fonts.mono
    UI_FONT = fonts.ui
    UI_FONT_SM = fonts.hint

    root.title("OMS公域造单平台 · 订单复制 / 公域平台正向造单 / 公域平台逆向造单")
    root.geometry("1180x820")
    root.minsize(980, 680)
    root.configure(bg=MAIN_BG)

    # 隐藏标签页标题栏（仅保留左侧导航切换）
    style.layout('TNotebook.Tab', [])

    # ==================== 整体布局 ====================
    # 主容器：左侧导航 + 右侧内容
    main_container = ttk.Frame(root, style="Main.TFrame")
    main_container.pack(fill=tk.BOTH, expand=True)

    # ==================== 左侧导航栏 ====================
    nav_frame = ttk.Frame(main_container, style="Nav.TFrame", width=260)
    nav_frame.pack(side=tk.LEFT, fill=tk.Y, expand=False)
    nav_frame.pack_propagate(False)

    # 导航栏顶部 - 应用标题
    nav_header = ttk.Frame(nav_frame, style="Nav.TFrame")
    nav_header.pack(fill=tk.X, pady=(20, 25), padx=15)

    app_title = tk.Label(
        nav_header,
        text="OMS公域造单平台",
        font=UI_FONT_NAV,
        bg=NAV_BG,
        fg=NAV_FG,
        justify=tk.LEFT,
    )
    app_title.pack(anchor=tk.W)

    app_subtitle = tk.Label(
        nav_header,
        text="中间表 · 订单 · 回传",
        font=UI_FONT_HINT,
        bg=NAV_BG,
        fg=NAV_FG_MUTED,
        justify=tk.LEFT,
    )
    app_subtitle.pack(anchor=tk.W, pady=(4, 0))

    # 导航分隔线
    ttk.Separator(nav_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=15, pady=(0, 15))

    # 功能导航菜单
    nav_menu_frame = ttk.Frame(nav_frame, style="Nav.TFrame")
    nav_menu_frame.pack(fill=tk.BOTH, expand=True, padx=8)

    current_nav = tk.StringVar(value="basic_data")

    nav_items = [
        ("基础数据", "basic_data"),
        ("平台订单中间表复制", "order_copy"),
        ("调整库存", "stock_adjust"),
        ("公域平台正向造单", "platform_order"),
        ("公域平台逆向造单", "reverse_order"),
    ]

    def select_nav(item_id):
        current_nav.set(item_id)
        if item_id in tab_frames:
            notebook.select(tab_frames[item_id])

    def create_nav_button(text, item_id):
        btn = ttk.Button(
            nav_menu_frame,
            text=text,
            command=lambda: select_nav(item_id),
            style="Nav.TButton",
        )
        btn.pack(fill=tk.X, padx=5, pady=4)
        return btn

    nav_buttons = [(create_nav_button(text, item_id), item_id) for text, item_id in nav_items]

    # 更新导航选中状态
    def update_nav_style(*args):
        for btn, item_id in nav_buttons:
            if current_nav.get() == item_id:
                btn.configure(style="NavSelected.TButton")
            else:
                btn.configure(style="Nav.TButton")

    current_nav.trace("w", update_nav_style)

    # ==================== 右侧主内容区 ====================
    content_frame = ttk.Frame(main_container, style="Main.TFrame")
    content_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(0, 12), pady=12)
    
    # 添加滚动容器
    content_canvas = tk.Canvas(content_frame, bg=MAIN_BG, highlightthickness=0)
    content_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    
    content_scrollbar = ttk.Scrollbar(content_frame, orient=tk.VERTICAL, command=content_canvas.yview)
    content_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    content_canvas.configure(yscrollcommand=content_scrollbar.set)
    content_canvas.bind('<Configure>', lambda e: content_canvas.configure(scrollregion=content_canvas.bbox('all')))
    
    scrollable_frame = ttk.Frame(content_canvas, style="Main.TFrame")
    scroll_window = content_canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')

    def _sync_scroll_region(event=None):
        content_canvas.configure(scrollregion=content_canvas.bbox('all'))

    def _sync_scroll_width(event):
        content_canvas.itemconfigure(scroll_window, width=event.width)
        _sync_scroll_region()

    def _wheel_eat_widget(widget) -> bool:
        """下拉框、文本框等控件滚动时不带动外层页面。"""
        w = widget
        while w:
            try:
                cls = w.winfo_class()
            except tk.TclError:
                break
            if cls in ("Listbox", "Text", "Treeview", "TCombobox"):
                return True
            parent = w.winfo_parent()
            if not parent:
                break
            try:
                w = root.nametowidget(parent)
            except tk.TclError:
                break
        return False

    def _on_mousewheel(event):
        if _wheel_eat_widget(event.widget):
            return "break"
        if event.delta:
            content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    content_canvas.bind("<Configure>", _sync_scroll_width)
    scrollable_frame.bind("<Configure>", _sync_scroll_region)
    content_canvas.bind_all("<MouseWheel>", _on_mousewheel)

    # 顶部标签页
    notebook = ttk.Notebook(scrollable_frame)
    notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

    # 标签页frame引用字典
    tab_frames = {}

    # ==================== 共享状态与脚手架（订单复制 / 调整库存 / 基础数据共用） ====================
    from settings import env_options_for_gui
    _env_options = env_options_for_gui() or {"TEST": "test"}
    env_options = _env_options
    env_labels = list(env_options.keys())
    default_env_label = env_labels[0] if env_labels else "TEST"

    db_var = tk.StringVar(value=default_env_label)
    selected_env_label = tk.StringVar(value=default_env_label)

    def _get_selected_env_key():
        return env_options.get(selected_env_label.get(), DEFAULT_ENV)

    def _update_env_info(event=None):
        """同步各标签页头部环境标识与数据库连接信息。"""
        env_name = selected_env_label.get()
        db_var.set(env_name)
        try:
            order_env_label.config(text=f"· {env_name}")
            order_db_var.set(env_name)
            current_db = get_db_config_by_env(_get_selected_env_key())
            order_db_info_var.set(
                f"订单库: {current_db['order_db']['host']} / {current_db['order_db']['database']}    "
                f"基础库: {current_db['base_db']['host']} / {current_db['base_db']['database']}"
            )
        except NameError:
            pass
        try:
            stock_env_label.config(text=f"· {env_name}")
            stock_env_var.set(env_name)
        except NameError:
            pass

    def _add_collapsible_desc(parent, text, wraplength=800):
        """创建可折叠说明区（默认隐藏）。"""
        frame = ttk.LabelFrame(parent, text=" 说明 ", style="Card.TLabelframe", padding=(8, 6))
        frame.pack(fill=tk.X, pady=(0, SECTION_GAP))
        toggle = ttk.Label(frame, text="▸ 展开说明", style="Card.TLabel", font=UI_FONT_SM, cursor="hand2")
        toggle.pack(anchor=tk.W)
        content = ttk.Label(
            frame, text=text, style="Muted.TLabel",
            font=UI_FONT, wraplength=wraplength, justify=tk.LEFT,
        )
        state = {"shown": False}

        def _toggle(event=None):
            if state["shown"]:
                content.pack_forget()
                toggle.config(text="▸ 展开说明")
            else:
                content.pack(anchor=tk.W, pady=(2, 0), after=toggle)
                toggle.config(text="▾ 收起说明")
            state["shown"] = not state["shown"]

        toggle.bind("<Button-1>", _toggle)
        return frame

    def make_entry_copyable(entry, value_var):
        def select_all(event=None):
            entry.focus_set()
            entry.selection_range(0, tk.END)
            return "break"

        def copy_value(event=None):
            value = value_var.get().strip()
            if value and value != "-":
                root.clipboard_clear()
                root.clipboard_append(value)
            return "break"

        entry.bind("<Button-1>", select_all)
        entry.bind("<Control-a>", select_all)
        entry.bind("<Control-A>", select_all)
        entry.bind("<Control-c>", copy_value)
        entry.bind("<Control-C>", copy_value)

    def show_copyable_dialog(title: str, content: str):
        """展示可选择、可 Ctrl+C 复制的结果窗口。"""
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.geometry("760x520")
        dialog.minsize(560, 360)
        dialog.transient(root)
        dialog.configure(bg=MAIN_BG)

        text_area = scrolledtext.ScrolledText(dialog, wrap=tk.WORD, padx=10, pady=10)
        configure_log_text(text_area, mono=MONO)
        text_area.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 8))
        text_area.insert(tk.END, content)
        text_area.focus_set()

        btn_bar = ttk.Frame(dialog, style="Main.TFrame")
        btn_bar.pack(fill=tk.X, padx=12, pady=(0, 12))

        def copy_selection_or_all():
            try:
                selected = text_area.get(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                selected = text_area.get("1.0", tk.END)
            root.clipboard_clear()
            root.clipboard_append(selected)

        ttk.Button(btn_bar, text="复制", command=copy_selection_or_all, style="Cyan.TButton").pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="关闭", command=dialog.destroy, style="Secondary.TButton").pack(side=tk.RIGHT)
        text_area.bind("<Control-c>", lambda event: (copy_selection_or_all(), "break"))
        text_area.bind("<Control-C>", lambda event: (copy_selection_or_all(), "break"))


    # ==================== 订单复制标签页 ====================
    order_frame = ttk.Frame(notebook, style="Main.TFrame", padding=CONTENT_PADDING)
    notebook.add(order_frame, text="平台订单中间表复制")
    tab_frames["order_copy"] = order_frame

    # 顶部标题栏
    order_header_frame = ttk.Frame(order_frame, style="Main.TFrame", padding=(0, 0, 0, 6))
    order_header_frame.pack(fill=tk.X, pady=(0, SECTION_GAP))

    order_header_title = tk.Label(order_header_frame, text="平台订单中间表复制",
                                  font=UI_FONT_MODULE, fg=TEXT_MAIN, bg=MAIN_BG)
    order_header_title.pack(side=tk.LEFT)

    order_env_label = tk.Label(order_header_frame, text=f"· {default_env_label}",
                               font=UI_FONT, fg=INFO_BLUE, bg=MAIN_BG)
    order_env_label.pack(side=tk.LEFT, padx=(8, 0))

    order_env_title_label = tk.Label(order_header_frame, text="环境",
                                     font=UI_FONT_HINT, fg=TEXT_MUTED, bg=MAIN_BG)
    order_env_title_label.pack(side=tk.RIGHT, padx=(0, 8))

    order_db_var = tk.StringVar(value=default_env_label)
    order_db_combo = ttk.Combobox(order_header_frame, textvariable=order_db_var,
                                  values=env_labels, state="readonly", width=8)
    order_db_combo.pack(side=tk.RIGHT)

    def _sync_order_header_env(event=None):
        db_var.set(order_db_var.get())
        selected_env_label.set(order_db_var.get())
        _update_env_info()
        root.after(50, load_shops)

    order_db_combo.bind("<<ComboboxSelected>>", _sync_order_header_env)

    order_db_info_var = tk.StringVar(value="")
    order_db_info_label = ttk.Label(order_frame, textvariable=order_db_info_var, style="Muted.TLabel")
    order_db_info_label.pack_forget()
    _update_env_info()

    # 功能说明区
    order_desc_frame = _add_collapsible_desc(
        order_frame,
        """
从 IP_B_PLATFORM_ORDER_SOURCE 表复制订单数据，支持修改店铺和商品SKU。
- 复制时自动生成新的ID和TID，避免主键冲突
- 可选：选择目标店铺进行跨店铺复制
- 可选：修改商品SKU编码
    """.strip(),
        wraplength=600,
    )

    # 订单复制区域
    copy_frame = ttk.LabelFrame(order_frame, text=" 订单信息 ", style="Card.TLabelframe")
    copy_frame.pack(fill=tk.X, pady=(0, SECTION_GAP))

    # 平台单号 TID
    tid_frame = ttk.Frame(copy_frame, style="Card.TFrame")
    tid_frame.pack(fill=tk.X, pady=6)
    ttk.Label(tid_frame, text="平台单号 TID:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    tid_entry = ttk.Entry(tid_frame, width=50)
    tid_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # 店铺选择
    shop_frame = ttk.Frame(copy_frame, style="Card.TFrame")
    shop_frame.pack(fill=tk.X, pady=6)
    ttk.Label(shop_frame, text="店铺名称:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    
    shop_var = tk.StringVar(value="请选择店铺（可选）")
    shop_combo = ttk.Combobox(
        shop_frame,
        textvariable=shop_var,
        state="readonly",
        width=45,
        font=UI_FONT,
    )
    shop_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

    shop_dict = {}  # name -> id 映射
    def load_shops():
        order_status_var.set("正在加载店铺列表...")
        root.update()
        try:
            env_key = _get_selected_env_key()
            manager = OrderCopyManager(env_key=env_key)
            shops = manager.get_all_shops()
            manager.close()

            if shops:
                shop_names = ["请选择店铺（可选）"]
                shop_dict.clear()
                shop_dict["请选择店铺（可选）"] = None
                
                for shop in shops:
                    display_name = f"{shop.get('cp_c_shop_title', '')} (ID: {shop.get('id', '')})"
                    shop_names.append(display_name)
                    shop_dict[display_name] = shop.get('id')

                shop_combo['values'] = shop_names
                shop_var.set("请选择店铺（可选）")
                order_status_var.set(f"{selected_env_label.get()} 环境已加载 {len(shops)} 个店铺")
            else:
                shop_combo['values'] = ["请选择店铺（可选）"]
                shop_var.set("请选择店铺（可选）")
                shop_dict.clear()
                shop_dict["请选择店铺（可选）"] = None
                order_status_var.set(f"{selected_env_label.get()} 环境未获取到店铺列表，请检查数据库连接")
        except Exception as e:
            order_status_var.set(f"加载店铺失败: {str(e)}")

    refresh_btn = ttk.Button(shop_frame, text="刷新店铺", command=load_shops, style="Info.TButton")
    refresh_btn.pack(side=tk.RIGHT, padx=(10, 0))

    # 商品SKU
    sku_frame = ttk.Frame(copy_frame, style="Card.TFrame")
    sku_frame.pack(fill=tk.X, pady=6)
    ttk.Label(sku_frame, text="商品SKU:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    sku_entry = ttk.Entry(sku_frame, width=50)
    sku_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Label(sku_frame, text="（可选，不填则使用原订单SKU）", style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(10, 0))

    # 操作按钮区
    btn_frame = ttk.Frame(copy_frame, style="Card.TFrame")
    btn_frame.pack(fill=tk.X, pady=8)
    
    copy_btn = ttk.Button(btn_frame, text="复制订单", style="Primary.TButton", width=20)
    copy_btn.pack(side=tk.LEFT, padx=(0, 10))
    
    clear_result_btn = ttk.Button(btn_frame, text="清空结果", style="Secondary.TButton")
    clear_result_btn.pack(side=tk.LEFT)

    # 状态显示
    new_order_tid_label = ttk.Label(btn_frame, text="新订单TID:", style="Muted.TLabel")
    new_order_tid_label.pack(side=tk.LEFT, padx=(18, 4))

    new_order_tid_var = tk.StringVar(value="-")
    new_order_tid_entry = ttk.Entry(btn_frame, textvariable=new_order_tid_var, width=24, state="readonly")
    new_order_tid_entry.pack(side=tk.LEFT)
    make_entry_copyable(new_order_tid_entry, new_order_tid_var)

    order_status_var = tk.StringVar(value="就绪")
    order_status_label = ttk.Label(btn_frame, textvariable=order_status_var, style="Muted.TLabel")
    order_status_label.pack(side=tk.RIGHT)

    # 结果显示区域
    result_frame = ttk.LabelFrame(order_frame, text=" 操作结果 ", style="Card.TLabelframe")
    result_frame.pack(fill=tk.BOTH, expand=True)

    order_result_text = scrolledtext.ScrolledText(result_frame, wrap=tk.WORD, padx=10, pady=10)
    configure_log_text(order_result_text, mono=MONO)
    order_result_text.pack(fill=tk.BOTH, expand=True)

    def clear_result():
        order_result_text.delete("1.0", tk.END)
        new_order_tid_var.set("-")
        order_status_var.set("就绪")

    def on_copy_order():
        source_tid = tid_entry.get().strip()
        if not source_tid:
            messagebox.showwarning("提示", "请输入平台单号 TID")
            return

        selected_shop = shop_var.get()
        shop_id = shop_dict.get(selected_shop)
        sku_ecode = sku_entry.get().strip() or None

        order_status_var.set("正在复制订单...")
        root.update()

        try:
            env_key = _get_selected_env_key()
            manager = OrderCopyManager(env_key=env_key)
            result = manager.copy_order(source_tid, shop_id, sku_ecode)

            order_result_text.delete("1.0", tk.END)
            order_result_text.insert(tk.END, json.dumps(result, ensure_ascii=False, indent=2))

            dialog_content = json.dumps(result, ensure_ascii=False, indent=2)
            if result['success']:
                new_tid = result.get('data', {}).get('new_tid', '-') if isinstance(result.get('data'), dict) else '-'
                new_order_tid_var.set(str(new_tid))
                order_status_var.set(f"{selected_env_label.get()} 环境复制成功！")
            else:
                new_order_tid_var.set("-")
                order_status_var.set("复制失败")
                show_copyable_dialog("复制失败", dialog_content)

        except Exception as e:
            error_text = f"错误: {str(e)}"
            new_order_tid_var.set("-")
            order_status_var.set(error_text)
            show_copyable_dialog("错误", error_text)

    copy_btn.config(command=on_copy_order)
    clear_result_btn.config(command=clear_result)

    # 初始化加载店铺
    load_shops()

    # ==================== 调整库存标签页 ====================
    stock_adjust_frame = ttk.Frame(notebook, style="Main.TFrame", padding=CONTENT_PADDING)
    notebook.add(stock_adjust_frame, text="调整库存")
    tab_frames["stock_adjust"] = stock_adjust_frame

    stock_header_frame = ttk.Frame(stock_adjust_frame, style="Main.TFrame", padding=(0, 0, 0, 6))
    stock_header_frame.pack(fill=tk.X, pady=(0, SECTION_GAP))
    tk.Label(stock_header_frame, text="调整库存", font=UI_FONT_MODULE, fg=TEXT_MAIN, bg=MAIN_BG).pack(side=tk.LEFT)
    stock_env_label = tk.Label(stock_header_frame, text=f"· {default_env_label}", font=UI_FONT, fg=INFO_BLUE, bg=MAIN_BG)
    stock_env_label.pack(side=tk.LEFT, padx=(8, 0))
    tk.Label(stock_header_frame, text="环境", font=UI_FONT_HINT, fg=TEXT_MUTED, bg=MAIN_BG).pack(side=tk.RIGHT, padx=(0, 8))
    stock_env_var = tk.StringVar(value=default_env_label)
    stock_env_combo = ttk.Combobox(stock_header_frame, textvariable=stock_env_var, values=env_labels, state="readonly", width=8)
    stock_env_combo.pack(side=tk.RIGHT)

    stock_desc_frame = _add_collapsible_desc(
        stock_adjust_frame,
        "按逻辑仓 + SKU + 调整数量生成库存调整单并审核；再按该逻辑仓所属聚合仓选择配销仓，生成分货单并审核。",
        wraplength=760,
    )

    stock_form = ttk.LabelFrame(stock_adjust_frame, text=" 库存调整信息 ", style="Card.TLabelframe")
    stock_form.pack(fill=tk.X, pady=(0, SECTION_GAP))

    logic_store_dict = {}
    sa_store_dict = {}
    stock_share_store = {}

    logic_row = ttk.Frame(stock_form, style="Card.TFrame")
    logic_row.pack(fill=tk.X, pady=6)
    ttk.Label(logic_row, text="逻辑仓:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    logic_store_var = tk.StringVar(value="请选择逻辑仓")
    logic_store_combo = ttk.Combobox(logic_row, textvariable=logic_store_var, state="readonly", width=34, font=UI_FONT)
    logic_store_combo.pack(side=tk.LEFT)

    sa_row = ttk.Frame(stock_form, style="Card.TFrame")
    sa_row.pack(fill=tk.X, pady=6)
    ttk.Label(sa_row, text="配销仓:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    sa_store_var = tk.StringVar(value="请先选择逻辑仓")
    sa_store_combo = ttk.Combobox(sa_row, textvariable=sa_store_var, state="readonly", width=34, font=UI_FONT)
    sa_store_combo.pack(side=tk.LEFT)
    share_store_var = tk.StringVar(value="聚合仓: -")
    ttk.Label(sa_row, textvariable=share_store_var, style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(10, 0))

    sku_row = ttk.Frame(stock_form, style="Card.TFrame")
    sku_row.pack(fill=tk.X, pady=6)
    ttk.Label(sku_row, text="SKU:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    stock_sku_entry = ttk.Entry(sku_row, width=34)
    stock_sku_entry.pack(side=tk.LEFT)
    ttk.Label(sku_row, text="（手工录入 SKU 编码）", style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(10, 0))

    qty_row = ttk.Frame(stock_form, style="Card.TFrame")
    qty_row.pack(fill=tk.X, pady=6)
    ttk.Label(qty_row, text="调整数量:", style="Card.TLabel", font=UI_FONT, width=15).pack(side=tk.LEFT, anchor="e", padx=(0, 10))
    stock_qty_entry = ttk.Entry(qty_row, width=14)
    stock_qty_entry.insert(0, "1")
    stock_qty_entry.pack(side=tk.LEFT)

    stock_btn_row = ttk.Frame(stock_form, style="Card.TFrame")
    stock_btn_row.pack(fill=tk.X, pady=8)
    stock_exec_btn = ttk.Button(stock_btn_row, text="执行调整库存", style="Primary.TButton")
    stock_exec_btn.pack(side=tk.LEFT, padx=(0, 8))
    stock_clear_btn = ttk.Button(stock_btn_row, text="清空结果", style="Secondary.TButton")
    stock_clear_btn.pack(side=tk.LEFT)
    stock_status_var = tk.StringVar(value="就绪")
    ttk.Label(stock_btn_row, textvariable=stock_status_var, style="Muted.TLabel").pack(side=tk.RIGHT)

    stock_result_frame = ttk.LabelFrame(stock_adjust_frame, text=" 操作结果 ", style="Card.TLabelframe")
    stock_result_frame.pack(fill=tk.BOTH, expand=True)
    stock_result_text = scrolledtext.ScrolledText(stock_result_frame, wrap=tk.WORD, padx=10, pady=10)
    configure_log_text(stock_result_text, mono=MONO)
    stock_result_text.pack(fill=tk.BOTH, expand=True)

    def _stock_env_key():
        return env_options.get(stock_env_var.get(), DEFAULT_ENV)

    def load_logic_stores():
        stock_status_var.set("正在加载逻辑仓...")
        root.update()
        manager = None
        try:
            manager = StockAdjustToolManager(_stock_env_key())
            stores = manager.get_logic_stores()
            logic_store_dict.clear()
            names = ["请选择逻辑仓"]
            logic_store_dict["请选择逻辑仓"] = None
            for store in stores:
                name = f"{store.get('ecode', '')} / {store.get('ename', '')} (ID: {store.get('id')})"
                names.append(name)
                logic_store_dict[name] = store
            logic_store_combo["values"] = names
            logic_store_var.set(names[0])
            sa_store_combo["values"] = ["请先选择逻辑仓"]
            sa_store_var.set("请先选择逻辑仓")
            share_store_var.set("聚合仓: -")
            stock_status_var.set(f"已加载 {len(stores)} 个逻辑仓")
        except Exception as e:
            stock_status_var.set(f"加载逻辑仓失败: {e}")
        finally:
            if manager:
                manager.close()

    def on_logic_store_selected(event=None):
        selected = logic_store_dict.get(logic_store_var.get())
        if not selected:
            return
        manager = None
        try:
            stock_status_var.set("正在加载配销仓...")
            root.update()
            manager = StockAdjustToolManager(_stock_env_key())
            share_store, sa_stores = manager.get_sa_stores_by_logic_store(selected.get("id"))
            stock_share_store.clear()
            if share_store:
                stock_share_store.update(share_store)
                share_store_var.set(f"聚合仓: {share_store.get('ecode', '')} / {share_store.get('ename', '')} (ID: {share_store.get('id')})")
            else:
                share_store_var.set("聚合仓: 未找到")
            sa_store_dict.clear()
            names = ["请选择配销仓"]
            sa_store_dict["请选择配销仓"] = None
            for store in sa_stores:
                name = f"{store.get('ecode', '')} / {store.get('ename', '')} (ID: {store.get('id')})"
                names.append(name)
                sa_store_dict[name] = store
            sa_store_combo["values"] = names
            sa_store_var.set(names[0])
            stock_status_var.set(f"已加载 {len(sa_stores)} 个配销仓")
        except Exception as e:
            stock_status_var.set(f"加载配销仓失败: {e}")
        finally:
            if manager:
                manager.close()

    def _format_stock_adjust_result(result: Dict[str, Any]) -> str:
        lines = []
        for item in result.get("steps", []):
            line = f"{item.get('step', '-')}{item.get('result', '-')}"
            if not item.get("success") and item.get("reason"):
                line += f"；失败原因：{item['reason']}"
            lines.append(line)
        return "\n".join(lines) if lines else json.dumps(result, ensure_ascii=False, indent=2)

    def execute_stock_adjust():
        logic_store = logic_store_dict.get(logic_store_var.get())
        sa_store = sa_store_dict.get(sa_store_var.get())
        sku_ecode = stock_sku_entry.get().strip()
        qty = stock_qty_entry.get().strip()
        if not logic_store:
            messagebox.showwarning("提示", "请选择逻辑仓")
            return
        if not stock_share_store:
            messagebox.showwarning("提示", "当前逻辑仓未找到聚合仓")
            return
        if not sa_store:
            messagebox.showwarning("提示", "请选择配销仓")
            return
        if not sku_ecode:
            messagebox.showwarning("提示", "请输入 SKU")
            return
        try:
            float(qty)
        except Exception:
            messagebox.showwarning("提示", "调整数量必须是数字")
            return
        stock_exec_btn.config(state=tk.DISABLED)
        stock_status_var.set("正在执行库存调整...")
        root.update()
        manager = None
        try:
            manager = StockAdjustToolManager(_stock_env_key())
            stock_result_text.delete("1.0", tk.END)

            def _on_step(item: Dict[str, Any]) -> None:
                line = f"{item.get('step', '-')}{item.get('result', '-')}"
                if not item.get("success") and item.get("reason"):
                    line += f"；失败原因：{item['reason']}"
                stock_result_text.insert(tk.END, line + "\n")
                stock_result_text.see(tk.END)
                root.update()

            result = manager.execute_flow(
                logic_store, dict(stock_share_store), sa_store, sku_ecode, qty, on_step=_on_step
            )
            stock_status_var.set("执行完成，请查看响应")
        except Exception as e:
            error_text = f"执行失败: {e}"
            stock_result_text.delete("1.0", tk.END)
            stock_result_text.insert(tk.END, error_text)
            stock_status_var.set(error_text)
            show_copyable_dialog("执行失败", error_text)
        finally:
            if manager:
                manager.close()
            stock_exec_btn.config(state=tk.NORMAL)

    def _sync_stock_env(event=None):
        db_var.set(stock_env_var.get())
        selected_env_label.set(stock_env_var.get())
        _update_env_info()
        load_logic_stores()

    stock_env_combo.bind("<<ComboboxSelected>>", _sync_stock_env)
    logic_store_combo.bind("<<ComboboxSelected>>", on_logic_store_selected)
    stock_exec_btn.config(command=execute_stock_adjust)
    stock_clear_btn.config(command=lambda: (stock_result_text.delete("1.0", tk.END), stock_status_var.set("就绪")))
    load_logic_stores()

    # ==================== 公域平台造单标签页 ====================
    platform_order_frame = ttk.Frame(notebook, style="Main.TFrame", padding=CONTENT_PADDING)
    notebook.add(platform_order_frame, text="公域平台造单")
    tab_frames["platform_order"] = platform_order_frame

    from platform_order_gui import PlatformOrderApp

    platform_order_app = PlatformOrderApp(platform_order_frame, standalone=False, auto_load=False)

    # ==================== 公域平台逆向造单标签页 ====================
    reverse_order_frame = ttk.Frame(notebook, style="Main.TFrame", padding=CONTENT_PADDING)
    notebook.add(reverse_order_frame, text="公域平台逆向造单")
    tab_frames["reverse_order"] = reverse_order_frame

    from reverse_order_gui import ReverseOrderApp

    ReverseOrderApp(reverse_order_frame, standalone=False)

    # ==================== 基础数据标签页 ====================
    basic_data_frame = ttk.Frame(notebook, style="Main.TFrame", padding=CONTENT_PADDING)
    notebook.add(basic_data_frame, text="基础数据")
    tab_frames["basic_data"] = basic_data_frame

    basic_header_frame = ttk.Frame(basic_data_frame, style="Main.TFrame", padding=(0, 0, 0, 6))
    basic_header_frame.pack(fill=tk.X, pady=(0, SECTION_GAP))
    tk.Label(basic_header_frame, text="基础数据", font=UI_FONT_MODULE, fg=TEXT_MAIN, bg=MAIN_BG).pack(side=tk.LEFT)
    tk.Label(basic_header_frame, text="· 逻辑仓档案 / 库存快照", font=UI_FONT, fg=INFO_BLUE, bg=MAIN_BG).pack(side=tk.LEFT, padx=(8, 0))

    basic_tip = tk.Label(
        basic_data_frame,
        text="先点击「查询逻辑仓」和「查询逻辑仓库存」刷新基础数据。",
        font=UI_FONT_SM, fg=TEXT_MUTED, bg=MAIN_BG, justify=tk.LEFT,
    )
    basic_tip.pack(fill=tk.X, pady=(0, SECTION_GAP))

    basic_btn_row = ttk.Frame(basic_data_frame, style="Main.TFrame")
    basic_btn_row.pack(fill=tk.X, pady=(0, SECTION_GAP))
    basic_stores_btn = ttk.Button(basic_btn_row, text="查询逻辑仓", style="Info.TButton")
    basic_stores_btn.pack(side=tk.LEFT, padx=(0, 8))
    basic_inventory_btn = ttk.Button(basic_btn_row, text="查询逻辑仓库存", style="Info.TButton")
    basic_inventory_btn.pack(side=tk.LEFT, padx=(0, 8))
    basic_status_var = tk.StringVar(value="就绪")
    tk.Label(basic_btn_row, textvariable=basic_status_var, font=UI_FONT_SM, fg=TEXT_MUTED, bg=MAIN_BG).pack(side=tk.LEFT, padx=(8, 0))

    basic_tables_frame = ttk.Frame(basic_data_frame, style="Main.TFrame")
    basic_tables_frame.pack(fill=tk.BOTH, expand=True)

    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT_MAIN, rowheight=26)
    style.configure("Treeview.Heading", background=NAV_BG, foreground=NAV_FG, font=UI_FONT)

    def _make_basic_table(parent, title):
        """创建带标题、可独立翻页的表格区。"""
        box = ttk.Frame(parent, style="Main.TFrame")
        box.pack(fill=tk.BOTH, expand=True, pady=(0, SECTION_GAP))
        tk.Label(box, text=title, font=UI_FONT, fg=TEXT_MAIN, bg=MAIN_BG).pack(anchor=tk.W, pady=(0, 4))
        holder = ttk.Frame(box, style="Main.TFrame")
        holder.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(holder, show="headings")
        vscroll = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview)
        hscroll = ttk.Scrollbar(holder, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        def _on_double_click_copy(event):
            """双击单元格：复制该单元格内容到剪贴板。"""
            t = event.widget
            row_id = t.identify_row(event.y)
            col_id = t.identify_column(event.x)
            if not row_id or not col_id:
                return
            col_index = int(col_id.replace("#", "")) - 1
            values = t.item(row_id, "values")
            if values and 0 <= col_index < len(values):
                text = str(values[col_index])
                try:
                    top = t.winfo_toplevel()
                    top.clipboard_clear()
                    top.clipboard_append(text)
                except Exception:
                    pass

        tree.bind("<Double-Button-1>", _on_double_click_copy)
        return tree

    # 逻辑仓档案、逻辑仓库存两个查询结果分开展示，避免互相覆盖
    basic_stores_tree = _make_basic_table(basic_tables_frame, "逻辑仓档案")
    basic_inventory_tree = _make_basic_table(basic_tables_frame, "逻辑仓库存")

    def _fill_basic_table(tree, columns, rows):
        tree["columns"] = [c["id"] for c in columns]
        for c in columns:
            tree.heading(c["id"], text=c["text"])
            tree.column(c["id"], width=c.get("width", 140), anchor=tk.W, stretch=True)
        for item in tree.get_children():
            tree.delete(item)
        for r in rows:
            values = [str(r.get(c["id"], "") if r.get(c["id"]) is not None else "") for c in columns]
            tree.insert("", tk.END, values=values)

    def _basic_env_key():
        return env_options.get(selected_env_label.get(), DEFAULT_ENV)

    def _query_basic_stores():
        basic_stores_btn.config(state=tk.DISABLED)
        basic_inventory_btn.config(state=tk.DISABLED)
        basic_status_var.set("正在查询逻辑仓档案...")
        root.update()

        def _worker():
            manager = None
            rows = []
            error = None
            try:
                manager = StockAdjustToolManager(_basic_env_key())
                rows = manager.query_logic_store_archive()
            except Exception as exc:
                error = str(exc)
            finally:
                if manager:
                    manager.close()
            root.after(0, lambda: _finish_basic_stores(rows, error))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_basic_stores(rows, error):
        basic_stores_btn.config(state=tk.NORMAL)
        basic_inventory_btn.config(state=tk.NORMAL)
        if error:
            basic_status_var.set(f"查询逻辑仓失败: {error}")
            messagebox.showerror("查询失败", error)
            return
        if not rows:
            basic_status_var.set("逻辑仓档案为空")
            messagebox.showinfo("提示", "逻辑仓档案为空")
            return
        set_basic_data(_basic_env_key(), logic_stores=rows)
        columns = [
            {"id": "ecode", "text": "逻辑仓编码", "width": 180},
            {"id": "ename", "text": "名称", "width": 240},
            {"id": "brand", "text": "品牌", "width": 160},
        ]
        _fill_basic_table(basic_stores_tree, columns, rows)
        basic_status_var.set(f"已查询 {len(rows)} 个逻辑仓，并写入基础数据")

    def _query_basic_inventory():
        basic_stores_btn.config(state=tk.DISABLED)
        basic_inventory_btn.config(state=tk.DISABLED)
        basic_status_var.set("正在查询逻辑仓库存...")
        root.update()

        def _worker():
            manager = None
            rows = []
            error = None
            try:
                manager = StockAdjustToolManager(_basic_env_key())
                rows = manager.query_logic_store_inventory()
            except Exception as exc:
                error = str(exc)
            finally:
                if manager:
                    manager.close()
            root.after(0, lambda: _finish_basic_inventory(rows, error))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_basic_inventory(rows, error):
        basic_stores_btn.config(state=tk.NORMAL)
        basic_inventory_btn.config(state=tk.NORMAL)
        if error:
            basic_status_var.set(f"查询逻辑仓库存失败: {error}")
            messagebox.showerror("查询失败", error)
            return
        set_basic_data(_basic_env_key(), logic_store_inventory=rows)
        columns = [
            {"id": "ecode", "text": "逻辑仓编码", "width": 180},
            {"id": "ename", "text": "名称", "width": 240},
            {"id": "ps_c_sku_ecode", "text": "SKU", "width": 220},
            {"id": "qty_available", "text": "可用库存", "width": 120},
        ]
        _fill_basic_table(basic_inventory_tree, columns, rows)
        basic_status_var.set(f"已查询 {len(rows)} 条逻辑仓库存记录，并写入基础数据")

    basic_stores_btn.config(command=_query_basic_stores)
    basic_inventory_btn.config(command=_query_basic_inventory)

    _base_select_nav = select_nav

    def select_nav(item_id):
        _base_select_nav(item_id)
        if item_id == "platform_order":
            platform_order_app.ensure_master_data(silent=True)

    # ==================== 标签页切换同步导航 ====================
    tab_nav_map = {
        0: "order_copy",
        1: "stock_adjust",
        2: "platform_order",
        3: "reverse_order",
        4: "basic_data",
    }

    def on_tab_change(event):
        """标签页切换时同步左侧导航。"""
        current_tab = notebook.index(notebook.select())
        nav_id = tab_nav_map.get(current_tab)
        if nav_id:
            current_nav.set(nav_id)
        if nav_id == "platform_order":
            platform_order_app.ensure_master_data(silent=True)

    notebook.bind("<<NotebookTabChanged>>", on_tab_change)
    update_nav_style()

    if initial_tab and initial_tab in tab_frames:
        select_nav(initial_tab)

    root.mainloop()


if __name__ == "__main__":
    tab_arg = None
    if "--tab" in sys.argv:
        idx = sys.argv.index("--tab")
        if idx + 1 < len(sys.argv):
            tab_arg = sys.argv[idx + 1].strip()
    _run_gui(initial_tab=tab_arg)