# -*- coding: utf-8 -*-
"""EC 电商下单工具。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

import requests

from ui_theme import bind_json_word_select


EC_STATE_FILE = Path(__file__).parent / "ui_state.json"


EC_URL = "http://47.86.49.123:21181/router"
EC_METHOD = "burgeon.r3.commonOrder.create"
EC_VERSION = "1.0"
EC_FORMAT = "json"

def _load_ec_env_options() -> dict:
    """从配置表读取 EC 环境下拉 {展示标签: env_key}。"""
    try:
        from settings import env_options_for_gui
        opts = env_options_for_gui()
        if opts:
            return opts
    except Exception:
        pass
    return {"TEST": "test"}


EC_ENV_OPTIONS = _load_ec_env_options()
EC_DEFAULT_ENV_LABEL = next(iter(EC_ENV_OPTIONS.keys()), "TEST")
EC_DEFAULT_ENV_KEY = next(iter(EC_ENV_OPTIONS.values()), "test")
EC_CHAIN_OPTIONS = [
    "平台订单报文-平台订单",
    "平台订单报文-平台订单-待审核",
    "平台订单报文-平台订单-待审核-配货中",
]

SAMPLE_SINGLE = {
    "platform": 9999,
    "shop_code": "004614",
    "real_shop_code": "3M87",
    "status": "WAIT_SELLER_SEND_GOODS",
    "post_fee": 0.00,
    "num": 1,
    "delivery_time": "2026-07-03 17:06:45",
    "trade_update_time": "2026-07-03 17:06:45",
    "buyer_id": "杨万里",
    "tid": "99974738443772290626",
    "tax_fee": 24.78,
    "plantfor_count": 1400.00,
    "total_fee": 1500.00,
    "receiver_name": "王二小",
    "payment": 0.00,
    "oaid": "#ew3v9/2eQOACuO89VSXkPi3oBzDHKztaA0kIZuJa87ce2nZ+DGJNK9v8wQF2D8ZSUy8Mn3OPe0/1FTqgCSEXk3Q4sd5Uoj+mGasu3dRpRuEVD3mw4Ha5TViNHPqgT0TVNqP+PfkP9A==",
    "ch_order_flag_tag": "",
    "buyer_message": "",
    "product_discount": 100.00,
    "seller_memo": "",
    "receiver_province": "臺灣省",
    "receiver_city": "臺北市",
    "receiver_district": "中正區100",
    "reserve_varchar12": "商家发货",
    "receiver_mobile": "18888888888",
    "receiver_address": "安源**道**号时代**小**单元***号",
    "reserve_decimal06": 1,
    "buyer_nick": "杨万里",
    "trade_create_time": "2026-07-03 17:06:45",
    "is_invoice": "true",
    "delivery_method": 4,
    "orderType": 0,
    "stagePayStatus": "",
    "subsidy_area": "",
    "subsidy_type": 0,
    "presale_deposit_time": "2026-07-03 17:06:45",
    "items": [{
        "num": 1,
        "num_iid": "1",
        "delivery_time": "2026-07-03 17:06:45",
        "oid": "99974738443772290626",
        "title": "ROG枪神8 Plus 18英寸RTX40系列电竞学生高端游戏笔记本电脑",
        "discount_fee": 100.00,
        "tid": "99974738443772290626",
        "is_present": "false",
        "gift_relation": "",
        "price": 1500.00,
        "total_fee": 1500.00,
        "payment": 0.00,
        "outer_sku_id": "TB0A26KCF48100S000",
        "reserve_decimal01": 0.00,
        "integral": 500,
        "pic_path": "",
        "sku_id": "3392587707357442",
        "sku_properties_name": "",
        "outerIid": "",
        "sku_tax_fee": 0,
        "salesType": 0,
        "subsidy_fee": 0,
        "product_code": "",
        "estimate_con_time": "2026-07-03 17:06:45"
    }],
    "invoice_info": {
        "invoice_title_type": "个人", "invoice_title": "XXXX", "invoice_type": 2,
        "invoice_amount": 0, "invoice_identifier": "", "invoice_bank": "",
        "invoice_account": "", "invoice_address": "", "invoice_telephone": "", "invoice_email": ""
    },
    "payment_info": {
        "pay_way": "alipay", "pay_way_name": "支付宝",
        "transaction_no": "2023330011100001", "pay_amount": 0.00,
        "pay_time": "2026-07-03 17:06:45", "remark": ""
    },
    "promotion_info": {
        "type_name": "订单优惠", "type": 151, "amount": 1400.00,
        "apportion_detail": [{
            "oid": "99974738443772290626", "sku_id": "3392587707357442",
            "qty": 1, "sku_type": 1, "apportion_type": 1,
            "apportion_amount": 1400.00, "apportion_rate": 1.00
        }]
    }
}

SAMPLE_DOUBLE = {
    "platform": 9999,
    "shop_code": "004614",
    "real_shop_code": "3M87",
    "status": "WAIT_SELLER_SEND_GOODS",
    "post_fee": 8.00,
    "num": 2,
    "delivery_time": "2026-07-03 17:06:45",
    "trade_update_time": "2026-07-03 17:06:45",
    "buyer_id": "杨万里",
    "tid": "99974738443772290635",
    "tax_fee": 24.78,
    "plantfor_count": 222.00,
    "total_fee": 3000.00,
    "receiver_name": "王二小",
    "payment": 2596.00,
    "oaid": "#ew3v9/2eQOACuO89VSXkPi3oBzDHKztaA0kIZuJa87ce2nZ+DGJNK9v8wQF2D8ZSUy8Mn3OPe0/1FTqgCSEXk3Q4sd5Uoj+mGasu3dRpRuEVD3mw4Ha5TViNHPqgT0TVNqP+PfkP9A==",
    "ch_order_flag_tag": "",
    "buyer_message": "",
    "product_discount": 190.00,
    "seller_memo": "",
    "receiver_province": "臺灣省",
    "receiver_city": "臺北市",
    "receiver_district": "中正區100",
    "reserve_varchar12": "商家发货",
    "receiver_mobile": "18888888888",
    "receiver_address": "安源**道**号时代**小**单元***号",
    "reserve_decimal06": 1,
    "buyer_nick": "杨万里",
    "trade_create_time": "2026-07-03 17:06:45",
    "is_invoice": "true",
    "delivery_method": 4,
    "orderType": 0,
    "stagePayStatus": "",
    "subsidy_area": "",
    "subsidy_type": 0,
    "presale_deposit_time": "2026-07-03 17:06:45",
    "items": [
        {
            "num": 1,
            "num_iid": "1",
            "delivery_time": "2026-07-03 17:06:45",
            "oid": "99974738443772290635",
            "title": "ROG枪神8 Plus 18英寸RTX40系列电竞学生高端游戏笔记本电脑",
            "discount_fee": 100.00,
            "tid": "99974738443772290635",
            "is_present": "false",
            "gift_relation": "",
            "price": 1500.00,
            "total_fee": 1500.00,
            "payment": 1289.00,
            "outer_sku_id": "TB0A26KCF48100S000",
            "reserve_decimal01": 0.00,
            "integral": 500,
            "pic_path": "",
            "sku_id": "3392587707357442",
            "sku_properties_name": "",
            "outerIid": "",
            "sku_tax_fee": 0,
            "salesType": 0,
            "subsidy_fee": 0,
            "product_code": "",
            "estimate_con_time": "2026-07-03 17:06:45"
        },
        {
            "num": 1,
            "num_iid": "1",
            "delivery_time": "2026-07-03 17:06:45",
            "oid": "99974738443772290635111",
            "title": "ROG枪神8 Plus 18英寸RTX40系列电竞学生高端游戏笔记本电脑",
            "discount_fee": 90.00,
            "tid": "99974738443772290635",
            "is_present": "false",
            "gift_relation": "",
            "price": 1500.00,
            "total_fee": 1500.00,
            "payment": 1299.00,
            "outer_sku_id": "TBC0YHY200010OS000",
            "reserve_decimal01": 0.00,
            "integral": 500,
            "pic_path": "",
            "sku_id": "3392587707357443",
            "sku_properties_name": "",
            "outerIid": "",
            "sku_tax_fee": 0,
            "salesType": 0,
            "subsidy_fee": 0,
            "product_code": "",
            "estimate_con_time": "2026-07-03 17:06:45"
        }
    ],
    "invoice_info": {
        "invoice_title_type": "个人", "invoice_title": "XXXX", "invoice_type": 2,
        "invoice_amount": 0, "invoice_identifier": "", "invoice_bank": "",
        "invoice_account": "", "invoice_address": "", "invoice_telephone": "", "invoice_email": ""
    },
    "payment_info": {
        "pay_way": "alipay", "pay_way_name": "支付宝",
        "transaction_no": "2023330011100001", "pay_amount": 2596.00,
        "pay_time": "2026-07-03 17:06:45", "remark": ""
    },
    "promotion_info": {
        "type_name": "订单优惠", "type": 151, "amount": 222.00,
        "apportion_detail": [
            {
                "oid": "99974738443772290635", "sku_id": "3392587707357442",
                "qty": 1, "sku_type": 1, "apportion_type": 1,
                "apportion_amount": 111.00, "apportion_rate": 0.50
            },
            {
                "oid": "99974738443772290635111", "sku_id": "3392587707357443",
                "qty": 1, "sku_type": 1, "apportion_type": 1,
                "apportion_amount": 111.00, "apportion_rate": 0.50
            }
        ]
    }
}

SAMPLE_BODY = SAMPLE_SINGLE


def build_sign(app_key: str, app_secret: str, session: str, timestamp: str, body_json: str) -> tuple[str, str]:
    params = {
        "appKey": app_key,
        "format": EC_FORMAT,
        "method": EC_METHOD,
        "session": session,
        "timestamp": timestamp,
        "v": EC_VERSION,
    }
    sign_source = app_secret
    for key in sorted(params):
        value = params[key]
        if key and value not in (None, ""):
            sign_source += key + str(value)
    sign_source += body_json + app_secret
    return hashlib.md5(sign_source.encode("utf-8")).hexdigest().upper(), sign_source


def replace_exact_value(data: Any, old_value: str, new_value: str) -> Any:
    if isinstance(data, dict):
        return {key: replace_exact_value(value, old_value, new_value) for key, value in data.items()}
    if isinstance(data, list):
        return [replace_exact_value(value, old_value, new_value) for value in data]
    return new_value if isinstance(data, str) and data == old_value else data


def replace_field_values(data: Any, field_name: str, new_value: str) -> int:
    count = 0
    if isinstance(data, dict):
        for key, value in data.items():
            if key == field_name:
                data[key] = new_value
                count += 1
            else:
                count += replace_field_values(value, field_name, new_value)
    elif isinstance(data, list):
        for value in data:
            count += replace_field_values(value, field_name, new_value)
    return count


def increment_tid(tid: str) -> str:
    match = re.search(r"(\d+)$", tid)
    if not match:
        raise ValueError("tid 必须以数字结尾，无法执行尾号 +1")
    number = match.group(1)
    next_number = str(int(number) + 1).zfill(len(number))
    return tid[:match.start(1)] + next_number


def replace_datetime_strings(data: Any, new_value: str) -> Any:
    """把报文中形如 YYYY-MM-DD HH:MM:SS 的时间字段统一替换为当前时间。"""
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    if isinstance(data, dict):
        return {key: replace_datetime_strings(value, new_value) for key, value in data.items()}
    if isinstance(data, list):
        return [replace_datetime_strings(value, new_value) for value in data]
    if isinstance(data, str) and pattern.match(data):
        return new_value
    return data


class EcOrderApp:
    def __init__(self, parent: tk.Misc, *, standalone: bool = True, tid_prefix: str = "999", title: str = "EC 电商下单", state_key: str = "ec_order"):
        self.root = parent
        self.tid_prefix = tid_prefix
        self.title = title
        self.state_key = state_key
        if standalone and isinstance(parent, tk.Tk):
            parent.title("EC 电商下单工具")
            parent.geometry("1120x820")
            parent.minsize(900, 680)

        self.url_var = tk.StringVar(value=EC_URL)
        self.app_key_var = tk.StringVar(value="5s9Dk7pR2tG8")
        self.app_secret_var = tk.StringVar(value="7sK9pR2dF5gT8bN4zQ1vX6cJ3mH0wA")
        self.session_var = tk.StringVar(value="l6VIIxB0kDSXdXpB")
        self.msg_type_var = tk.StringVar(value="单明细")
        self.logic_store_var = tk.StringVar(value="")
        self._logic_store_map = {}
        self._logic_store_after_id = None
        self.outer_sku1_var = tk.StringVar(value="TB0A26KCF48100S000")
        self.outer_sku2_var = tk.StringVar(value="TBC0YHY200010OS000")
        self.status_var = tk.StringVar(value="就绪")
        self.env_var = tk.StringVar(value=EC_DEFAULT_ENV_LABEL)
        self.chain_var = tk.StringVar(value=EC_CHAIN_OPTIONS[0])
        self.current_tid_var = tk.StringVar(value="-")
        self.last_tid_var = tk.StringVar(value="-")
        self.create_status_var = tk.StringVar(value="待执行")
        self.cancel_event = threading.Event()
        self._current_tid = ""
        self._build_ui()
        self._apply_template(SAMPLE_SINGLE)
        self._load_state()
        self._load_logic_stores(silent=True)

    def _load_state(self) -> None:
        """恢复上次保存的 EC 下单配置与报文。"""
        saved = None
        try:
            if EC_STATE_FILE.exists():
                with open(EC_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        saved = data.get(self.state_key)
        except Exception:
            saved = None
        if not isinstance(saved, dict):
            return

        if saved.get("url"):
            self.url_var.set(saved["url"])
        if saved.get("app_key"):
            self.app_key_var.set(saved["app_key"])
        if saved.get("app_secret"):
            self.app_secret_var.set(saved["app_secret"])
        if saved.get("session"):
            self.session_var.set(saved["session"])
        if saved.get("outer_sku1"):
            self.outer_sku1_var.set(saved["outer_sku1"])
        if saved.get("outer_sku2"):
            self.outer_sku2_var.set(saved["outer_sku2"])
        if saved.get("env") in EC_ENV_OPTIONS:
            self.env_var.set(saved["env"])
        if saved.get("chain") in EC_CHAIN_OPTIONS:
            self.chain_var.set(saved["chain"])
        if saved.get("current_tid"):
            self._current_tid = str(saved["current_tid"])
        if saved.get("last_tid"):
            self.last_tid_var.set(str(saved["last_tid"]))

        msg_type = saved.get("msg_type")
        if msg_type in ("单明细", "2行明细"):
            self.msg_type_var.set(msg_type)
        # 按报文类型重建 SKU 输入框
        if self.msg_type_var.get() == "2行明细":
            self._build_sku_double()
        else:
            self._build_sku_single()

        body = saved.get("body")
        if body:
            self.body_text.delete("1.0", tk.END)
            self.body_text.insert("1.0", body)
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and parsed.get("tid"):
                    self._current_tid = str(parsed["tid"])
            except Exception:
                pass
        self.current_tid_var.set(self._current_tid or "-")

    def _save_state(self) -> None:
        """保存 EC 下单配置与报文体（读改写，避免覆盖其他页面）。"""
        try:
            state = {}
            if EC_STATE_FILE.exists():
                with open(EC_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        state = data
            state[self.state_key] = {
                "url": self.url_var.get(),
                "app_key": self.app_key_var.get(),
                "app_secret": self.app_secret_var.get(),
                "session": self.session_var.get(),
                "env": self.env_var.get(),
                "chain": self.chain_var.get(),
                "msg_type": self.msg_type_var.get(),
                "outer_sku1": self.outer_sku1_var.get(),
                "outer_sku2": self.outer_sku2_var.get(),
                "current_tid": self._current_tid,
                "last_tid": self.last_tid_var.get(),
                "body": self.body_text.get("1.0", tk.END).strip(),
            }
            with open(EC_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存 EC 状态失败: {e}")

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(container, text=self.title, font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 8))

        # ---- 使用说明（可折叠，默认收起） ----
        help_box = ttk.LabelFrame(container, text=" 说明 ", padding=6)
        help_box.pack(fill=tk.X, pady=(0, 8))
        help_toggle = ttk.Label(help_box, text="▸ 展开说明", cursor="hand2")
        help_toggle.pack(anchor=tk.W)
        help_content = ttk.Label(
            help_box,
            text=(
                "本工具用于 EC 电商下单，操作步骤如下：\n"
                "1. 选择报文类型：单明细或2行明细；\n"
                "2. 选择逻辑仓：下拉框支持输入关键字模糊搜索；\n"
                "3. 点击「查询」：自动将该逻辑仓库存可用量最大（2行明细取前2个）的 SKU 填入 outer_sku_id 框，可手动修改；\n"
                "4. 点击「一键替换报文」：将 outer_sku_id 框内容写入请求体 items[].outer_sku_id；\n"
                "5. 检查请求报文后点击「发送 EC 订单」。"
            ),
            justify=tk.LEFT,
        )
        help_state = {"shown": False}

        def _help_toggle(event=None):
            if help_state["shown"]:
                help_content.pack_forget()
                help_toggle.config(text="▸ 展开说明")
            else:
                help_content.pack(anchor=tk.W, pady=(2, 0), after=help_toggle)
                help_toggle.config(text="▾ 收起说明")
            help_state["shown"] = not help_state["shown"]

        help_toggle.bind("<Button-1>", _help_toggle)

        # ---- 接口与鉴权（可折叠，默认收起） ----
        auth = ttk.LabelFrame(container, text=" 接口与鉴权 ", padding=6)
        auth.pack(fill=tk.X, pady=(0, 8))
        auth_toggle = ttk.Label(auth, text="▸ 展开接口与鉴权", cursor="hand2")
        auth_toggle.pack(anchor=tk.W)

        auth_content = ttk.Frame(auth)
        fields = [
            ("URL", self.url_var, False),
            ("appKey", self.app_key_var, False),
            ("appSecret", self.app_secret_var, True),
            ("session", self.session_var, False),
        ]
        for index, (label, variable, secret) in enumerate(fields):
            col = index * 2
            ttk.Label(auth_content, text=label + ":").grid(row=0, column=col, sticky=tk.E, padx=(2, 3), pady=3)
            ttk.Entry(auth_content, textvariable=variable, show="*" if secret else "").grid(
                row=0, column=col + 1, sticky=tk.EW, padx=(0, 6), pady=3
            )
        auth_content.columnconfigure(1, weight=3)
        auth_content.columnconfigure(3, weight=1)
        auth_content.columnconfigure(5, weight=1)
        auth_content.columnconfigure(7, weight=1)

        auth_state = {"shown": False}

        def _auth_toggle(event=None):
            if auth_state["shown"]:
                auth_content.pack_forget()
                auth_toggle.config(text="▸ 展开接口与鉴权")
            else:
                auth_content.pack(fill=tk.X, after=auth_toggle, pady=(4, 0))
                auth_toggle.config(text="▾ 收起接口与鉴权")
            auth_state["shown"] = not auth_state["shown"]

        auth_toggle.bind("<Button-1>", _auth_toggle)

        # ---- 环境与订单节点串联 ----
        chain_box = ttk.LabelFrame(container, text=" 环境与订单节点串联 ", padding=8)
        chain_box.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(chain_box, text="环境:").pack(side=tk.LEFT)
        env_combo = ttk.Combobox(
            chain_box, textvariable=self.env_var,
            values=list(EC_ENV_OPTIONS.keys()), state="readonly", width=6,
        )
        env_combo.pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(chain_box, text="订单节点串联:").pack(side=tk.LEFT)
        chain_combo = ttk.Combobox(
            chain_box, textvariable=self.chain_var,
            values=EC_CHAIN_OPTIONS, state="readonly", width=34,
        )
        chain_combo.pack(side=tk.LEFT, padx=(4, 0))

        # ---- 单号记录 ----
        tid_box = ttk.LabelFrame(container, text=" 单号记录 ", padding=8)
        tid_box.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(tid_box, text="新单号:").pack(side=tk.LEFT)
        current_tid_entry = ttk.Entry(tid_box, textvariable=self.current_tid_var, width=24, state="readonly")
        current_tid_entry.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Button(tid_box, text="复制", command=lambda: self._copy_var(self.current_tid_var)).pack(side=tk.LEFT)
        ttk.Label(tid_box, text="上次请求单号:").pack(side=tk.LEFT, padx=(16, 4))
        last_tid_entry = ttk.Entry(tid_box, textvariable=self.last_tid_var, width=24, state="readonly")
        last_tid_entry.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Button(tid_box, text="复制", command=lambda: self._copy_var(self.last_tid_var)).pack(side=tk.LEFT)

        # ---- 报文类型 + SKU 替换 ----
        replace_box = ttk.LabelFrame(container, text=" 报文类型与商品 SKU ", padding=8)
        replace_box.pack(fill=tk.X, pady=(0, 8))

        # 第一行：报文类型 + 逻辑仓 + 查询
        row1 = ttk.Frame(replace_box)
        row1.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row1, text="报文类型:").pack(side=tk.LEFT)
        msg_combo = ttk.Combobox(
            row1, textvariable=self.msg_type_var,
            values=["单明细", "2行明细"], state="readonly", width=10,
        )
        msg_combo.pack(side=tk.LEFT, padx=(4, 16))
        msg_combo.bind("<<ComboboxSelected>>", self._on_msg_type_change)

        ttk.Label(row1, text="逻辑仓:").pack(side=tk.LEFT)
        self.logic_store_combo = ttk.Combobox(
            row1, textvariable=self.logic_store_var, width=30,
        )
        self.logic_store_combo.pack(side=tk.LEFT, padx=(4, 8))
        self.logic_store_combo.bind("<KeyRelease>", self._on_logic_store_key)
        self.query_sku_btn = ttk.Button(row1, text="查询", command=self._query_max_sku)
        self.query_sku_btn.pack(side=tk.LEFT)

        # 第二行：SKU 替换区域（动态切换单行/双行） + 一键替换报文
        row2 = ttk.Frame(replace_box)
        row2.pack(fill=tk.X)
        self.sku_frame = ttk.Frame(row2)
        self.sku_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._build_sku_single()

        ttk.Button(row2, text="一键替换报文", command=self.replace_outer_sku).pack(side=tk.RIGHT, padx=(8, 0))

        # ---- 请求报文 JSON ----
        body_frame = ttk.LabelFrame(container, text=" 请求报文 JSON ", padding=8)
        body_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        toolbar = ttk.Frame(body_frame)
        toolbar.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(toolbar, text="格式化", command=self.format_body).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="恢复示例", command=self._restore_sample).pack(side=tk.LEFT, padx=6)
        self.send_btn = ttk.Button(toolbar, text="发送 EC 订单", command=self.send_order)
        self.send_btn.pack(side=tk.RIGHT)
        ttk.Label(toolbar, textvariable=self.status_var).pack(side=tk.RIGHT, padx=10)
        self.body_text = scrolledtext.ScrolledText(body_frame, wrap=tk.NONE, height=20, font=("Consolas", 10))
        self.body_text.pack(fill=tk.BOTH, expand=True)
        bind_json_word_select(self.body_text)

        # ---- 执行结果（含制单状态在标题行） ----
        response_outer = ttk.Frame(container)
        response_outer.pack(fill=tk.BOTH, expand=True)
        # 自定义标题行：左侧标题 + 右侧制单状态
        resp_header = ttk.Frame(response_outer)
        resp_header.pack(fill=tk.X)
        ttk.Label(resp_header, text=" 执行结果 ", font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Separator(resp_header, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(resp_header, text="制单状态:").pack(side=tk.LEFT)
        self.create_status_label = tk.Label(
            resp_header, textvariable=self.create_status_var,
            font=("Microsoft YaHei UI", 11, "bold"), fg="#FF8F00",
        )
        self.create_status_label.pack(side=tk.LEFT, padx=(4, 0))

        def _terminate():
            self.cancel_event.set()
            self.status_var.set("已请求终止，正在结束当前流程...")

        self.terminate_btn = ttk.Button(resp_header, text="终止", command=_terminate)
        # 仅当制单状态=执行中时显示；由 _on_create_status_change 控制 pack / pack_forget

        self.create_status_var.trace_add("write", self._on_create_status_change)
        ttk.Separator(resp_header, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(4, 0))

        response_body = ttk.Frame(response_outer, padding=(4, 4, 4, 4))
        response_body.pack(fill=tk.BOTH, expand=True)
        self.response_text = scrolledtext.ScrolledText(response_body, wrap=tk.WORD, height=9, font=("Consolas", 10))
        self.response_text.pack(fill=tk.BOTH, expand=True)
        bind_json_word_select(self.response_text)

    # -------- SKU 输入框切换 --------

    def _copy_var(self, value_var) -> None:
        value = value_var.get().strip()
        if value and value != "-":
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.status_var.set(f"已复制单号: {value}")

    def _on_create_status_change(self, *args) -> None:
        """:param args: Tk trace 回调占位参数。"""
        val = self.create_status_var.get()
        color = {
            "待执行": "#FF8F00",
            "执行中": "#FF6F00",
            "执行完成": "#2E7D32",
            "执行失败": "#C62828",
            "已终止": "#C62828",
        }.get(val, "#FF8F00")
        self.create_status_label.config(fg=color)
        if val == "执行中":
            self.terminate_btn.pack(side=tk.LEFT, padx=(10, 0))
        else:
            self.terminate_btn.pack_forget()

    def _clear_sku_frame(self) -> None:
        for child in self.sku_frame.winfo_children():
            child.destroy()

    def _build_sku_single(self) -> None:
        self._clear_sku_frame()
        ttk.Label(self.sku_frame, text="outer_sku_id:").pack(side=tk.LEFT)
        ttk.Entry(self.sku_frame, textvariable=self.outer_sku1_var, width=38).pack(side=tk.LEFT, padx=6)

    def _build_sku_double(self) -> None:
        self._clear_sku_frame()
        ttk.Label(self.sku_frame, text="outer_sku1:").pack(side=tk.LEFT)
        ttk.Entry(self.sku_frame, textvariable=self.outer_sku1_var, width=28).pack(side=tk.LEFT, padx=4)
        ttk.Label(self.sku_frame, text="outer_sku2:").pack(side=tk.LEFT)
        ttk.Entry(self.sku_frame, textvariable=self.outer_sku2_var, width=28).pack(side=tk.LEFT, padx=4)

    def _rebase_tid(self, body: Any, base_tid: str, new_tid: str) -> Any:
        """把模板中基准 tid 及 tid+111 统一替换为新单号。"""
        if base_tid and new_tid and base_tid != new_tid:
            body = replace_exact_value(body, base_tid, new_tid)
            body = replace_exact_value(body, base_tid + "111", new_tid + "111")
        return body

    def _gen_tid(self) -> str:
        """按 tid 前缀 + 当前时间（到毫秒）生成 20 位单号，避免重复。"""
        now = datetime.now()
        return self.tid_prefix + now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"

    def _apply_template(self, template: Any) -> None:
        body = copy.deepcopy(template)
        new_tid = self._gen_tid()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base_tid = str(body.get("tid", "")) if isinstance(body, dict) else ""
        body = self._rebase_tid(body, base_tid, new_tid)
        body = replace_datetime_strings(body, now_str)
        self._current_tid = new_tid
        self._set_body(body)
        self.current_tid_var.set(new_tid)

    def _on_msg_type_change(self, event=None) -> None:
        if self.msg_type_var.get() == "2行明细":
            self._build_sku_double()
            self._apply_template(SAMPLE_DOUBLE)
        else:
            self._build_sku_single()
            self._apply_template(SAMPLE_SINGLE)

    # -------- 逻辑仓查询 --------

    def _fill_logic_stores(self, stores) -> int:
        """用查询结果填充逻辑仓下拉，返回条目数。"""
        self._logic_store_map.clear()
        names = []
        for store in stores or []:
            name = str(store.get("ename") or store.get("ecode") or "").strip()
            store_id = store.get("id")
            if not name or store_id is None:
                continue
            self._logic_store_map[name] = store_id
            names.append(name)
        self.logic_store_combo["values"] = names
        return len(names)

    def _load_logic_stores(self, silent: bool = False) -> None:
        """从基础数据读取逻辑仓列表，填充下拉框。"""
        env_key = EC_ENV_OPTIONS.get(self.env_var.get(), EC_DEFAULT_ENV_KEY)
        from api_tool import get_basic_logic_stores
        stores = get_basic_logic_stores(env_key)
        if stores:
            count = self._fill_logic_stores(stores)
            self.status_var.set(f"已从基础数据加载 {count} 个逻辑仓，选择后点击查询")
            return
        if silent:
            self._logic_store_map.clear()
            self.logic_store_combo["values"] = []
            self.logic_store_var.set("")
            return
        self.status_var.set("基础数据为空")
        messagebox.showwarning("提示", "先更新工具基础数据：请到「基础数据」页点击「查询逻辑仓」")

    def reload_logic_stores(self, silent: bool = False) -> None:
        """标签页切换时静默刷新逻辑仓下拉。"""
        self._load_logic_stores(silent=silent)

    def _on_logic_store_key(self, event=None) -> None:
        """输入防抖：停止输入后触发模糊查询。"""
        if self._logic_store_after_id is not None:
            self.root.after_cancel(self._logic_store_after_id)
            self._logic_store_after_id = None
        self._logic_store_after_id = self.root.after(300, self._query_logic_stores)

    def _query_logic_stores(self) -> None:
        """按输入关键字在基础数据中模糊筛选逻辑仓。"""
        self._logic_store_after_id = None
        keyword = self.logic_store_var.get().strip().lower()
        env_key = EC_ENV_OPTIONS.get(self.env_var.get(), EC_DEFAULT_ENV_KEY)
        from api_tool import get_basic_logic_stores
        stores = get_basic_logic_stores(env_key)
        if not stores:
            self._load_logic_stores()
            return
        if not keyword:
            self._fill_logic_stores(stores)
            return
        filtered = [
            s for s in stores
            if keyword in str(s.get("ename") or "").lower()
            or keyword in str(s.get("ecode") or "").lower()
        ]
        count = self._fill_logic_stores(filtered)
        self.status_var.set(f"匹配到 {count} 个逻辑仓")
        if count:
            self.logic_store_combo.event_generate("<Down>")

    def _query_max_sku(self) -> None:
        name = self.logic_store_var.get().strip()
        logic_store_id = self._logic_store_map.get(name)
        if logic_store_id is None:
            messagebox.showwarning("提示", "请先选择逻辑仓")
            return

        is_double = self.msg_type_var.get() == "2行明细"
        limit = 2 if is_double else 1
        env_key = EC_ENV_OPTIONS.get(self.env_var.get(), EC_DEFAULT_ENV_KEY)
        from api_tool import get_basic_inventory_by_store, get_basic_available_skus_by_store
        if not get_basic_inventory_by_store(env_key, logic_store_id):
            self.status_var.set("基础数据库存为空")
            messagebox.showwarning("提示", "先更新工具基础数据：请到「基础数据」页点击「查询逻辑仓库存」")
            return
        rows = get_basic_available_skus_by_store(env_key, logic_store_id, limit=limit)
        skus = [str(r.get("ps_c_sku_ecode") or "").strip() for r in rows if str(r.get("ps_c_sku_ecode") or "").strip()]
        self._apply_max_skus(name, skus, None, is_double)

    def _apply_max_skus(self, name: str, skus, error, is_double: bool) -> None:
        self.query_sku_btn.config(state=tk.NORMAL)
        if error:
            self.status_var.set(f"查询失败: {error}")
            return
        need = 2 if is_double else 1
        if len(skus) < need:
            self.status_var.set(f"逻辑仓[{name}]可用 SKU 不足 {need} 个（仅查到 {len(skus)} 个）")
            messagebox.showwarning("提示", f"逻辑仓[{name}]可用 SKU 不足 {need} 个，请检查库存")
            return
        self.outer_sku1_var.set(skus[0])
        if is_double:
            self.outer_sku2_var.set(skus[1])
            self.status_var.set(f"已查询逻辑仓[{name}]可用量前2个 SKU: {skus[0]}、{skus[1]}")
        else:
            self.status_var.set(f"已查询到逻辑仓[{name}]可用量最大 SKU: {skus[0]}")

    def _restore_sample(self) -> None:
        if self.msg_type_var.get() == "2行明细":
            self._apply_template(SAMPLE_DOUBLE)
        else:
            self._apply_template(SAMPLE_SINGLE)

    # -------- 报文操作 --------

    def _set_body(self, body: Any) -> None:
        self.body_text.delete("1.0", tk.END)
        self.body_text.insert("1.0", json.dumps(body, ensure_ascii=False, indent=2))

    def _get_body(self) -> tuple[Any, str]:
        raw = self.body_text.get("1.0", tk.END).strip()
        return json.loads(raw), raw

    def format_body(self) -> None:
        try:
            body, _ = self._get_body()
            self._set_body(body)
            self.status_var.set("JSON 格式正确")
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON 格式错误", str(exc))

    def replace_outer_sku(self) -> None:
        try:
            body, _ = self._get_body()
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON 格式错误", str(exc))
            return

        items = body.get("items", []) if isinstance(body, dict) else []
        if self.msg_type_var.get() == "2行明细" and len(items) >= 2:
            sku1 = self.outer_sku1_var.get().strip()
            sku2 = self.outer_sku2_var.get().strip()
            if sku1:
                items[0]["outer_sku_id"] = sku1
            if sku2:
                items[1]["outer_sku_id"] = sku2
            self._set_body(body)
            self.status_var.set(f"已替换: outer_sku_id[0]={sku1 or '(未填)'}  outer_sku_id[1]={sku2 or '(未填)'}")
        else:
            value = self.outer_sku1_var.get().strip()
            if not value:
                messagebox.showwarning("提示", "请输入 outer_sku_id")
                return
            count = replace_field_values(body, "outer_sku_id", value)
            self._set_body(body)
            self.status_var.set(f"已替换 {count} 个 outer_sku_id")

    # -------- 发送 --------

    def _append_line(self, msg: str) -> None:
        self.response_text.insert(tk.END, msg + "\n")
        self.response_text.see(tk.END)

    def send_order(self) -> None:
        try:
            body, body_json = self._get_body()
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON 格式错误", str(exc))
            return
        old_tid = str(body.get("tid", "")).strip() if isinstance(body, dict) else ""
        if not old_tid:
            messagebox.showwarning("提示", "请求报文缺少顶层 tid")
            return
        app_key = self.app_key_var.get().strip()
        app_secret = self.app_secret_var.get().strip()
        session = self.session_var.get().strip()
        if not all((app_key, app_secret, session)):
            messagebox.showwarning("提示", "appKey、appSecret、session 均不能为空")
            return

        # 主线程一次性读取 UI 状态，避免子线程访问 Tk 变量
        url = self.url_var.get().strip()
        chain = self.chain_var.get()
        env_key = EC_ENV_OPTIONS.get(self.env_var.get(), EC_DEFAULT_ENV_KEY)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sign, sign_source = build_sign(app_key, app_secret, session, timestamp, body_json)
        params = {
            "method": EC_METHOD, "v": EC_VERSION, "session": session,
            "sign": sign, "format": EC_FORMAT, "appKey": app_key, "timestamp": timestamp,
        }

        self.cancel_event.clear()
        self.send_btn.config(state=tk.DISABLED)
        self.status_var.set("正在执行...")
        self.create_status_var.set("执行中")
        self.response_text.delete("1.0", tk.END)

        def _worker() -> None:
            from api_tool import (
                OperationCancelled,
                advance_retail_order_to_target,
                query_platform_order_by_tid,
                query_platform_order_source_by_tid,
                query_retail_order_by_tid,
                run_platform_order_convert,
                run_source_batch_transfer,
            )

            def post(msg: str) -> None:
                self.root.after(0, lambda m=msg: self._append_line(m))

            def _sleep(seconds: float) -> None:
                deadline = time.time() + seconds
                while time.time() < deadline:
                    if self.cancel_event.is_set():
                        raise OperationCancelled("用户终止")
                    time.sleep(min(0.5, deadline - time.time()))

            order_success = False
            chain_error = None
            cancelled = False
            try:
                post(f"▶ 开始执行（节点：{chain}）")
                post("▸ 下单中。。")
                response = requests.post(
                    url, params=params, data=body_json.encode("utf-8"),
                    headers={"Content-Type": "application/json"}, timeout=30,
                )
                try:
                    resp_json = response.json()
                except Exception:
                    resp_json = None
                try:
                    response_body = json.dumps(resp_json, ensure_ascii=False, indent=2)
                except Exception:
                    response_body = response.text
                business_ok = True
                if isinstance(resp_json, dict):
                    code = resp_json.get("code")
                    if code is not None and str(code) not in ("0", "200"):
                        business_ok = False
                order_success = (200 <= response.status_code < 300) and business_ok
                post(f"▸ 下单{'完成' if order_success else '失败'}。。（HTTP {response.status_code}）")
                post(f"响应体:\n{response_body}")

                # 若用户已点击终止，直接结束流程（仍会递增单号）
                if self.cancel_event.is_set():
                    raise OperationCancelled("用户终止")

                if order_success:
                    # 1) 查平台订单报文 trans_status
                    post("▸ 查询平台订单报文(SOURCE)中。。")
                    source = None
                    for _ in range(8):
                        source = query_platform_order_source_by_tid(env_key, old_tid)
                        if source:
                            break
                        _sleep(2)
                    if not source:
                        raise RuntimeError(f"未找到平台订单报文 tid={old_tid}")
                    trans_status = source.get("trans_status")
                    post(f"▸ 查询SOURCE完成（id={source.get('id')}, trans_status={trans_status}）")

                    if trans_status in (4, 5):
                        raise RuntimeError(f"平台订单报文转换异常: trans_status={trans_status}")

                    if trans_status == 0:
                        post("▸ 执行单据转换(batchTransfer)中。。")
                        run_source_batch_transfer(env_key, source["id"])
                        post("▸ 单据转换(batchTransfer)完成。。")
                        trans_status = None
                        for _ in range(10):
                            s2 = query_platform_order_source_by_tid(env_key, old_tid)
                            if s2:
                                trans_status = s2.get("trans_status")
                                if trans_status in (2, 4, 5):
                                    break
                            _sleep(2)
                        post(f"▸ 转换结果 trans_status={trans_status}")
                    elif trans_status == 2:
                        post("▸ SOURCE 已转换(trans_status=2)，跳过单据转换")

                    if trans_status in (4, 5):
                        raise RuntimeError(f"平台订单报文转换异常: trans_status={trans_status}")
                    if trans_status != 2:
                        raise RuntimeError(f"平台订单报文转换未完成: trans_status={trans_status}")

                    # 2) 待审核 / 配货中：平台订单 -> 零售发货单 -> 推进
                    if chain in ("平台订单报文-平台订单-待审核", "平台订单报文-平台订单-待审核-配货中"):
                        post("▸ 查询平台订单(IP_B_PLATFORM_ORDER)中。。")
                        po = None
                        for _ in range(8):
                            po = query_platform_order_by_tid(env_key, old_tid)
                            if po:
                                break
                            _sleep(2)
                        if not po:
                            raise RuntimeError(f"未找到平台订单 tid={old_tid}")
                        post(f"▸ 查询平台订单完成（id={po.get('id')}）")

                        post("▸ 执行单据转换(orderTransfer)中。。")
                        run_platform_order_convert(env_key, po["id"])
                        post("▸ 单据转换(orderTransfer)完成。。")

                        post("▸ 查询零售发货单中。。")
                        retail = None
                        for _ in range(10):
                            retail = query_retail_order_by_tid(env_key, old_tid)
                            if retail:
                                break
                            _sleep(2)
                        if not retail:
                            raise RuntimeError(f"未找到零售发货单 tid={old_tid}")
                        post(f"▸ 查询零售发货单完成（id={retail.get('id')}, status={retail.get('order_status')}）")

                        if retail.get("order_status") == 7:
                            post("▸ 零售发货单状态=已取消，结束制单流程")
                        else:
                            target = 1 if chain == "平台订单报文-平台订单-待审核" else 4
                            post(f"▸ 推进零售发货单到状态{target}中。。")
                            adv = advance_retail_order_to_target(
                                env_key, retail["id"], target_status=target, progress=post,
                                cancel_event=self.cancel_event,
                            )
                            post(f"▸ 推进完成（status={adv.get('status')}）")
                            for ex in adv.get("executed", []):
                                ok_flag = "成功" if ex.get("ok") else "失败"
                                post(f"   - 触发 {ex.get('name')}#{ex.get('id')}: {ok_flag} {ex.get('msg', '')}")

                    post("▸ 节点串联完成。。")
            except OperationCancelled:
                cancelled = True
                chain_error = "用户终止"
                post("▸ 已终止当前流程。。")
            except Exception as exc:
                chain_error = str(exc)
                post(f"▸ 执行异常。。（{chain_error}）")
            finally:
                self.root.after(0, lambda: self._finish_request(order_success, body, old_tid, chain_error, cancelled))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_request(self, order_success: bool, body: Any, old_tid: str, chain_error: str, cancelled: bool = False) -> None:
        if cancelled:
            self.create_status_var.set("已终止")
        else:
            self.create_status_var.set("执行完成" if (order_success and chain_error is None) else "执行失败")

        # 成功或终止时都继续替换单号
        if order_success or cancelled:
            if order_success:
                self.last_tid_var.set(old_tid)
            try:
                new_tid = self._gen_tid()
                self._current_tid = new_tid
                self.current_tid_var.set(new_tid)
                self._set_body(self._rebase_tid(body, old_tid, new_tid))
                if cancelled:
                    self.status_var.set(f"已终止当前流程，已刷新新单号：{old_tid} → {new_tid}")
                elif chain_error:
                    self.status_var.set(f"下单成功已刷新新单号，但串联失败: {chain_error}")
                else:
                    self.status_var.set(f"发送完成，已刷新新单号：{old_tid} → {new_tid}")
            except Exception as exc:
                new_tid = None
                self.status_var.set(f"发送完成，但单号生成失败: {exc}")
        else:
            if chain_error:
                self.status_var.set(f"下单失败: {chain_error}")
            else:
                self.status_var.set("下单失败，报文单号未变更")
        self.send_btn.config(state=tk.NORMAL)
        # 每次执行完保存配置与报文体，下次打开自动恢复
        self._save_state()


if __name__ == "__main__":
    root = tk.Tk()
    EcOrderApp(root)
    root.mainloop()