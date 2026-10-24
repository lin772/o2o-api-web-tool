# -*- coding: utf-8 -*-
"""公域逆向造单：查零售发货单、写 ip_b_platform_return_order、触发售后转换。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from oms_client import OmsClient
from order_service import _with_db_retry, db_connect, format_db_error
from reverse_message_service import ReverseBuildResult, build_reverse_preview
from settings import get_env_config, get_platform_meta, load_config, platform_display_name


def query_retail_order(env_key: str, tid: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """按平台单号查零售发货单主表 + 明细（对齐 ocBOrderMapper.selectOrderByTid）。"""
    tid = (tid or "").strip()
    if not tid:
        raise ValueError("平台单号不能为空")

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tid, cp_c_shop_id, platform, user_nick AS buyer_nick, pay_time, "
                "delivery_method, is_o2o_order, receiver_name, receiver_mobile, receiver_phone, "
                "receiver_address, cp_c_region_province_ename, cp_c_region_city_ename, "
                "cp_c_region_area_ename, cp_c_region_town_ename, "
                "cp_c_shop_seller_nick AS seller_nick "
                "FROM oc_b_order WHERE tid = %s AND isactive = 'Y' ORDER BY id DESC LIMIT 1",
                (tid,),
            )
            order = cur.fetchone()
            if not order:
                raise ValueError(f"未找到零售发货单 tid={tid}")
            cur.execute(
                "SELECT id, ooid, sku_numiid AS pt_sku_num_iid, num_iid AS pt_num_iid, "
                "ps_c_sku_ecode, ps_c_pro_ename, pt_pro_name, real_amt, price, qty, is_gift "
                "FROM oc_b_order_item WHERE oc_b_order_id = %s AND isactive = 'Y' ORDER BY id",
                (order["id"],),
            )
            items = cur.fetchall() or []
            return order, items

    return _with_db_retry(env_key, "order_db", run)


def _resolve_shop(env_key: str, shop_id: Any, seller_hint: str = "") -> Dict[str, Any]:
    if not shop_id:
        return {"seller_nick": seller_hint, "id": None}

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ecode, seller_nick, cp_c_shop_title, cp_c_platform_id, "
                "ad_client_id, ad_org_id FROM cp_c_shop WHERE id = %s AND isactive = 'Y'",
                (shop_id,),
            )
            return cur.fetchone()

    row = _with_db_retry(env_key, "base_db", run)
    if not row:
        return {"seller_nick": seller_hint, "id": shop_id}
    return row


def _retail_order_platform_id(order: Dict[str, Any], shop: Optional[Dict[str, Any]] = None) -> Optional[int]:
    raw = order.get("platform")
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    if shop and shop.get("cp_c_platform_id") is not None:
        return int(shop["cp_c_platform_id"])
    return None


def validate_retail_order_platform(
    order: Dict[str, Any],
    selected_platform: str,
    shop: Optional[Dict[str, Any]] = None,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> None:
    """校验零售发货单平台与界面所选平台一致（oc_b_order.platform）。"""
    cfg = cfg or load_config()
    expected_id = get_platform_meta(selected_platform, cfg).get("platform_id")
    if expected_id is None:
        raise ValueError(f"平台「{selected_platform}」未在 config.yaml 的 platform_meta 中配置 platform_id")
    expected_id = int(expected_id)
    actual_id = _retail_order_platform_id(order, shop)
    if actual_id is None:
        raise ValueError(
            f"零售发货单 tid={order.get('tid')} 未记录平台信息（platform 为空），无法校验是否与「{selected_platform}」一致"
        )
    if actual_id != expected_id:
        actual_label = platform_display_name(actual_id, cfg)
        raise ValueError(
            f"平台单号 tid={order.get('tid')} 对应零售发货单平台为「{actual_label}」"
            f"（platform={actual_id}），与当前所选平台「{selected_platform}」"
            f"（platform={expected_id}）不一致，请核对平台或平台单号后重试"
        )


def ensure_retail_order_platform_match(
    env_key: str,
    selected_platform: str,
    tid: str,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """查询零售发货单并校验平台与界面所选一致。"""
    cfg = cfg or load_config()
    order, items = query_retail_order(env_key, tid)
    shop = _resolve_shop(env_key, order.get("cp_c_shop_id"), order.get("seller_nick") or "")
    validate_retail_order_platform(order, selected_platform, shop, cfg=cfg)
    return order, items


def build_reverse_message(
    env_key: str,
    platform: str,
    scenario: str,
    tid: str,
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    cfg = load_config()
    order, items = ensure_retail_order_platform_match(env_key, platform, tid, cfg=cfg)
    shop = _resolve_shop(env_key, order.get("cp_c_shop_id"), order.get("seller_nick") or "")
    if shop.get("seller_nick"):
        order["seller_nick"] = shop["seller_nick"]
    return build_reverse_preview(platform, scenario, tid, order, items, return_no=return_no)


def insert_platform_return_order(env_key: str, build: ReverseBuildResult) -> int:
    """写入 ip_b_platform_return_order（original_message 仅存 data[0]，与 MQ 一致）。"""
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    shop = _resolve_shop(env_key, build.shop_id, build.seller_nick)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    original = build.storage_original_message()

    order_conn = db_connect(env["order_db"], cfg)
    try:
        row = {
            "cp_c_shop_id": shop.get("id") or build.shop_id,
            "seller_nick": shop.get("seller_nick") or build.seller_nick,
            "cp_c_platform_id": build.platform_id,
            "tid": build.tid,
            "return_no": build.return_no,
            "original_message": original,
            "platform_interface_name": build.platform_interface_name,
            "istrans": 0,
            "istrans_count": 0,
            "isactive": "Y",
            "creationdate": now,
            "modifieddate": now,
        }
        defaults = cfg.get("return_insert_defaults") or cfg.get("source_defaults") or {}
        for k in ("ad_client_id", "ad_org_id", "ownerid"):
            if k in defaults:
                row[k] = defaults[k]
        if shop.get("ad_client_id") is not None:
            row["ad_client_id"] = shop["ad_client_id"]
        if shop.get("ad_org_id") is not None:
            row["ad_org_id"] = shop["ad_org_id"]

        with order_conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM ip_b_platform_return_order")
            cols = {r["Field"] for r in cur.fetchall()}
            data = {k: v for k, v in row.items() if k in cols}
            cur.execute(
                f"INSERT INTO ip_b_platform_return_order ({', '.join(data)}) "
                f"VALUES ({', '.join(['%s'] * len(data))})",
                tuple(data.values()),
            )
            row_id = cur.lastrowid
        order_conn.commit()
        return row_id
    finally:
        order_conn.close()


def run_reverse_transfer(
    env_key: str, return_order_id: int, on_step: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    """平台售后申请中间表 -> 售后申请单（ocBPlatformReturnOrder/batchConvert）。"""
    def emit(msg: str) -> None:
        if on_step is not None:
            on_step(msg)

    cfg = load_config()
    env = get_env_config(env_key, cfg)
    transfer_cfg = cfg.get("reverse_transfer") or cfg.get("transfer") or {}
    oms_cfg = env["oms"]
    login = oms_cfg.get("login", {})
    timeout = int(transfer_cfg.get("poll_timeout_seconds", 45))
    result: Dict[str, Any] = {"return_order_id": return_order_id, "steps": []}

    if not transfer_cfg.get("auto_batch_convert", True):
        emit("▸ 已跳过售后转换（reverse_transfer.auto_batch_convert=false）")
        result["steps"].append("已跳过售后转换（reverse_transfer.auto_batch_convert=false）")
        return result

    emit("▸ 调用售后转换接口(batchConvert)中。。")
    client = OmsClient(oms_cfg["base_url"], login.get("username", ""), login.get("password", ""))
    client.login()
    code, body = client.batch_convert_platform_return(return_order_id)
    emit(f"▸ 售后转换接口已提交（http={code}）")
    result["steps"].append({"batchConvert": {"http": code, "body": body}})

    order_conn = db_connect(env["order_db"], cfg)
    try:
        deadline = time.time() + timeout
        round_no = 0
        while time.time() < deadline:
            round_no += 1
            with order_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, return_no, tid, istrans FROM ip_b_platform_return_order WHERE id = %s",
                    (return_order_id,),
                )
                po = cur.fetchone()
                if po and int(po.get("istrans") or 0) == 2:
                    result["platform_return_order"] = po
                    emit("▸ 售后转换完成（平台售后申请单已生成）")
                    return result
                cur.execute(
                    "SELECT id, return_no, tid FROM oc_b_after_sale_apply_order "
                    "WHERE return_no = %s ORDER BY id DESC LIMIT 1",
                    (po.get("return_no") if po else "",),
                )
                apply = cur.fetchone()
                if apply:
                    result["after_sale_apply_order"] = apply
                    emit("▸ 售后转换完成（售后申请单已生成）")
                    return result
            emit(f"▸ 等待售后转换结果中（第 {round_no} 轮查询）")
            time.sleep(2)
    finally:
        order_conn.close()

    emit("▸ 售后转换超时，未查询到转换结果")
    return result
