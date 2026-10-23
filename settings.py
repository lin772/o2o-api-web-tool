# -*- coding: utf-8 -*-
"""配置加载：每次调用均重新读取 config/config.yaml，手工修改可即时生效。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError as e:
    raise ImportError("请先安装 PyYAML: pip install pyyaml") from e

try:
    from src import db_manager
except Exception:  # noqa: BLE001
    db_manager = None  # type: ignore[assignment]

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "config" / "config.yaml"
MESSAGE_ROOT = TOOL_DIR / "公域报文汇总"

# 组合环境 key 分隔符：project_name + SEP + env，用于多项目同名环境（如标品 TEST / 非标品 TEST）时唯一定位。
ENV_KEY_SEP = "||"


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def derive_env_tag(env_key: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """把环境 key 稳定派生为取号隔离维度 env_tag（如 env_A / env_B）。

    取号要求「A 库内部唯一、B 库内部唯一，A/B 之间允许重复」，隔离维度即
    env_tag。当前工程里的 env_key 形如「项目名称||环境」（小写），并没有现成的
    env_A/env_B 标签，因此这里做稳定派生：

      1. 若 config/config.yaml 里配置了 env_tag_map（env_key -> env_tag 的显式映射），
         优先按配置返回，可精确指定 env_A / env_B；
      2. 否则把 env_key 归一化：小写 + 非字母数字字符折叠为下划线，去掉首尾下划线；
      3. 归一化后为空则退回 "default"。

    归一化保证同一环境每次取号得到相同 env_tag，且不同环境得到不同 env_tag，
    从而天然满足「同环境同前缀同一天原子自增、不同环境互相隔离」的约束。
    """
    cfg = cfg or load_config()
    env_tag_map = (cfg.get("env_tag_map") or {}) if isinstance(cfg, dict) else {}
    key = (env_key or "").strip()
    if key and isinstance(env_tag_map, dict) and key in env_tag_map:
        mapped = str(env_tag_map[key]).strip()
        if mapped:
            return mapped
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", key.lower()).strip("_")
    return normalized or "default"


def _env_rows() -> List[Dict[str, Any]]:
    if db_manager is None:
        return []
    try:
        return db_manager.get_env_configs_with_password()
    except Exception:
        return []


def _db_rows() -> List[Dict[str, Any]]:
    if db_manager is None:
        return []
    try:
        return db_manager.get_db_configs_with_password()
    except Exception:
        return []


def _xxl_rows() -> List[Dict[str, Any]]:
    if db_manager is None:
        return []
    try:
        return db_manager.get_xxl_configs_with_password()
    except Exception:
        return []


def _db_role_for_name(db_name: str) -> str:
    """把库名映射为角色 key：pre_r3_base -> base_db 等。"""
    name = (db_name or "").strip().lower()
    if not name:
        return ""
    for role in ("base", "order", "sg", "ac", "ad"):
        if name == role or name.endswith("_" + role):
            return role + "_db"
    for role in ("base", "order", "sg", "ac", "ad"):
        if role in name:
            return role + "_db"
    return ""


def _int_port(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 3306


def _combined_key(project: str, env: str) -> str:
    """生成环境 key：有项目名时用「项目名称||环境」，否则仅环境（均小写）。"""
    p = (project or "").strip()
    e = (env or "").strip()
    if p:
        return f"{p}{ENV_KEY_SEP}{e}".lower()
    return e.lower()


def _display_label(project: str, env: str) -> str:
    """生成环境展示标签：有项目名时用「项目名称 / 环境」，否则仅环境。"""
    p = (project or "").strip()
    e = (env or "").strip()
    if p:
        return f"{p} / {e}"
    return e


def _parse_env_key(env_key: Optional[str]) -> tuple:
    """解析环境 key -> (project_name, env)，均为小写；纯 env 时 project_name 为 None。"""
    key = (env_key or "").strip().lower()
    if ENV_KEY_SEP in key:
        project, env = key.split(ENV_KEY_SEP, 1)
        return (project.strip(), env.strip())
    return (None, key)


def _row_matches_env(row: Dict[str, Any], project: Optional[str], env: str) -> bool:
    """判断配置行是否匹配 (project, env)；project 为空时不限定项目，保持旧逻辑兼容。"""
    row_env = (row.get("env") or "").strip().lower()
    if env and row_env != env:
        return False
    if project:
        row_project = (row.get("project_name") or "").strip().lower()
        if row_project != project:
            return False
    return True


def list_env_keys(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """返回去重后的环境下拉 key（小写），优先读 env_config 表。

    多项目场景下使用「项目名称||环境」组合 key，确保同名环境可区分。
    """
    keys: List[str] = []
    seen = set()
    for row in _env_rows():
        env = (row.get("env") or "").strip()
        if not env:
            continue
        key = _combined_key(row.get("project_name"), env)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if keys:
        return keys
    cfg = cfg or load_config()
    envs = cfg.get("environments") or {}
    if envs:
        return list(envs.keys())
    return ["test"] if "TEST" in cfg else []


def env_label_map(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """返回 {env_key: label}，优先读 env_config 表。

    多项目场景下 key 为「项目名称||环境」，label 为「项目名称 / 环境」。
    """
    result: Dict[str, str] = {}
    for row in _env_rows():
        env = (row.get("env") or "").strip()
        if not env:
            continue
        key = _combined_key(row.get("project_name"), env)
        if key not in result:
            result[key] = _display_label(row.get("project_name"), env)
    if result:
        return result
    cfg = cfg or load_config()
    envs = cfg.get("environments") or {}
    if envs:
        return {k: (v.get("label") or k.upper()) for k, v in envs.items()}
    return {"test": "TEST"} if "TEST" in cfg else {}


def get_env_by_label(label: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    target = (label or "").strip()
    target_lower = target.lower()
    # 直接传入组合 key（项目名称||环境）时原样返回（小写），避免被误解析为纯环境
    if ENV_KEY_SEP in target:
        return target_lower
    labels = env_label_map(cfg)
    for key, lbl in labels.items():
        if (lbl or "").strip().lower() == target_lower or key == target_lower:
            return key
    return next(iter(labels), "test") if labels else "test"


def env_options_for_gui(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """返回 GUI 环境下拉 {展示标签: env_key}，如 {"TEST": "test"}。"""
    return {label: key for key, label in env_label_map(cfg).items()}


def get_oms_env_view(env_key: str) -> Optional[Dict[str, Any]]:
    """从 env_config 表合成 OMS 视图（嵌套 login 结构），未找到返回 None。"""
    project, env = _parse_env_key(env_key)
    for row in _env_rows():
        if not _row_matches_env(row, project, env):
            continue
        return {
            "base_url": (row.get("page_url") or "").strip(),
            "login": {
                "username": (row.get("account") or "").strip(),
                "password": row.get("password") or "",
            },
        }
    return None


def get_db_config_view(env_key: Optional[str] = None) -> Dict[str, Any]:
    """从 db_config 表合成数据库角色视图（base_db/order_db/sg_db/ac_db/ad_db）。"""
    project, env = _parse_env_key(env_key or "")
    result: Dict[str, Any] = {}
    for row in _db_rows():
        if not _row_matches_env(row, project, env):
            continue
        role = _db_role_for_name(row.get("db_name"))
        if not role or role in result:
            continue
        result[role] = {
            "host": row.get("db_address") or "",
            "port": _int_port(row.get("port")),
            "user": row.get("username") or "",
            "password": row.get("password") or "",
            "database": row.get("db_name") or "",
            "charset": "utf8mb4",
        }
    if "order_db" not in result and "base_db" in result:
        result["order_db"] = result["base_db"]
    return result


def get_xxl_job_map() -> Dict[str, Any]:
    """从 xxl_config 表合成 {key: {url, username, password}}。

    多项目场景下 key 为「项目名称||环境」，同时保留纯 env key 以兼容旧调用。
    """
    result: Dict[str, Any] = {}
    for row in _xxl_rows():
        env = (row.get("env") or "").strip()
        addr = (row.get("address") or "").strip()
        if not env or not addr:
            continue
        project = (row.get("project_name") or "").strip()
        cfg = {
            "url": addr,
            "username": (row.get("account") or "").strip() or "admin",
            "password": row.get("password") or "",
        }
        result[_combined_key(project, env)] = cfg
        # 向后兼容：纯 env key 仅在未被其他项目同名环境占用时保留
        plain = env.lower()
        if plain not in result:
            result[plain] = cfg
    return result


def get_env_config(env_key: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按环境合成完整环境视图（oms + 5 个数据库），数据来自配置表。"""
    project, env = _parse_env_key(env_key)
    oms = get_oms_env_view(env_key)
    db_view = get_db_config_view(env_key)
    if oms is None and not db_view:
        raise ValueError(f"未找到环境 [{env_key or ''}] 的配置，请先在「环境配置」「数据库配置」中维护")
    label = env or (env_key or "")
    for row in _env_rows():
        if _row_matches_env(row, project, env):
            label = (row.get("env") or "").strip() or label
            break
    return {
        "label": label,
        "oms": oms or {"base_url": "", "login": {"username": "", "password": ""}},
        "order_db": db_view.get("order_db") or db_view.get("base_db") or {},
        "base_db": db_view.get("base_db") or {},
        "sg_db": db_view.get("sg_db") or {},
        "ac_db": db_view.get("ac_db") or {},
        "ad_db": db_view.get("ad_db") or {},
    }


