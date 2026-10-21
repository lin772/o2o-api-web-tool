# -*- coding: utf-8 -*-
"""统一日志配置：业务日志与请求日志经由此处输出到控制台。"""

import logging

_configured = False


def get_logger(name: str = "o2o_web") -> logging.Logger:
    """返回指定名称的 logger，并在首次调用时完成全局日志配置。

    配置只做一次，避免重复初始化覆盖其他模块设置的 handler。
    """
    global _configured
    if not _configured:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        _configured = True
    return logging.getLogger(name)