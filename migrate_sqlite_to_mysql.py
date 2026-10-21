# -*- coding: utf-8 -*-
"""SQLite -> MySQL 一键迁移脚本。

用法：
    python migrate_sqlite_to_mysql.py

流程：
    1. 解析 WSL docker-desktop 的 eth0 IP（MySQL 宿主机地址）
    2. 扫描本地 SQLite 文件中的全部数据表（自动排除 sqlite_ 内部表）
    3. 按 SQLite 实际表结构自动生成 MySQL 8.0 兼容 DDL 并在 MySQL 建表
    4. 逐表读取 SQLite 全量数据并逐条插入 MySQL（显式保留原 id）
    5. 校验 MySQL 每张表行数与 SQLite 源表行数一致

依赖：仅内置模块 + pymysql（项目已有依赖，非新增第三方包）。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pymysql

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "api_history.db"

MYSQL_PORT = 3306
MYSQL_USER = "root"
MYSQL_PASSWORD = "123456"
MYSQL_DATABASE = "o2o_api_db"

# 手动兜底：WSL 自动获取 IP 失败时，可在此写死 Docker 宿主机 IP（留空则退出）。
FALLBACK_MYSQL_HOST = ""

# SQLite 内部表前缀，迁移时自动跳过。
SQLITE_INTERNAL_PREFIX = "sqlite_"


def get_wsl_docker_ip() -> str:
    """运行时执行 WSL 命令获取 docker-desktop 的 eth0 inet IP。"""
    cmd = ["wsl", "-d", "docker-desktop", "--", "ip", "addr", "show", "eth0"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"无法执行 WSL 命令获取 Docker IP：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"WSL 命令执行失败（退出码 {proc.returncode}）：{(proc.stderr or '').strip()}"
        )
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout or "")
    if not match:
        raise RuntimeError(f"未在输出中匹配到 inet IP：{proc.stdout}")
    return match.group(1)


def resolve_mysql_host() -> str:
    """解析 MySQL host，失败时打印红色警告并友好退出。"""
    try:
        host = get_wsl_docker_ip()
    except Exception as exc:
        print(f"\033[91m[错误] {exc}\033[0m")
        if FALLBACK_MYSQL_HOST:
            print(f"[提示] 使用手动 fallback IP：{FALLBACK_MYSQL_HOST}")
            return FALLBACK_MYSQL_HOST
        raise SystemExit("\033[91m无法获取 WSL Docker IP，程序退出。\033[0m")
    print(f"当前WSL Docker IP：{host}")
    return host


# ---------------------------------------------------------------------------
# 类型 / DDL 适配
# ---------------------------------------------------------------------------
def sqlite_type_to_mysql(decl_type: str, dflt_value) -> str:
    """把 SQLite 声明类型映射为 MySQL 类型。

    - INTEGER -> INT / BIGINT
    - TIMESTAMP/DATETIME/DATE -> DATETIME
    - 带 DEFAULT 字面量的 TEXT 列 -> VARCHAR（MySQL TEXT 不能带默认值）
    - 其余 TEXT -> TEXT
    """
    t = (decl_type or "").upper().strip()

    # 保留显式长度：VARCHAR(n) / CHAR(n)
    m = re.search(r"(VARCHAR|CHAR|CHARACTER)\s*\(\s*(\d+)\s*\)", t)
    if m:
        return f"VARCHAR({m.group(2)})"

    if "INT" in t:
        return "BIGINT" if "BIG" in t else "INT"
    if "TIME" in t or "DATE" in t:
        return "DATETIME"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "DOUBLE"
    if "BOOL" in t:
        return "TINYINT(1)"

    # TEXT 且带字面量默认值（如 '正向'）必须改用 VARCHAR
    if dflt_value is not None and _is_literal_default(dflt_value):
        return "VARCHAR(255)"
    return "TEXT"


def _is_literal_default(dflt_value) -> bool:
    s = str(dflt_value).strip()
    upper = s.upper()
    if upper in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
        return False
    if upper in ("NULL",):
        return False
    return True


def mysql_default(dflt_value) -> str:
    """把 SQLite 默认值翻译为 MySQL DEFAULT 子句（含前导空格）。"""
    if dflt_value is None:
        return ""
    s = str(dflt_value).strip()
    upper = s.upper()
    if upper == "CURRENT_TIMESTAMP":
        return " DEFAULT CURRENT_TIMESTAMP"
    if upper == "CURRENT_DATE":
        return " DEFAULT CURRENT_DATE"
    if upper == "CURRENT_TIME":
        return " DEFAULT CURRENT_TIME"
    # 字面量默认值（SQLite 里通常已带单引号，如 '正向'）
    return f" DEFAULT {s}"


def build_create_table(table: str, cols: list) -> str:
    """根据 SQLite PRAGMA table_info 结果生成 MySQL CREATE TABLE 语句。"""
    lines: list = []
    pk_cols: list = []
    for cid, name, decl_type, notnull, dflt, pk in cols:
        if pk:
            lines.append(f"  `{name}` INT NOT NULL AUTO_INCREMENT")
            pk_cols.append(name)
            continue
        col_type = sqlite_type_to_mysql(decl_type, dflt)
        seg = f"  `{name}` {col_type}"
        if notnull:
            seg += " NOT NULL"
        seg += mysql_default(dflt)
        lines.append(seg)

    if pk_cols:
        pk_sql = ", ".join(f"`{n}`" for n in pk_cols)
        lines.append(f"  PRIMARY KEY ({pk_sql})")

    body = ",\n".join(lines)
    return (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n{body}\n)"
        f" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )


# ---------------------------------------------------------------------------
# 迁移主流程
# ---------------------------------------------------------------------------
def scan_sqlite_tables(sqlite_conn) -> list:
    """返回全部业务数据表名（排除 sqlite_ 内部表）。"""
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    tables = []
    for (name,) in rows:
        if name.startswith(SQLITE_INTERNAL_PREFIX):
            print(f"[跳过] 内部表：{name}")
            continue
        tables.append(name)
    return tables


def get_table_columns(sqlite_conn, table: str) -> list:
    """返回表结构，每项为 (cid, name, type, notnull, dflt_value, pk)。"""
    return sqlite_conn.execute(
        f"PRAGMA table_info(`{table}`)"
    ).fetchall()


def table_row_count(sqlite_conn, table: str) -> int:
    return sqlite_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def read_table_data(sqlite_conn, table: str, cols: list):
    """读取整张表数据，返回 (列名列表, 行列表)。"""
    col_names = [c[1] for c in cols]
    rows = sqlite_conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return col_names, rows


def migrate_table(mysql_conn, table: str, cols: list, rows: list) -> int:
    """把一张表的数据逐条写入 MySQL，返回成功插入条数。"""
    col_names = [c[1] for c in cols]
    if not rows:
        return 0

    cols_sql = ", ".join(f"`{c}`" for c in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    insert_sql = f"INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})"

    cur = mysql_conn.cursor()
    inserted = 0
    try:
        for row in rows:
            cur.execute(insert_sql, tuple(row))
            inserted += 1
        mysql_conn.commit()
    except Exception:
        mysql_conn.rollback()
        raise
    finally:
        cur.close()
    return inserted


def main() -> None:
    print("=" * 64)
    print("SQLite -> MySQL 迁移开始")
    print("=" * 64)

    # 1. 解析 host
    host = resolve_mysql_host()

    # 2. 读取 SQLite
    if not SQLITE_PATH.is_file():
        raise SystemExit(f"\033[91m未找到 SQLite 文件：{SQLITE_PATH}\033[0m")
    print(f"[SQLite] {SQLITE_PATH}")

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        tables = scan_sqlite_tables(sqlite_conn)
        print(f"\n发现业务数据表 {len(tables)} 张：{tables}")
        if not tables:
            raise SystemExit("\033[91m未发现任何可迁移的数据表。\033[0m")

        table_schemas = {}
        sqlite_counts = {}
        for table in tables:
            cols = get_table_columns(sqlite_conn, table)
            table_schemas[table] = cols
            sqlite_counts[table] = table_row_count(sqlite_conn, table)
            print(f"  - {table}: {sqlite_counts[table]} 条")

        # 3. 连接 MySQL 并建库建表
        print(f"\n[MySQL] 连接 {host}:{MYSQL_PORT} ...")
        server_conn = pymysql.connect(
            host=host,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            charset="utf8mb4",
        )
        try:
            with server_conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}`"
                    " DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            server_conn.commit()
        finally:
            server_conn.close()

        mysql_conn = pymysql.connect(
            host=host,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset="utf8mb4",
        )
        try:
            with mysql_conn.cursor() as cur:
                for table in tables:
                    ddl = build_create_table(table, table_schemas[table])
                    print(f"\n[建表] {table}")
                    print(ddl)
                    cur.execute(ddl)
            mysql_conn.commit()

            # 4. 逐表迁移数据
            print("\n[迁移数据]")
            for table in tables:
                cols = table_schemas[table]
                col_names, rows = read_table_data(sqlite_conn, table, cols)
                inserted = migrate_table(mysql_conn, table, cols, rows)
                print(f"  - {table}: 迁移 {inserted} 条")

            # 5. 校验行数一致性
            print("\n[校验] MySQL 与 SQLite 行数对比")
            all_ok = True
            with mysql_conn.cursor() as cur:
                for table in tables:
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    mysql_count = cur.fetchone()[0]
                    src_count = sqlite_counts[table]
                    flag = "OK" if mysql_count == src_count else "不一致"
                    if mysql_count != src_count:
                        all_ok = False
                    print(
                        f"  - {table}: SQLite={src_count}  MySQL={mysql_count}  [{flag}]"
                    )
        finally:
            mysql_conn.close()
    finally:
        sqlite_conn.close()

    print("\n" + "=" * 64)
    if all_ok:
        print("迁移完成，所有表行数校验一致。")
    else:
        print("\033[91m迁移存在行数不一致，请检查上方输出。\033[0m")
        sys.exit(1)
    print("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[中断] 用户取消。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback

        print(f"\033[91m[迁移失败] {type(exc).__name__}: {exc}\033[0m")
        traceback.print_exc()
        sys.exit(1)