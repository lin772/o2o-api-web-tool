# -*- coding: utf-8 -*-
"""临时数据库连通性测试。"""
import traceback

from settings import load_config, get_env_config
from order_service import db_connect

CFG = load_config()
ENV = get_env_config("test", CFG)


def test_db(name, db_key, table):
    db_cfg = ENV[db_key]
    print(f"[{name}] {db_cfg['user']}@{db_cfg['host']}:{db_cfg.get('port')}/{db_cfg['database']} ...", flush=True)
    try:
        conn = db_connect(db_cfg, CFG)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                row = cur.fetchone()
                print(f"    成功：表 {table} 行数 = {row['c']}", flush=True)
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"    失败：{type(e).__name__}: {e}", flush=True)
        return False


def main():
    ok = True
    ok &= test_db("base", "base_db", "cp_c_shop")
    ok &= test_db("order", "order_db", "oc_b_order")
    ok &= test_db("order", "order_db", "IP_B_PLATFORM_ORDER_SOURCE")
    print("RESULT:", "ALL_OK" if ok else "SOME_FAILED", flush=True)


if __name__ == "__main__":
    main()
