# -*- coding: utf-8 -*-
"""公域订单直写 IP 中间表：淘宝/京东 主表 + 明细子表。

复现 oms-ip-hub Java 侧「MQ 监听 -> 解析报文 -> checkJsonkeycache 白名单过滤
-> checkOrder*Json 字段重命名/追加 -> saveMasterTable/saveChildTable」逻辑，
仅在工具 Python 侧落主表与明细，不投真 MQ、不改 Java 业务代码。

落库范围：
- 淘宝 ip_b_taobao_order + ip_b_taobao_order_item（含主赠品关联）+ ip_b_taobao_order_promotion；
- 京东 ip_b_jingdong_order + ip_b_jingdong_order_item + ip_b_jingdong_order_promotion(skuList)
  + ip_b_jingdong_coupondtai(couponDetailList) + ip_b_jingdong_order_invoice(invoiceEasyInfo/vatInfo)
  + ip_b_jingdong_order_promotion_new(ocsQueryList) + ip_b_jingdong_user(consigneeInfo)；
- standplat 平台明细由 OMS 转单侧落库，工具侧继续走 order_service.insert_source，不在此直写。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from order_service import db_connect, get_sequence_id
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

# ---------------------------------------------------------------------------
# 明细子表白名单（omsTableFieldRelation.json insertcols，逐字保留）
# ---------------------------------------------------------------------------
TAOBAO_ITEM_TABLE = "ip_b_taobao_order_item"
TAOBAO_ITEM_WHITELIST = (
    "num,cid,end_time,consign_time,refund_id,buyer_rate,seller_rate,oid,num_iid,status,title,"
    "price,sku_id,outer_sku_id,total_fee,pic_path,refund_status,outer_iid,seller_type,payment,"
    "discount_fee,adjust_fee,sku_properties_name,shipping_type,part_mjz_discount,divide_order_fee,"
    "estimate_con_time,invoice_no,logistics_company,expand_card_basic_price_used_suborder,"
    "expand_card_expand_price_used_suborder"
)
TAOBAO_PROMOTION_TABLE = "ip_b_taobao_order_promotion"
TAOBAO_PROMOTION_WHITELIST = "oid,promotion_name,discount_fee,promotion_desc,promotion_id"
TAOBAO_CHILD_FK = "ip_b_taobao_order_id"

JINGDONG_ITEM_TABLE = "ip_b_jingdong_order_item"
JINGDONG_ITEM_WHITELIST = (
    "productno,wareid,skuname,outerskuid,skuid,jdprice,itemtotal,giftpoint,"
    "reserve_decimal01,reserve_decimal02"
)
JINGDONG_PROMOTION_TABLE = "ip_b_jingdong_order_promotion"
JINGDONG_PROMOTION_WHITELIST = (
    "skucode,count,skuname,basediscount,manjian,venderfee,basefee,remotefee,coupon,jingdou,"
    "balance,superredenvelope,plus95,tuihuanhuowuyou,taxfee,luodipeiservice,shouldpay,skuprice,"
    "jingquan,dongquan,xianpinleijingquan,xianpinleidongquan,pingtaichengdanyouhuiquan,"
    "lijinyouhui,zhifuyingxiaoyouhui,jdzhifuyouhui,promotiondiscount,expirygiftdiscount"
)
JINGDONG_COUPONDTAI_TABLE = "ip_b_jingdong_coupondtai"
JINGDONG_COUPONDTAI_WHITELIST = "couponprice,coupontype,orderid,skuid"
JINGDONG_CHILD_FK = "ip_b_jingdong_order_id"

# 京东收货人/发票/优惠展开子表（对应 JdOrderDaoCmdImpl.saveOrderConsigneeDetail /
# saveOrderInvoice / batchSaveOrderPromotionDetailNew）
JINGDONG_USER_TABLE = "ip_b_jingdong_user"
JINGDONG_USER_WHITELIST = "fullname,fulladdress,telephone,mobile,province,city,county"
JINGDONG_INVOICE_TABLE = "ip_b_jingdong_order_invoice"
JINGDONG_PROMOTION_NEW_TABLE = "ip_b_jingdong_order_promotion_new"

# 淘宝 item 主赠品关联 + 国补 + 透传的后补字段（checkJsonkeycache 后回填）
TAOBAO_ITEM_EXTRA_FIELDS = (
    "os_fg_item_id",
    "os_gift_count",
    "gov_zhaoshangpi",
    "gov_energy_level",
    "gov_main_subject",
    "gov_sn_check",
    "bybt_sn_code_tag",
    "is_bybt_order",
    "ypds_platform_order_id",
    "ypds_order_supply_price",
    "ypds_order_type",
    "ypds_platform_pay_order_id",
)

# 京东明细字段 camelCase -> snake_case 重命名（对应 JdOrderDaoCmdImpl.checkOrder*Json）
JD_ITEM_RENAME_MAP = {
    "productNo": "product_no",
    "wareId": "ware_id",
    "skuName": "sku_name",
    "outerSkuId": "outer_sku_id",
    "skuId": "sku_id",
    "jdPrice": "jd_price",
    "itemTotal": "item_total",
    "giftPoint": "gift_point",
}

# couponDetailList -> ip_b_jingdong_coupondtai（checkOrderPromotionJson）
JD_COUPON_RENAME_MAP = {
    "couponPrice": "coupon_price",
    "couponType": "coupon_type",
    "orderId": "order_id",
    "skuId": "sku_id",
}

# skuList -> ip_b_jingdong_order_promotion（checkOrderPromotionInfoJson）
JD_PROMOTION_RENAME_MAP = {
    "count": "qty",
    "plus95": "jingdong_plus_vip",
    "balance": "amount",
    "shouldPay": "price_payable",
    "luoDiPeiService": "floor_delivery",
    "skuName": "pro_name",
    "superRedEnvelope": "super_red_packet",
    "tuiHuanHuoWuYou": "return_exchange_noworry",
    "manJian": "full_reduction",
    "skuCode": "pro_ecode",
    "taxFee": "price_tax",
    "baseDiscount": "basic_preference",
    "venderFee": "price_vender",
    "baseFee": "price_base",
    "remoteFee": "price_remote",
    "skuPrice": "sku_price",
    "jingQuan": "jing_quan",
    "dongQuan": "dong_quan",
    "xianPinLeiJingQuan": "limited_category_jing_quan",
    "xianPinLeiDongQuan": "limited_category_dong_quan",
    "pingTaiChengDanYouHuiQuan": "plat_rate_coupon",
    "liJinYouHui": "gift_discounts",
    "zhiFuYingXiaoYouHui": "pay_discounts",
    "jdZhiFuYouHui": "jingdong_pay_discounts",
    "promotionDiscount": "promotion_discount",
    "expiryGiftDiscount": "expiry_gift_discount",
}


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


def _taobao_array(trade: Dict[str, Any], parent: str, child: str) -> List[Dict[str, Any]]:
    container = trade.get(parent)
    if isinstance(container, dict):
        arr = container.get(child)
    else:
        arr = container
    if not isinstance(arr, list):
        return []
    return [x for x in arr if isinstance(x, dict)]


def build_taobao_order_items(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """遍历 trade.orders.order[]，并在落库边界再次保证淘宝明细 OID 按行唯一。"""
    source_items = _taobao_array(trade, "orders", "order")
    tid = str(trade.get("tid") or "")
    if len(source_items) > 9:
        raise ValueError(f"淘宝多明细目前仅支持最多 9 个明细（当前 {len(source_items)} 个）")

    rows: List[Dict[str, Any]] = []
    single = len(source_items) <= 1
    for index, item in enumerate(source_items, start=1):
        # 淘宝 oid 必须保持 19 位；主赠品也按明细位置分配不同 OID，不能依赖原报文 oid。
        new_oid = tid if single else f"{tid[:3]}{index}{index}{tid[5:]}"
        item = {**item, "oid": new_oid, "oid_str": new_oid}
        # 先取局部变量（白名单过滤前），主赠品关联 os_fg_item_id/os_gift_count 在此
        extra = {k: item.get(k) for k in TAOBAO_ITEM_EXTRA_FIELDS}
        brand_light_shop_store_id = item.get("brand_light_shop_store_id")

        row = check_jsonkey_cache(item, TAOBAO_ITEM_WHITELIST)

        # outer_sku_id 缺失时回退 outer_iid
        if "outer_sku_id" not in row and "outer_iid" in row:
            row["outer_sku_id"] = row["outer_iid"]

        # 后补字段回填（主赠品 os_fg_item_id/os_gift_count + 国补 gov_* + bybt/ypds 透传）
        for k in TAOBAO_ITEM_EXTRA_FIELDS:
            row[k] = extra[k]
        row["reserve_varchar01"] = brand_light_shop_store_id

        # checkOrderProductJson = deleteNullKeyForObj
        _delete_none(row)
        rows.append(row)
    return rows


def build_taobao_order_promotions(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """复现 TbOrderDaoCmdImpl.batchSaveOrderPromotionDetail：遍历 trade.promotion_details.promotion_detail[]。"""
    rows: List[Dict[str, Any]] = []
    for p in _taobao_array(trade, "promotion_details", "promotion_detail"):
        rows.append(check_jsonkey_cache(p, TAOBAO_PROMOTION_WHITELIST))
    return rows


def build_jingdong_order_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """复现 JdOrderDaoCmdImpl.batchSaveOrderProductDetail：遍历 order.itemInfoList[]。"""
    arr = order.get("itemInfoList")
    if not isinstance(arr, list):
        return []
    rows: List[Dict[str, Any]] = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        item_ext = it.get("itemExt")
        row = check_jsonkey_cache(it, JINGDONG_ITEM_WHITELIST)
        if item_ext is not None:
            row["itemExt"] = item_ext

        for camel, snake in JD_ITEM_RENAME_MAP.items():
            row[snake] = row.get(camel)
            row.pop(camel, None)

        if "itemExt" in row:
            ext = _parse_json_dict(row["itemExt"])
            if ext and "skuUuid" in ext:
                row["sku_uuid"] = ext["skuUuid"]
            row.pop("itemExt", None)

        _delete_none(row)
        rows.append(row)
    return rows


def build_jingdong_order_promotions(order: Dict[str, Any], order_id: str) -> List[Dict[str, Any]]:
    """复现 JdOrderDaoCmdImpl.saveOrderPromotion/batchSaveOrderPromotion：遍历 order.skuList[]。

    skuList 长度 == 1 时单条保存并回填 tid=orderId；长度 > 1 时批量保存不回填 tid。
    """
    arr = order.get("skuList")
    if not isinstance(arr, list):
        return []
    single = len(arr) == 1
    rows: List[Dict[str, Any]] = []
    for p in arr:
        if not isinstance(p, dict):
            continue
        row = check_jsonkey_cache(p, JINGDONG_PROMOTION_WHITELIST)
        for camel, snake in JD_PROMOTION_RENAME_MAP.items():
            row[snake] = row.get(camel)
            row.pop(camel, None)
        if single:
            row["tid"] = order_id
        _delete_none(row)
        rows.append(row)
    return rows


def build_jingdong_coupondtai(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """复现 JdOrderDaoCmdImpl.batchSaveOrderPromotionDetail：遍历 order.couponDetailList[]。"""
    arr = order.get("couponDetailList")
    if not isinstance(arr, list):
        return []
    rows: List[Dict[str, Any]] = []
    for p in arr:
        if not isinstance(p, dict):
            continue
        row = check_jsonkey_cache(p, JINGDONG_COUPONDTAI_WHITELIST)
        for camel, snake in JD_COUPON_RENAME_MAP.items():
            row[snake] = row.get(camel)
            row.pop(camel, None)
        _delete_none(row)
        rows.append(row)
    return rows


def build_jingdong_user(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """复现 JdOrderService.insert 收货人预处理 + saveOrderConsigneeDetail。

    fullname/fullAddress 先删除非 BMP 字符（replaceAll("[^\\u0000-\\uFFFF]","")），
    再白名单过滤，最后 fullAddress -> full_address。
    """
    consignee = order.get("consigneeInfo")
    if not isinstance(consignee, dict):
        return None
    for key in ("fullname", "fullAddress"):
        val = consignee.get(key)
        if isinstance(val, str) and val.strip():
            consignee[key] = re.sub(r"[^\u0000-\uffff]", "", val)

    row = check_jsonkey_cache(consignee, JINGDONG_USER_WHITELIST)
    if "fullAddress" in row:
        row["full_address"] = row.get("fullAddress")
        row.pop("fullAddress", None)
    _delete_none(row)
    return row


def build_jingdong_invoice(order: Dict[str, Any], order_id: str) -> Optional[Dict[str, Any]]:
    """复现 JdOrderDaoCmdImpl.saveOrderInvoice：invoiceEasyInfo/vatInfo 手工构造，无白名单。

    invoiceType 为空或 "0" 时不落发票；"3" 时 invoice_type 置 1。
    """
    invoice_easy = _parse_json_dict(order.get("invoiceEasyInfo"))
    vat = _parse_json_dict(order.get("vatInfo"))
    invoice_type = invoice_easy.get("invoiceType")
    if invoice_type is None or str(invoice_type) in ("", "0"):
        return None

    invoice_title_type = invoice_easy.get("invoiceTitleType")
    if invoice_title_type is None or str(invoice_title_type) == "":
        name_type = "企业" if str(invoice_type) in ("2", "4") else "个人"
    else:
        name_type = "个人" if str(invoice_title_type) == "4" else "企业"

    invoice_title = invoice_easy.get("invoiceTitle")
    row = {
        "tid": order_id,
        "name_type": name_type,
        "invoice_name": invoice_title if invoice_title else "个人",
        "invoice_address": vat.get("addressRegIstered"),
        "invoice_bank": vat.get("depositBank"),
        "invoice_identifier": invoice_easy.get("invoiceCode"),
        "invoice_telephone": vat.get("phoneRegIstered"),
        "invoice_amount": invoice_easy.get("shouldInvoiceAmount"),
        "invoice_email": vat.get("invoiceConsigneeEmail"),
        "invoice_type": invoice_type,
    }
    if str(invoice_type) == "3":
        row["invoice_type"] = 1
    _delete_none(row)
    return row


def build_jingdong_promotion_new(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """复现 JdOrderDaoCmdImpl.batchSaveOrderPromotionDetailNew：手工展开 ocsQueryList[].amountExpands[].orderCostAmounts[]。"""
    arr = order.get("ocsQueryList")
    if not isinstance(arr, list):
        return []
    rows: List[Dict[str, Any]] = []
    for obj in arr:
        if not isinstance(obj, dict):
            continue
        amount_expands = obj.get("amountExpands")
        if not isinstance(amount_expands, list) or not amount_expands:
            continue

        base = {
            "sku_uuid": obj.get("skuUuid"),
            "tid": obj.get("orderId"),
            "qty": obj.get("num"),
            "sku_type": obj.get("skuType"),
            "sku_id": obj.get("skuId"),
        }
        for ae in amount_expands:
            if not isinstance(ae, dict):
                continue
            common = {
                **base,
                "amount": ae.get("amount"),
                "type_name": ae.get("typeName"),
                "type": ae.get("type"),
            }
            order_cost_amounts = ae.get("orderCostAmounts")
            if isinstance(order_cost_amounts, list) and order_cost_amounts:
                for oca in order_cost_amounts:
                    if not isinstance(oca, dict):
                        continue
                    row = {
                        **common,
                        "bear_amount": oca.get("bearAmount"),
                        "bearer": oca.get("bearer"),
                        "bear_rate": oca.get("bearRate"),
                    }
                    _delete_none(row)
                    rows.append(row)
            else:
                _delete_none(common)
                rows.append(common)
    return rows


def insert_master_table(env_key: str, table_name: str, row: Dict[str, Any], shop: Dict[str, Any]) -> int:
    """复现 PackageJsonSqlUtil.saveMasterTable：落一张主表（淘宝/京东），返回主键 ID。"""
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    conn = db_connect(env["order_db"], cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table_name}")
            cols_map = {str(r["Field"]).lower(): r["Field"] for r in cur.fetchall()}

        if "id" not in cols_map:
            raise ValueError(f"{table_name} 缺少 id 主键列")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mid = get_sequence_id(conn, table_name)
        candidate = {**row, "id": mid, "insertdate": now, "cp_c_shop_id": shop["id"]}

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


def insert_child_table(env_key: str, table_name: str, rows: List[Dict[str, Any]], foreign_key: str, mid: int) -> int:
    """复现 PackageJsonSqlUtil.batchSaveChildTable：逐行落子表，返回插入行数。

    ID 与 Java 侧 saveChildTable 一致，经由同源 ad_sequence 序列表发号生成。
    """
    cfg = load_config()
    env = get_env_config(env_key, cfg)
    conn = db_connect(env["order_db"], cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table_name}")
            cols_map = {str(r["Field"]).lower(): r["Field"] for r in cur.fetchall()}

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with conn.cursor() as cur:
            for row in rows:
                candidate = {
                    **row,
                    "id": get_sequence_id(conn, table_name),
                    foreign_key: mid,
                    "creationdate": now,
                }
                data: Dict[str, Any] = {}
                for k, v in candidate.items():
                    if v is None:
                        continue
                    real_col = cols_map.get(str(k).lower())
                    if real_col is None:
                        continue
                    data[real_col] = v

                if not data:
                    continue

                columns = ", ".join(data)
                placeholders = ", ".join(["%s"] * len(data))
                cur.execute(
                    f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})",
                    tuple(data.values()),
                )
                inserted += 1

        conn.commit()
        return inserted
    finally:
        conn.close()


def insert_taobao_order(env_key: str, raw: Any, shop: Dict[str, Any]) -> Tuple[int, str]:
    row, tid = build_taobao_order_row(raw)
    mid = insert_master_table(env_key, TAOBAO_TABLE, row, shop)

    trade = _extract_taobao_trade(raw)
    if isinstance(trade, dict):
        items = build_taobao_order_items(trade)
        if items:
            insert_child_table(env_key, TAOBAO_ITEM_TABLE, items, TAOBAO_CHILD_FK, mid)

        promotions = build_taobao_order_promotions(trade)
        if promotions:
            insert_child_table(env_key, TAOBAO_PROMOTION_TABLE, promotions, TAOBAO_CHILD_FK, mid)

    return mid, tid


def insert_jingdong_order(env_key: str, raw: Any, shop: Dict[str, Any]) -> Tuple[int, str]:
    row, order_id = build_jingdong_order_row(raw)
    mid = insert_master_table(env_key, JINGDONG_TABLE, row, shop)

    orders = _extract_jd_orders(raw)
    if orders:
        order = orders[0]

        invoice = build_jingdong_invoice(order, order_id)
        if invoice:
            insert_child_table(env_key, JINGDONG_INVOICE_TABLE, [invoice], JINGDONG_CHILD_FK, mid)

        items = build_jingdong_order_items(order)
        if items:
            insert_child_table(env_key, JINGDONG_ITEM_TABLE, items, JINGDONG_CHILD_FK, mid)

        coupondtai = build_jingdong_coupondtai(order)
        if coupondtai:
            insert_child_table(env_key, JINGDONG_COUPONDTAI_TABLE, coupondtai, JINGDONG_CHILD_FK, mid)

        promotion_new = build_jingdong_promotion_new(order)
        if promotion_new:
            insert_child_table(env_key, JINGDONG_PROMOTION_NEW_TABLE, promotion_new, JINGDONG_CHILD_FK, mid)

        user = build_jingdong_user(order)
        if user:
            insert_child_table(env_key, JINGDONG_USER_TABLE, [user], JINGDONG_CHILD_FK, mid)

        promotions = build_jingdong_order_promotions(order, order_id)
        if promotions:
            insert_child_table(env_key, JINGDONG_PROMOTION_TABLE, promotions, JINGDONG_CHILD_FK, mid)

    return mid, order_id