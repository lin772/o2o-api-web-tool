# -*- coding: utf-8 -*-
"""公域订单直写 IP 中间表：淘宝 ip_b_taobao_order / 京东 ip_b_jingdong_order。

复现 oms-ip-hub Java 侧「MQ 监听 -> 解析报文 -> checkJsonkeycache 白名单过滤
-> checkOrderJson 字段重命名/追加 -> saveMasterTable」逻辑，仅在工具 Python 侧落主表，
不投真 MQ、不改 Java 业务代码。

范围：仅落淘宝/京东两张主表；standplat 平台继续走 order_service.insert_source。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from order_service import db_connect
from settings import get_env_config, load_config

# ---------------------------------------------------------------------------
# 白名单（omsTableFieldRelation.json insertcols，逐字保留）
# ---------------------------------------------------------------------------
TAOBAO_TABLE = "ip_b_taobao_order"
TAOBAO_WHITELIST = (
    "num,point_fee,seller_flag,buyer_obtain_point_fee,real_point_fee,created,pay_time,"
    "modified,end_time,alipay_id,is_lgtype,is_brand_sale,is_force_wlb,buyer_rate,seller_rate,"
    "tid,num_iid,status,title,type,price,discount_fee,total_fee,receiver_mobile,"
    "receiver_phonereceived_payment,seller_memo,receiver_name,receiver_state,receiver_city,"
    "receiver_district,receiver_address,receiver_zip,commission_fee,seller_nick,pic_path,"
    "payment,post_fee,send_time,adjust_fee,cod_fee,trade_from,cod_status,buyer_message,"
    "credit_card_fee,alipay_no,buyer_nick,buyer_email,shipping_type,step_trade_status,"
    "step_paid_fee,et_shop_id,tag,buyer_alipay_no,order_tax_fee,is_wmly,omni_package,oaid,"
    "basic_price,expand_price,basic_price_used,expand_price_used"
)

JINGDONG_TABLE = "ip_b_jingdong_order"
JINGDONG_WHITELIST = (
    "paymentconfirmtime,orderremark,invoiceinfo,deliverytype,orderstateremark,orderstate,"
    "paytype,orderid,sellerdiscount,freightprice,ordersellerprice,orderpayment,ordertotalprice,"
    "orderendtime,orderstarttime,venderremark,payment_typestr,should_pay,partner,sorting_code,"
    "cky2_name,bf_deli_good_glag,pin,venderid,storeorder,nick,modified,storeid,balanceused,ordersign"
)

# 淘宝 9 个国补字段（TbOrderDaoCmdImpl.saveOrder 局部变量）
TAOBAO_GOV_FIELDS = (
    "use_gov_subsidy",
    "gov_sn_check",
    "tmall_coupon_fee",
    "gov_subsidy_amount_exact",
    "gov_main_subject",
    "gov_subsidy_type",
    "gov_subsidy_type_extra",
    "ypds_payment",
    "is_daixiao",
)

# 京东 camelCase -> snake_case 重命名（JdOrderDaoCmdImpl.checkOrderJson）
JD_RENAME_MAP = {
    "paymentConfirmTime": "payment_confirm_time",
    "orderRemark": "order_remark",
    "invoiceInfo": "invoice_info",
    "deliveryType": "delivery_type",
    "orderStateRemark": "order_state_remark",
    "storeOrder": "store_order",
    "orderState": "order_state",
    "payType": "pay_type",
    "orderId": "order_id",
    "sellerDiscount": "seller_discount",
    "freightPrice": "freight_price",
    "orderSellerPrice": "order_seller_price",
    "orderPayment": "order_payment",
    "orderTotalPrice": "order_total_price",
    "orderEndTime": "order_end_time",
    "orderStartTime": "order_start_time",
    "venderId": "vender_id",
}

# orderPrintData(attrObj) -> 主表字段（保留 source key 大小写，即 payment_typeStr 为 camelCase）
JD_ATTR_FIELDS = ("payment_typeStr", "should_pay", "partner", "sorting_code", "cky2_name", "bf_deli_good_glag")

# itemInfoList[0].itemExt -> 主表字段
JD_ITEMEXT_MAP = {
    "pop_supply_shop_id": "popSupplyShopId",
    "pop_supply_shop_name": "popSupplyShopName",
    "pop_supply_company_name": "popSupplyCompanyName",
    "pop_supply_company_id": "popSupplyCompanyId",
}

_RESERVE_VARCHAR05_SIZE = 60000


def _split_whitelist(whitelist: str) -> set:
    return {tok.strip().lower() for tok in whitelist.split(",") if tok.strip()}


def check_jsonkey_cache(obj: Dict[str, Any], whitelist: str) -> Dict[str, Any]:
    """复现 PackageJsonSqlUtil.checkJsonkeycache：caseless 白名单过滤 + 删空串/字面 null。"""
    keys = _split_whitelist(whitelist)
    result: Dict[str, Any] = {}
    for key, value in obj.items():
        if str(key).lower() not in keys:
            continue
        if str(key) != "merged_code":
            if isinstance(value, str) and value == "":
                continue
            if isinstance(value, str) and value.lower() == "null":
                continue
        result[key] = value
    return result


def _delete_none(obj: Dict[str, Any]) -> None:
    for key in [k for k, v in obj.items() if v is None]:
        del obj[key]


def _extract_taobao_trade(raw: Any) -> Optional[Dict[str, Any]]:
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            trade = item["jdp_response"]["trade_fullinfo_get_response"]["trade"]
            if isinstance(trade, dict):
                return trade
        except (KeyError, TypeError):
            continue
    return None


def _extract_jd_orders(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        oi = raw.get("orderInfoList")
        if isinstance(oi, list):
            return [o for o in oi if isinstance(o, dict)]
        if "orderId" in raw:
            return [raw]
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for item in raw:
            out.extend(_extract_jd_orders(item))
        return out
    return []


def _jd_buy_address_id(order: Dict[str, Any]) -> Any:
    bid = order.get("buy_address_id")
    if bid is not None and str(bid) != "":
        return bid
    consignee = order.get("consigneeInfo")
    if isinstance(consignee, dict):
        bid = consignee.get("buy_address_id")
        if bid is None or str(bid) == "":
            bid = consignee.get("oaid")
        return bid
    return None


def _parse_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0


def _to_long(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None


def build_taobao_order_row(raw: Any) -> Tuple[Dict[str, Any], str]:
    """复现 TbOrderDaoCmdImpl.saveOrder：返回 (row, tid)。"""
    trade = _extract_taobao_trade(raw)
    if not isinstance(trade, dict):
        raise ValueError("淘宝报文缺少 jdp_response.trade_fullinfo_get_response.trade")

    # 先取 9 个国补字段局部变量（白名单过滤前）
    gov = {k: trade.get(k) for k in TAOBAO_GOV_FIELDS}

    row = check_jsonkey_cache(trade, TAOBAO_WHITELIST)

    # use_gov_subsidy 补值
    use_gov_subsidy = gov["use_gov_subsidy"]
    if use_gov_subsidy is None:
        row["use_gov_subsidy"] = "0"
        if gov["gov_main_subject"] is not None:
            row["use_gov_subsidy"] = 1
    else:
        row["use_gov_subsidy"] = use_gov_subsidy

    # 其余国补字段回填
    for k in TAOBAO_GOV_FIELDS:
        if k != "use_gov_subsidy":
            row[k] = gov[k]

    # checkOrderJson：3plTiming 不在白名单，过滤后取值为 null，最终被 deleteNullKeyForObj 删除
    row["THREEPLTIMING"] = row.get("3plTiming")
    row.pop("3plTiming", None)
    row["istrans"] = 0

    if str(row.get("step_trade_status") or "") == "FRONT_PAID_FINAL_NOPAID":
        row["presale_deposit_time"] = row.get("pay_time")

    _delete_none(row)

    tid = trade.get("tid")
    if tid is None:
        raise ValueError("淘宝 trade 缺少 tid")
    return row, str(tid)


def build_jingdong_order_row(raw: Any) -> Tuple[Dict[str, Any], str]:
    """复现 JdOrderDaoCmdImpl.saveOrder：返回 (row, order_id)。"""
    orders = _extract_jd_orders(raw)
    if not orders:
        raise ValueError("京东报文缺少 orderInfoList")
    order = orders[0]

    order_str = json.dumps(order, ensure_ascii=False)
    direct_parent_order_id = order.get("directParentOrderId")
    buy_address_id = _jd_buy_address_id(order)

    sendpay_map = order.get("sendpayMap")
    sendpay_str: Optional[str] = None
    if isinstance(sendpay_map, dict):
        sendpay_str = json.dumps(sendpay_map, ensure_ascii=False)
    elif isinstance(sendpay_map, str) and sendpay_map.strip():
        sendpay_str = sendpay_map

    order_ext = order.get("orderExt") if isinstance(order.get("orderExt"), dict) else None

    row = check_jsonkey_cache(order, JINGDONG_WHITELIST)

    row["istrans"] = 0
    for camel, snake in JD_RENAME_MAP.items():
        row[snake] = row.get(camel)
        row.pop(camel, None)

    # orderPrintData(attrObj) -> 主表字段（保留 source key 大小写）
    order_print = _parse_json_dict(order.get("orderPrintData"))
    if order_print:
        for f in JD_ATTR_FIELDS:
            v = order_print.get(f)
            if v is not None:
                row[f] = v

    # itemInfoList[0].itemExt
    item_info_list = order.get("itemInfoList")
    if isinstance(item_info_list, list) and item_info_list:
        first = item_info_list[0]
        if isinstance(first, dict):
            item_ext = _parse_json_dict(first.get("itemExt"))
            for snake, camel in JD_ITEMEXT_MAP.items():
                v = item_ext.get(camel)
                if v is not None:
                    row[snake] = v

    if buy_address_id is not None and str(buy_address_id) != "":
        row["buy_address_id"] = buy_address_id

    if sendpay_str:
        row["send_pay_map"] = sendpay_str
        sendpay_obj = _parse_json_dict(sendpay_str)
        value1 = _to_long(sendpay_obj.get("687"))
        value2 = _to_long(sendpay_obj.get("1064"))
        if (value1 is not None and value1 >= 52) or (value2 is not None and value2 > 0):
            row["is_country_repair"] = 1
        else:
            row["is_country_repair"] = 0
        value3 = _to_long(sendpay_obj.get("1086"))
        row["consolidate_type"] = "1" if (value3 is not None and value3 == 1) else "0"
    else:
        row["is_country_repair"] = 0
        row["consolidate_type"] = "0"

    if order_ext:
        gov_flag = order_ext.get("isGovSubsidyOrder")
        if gov_flag is True or (isinstance(gov_flag, str) and gov_flag.strip().lower() == "true"):
            row["is_country_repair"] = "1"

    row["reserve_bigint01"] = direct_parent_order_id
    row["reserve_varchar05"] = order_str[:_RESERVE_VARCHAR05_SIZE]

    _delete_none(row)

    order_id = order.get("orderId")
    if order_id is None:
        raise ValueError("京东 orderInfoList[0] 缺少 orderId")
    return row, str(order_id)


def insert_master_table(env_key: str, table_name: str, row: Dict[str, Any], shop: Dict[str, Any]) -> int:
    """复现 PackageJsonSqlUtil.saveMasterTable：落一张主表（淘宝/京东），返回主键 ID。"""
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    conn = db_connect(env["order_db"], cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table_name}")
            cols_map = {str(r["Field"]).lower(): r["Field"] for r in cur.fetchall()}

        id_col = cols_map.get("id")
        if id_col is None:
            raise ValueError(f"{table_name} 缺少 id 主键列")

        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX({id_col}) AS m FROM {table_name}")
            mid = int((cur.fetchone() or {}).get("m") or 0) + 1

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        candidate = {**row, "ID": mid, "insertdate": now, "cp_c_shop_id": shop["id"]}

        data: Dict[str, Any] = {}
        for k, v in candidate.items():
            if v is None:
                continue
            real_col = cols_map.get(str(k).lower())
            if real_col is None:
                continue
            data[real_col] = v

        if not data:
            raise ValueError(f"{table_name} 无可插入字段")

        columns = ", ".join(data)
        placeholders = ", ".join(["%s"] * len(data))
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})", tuple(data.values()))

        conn.commit()
        return mid
    finally:
        conn.close()


def insert_taobao_order(env_key: str, raw: Any, shop: Dict[str, Any]) -> Tuple[int, str]:
    row, tid = build_taobao_order_row(raw)
    mid = insert_master_table(env_key, TAOBAO_TABLE, row, shop)
    return mid, tid


def insert_jingdong_order(env_key: str, raw: Any, shop: Dict[str, Any]) -> Tuple[int, str]:
    row, order_id = build_jingdong_order_row(raw)
    mid = insert_master_table(env_key, JINGDONG_TABLE, row, shop)
    return mid, order_id