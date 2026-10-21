# -*- coding: utf-8 -*-
import pymysql

PWD = "BurgeoN$$316"
HOST = "t7p2qcho6pfk0-mi.aliyun-cn-hongkong-internet.oceanbase.cloud"


def q(host, db, sql, params=()):
    c = pymysql.connect(
        host=host, port=3306, user="bj_dev", password=PWD,
        database=db, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
    )
    try:
        cur = c.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        c.close()


rows = q(
    HOST, "dev_r3_base",
    "SELECT id, ecode, ename, BRAND_CODE, ps_c_brand_ids, ad_client_id "
    "FROM cp_c_store WHERE isactive='Y' ORDER BY id DESC LIMIT 8",
)
print("cp_c_store samples:")
for r in rows:
    print(r)

print()
print("sg_b_storage samples:")
rows2 = q(
    HOST, "dev_r3_sg",
    "SELECT id, cp_c_store_id, cp_c_store_ecode, cp_c_store_ename, ps_c_sku_ecode, "
    "ps_c_brand_id, qty_available, isactive FROM sg_b_storage ORDER BY id DESC LIMIT 8",
)
for r in rows2:
    print(r)

print()
print("ps_c_brand samples:")
rows3 = q(
    HOST, "dev_r3_base",
    "SELECT id, ps_c_brand_id, ecode, ename FROM ps_c_brand ORDER BY id LIMIT 20",
)
for r in rows3:
    print(r)