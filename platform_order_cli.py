#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公域造单命令行入口（配置见 config/config.yaml）"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import urljoin

from message_service import build_preview_message, detect_message_family, format_message_json
from order_service import finalize_build_for_insert, insert_source, query_shops, run_transfer
from ip_order_service import insert_jingdong_order, insert_taobao_order
from oms_client import OmsClient
from settings import get_env_config, list_env_keys, list_platform_folders, list_scenarios, load_config


def main(argv=None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="公域平台订单造单 CLI")
    parser.add_argument("--env", default=(list_env_keys(cfg) or ["test"])[0])
    parser.add_argument("--platform", default="抖音")
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--seller-nick", default=None)
    parser.add_argument("--sku", action="append", default=[])
    parser.add_argument("--no-transfer", action="store_true")
    args = parser.parse_args(argv)

    platform = args.platform
    scenario = args.scenario or (list_scenarios(platform)[0] if list_scenarios(platform) else "")
    shops = query_shops(args.env)
    seller = args.seller_nick or (shops[0]["seller_nick"] if shops else "")
    shop = next((s for s in shops if s["seller_nick"] == seller), shops[0] if shops else None)
    if not shop:
        print("未找到店铺"); return 1

    build = build_preview_message(platform, scenario, seller, shop, args.sku)
    print(format_message_json(build.mq_message))
    if args.no_transfer:
        return 0

    family = detect_message_family(build.mq_message)
    if family in ("jdp_array", "jdp_object"):
        mid, tid = insert_taobao_order(args.env, build.mq_message, shop)
        print("淘宝订单已写入中间表 ip_b_taobao_order id", mid, "tid", tid)
        return 0
    if family == "jd_array":
        mid, order_id = insert_jingdong_order(args.env, build.mq_message, shop)
        print("京东订单已写入中间表 ip_b_jingdong_order id", mid, "orderId", order_id)
        return 0

    db_msg, tid = finalize_build_for_insert(args.env, build, build.mq_message)
    sid = insert_source(args.env, build, db_msg, tid)
    print("source_id", sid, "tid", tid)
    print(json.dumps(run_transfer(args.env, sid, tid), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
