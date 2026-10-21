# -*- coding: utf-8 -*-
"""HTTP 请求封装：统一发起外部接口调用并规范异常。

支持 GET / POST / PUT / DELETE，raw / json / form 三种请求体，
支持关闭 SSL 证书校验（内网 https），捕获全量异常并统一返回。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import requests


def run_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    query: Optional[dict] = None,
    body: Optional[str] = None,
    body_type: str = "json",
    timeout: float = 15.0,
    ignore_ssl: bool = True,
) -> dict:
    """发起一次 HTTP 请求，返回标准化结果 dict。

    返回字段：
        success           是否成功拿到响应（网络层）
        status_code       响应状态码，网络失败为 0
        response_headers  响应头 dict
        response_body     响应体文本
        cost_ms           耗时（毫秒）
        error_msg         错误信息（成功时为空字符串）
    """
    method = (method or "GET").upper()
    start = time.perf_counter()

    result: dict = {
        "success": False,
        "status_code": 0,
        "response_headers": {},
        "response_body": "",
        "cost_ms": 0,
        "error_msg": "",
    }

    try:
        kwargs: dict[str, Any] = {
            "params": query or {},
            "timeout": timeout,
            "verify": not ignore_ssl,
        }
        if headers:
            kwargs["headers"] = headers

        if body:
            if body_type == "json":
                try:
                    kwargs["json"] = json.loads(body)
                except Exception:
                    kwargs["data"] = body
            elif body_type == "form":
                kwargs["data"] = body
            else:  # raw
                kwargs["data"] = body
                merged = dict(headers or {})
                merged.setdefault("Content-Type", "text/plain")
                kwargs["headers"] = merged

        resp = requests.request(method, url, **kwargs)

        result.update(
            success=True,
            status_code=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text,
        )
    except requests.exceptions.Timeout:
        result["error_msg"] = f"请求超时（超过 {timeout} 秒）"
    except requests.exceptions.SSLError:
        result["error_msg"] = "SSL 证书校验失败（可尝试开启忽略 SSL 校验）"
    except requests.exceptions.ConnectionError as exc:
        result["error_msg"] = f"连接失败（DNS 解析失败或目标拒绝连接）：{exc}"
    except requests.exceptions.RequestException as exc:
        result["error_msg"] = f"请求异常：{exc}"
    except Exception as exc:  # 兜底，避免未预期异常穿透
        result["error_msg"] = f"未知错误：{exc}"
    finally:
        result["cost_ms"] = int((time.perf_counter() - start) * 1000)

    return result