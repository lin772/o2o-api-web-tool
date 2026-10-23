# -*- coding: utf-8 -*-
"""OMS 登录与转单 API 封装（仅依赖 requests）"""

from __future__ import annotations

import base64
import hashlib
import random
import string
import time
from typing import Any, Dict, Tuple
from urllib.parse import quote, urljoin

try:
    import requests
    import urllib3
except ImportError as e:
    raise ImportError("请先安装 requests: pip install requests") from e


# ==================== 地址链路 RSA 签名工具 ====================

def encode_uri_component(value: str) -> str:
    """与后端 RsaSignUtil.encodeURIComponent / 前端 encodeURIComponent 对齐的 URL 编码。

    Java URLEncoder 保留字母数字与 ``. - * _``，空格转 ``+`` 后再转 ``%20``，
    并将 ``%21/%27/%28/%29/%7E`` 还原为 ``!'()~``。Python quote 默认只保留 ``_.-~`` 与字母数字，
    故通过 safe="*" 额外保留 ``*``，使二者最终输出一致。
    """
    encoded = quote(str(value), safe="*")
    return (
        encoded
        .replace("%21", "!")
        .replace("%27", "'")
        .replace("%28", "(")
        .replace("%29", ")")
        .replace("%7E", "~")
    )


def build_sign_string(params: Dict[str, Any]) -> str:
    """构建待签名字符串：过滤空值 -> key 升序 -> key=encodeURIComponent(value) 用 & 连接。"""
    filtered = {}
    for key, value in params.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            filtered[key] = text
    parts = [f"{key}={encode_uri_component(filtered[key])}" for key in sorted(filtered)]
    return "&".join(parts)


def generate_nonce(length: int = 16) -> str:
    """生成随机 nonce。"""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def rsa_public_encrypt(plain_text: str, public_key: str) -> str:
    """使用 RSA 公钥对明文做 PKCS1 v1.5 加密并返回 base64 字符串。"""
    try:
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_v1_5
    except ImportError as e:
        raise ImportError("请先安装 pycryptodome: pip install pycryptodome") from e

    key = RSA.import_key(public_key)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(plain_text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def sign_address_payload(payload: Dict[str, Any], public_key: str) -> Dict[str, Any]:
    """为 saveOrderAddress 请求体生成 timestamp/nonce/sign（公钥加密 SHA256 摘要）。"""
    data = {k: v for k, v in payload.items() if k != "sign"}
    data["timestamp"] = str(int(time.time() * 1000))
    data["nonce"] = generate_nonce()
    sign_string = build_sign_string(data)
    sha256_hex = hashlib.sha256(sign_string.encode("utf-8")).hexdigest()
    data["sign"] = rsa_public_encrypt(sha256_hex, public_key)
    return data


class OmsClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        # 与浏览器 AJAX 请求保持一致：OMS 的 save/submit 等接口依赖这些头做鉴权/CSRF 校验
        self.session.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url + "/",
        })
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def login(self) -> Dict[str, Any]:
        captcha_resp = self.session.get(f"{self.base_url}/p/c/getCaptcha", timeout=20)
        captcha_resp.raise_for_status()
        captcha = captcha_resp.json().get("captcha", "")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = self.session.post(
            f"{self.base_url}/p/c/login",
            data={
                "username": self.username,
                "password": self.password,
                "key": "",
                "flag": "1",
                "captcha": captcha,
                "language": "zh-CN",
            },
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 1:
            raise RuntimeError(f"OMS 登录失败: {data}")
        return data

    def batch_transfer_source(self, source_id: int) -> Tuple[int, Dict[str, Any]]:
        """平台订单报文 -> 平台订单（等价 MQ 转单）"""
        url = urljoin(self.base_url + "/", "api/cs/oc/oms/ipBPlatformOrderSource/batchTransfer")
        resp = self.session.post(url, json={"ids": [source_id]}, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def batch_convert_platform_return(self, return_order_id: int) -> Tuple[int, Dict[str, Any]]:
        """ip_b_platform_return_order -> oc_b_after_sale_apply_order"""
        url = urljoin(self.base_url + "/", "api/cs/oc/oms/ocBPlatformReturnOrder/batchConvert")
        resp = self.session.post(url, json={"ids": [return_order_id]}, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def transfer_platform_order(self, platform_order_id: int) -> Tuple[int, Dict[str, Any]]:
        """平台订单 -> 零售发货单"""
        url = urljoin(self.base_url + "/", "api/cs/oc/oms/platformOrder/orderTransfer")
        payload = {
            "query": {},
            "table": "IP_B_PLATFORM_ORDER",
            "tableid": 249231869,
            "ids": [str(platform_order_id)],
            "menu": "adtable_249231869",
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def ip_common_transfer(self, table: str, tableid: int, ids, menu: str) -> Tuple[int, Dict[str, Any]]:
        """淘宝/京东等中间表通用转单（api/cs/ip/common/transfer），转单后直接生成零售发货单 oc_b_order。"""
        url = urljoin(self.base_url + "/", "api/cs/ip/common/transfer")
        id_list = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        payload = {
            "query": {"ISTRANS": ["=0"]},
            "table": table,
            "tableid": tableid,
            "ids": [str(x) for x in id_list],
            "menu": menu,
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def set_un_transfer(self, platform_order_id: int) -> Tuple[int, Dict[str, Any]]:
        """平台订单-标记未转换"""
        url = urljoin(self.base_url + "/", "api/cs/oc/oms/platformOrder/setUnTransfer")
        resp = self.session.post(url, json={"ids": [int(platform_order_id)]}, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def platform_order_save_address(self, signed_payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """平台订单-保存用户收货地址（请求体需含 RSA 签名 timestamp/nonce/sign）。"""
        url = urljoin(self.base_url + "/", "api/fi/platformOrder/saveOrderAddress")
        resp = self.session.post(url, json=signed_payload, timeout=self.timeout)
        return resp.status_code, self._json(resp)

    def audit_order(self, retail_order_id: int) -> Tuple[int, Dict[str, Any]]:
        """零售发货单-手工审核（审核按钮）。"""
        url = urljoin(self.base_url + "/", "api/cs/oc/oms/v1/auditOrder")
        resp = self.session.post(
            url,
            json={"ids": [int(retail_order_id)], "type": 1},
            timeout=self.timeout,
        )
        return resp.status_code, self._json(resp)

    @staticmethod
    def _json(resp: requests.Response) -> Dict[str, Any]:
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:2000]}
