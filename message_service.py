# -*- coding: utf-8 -*-
"""公域报文解析、SKU 槽位识别、店铺/SKU 替换与预览构建。"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from settings import MESSAGE_ROOT, get_platform_meta, load_config, sku_field_keys


@dataclass
class SkuSlot:
    index: int
    label: str
    field_name: str
    path: List[Any]
    current_value: str = ""
    is_present: bool = False
    title: str = ""


@dataclass
class BuildResult:
    mq_message: Any
    db_message: str
    tid: str
    platform_id: int
    method: str
    seller_nick: str
    shop: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


def load_template(platform: str, scenario: str) -> Any:
    path = MESSAGE_ROOT / platform / scenario
    if not path.is_file():
        raise FileNotFoundError(f"场景报文不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def detect_message_family(raw: Any) -> str:
    if isinstance(raw, dict) and "orderInfoList" in raw:
        return "jd_array"
    if isinstance(raw, dict) and "data" in raw:
        return "standplat"
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "jdp_response" in raw[0]:
        return "jdp_array"
    if isinstance(raw, dict) and "jdp_response" in raw:
        return "jdp_object"
    return "unknown"


def _is_line_item(obj: Dict[str, Any]) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = sku_field_keys()
    if not any(k in obj for k in keys):
        return False
    markers = ("oid", "num_iid", "title", "sku_id", "outer_iid", "num")
    return any(m in obj for m in markers)


def _walk_find_items(obj: Any, path: Optional[List[Any]] = None) -> List[Tuple[List[Any], Dict[str, Any]]]:
    path = path or []
    found: List[Tuple[List[Any], Dict[str, Any]]] = []
    if isinstance(obj, dict):
        if _is_line_item(obj):
            found.append((path, obj))
        for key, val in obj.items():
            if key == "items" and isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, dict):
                        found.append((path + ["items", i], item))
            elif key == "orders" and isinstance(val, dict) and "order" in val:
                orders = val["order"]
                order_list = orders if isinstance(orders, list) else [orders]
                for i, order in enumerate(order_list):
                    if isinstance(order, dict):
                        if _is_line_item(order):
                            found.append((path + ["orders", "order", i], order))
                        else:
                            found.extend(_walk_find_items(order, path + ["orders", "order", i]))
            elif key == "order" and isinstance(val, (dict, list)):
                orders = val if isinstance(val, list) else [val]
                for i, order in enumerate(orders):
                    if isinstance(order, dict):
                        if _is_line_item(order):
                            found.append((path + ["order", i], order))
                        else:
                            found.extend(_walk_find_items(order, path + ["order", i]))
            else:
                found.extend(_walk_find_items(val, path + [key]))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_walk_find_items(item, path + [i]))
    return found


def detect_sku_slots(raw: Any) -> List[SkuSlot]:
    keys = sku_field_keys()
    items = _walk_find_items(raw)
    slots: List[SkuSlot] = []
    for idx, (path, item) in enumerate(items):
        field_name = next((k for k in keys if k in item), keys[0])
        present = str(item.get("is_present", "false")).lower() == "true"
        title = str(item.get("title") or "")
        kind = "赠品" if present else "主品"
        slots.append(
            SkuSlot(
                index=idx,
                label=f"明细{idx + 1}（{kind}）",
                field_name=field_name,
                path=path,
                current_value=str(item.get(field_name) or ""),
                is_present=present,
                title=title,
            )
        )
    return slots


def _get_by_path(obj: Any, path: List[Any]) -> Any:
    cur = obj
    for p in path:
        cur = cur[p]
    return cur


def _set_by_path(obj: Any, path: List[Any], value: Any) -> None:
    cur = obj
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value


def replace_tid_deep(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, dict):
        return {k: replace_tid_deep(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_tid_deep(v, old, new) for v in obj]
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, (int, float)) and str(obj) == old:
        return type(obj)(new) if str(type(obj)) else new
    return obj


def extract_tid(raw: Any) -> Optional[str]:
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            tid = data[0].get("tid")
            if tid is not None:
                return str(tid)
        if "tid" in raw:
            return str(raw["tid"])
        order_list = raw.get("orderInfoList")
        if isinstance(order_list, list) and order_list and isinstance(order_list[0], dict):
            order_id = order_list[0].get("orderId")
            if order_id is not None:
                return str(order_id)
    if isinstance(raw, list) and raw:
        return extract_tid(raw[0])
    if isinstance(raw, dict) and "jdp_response" in raw:
        try:
            trade = raw["jdp_response"]["trade_fullinfo_get_response"]["trade"]
            if trade.get("tid") is not None:
                return str(trade["tid"])
        except (KeyError, TypeError):
            pass
    found = re.search(r'"tid"\s*:\s*"?(\d+)"?', json.dumps(raw, ensure_ascii=False))
    return found.group(1) if found else None


def extract_seller_nick(raw: Any) -> Optional[str]:
    """从报文中提取卖家昵称。

    覆盖三类结构：standplat 顶层 sellernick/seller_nick、
    淘宝 jdp_response.trade.seller_nick、京东 orderInfoList[0].nick。
    """
    if isinstance(raw, list):
        for item in raw:
            nick = extract_seller_nick(item)
            if nick:
                return nick
        return None
    if not isinstance(raw, dict):
        return None
    for key in ("seller_nick", "sellernick"):
        v = raw.get(key)
        if v:
            return str(v).strip()
    order_list = raw.get("orderInfoList")
    if isinstance(order_list, list) and order_list and isinstance(order_list[0], dict):
        nick = order_list[0].get("nick") or order_list[0].get("seller_nick")
        if nick:
            return str(nick).strip()
    if "jdp_response" in raw:
        try:
            trade = raw["jdp_response"]["trade_fullinfo_get_response"]["trade"]
            if isinstance(trade, dict):
                nick = trade.get("seller_nick")
                if nick:
                    return str(nick).strip()
        except (KeyError, TypeError):
            pass
    return None


def apply_seller_nick(raw: Any, seller_nick: str, family: str) -> None:
    if family == "standplat" and isinstance(raw, dict):
        raw["sellernick"] = seller_nick
        raw["seller_nick"] = seller_nick
        return
    if family == "jd_array" and isinstance(raw, dict):
        order_list = raw.get("orderInfoList")
        if isinstance(order_list, list):
            for order in order_list:
                if isinstance(order, dict):
                    order["nick"] = seller_nick
        return
    if family in ("jdp_array", "jdp_object"):
        targets = raw if isinstance(raw, list) else [raw]
        for item in targets:
            if not isinstance(item, dict):
                continue
            try:
                trade = item["jdp_response"]["trade_fullinfo_get_response"]["trade"]
                trade["seller_nick"] = seller_nick
            except (KeyError, TypeError):
                pass


def apply_sku_values(raw: Any, slots: List[SkuSlot], sku_values: List[str]) -> None:
    for slot, sku in zip(slots, sku_values):
        if not sku:
            continue
        try:
            item = _get_by_path(raw, slot.path)
            if isinstance(item, dict):
                item[slot.field_name] = sku
        except (KeyError, IndexError, TypeError):
            pass


def build_standplat_db_message(mq: Dict[str, Any], seller_nick: str, platform_id: int) -> str:
    data_arr = mq.get("data")
    if not isinstance(data_arr, list) or not data_arr:
        raise ValueError("standplat 报文 data 必须为非空数组")
    wrapped = {
        "sellernick": seller_nick,
        "platform": platform_id,
        "data": copy.deepcopy(data_arr[0]),
    }
    return json.dumps(wrapped, ensure_ascii=False)


def build_preview_message(
    platform: str,
    scenario: str,
    seller_nick: str,
    shop: Dict[str, Any],
    sku_values: List[str],
    new_tid: Optional[str] = None,
    base_raw: Optional[Any] = None,
) -> BuildResult:
    cfg = load_config()
    meta = get_platform_meta(platform, cfg)
    platform_id = meta.get("platform_id")
    method = meta.get("method") or ""
    warnings: List[str] = []

    if platform_id is None:
        raise ValueError(
            f"平台 [{platform}] 未在 config/config.yaml -> platform_meta 中配置 platform_id，请补充后重试"
        )

    raw = copy.deepcopy(base_raw if base_raw is not None else load_template(platform, scenario))
    family = detect_message_family(raw)
    expected = meta.get("message_format", "standplat")
    if expected == "standplat" and family != "standplat":
        warnings.append(f"报文结构与平台配置 message_format={expected} 不一致，当前识别为 {family}")
    if expected == "jdp_array" and family not in ("jdp_array", "jdp_object"):
        warnings.append(f"报文结构与平台配置 message_format={expected} 不一致，当前识别为 {family}")
    if expected == "jd_array" and family != "jd_array":
        warnings.append(f"报文结构与平台配置 message_format={expected} 不一致，当前识别为 {family}")

    old_tid = extract_tid(raw)
    if not old_tid:
        raise ValueError("无法从报文中识别 tid，请检查模板或手动在预览区修改")

    keep_original_tid = family in ("jdp_array", "jdp_object", "jd_array")
    tid = old_tid if keep_original_tid else (new_tid or _gen_tid_placeholder(old_tid))

    if not keep_original_tid:
        raw = replace_tid_deep(raw, old_tid, tid)
    apply_seller_nick(raw, seller_nick, family)

    slots = detect_sku_slots(raw)
    if sku_values:
        if len(sku_values) < len(slots):
            warnings.append(f"报文含 {len(slots)} 个 SKU 槽位，仅提供了 {len(sku_values)} 个条码，未填写的保持原值")
        apply_sku_values(raw, slots, sku_values)

    if family == "standplat":
        if isinstance(raw, dict):
            raw["platform"] = str(platform_id)
        db_message = build_standplat_db_message(raw, seller_nick, int(platform_id))
        mq_message = raw
    else:
        mq_message = raw
        db_message = json.dumps(raw, ensure_ascii=False)

    if not method:
        warnings.append(f"平台 [{platform}] 的 method 未配置，入库后转单可能失败")

    return BuildResult(
        mq_message=mq_message,
        db_message=db_message,
        tid=tid,
        platform_id=int(platform_id),
        method=method,
        seller_nick=seller_nick,
        shop=shop,
        warnings=warnings,
    )


def _gen_tid_placeholder(old_tid: str) -> str:
    """预览阶段用占位 tid；真正入库前由 order_service 分配库内唯一 tid。"""
    suffix = datetime.now().strftime("%H%M%S")
    if old_tid.isdigit():
        # 保持原 tid 的完整长度，仅替换末尾 6 位为时间戳
        if len(old_tid) > 6:
            return old_tid[:-6] + suffix
        return str(int(old_tid) + int(suffix))
    return f"{old_tid}_{suffix}"


def format_message_json(message: Any) -> str:
    return json.dumps(message, ensure_ascii=False, indent=2)


def parse_message_text(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("报文内容为空")
    return json.loads(text)


def db_message_from_editor_text(text: str, platform: str, seller_nick: str, shop: Dict[str, Any]) -> Tuple[str, str, str]:
    """从用户编辑的报文文本生成入库 message 与 tid。"""
    raw = parse_message_text(text)
    meta = get_platform_meta(platform)
    family = detect_message_family(raw)
    tid = extract_tid(raw)
    if not tid:
        raise ValueError("编辑后的报文无法识别 tid")
    platform_id = meta.get("platform_id")
    if platform_id is None:
        raise ValueError(f"平台 {platform} 缺少 platform_id 配置")
    if family == "standplat" and isinstance(raw, dict):
        db_msg = build_standplat_db_message(raw, seller_nick, int(platform_id))
    else:
        db_msg = json.dumps(raw, ensure_ascii=False)
    method = meta.get("method") or ""
    return db_msg, tid, method