def list_platform_folders() -> List[str]:
    if not MESSAGE_ROOT.is_dir():
        return []
    return sorted(
        d.name for d in MESSAGE_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def scenario_exclude_keywords(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    keys = cfg.get("scenario_exclude_keywords")
    if keys is None:
        return ["拒收", "退货", "换货"]
    return [str(k) for k in keys if k]


def is_excluded_scenario(filename: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    name = filename.lower()
    return any(kw.lower() in name for kw in scenario_exclude_keywords(cfg))


def reverse_scenario_keywords(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    keys = cfg.get("reverse_scenario_keywords")
    if keys is None:
        return ["拒收", "退货", "换货"]
    return [str(k) for k in keys if k]


def is_reverse_scenario(filename: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    name = filename.lower()
    return any(kw.lower() in name for kw in reverse_scenario_keywords(cfg))


def list_reverse_scenarios(platform: str, cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    folder = MESSAGE_ROOT / platform
    if not folder.is_dir():
        return []
    return sorted(
        f.name
        for f in folder.iterdir()
        if f.is_file()
        and f.suffix.lower() in {".txt", ".json"}
        and is_reverse_scenario(f.name, cfg)
    )


def list_scenarios(platform: str, cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    folder = MESSAGE_ROOT / platform
    if not folder.is_dir():
        return []
    return sorted(
        f.name
        for f in folder.iterdir()
        if f.is_file()
        and f.suffix.lower() in {".txt", ".json"}
        and not is_excluded_scenario(f.name, cfg)
    )


def sku_query_page_size(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or load_config()
    return max(10, int((cfg.get("sku_query") or {}).get("page_size", 50)))


def db_connect_options(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """数据库连接超时与重试（OceanBase 公网偶发 2013 需重试）。"""
    cfg = cfg or load_config()
    opts = cfg.get("db_connect") or {}
    return {
        "connect_timeout": max(5, int(opts.get("connect_timeout", 30))),
        "read_timeout": max(10, int(opts.get("read_timeout", 60))),
        "write_timeout": max(10, int(opts.get("write_timeout", 60))),
        "retry_times": max(1, int(opts.get("retry_times", 3))),
        "retry_delay_seconds": max(0.5, float(opts.get("retry_delay_seconds", 2))),
    }


def get_platform_meta(platform: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    meta = (cfg.get("platform_meta") or {}).get(platform) or {}
    return {
        "platform_id": meta.get("platform_id"),
        "method": meta.get("method") or "",
        "message_format": meta.get("message_format") or "standplat",
    }


def platform_folder_by_id(platform_id: int, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """根据 OMS platform_id 反查配置中的平台文件夹名（如 57 -> 抖音）。"""
    cfg = cfg or load_config()
    pid = int(platform_id)
    for name, meta in (cfg.get("platform_meta") or {}).items():
        if meta.get("platform_id") is not None and int(meta["platform_id"]) == pid:
            return name
    return None


def platform_display_name(platform_id: int, cfg: Optional[Dict[str, Any]] = None) -> str:
    folder = platform_folder_by_id(platform_id, cfg)
    return folder if folder else f"platform_id={platform_id}"


def sku_field_keys(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    return list(cfg.get("sku_field_keys") or ["outer_sku_id", "outerSkuId", "outer_sku_code"])
