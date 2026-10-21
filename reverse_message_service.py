# -*- coding: utf-8 -*-
"""公域逆向报文：standplat / 淘宝 / 京东，按零售发货单主子表回填。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from message_service import detect_message_family, format_message_json, load_template, parse_message_text
from settings import get_platform_meta

Envelope = Union[Dict[str, Any], List[Any]]

PLATFORM_INTERFACE = {
    "standplat": "general_order",
    "taobao_refund": "taobao_return",
    "taobao_exchange": "taobao_exchange",
    "jd_refund": "jingdong_return",
    "jd_exchange": "jingdong_return",
}


@dataclass
class ReverseBuildResult:
    envelope: Envelope
    payload: Dict[str, Any]
    tid: str
    return_no: str
    platform_id: int
    seller_nick: str
    shop_id: Optional[int]
    message_family: str
    platform_interface_name: str
    warnings: List[str] = field(default_factory=list)

    def storage_original_message(self) -> str:
        """与 IP 落库一致：standplat 存单条 data；淘宝退单存数组元素；淘宝换货存 exchange 元素。"""
        if self.message_family == "standplat":
            return json.dumps(self.payload, ensure_ascii=False)
        if self.message_family == "taobao_refund":
            block = self.envelope[0] if isinstance(self.envelope, list) else self.envelope
            return json.dumps(block, ensure_ascii=False)
        if self.message_family == "taobao_exchange":
            return json.dumps(self.payload, ensure_ascii=False)
        if self.message_family in ("jd_refund", "jd_exchange"):
            block = self.envelope[0] if isinstance(self.envelope, list) else self.envelope
            return json.dumps(block, ensure_ascii=False)
        return json.dumps(self.payload, ensure_ascii=False)


def _jd_elem_is_exchange(elem: Dict[str, Any]) -> bool:
    if str(elem.get("customerExpect") or "") == "20":
        return True
    if elem.get("customerExpectName") == "换货":
        return True
    asc = elem.get("jingdong_asc_query_view_responce") or {}
    if str(asc.get("customerExpect") or "") == "20":
        return True
    if asc.get("customerExpectName") == "换货":
        return True
    return False


def _is_jd_elem(elem: Dict[str, Any]) -> bool:
    return "afsServiceId" in elem or "orderid" in elem


def detect_reverse_family(raw: Envelope, scenario: str = "", platform: str = "") -> str:
    name = scenario or ""
    is_exchange_scenario = "换货" in name
    is_jd = platform in ("京东",)

    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        first = raw[0]
        if "jdp_response" in first:
            return "taobao_refund"
        if _is_jd_elem(first):
            if is_exchange_scenario or _jd_elem_is_exchange(first):
                return "jd_exchange"
            return "jd_refund"
    if isinstance(raw, dict):
        if "data" in raw:
            return "standplat"
        if "jdp_response" in raw:
            return "taobao_refund"
        if _is_jd_elem(raw):
            if is_exchange_scenario or _jd_elem_is_exchange(raw):
                return "jd_exchange"
            return "jd_refund"
        if "tmall_exchange_receive_get_response" in raw:
            if is_jd and is_exchange_scenario:
                return "jd_exchange"
            return "taobao_exchange"
    fam = detect_message_family(raw if isinstance(raw, dict) else {})
    if fam == "standplat":
        return "standplat"
    if is_exchange_scenario and is_jd:
        return "jd_exchange"
    if is_exchange_scenario and isinstance(raw, dict):
        return "taobao_exchange"
    return "unknown"


def _money(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _int_like(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _gen_return_no(seed: Any = None, *, prefix: str = "") -> str:
    suffix = datetime.now().strftime("%H%M%S%f")[:11]
    base = str(seed or "147000000000000000")
    if base.isdigit():
        return str(int(base[:14]) + int(suffix[-9:]))
    return f"{prefix}{suffix}"


def _is_exchange_scenario(scenario: str, payload: Dict[str, Any]) -> bool:
    if "换货" in (scenario or ""):
        return True
    return _int_like(payload.get("refund_type")) == 3


def _apply_exchange_receiver(payload: Dict[str, Any], order: Dict[str, Any]) -> None:
    mapping = {
        "exchange_receiver_name": order.get("receiver_name"),
        "exchange_receiver_mobile": order.get("receiver_mobile"),
        "exchange_receiver_phone": order.get("receiver_phone"),
        "exchange_receiver_address": order.get("receiver_address"),
        "exchange_receiver_province": order.get("cp_c_region_province_ename"),
        "exchange_receiver_city": order.get("cp_c_region_city_ename"),
        "exchange_receiver_district": order.get("cp_c_region_area_ename"),
        "exchange_receiver_street": order.get("cp_c_region_town_ename"),
    }
    for k, v in mapping.items():
        if v not in (None, ""):
            payload[k] = v


def _item_from_retail(row: Dict[str, Any], return_no: str, *, exchange: bool = False) -> Dict[str, Any]:
    amt = _money(row.get("real_amt") or row.get("price"))
    qty = _int_like(row.get("qty"), 1)
    title = row.get("ps_c_pro_ename") or row.get("pt_pro_name") or row.get("title") or ""
    sku_id = str(row.get("pt_sku_num_iid") or "")
    sku_code = str(row.get("ps_c_sku_ecode") or "")
    item = {
        "sub_order_id": str(row.get("ooid") or ""),
        "title": title,
        "price": amt,
        "refund_fee": amt,
        "return_quantity": qty,
        "sku_id": sku_id,
        "sku": sku_code,
        "outer_goods_id": sku_code,
        "productcode": str(row.get("pt_num_iid") or ""),
        "return_no": return_no,
    }
    if exchange:
        item.update(
            {
                "exchange_sku_id": sku_id,
                "exchange_outer_id": sku_code,
                "exchange_num": qty,
                "exchange_price": amt,
                "exchange_item_name": title,
                "reserve_varchar03": sku_id,
                "reserve_bigint01": qty,
            }
        )
    return item


def _apply_retail_to_item(
    item: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    return_no: str,
    *,
    is_gift: bool = False,
    exchange: bool = False,
) -> None:
    if is_gift:
        item["is_gift"] = 1
        item["return_no"] = return_no
        if row:
            item["sub_order_id"] = str(row.get("ooid") or item.get("sub_order_id") or "")
            item["title"] = row.get("ps_c_pro_ename") or row.get("pt_pro_name") or item.get("title") or ""
        return
    if not row:
        return
    mapped = _item_from_retail(row, return_no, exchange=exchange)
    for k, v in mapped.items():
        if v not in ("", None):
            item[k] = v


def _match_items_by_ooid(
    template_items: List[Dict[str, Any]],
    retail_items: List[Dict[str, Any]],
) -> Tuple[List[tuple], List[Dict[str, Any]]]:
    by_ooid = {str(r.get("ooid")): r for r in retail_items if r.get("ooid")}
    pairs = []
    used = set()
    for t_item in template_items:
        oid = str(t_item.get("sub_order_id") or "")
        row = by_ooid.get(oid)
        if row:
            used.add(oid)
        pairs.append((t_item, row))
    extras = [r for r in retail_items if str(r.get("ooid")) not in used]
    return pairs, extras


def _primary_rows(order_items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    gift_rows = [r for r in order_items if str(r.get("is_gift")) in ("1", "Y")]
    main_rows = [r for r in order_items if str(r.get("is_gift")) not in ("1", "Y")]
    return main_rows, gift_rows


def _build_standplat(
    platform: str,
    scenario: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Dict[str, Any],
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    meta = get_platform_meta(platform)
    platform_id = int(meta["platform_id"])
    envelope = copy.deepcopy(raw)
    payload = copy.deepcopy(envelope["data"][0] if envelope.get("data") else {})
    warnings: List[str] = []

    new_return_no = return_no or _gen_return_no(payload.get("return_no"))
    payload["order_no"] = tid
    payload["return_no"] = new_return_no
    exchange = _is_exchange_scenario(scenario, payload)

    seller = order.get("seller_nick") or envelope.get("sellernick") or ""
    envelope["sellernick"] = seller
    envelope["platform"] = str(platform_id)
    if order.get("buyer_nick"):
        payload["buyer_nick"] = order["buyer_nick"]
    if exchange:
        _apply_exchange_receiver(payload, order)

    main_rows, gift_rows = _primary_rows(order_items)
    items = payload.get("items")
    if isinstance(items, list) and items:
        pairs, extras = _match_items_by_ooid(items, order_items)
        for t_item, row in pairs:
            is_gift = str(t_item.get("is_gift")) in ("1", "Y") or (
                row and str(row.get("is_gift")) in ("1", "Y")
            )
            _apply_retail_to_item(t_item, row, new_return_no, is_gift=is_gift, exchange=exchange)
        for extra in extras:
            if str(extra.get("is_gift")) in ("1", "Y"):
                items.append({**_item_from_retail(extra, new_return_no, exchange=exchange), "is_gift": 1})
    elif main_rows:
        payload["items"] = [_item_from_retail(r, new_return_no, exchange=exchange) for r in main_rows]
        for g in gift_rows:
            payload["items"].append({**_item_from_retail(g, new_return_no, exchange=exchange), "is_gift": 1})
        warnings.append("场景模板无 items，已按零售发货单明细生成")
    else:
        warnings.append("未查到零售发货单明细，仅替换 order_no/return_no")

    if main_rows:
        primary = main_rows[0]
        payload["sub_order_id"] = str(primary.get("ooid") or payload.get("sub_order_id") or tid)
        total = sum(_money(r.get("real_amt") or r.get("price")) for r in main_rows)
        if total > 0 and not exchange:
            payload["refund_amount"] = total
            payload["item_refund_amount"] = total
    else:
        payload["sub_order_id"] = payload.get("sub_order_id") or tid

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload.setdefault("created", now)
    payload["modified"] = now
    envelope["data"] = [payload]

    return ReverseBuildResult(
        envelope=envelope,
        payload=payload,
        tid=tid,
        return_no=new_return_no,
        platform_id=platform_id,
        seller_nick=seller,
        shop_id=order.get("cp_c_shop_id"),
        message_family="standplat",
        platform_interface_name=PLATFORM_INTERFACE["standplat"],
        warnings=warnings,
    )


def _taobao_refund_block(envelope: Envelope) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    block = envelope[0] if isinstance(envelope, list) else envelope
    refund = (
        block.get("jdp_response", {})
        .get("refund_get_response", {})
        .get("refund", {})
    )
    return block, refund


def _build_taobao_refund(
    platform: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Envelope,
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    platform_id = int(get_platform_meta(platform)["platform_id"])
    envelope = copy.deepcopy(raw)
    block, refund = _taobao_refund_block(envelope)
    warnings: List[str] = []
    main_rows, _ = _primary_rows(order_items)
    primary = main_rows[0] if main_rows else {}

    new_return_no = return_no or _gen_return_no(refund.get("refund_id"))
    seller = order.get("seller_nick") or refund.get("seller_nick") or ""

    refund["tid"] = tid
    refund["refund_id"] = new_return_no
    refund["seller_nick"] = seller
    if order.get("buyer_nick"):
        refund["buyer_nick"] = order["buyer_nick"]
    if primary:
        refund["oid"] = _int_like(primary.get("ooid"), refund.get("oid") or 0)
        refund["num_iid"] = _int_like(primary.get("pt_num_iid"), refund.get("num_iid") or 0)
        refund["title"] = primary.get("ps_c_pro_ename") or primary.get("pt_pro_name") or refund.get("title")
        refund["sku"] = str(primary.get("ps_c_sku_ecode") or refund.get("sku") or "")
        amt = _money(primary.get("real_amt") or primary.get("price"))
        refund["price"] = str(amt)
        refund["refund_fee"] = str(amt)
        refund["total_fee"] = str(amt)
        refund["num"] = _int_like(primary.get("qty"), refund.get("num") or 1)
    else:
        warnings.append("未查到零售明细，仅更新 tid/refund_id")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    refund.setdefault("created", now)
    refund["modified"] = now

    return ReverseBuildResult(
        envelope=envelope if isinstance(envelope, list) else [block],
        payload=refund,
        tid=tid,
        return_no=new_return_no,
        platform_id=platform_id,
        seller_nick=seller,
        shop_id=order.get("cp_c_shop_id"),
        message_family="taobao_refund",
        platform_interface_name=PLATFORM_INTERFACE["taobao_refund"],
        warnings=warnings,
    )


def _build_taobao_exchange(
    platform: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Dict[str, Any],
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    platform_id = int(get_platform_meta(platform)["platform_id"])
    envelope = copy.deepcopy(raw)
    warnings: List[str] = []
    resp = envelope.get("tmall_exchange_receive_get_response", {})
    exchanges = resp.get("results", {}).get("exchange", [])
    if not exchanges:
        raise ValueError("淘宝换货报文缺少 tmall_exchange_receive_get_response.results.exchange")
    ex = exchanges[0]

    main_rows, _ = _primary_rows(order_items)
    primary = main_rows[0] if main_rows else {}
    new_return_no = return_no or _gen_return_no(ex.get("dispute_id"))
    seller = order.get("seller_nick") or ex.get("seller_nick") or envelope.get("nickname") or ""

    ex["dispute_id"] = new_return_no
    ex["biz_order_id"] = tid
    ex["alipay_no"] = tid
    ex["seller_nick"] = seller
    if order.get("buyer_nick"):
        ex["buyer_nick"] = order["buyer_nick"]
    if primary:
        ex["title"] = primary.get("ps_c_pro_ename") or primary.get("pt_pro_name") or ex.get("title")
        bought = str(primary.get("pt_sku_num_iid") or ex.get("bought_sku") or "")
        ex["bought_sku"] = bought
        if not ex.get("exchange_sku"):
            ex["exchange_sku"] = bought
        ex["num"] = _int_like(primary.get("qty"), ex.get("num") or 1)
    else:
        warnings.append("未查到零售明细，仅更新 biz_order_id/dispute_id")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ex.setdefault("created", now)
    ex["modified"] = now
    envelope["nickname"] = seller

    return ReverseBuildResult(
        envelope=envelope,
        payload=ex,
        tid=tid,
        return_no=new_return_no,
        platform_id=platform_id,
        seller_nick=seller,
        shop_id=order.get("cp_c_shop_id"),
        message_family="taobao_exchange",
        platform_interface_name=PLATFORM_INTERFACE["taobao_exchange"],
        warnings=warnings,
    )


def _apply_jd_receiver(elem: Dict[str, Any], order: Dict[str, Any]) -> None:
    """换货场景：收件人信息从零售发货单回填（对齐 AfterSaleOrderConverJd）。"""
    addr = order.get("receiver_address") or ""
    name = order.get("receiver_name") or order.get("buyer_nick") or elem.get("name") or ""
    mobile = order.get("receiver_mobile") or ""
    phone = order.get("receiver_phone") or mobile
    mapping = {
        "name": name,
        "customername": name,
        "contactName": name,
        "contactMobile": mobile,
        "contactTel": phone,
        "customermobilephone": mobile,
        "pickwareAddress": addr or elem.get("pickwareAddress"),
        "returnWareAddress": addr or elem.get("returnWareAddress"),
        "detailAddress": addr or elem.get("detailAddress"),
    }
    for k, v in mapping.items():
        if v not in (None, ""):
            elem[k] = v
    asc = elem.get("jingdong_asc_query_view_responce")
    if isinstance(asc, dict) and addr:
        for key in ("pickwareAddress", "returnWareAddress"):
            nested = asc.get(key)
            if isinstance(nested, dict):
                nested["detailAddress"] = addr
            elif isinstance(nested, str):
                asc[key] = addr
        customer = asc.get("customerInfo")
        if isinstance(customer, dict):
            contact = customer.get("contactInfo")
            if isinstance(contact, dict):
                if name:
                    contact["contactName"] = name
                if mobile:
                    contact["contactMobile"] = mobile
                if phone:
                    contact["contactTel"] = phone


def _build_jd_core(
    platform: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Envelope,
    *,
    return_no: Optional[str] = None,
    exchange: bool = False,
) -> ReverseBuildResult:
    platform_id = int(get_platform_meta(platform)["platform_id"])
    envelope = copy.deepcopy(raw)
    if not isinstance(envelope, list):
        envelope = [envelope]
    elem = envelope[0]
    warnings: List[str] = []
    main_rows, _ = _primary_rows(order_items)
    primary = main_rows[0] if main_rows else {}

    new_return_no = return_no or _gen_return_no(elem.get("afsServiceId"))
    seller = order.get("seller_nick") or elem.get("nick") or ""

    elem["orderid"] = _int_like(tid, elem.get("orderid") or 0)
    elem["afsServiceId"] = _int_like(new_return_no, elem.get("afsServiceId") or 0)
    elem["nick"] = seller
    freight = elem.get("freightmessage")
    if isinstance(freight, dict):
        freight["afsServiceId"] = elem["afsServiceId"]
    if order.get("buyer_nick"):
        elem["name"] = order["buyer_nick"]

    if exchange:
        elem["customerExpect"] = "20"
        elem["customerExpectName"] = "换货"
        asc = elem.get("jingdong_asc_query_view_responce")
        if isinstance(asc, dict):
            asc["customerExpect"] = 20
            asc["customerExpectName"] = "换货"
        _apply_jd_receiver(elem, order)

    if primary:
        title = primary.get("ps_c_pro_ename") or primary.get("pt_pro_name") or elem.get("warename")
        sku_id = str(primary.get("pt_sku_num_iid") or primary.get("pt_num_iid") or "")
        elem["warename"] = title
        elem["skuId"] = sku_id or elem.get("skuId")
        if primary.get("pt_num_iid"):
            elem["wareId"] = _int_like(primary.get("pt_num_iid"), elem.get("wareId") or 0)
        if primary.get("pt_sku_num_iid"):
            elem["skuUuid"] = str(primary.get("pt_sku_num_iid"))
        elem["price"] = _money(primary.get("real_amt") or primary.get("price"))
        elem["servicecount"] = _int_like(primary.get("qty"), elem.get("servicecount") or 1)
        if exchange:
            elem["changeWareSku"] = sku_id or elem.get("changeWareSku")
            elem["changeWareName"] = title or elem.get("changeWareName")
        details = elem.get("applyDetailList")
        if isinstance(details, list) and details:
            d0 = details[0]
            d0["wareName"] = title
            if primary.get("pt_num_iid"):
                d0["skuId"] = _int_like(primary.get("pt_num_iid"), d0.get("skuId") or 0)
            d0["skuUuid"] = str(primary.get("pt_sku_num_iid") or d0.get("skuUuid") or "")
        elif primary:
            elem["applyDetailList"] = [
                {
                    "wareName": title,
                    "wareId": _int_like(primary.get("pt_num_iid"), 0),
                    "skuId": _int_like(primary.get("pt_num_iid"), 0),
                    "skuUuid": str(primary.get("pt_sku_num_iid") or ""),
                    "wareTypeName": "主商品",
                }
            ]
        asc = elem.get("jingdong_asc_query_view_responce")
        if isinstance(asc, dict):
            asc["orderId"] = _int_like(tid, asc.get("orderId") or 0)
            asc["serviceId"] = elem["afsServiceId"]
            asc_details = asc.get("applyDetailList")
            if isinstance(asc_details, list) and asc_details and primary:
                asc_details[0]["wareName"] = title
                if primary.get("pt_num_iid"):
                    asc_details[0]["skuId"] = _int_like(primary.get("pt_num_iid"), asc_details[0].get("skuId") or 0)
                asc_details[0]["skuUuid"] = str(primary.get("pt_sku_num_iid") or asc_details[0].get("skuUuid") or "")
    else:
        warnings.append("未查到零售明细，仅更新 orderid/afsServiceId")

    family = "jd_exchange" if exchange else "jd_refund"
    return ReverseBuildResult(
        envelope=envelope,
        payload=elem,
        tid=tid,
        return_no=str(elem.get("afsServiceId")),
        platform_id=platform_id,
        seller_nick=seller,
        shop_id=order.get("cp_c_shop_id"),
        message_family=family,
        platform_interface_name=PLATFORM_INTERFACE[family],
        warnings=warnings,
    )


def _build_jd_refund(
    platform: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Envelope,
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    return _build_jd_core(
        platform, tid, order, order_items, raw, return_no=return_no, exchange=False
    )


def _build_jd_exchange(
    platform: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    raw: Envelope,
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    return _build_jd_core(
        platform, tid, order, order_items, raw, return_no=return_no, exchange=True
    )


def build_reverse_preview(
    platform: str,
    scenario: str,
    tid: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    *,
    return_no: Optional[str] = None,
) -> ReverseBuildResult:
    raw = load_template(platform, scenario)
    family = detect_reverse_family(raw, scenario, platform)

    if family == "standplat":
        if not isinstance(raw, dict):
            raise ValueError("standplat 逆向报文须为对象且含 data 数组")
        return _build_standplat(platform, scenario, tid, order, order_items, raw, return_no=return_no)
    if family == "taobao_refund":
        return _build_taobao_refund(platform, tid, order, order_items, raw, return_no=return_no)
    if family == "taobao_exchange":
        if not isinstance(raw, dict):
            raise ValueError("淘宝换货报文须为对象")
        return _build_taobao_exchange(platform, tid, order, order_items, raw, return_no=return_no)
    if family == "jd_refund":
        return _build_jd_refund(platform, tid, order, order_items, raw, return_no=return_no)
    if family == "jd_exchange":
        return _build_jd_exchange(platform, tid, order, order_items, raw, return_no=return_no)

    raise ValueError(f"不支持的逆向报文结构（平台={platform}，场景={scenario}）")


def parse_reverse_editor(text: str, last_build: Optional[ReverseBuildResult] = None) -> ReverseBuildResult:
    """从预览区解析用户编辑后的报文。"""
    envelope = json.loads(text) if text.strip().startswith(("{", "[")) else parse_message_text(text)
    scenario_hint = ""
    family = detect_reverse_family(envelope, scenario_hint)

    if family == "standplat":
        if not isinstance(envelope, dict) or not envelope.get("data"):
            raise ValueError("standplat 报文须含 data 数组")
        payload = envelope["data"][0]
        return ReverseBuildResult(
            envelope=envelope,
            payload=payload,
            tid=str(payload.get("order_no") or ""),
            return_no=str(payload.get("return_no") or ""),
            platform_id=int(envelope.get("platform") or (last_build.platform_id if last_build else 0)),
            seller_nick=str(envelope.get("sellernick") or ""),
            shop_id=last_build.shop_id if last_build else None,
            message_family="standplat",
            platform_interface_name=PLATFORM_INTERFACE["standplat"],
        )
    if family == "taobao_refund":
        block, refund = _taobao_refund_block(envelope)
        return ReverseBuildResult(
            envelope=envelope if isinstance(envelope, list) else [block],
            payload=refund,
            tid=str(refund.get("tid") or ""),
            return_no=str(refund.get("refund_id") or ""),
            platform_id=2,
            seller_nick=str(refund.get("seller_nick") or ""),
            shop_id=last_build.shop_id if last_build else None,
            message_family="taobao_refund",
            platform_interface_name=PLATFORM_INTERFACE["taobao_refund"],
        )
    if family == "taobao_exchange":
        ex = envelope.get("tmall_exchange_receive_get_response", {}).get("results", {}).get("exchange", [{}])[0]
        return ReverseBuildResult(
            envelope=envelope,
            payload=ex,
            tid=str(ex.get("biz_order_id") or ex.get("alipay_no") or ""),
            return_no=str(ex.get("dispute_id") or ""),
            platform_id=2,
            seller_nick=str(ex.get("seller_nick") or envelope.get("nickname") or ""),
            shop_id=last_build.shop_id if last_build else None,
            message_family="taobao_exchange",
            platform_interface_name=PLATFORM_INTERFACE["taobao_exchange"],
        )
    if family in ("jd_refund", "jd_exchange"):
        elem = envelope[0] if isinstance(envelope, list) else envelope
        family = "jd_exchange" if _jd_elem_is_exchange(elem) else "jd_refund"
        platform_id = last_build.platform_id if last_build else 4
        return ReverseBuildResult(
            envelope=envelope if isinstance(envelope, list) else [elem],
            payload=elem,
            tid=str(elem.get("orderid") or ""),
            return_no=str(elem.get("afsServiceId") or ""),
            platform_id=platform_id,
            seller_nick=str(elem.get("nick") or ""),
            shop_id=last_build.shop_id if last_build else None,
            message_family=family,
            platform_interface_name=PLATFORM_INTERFACE[family],
        )
    raise ValueError("无法识别预览区报文结构")


def format_reverse_json(envelope: Envelope) -> str:
    return format_message_json(envelope)
