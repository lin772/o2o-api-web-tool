---
title: 默认模块
language_tabs:
  - shell: Shell
  - http: HTTP
  - javascript: JavaScript
  - ruby: Ruby
  - python: Python
  - php: PHP
  - java: Java
  - go: Go
toc_footers: []
includes: []
search: true
code_clipboard: true
highlight_theme: darkula
headingLevel: 2
generator: "@tarslib/widdershins v4.0.30"

---

# 默认模块

Base URLs:

# Authentication

# Default

## POST 模拟EC电商下单sandbox

POST /router

> Body 请求参数

```json
{
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
  "items": [
    {
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
    }
  ],
  "invoice_info": {
    "invoice_title_type": "个人",
    "invoice_title": "XXXX",
    "invoice_type": 2,
    "invoice_amount": 0,
    "invoice_identifier": "",
    "invoice_bank": "",
    "invoice_account": "",
    "invoice_address": "",
    "invoice_telephone": "",
    "invoice_email": ""
  },
  "payment_info": {
    "pay_way": "alipay",
    "pay_way_name": "支付宝",
    "transaction_no": "2023330011100001",
    "pay_amount": 0.00,
    "pay_time": "2026-07-03 17:06:45",
    "remark": ""
  },
  "promotion_info": {
    "type_name": "订单优惠",
    "type": 151,
    "amount": 1400.00,
    "apportion_detail": [
      {
        "oid": "99974738443772290626",
        "sku_id": "3392587707357442",
        "qty": 1,
        "sku_type": 1,
        "apportion_type": 1,
        "apportion_amount": 1400.00,
        "apportion_rate": 1.00
      }
    ]
  }
}
```

### 请求参数

|名称|位置|类型|必选|说明|
|---|---|---|---|---|
|method|query|string| 是 |none|
|v|query|string| 是 |none|
|session|query|string| 是 |none|
|sign|query|string| 是 |none|
|format|query|string| 是 |none|
|appKey|query|string| 是 |none|
|timestamp|query|string| 是 |none|
|Content-Type|header|string| 是 |none|
|body|body|object| 否 |none|
|» platform|body|integer| 是 |none|
|» shop_code|body|string| 是 |none|
|» real_shop_code|body|string| 是 |none|
|» status|body|string| 是 |none|
|» post_fee|body|integer| 是 |none|
|» num|body|integer| 是 |none|
|» delivery_time|body|string| 是 |none|
|» trade_update_time|body|string| 是 |none|
|» buyer_id|body|string| 是 |none|
|» tid|body|string| 是 |none|
|» tax_fee|body|number| 是 |none|
|» plantfor_count|body|integer| 是 |none|
|» total_fee|body|integer| 是 |none|
|» receiver_name|body|string| 是 |none|
|» payment|body|integer| 是 |none|
|» oaid|body|string| 是 |none|
|» ch_order_flag_tag|body|string| 是 |none|
|» buyer_message|body|string| 是 |none|
|» product_discount|body|integer| 是 |none|
|» seller_memo|body|string| 是 |none|
|» receiver_province|body|string| 是 |none|
|» receiver_city|body|string| 是 |none|
|» receiver_district|body|string| 是 |none|
|» reserve_varchar12|body|string| 是 |none|
|» receiver_mobile|body|string| 是 |none|
|» receiver_address|body|string| 是 |none|
|» reserve_decimal06|body|integer| 是 |none|
|» buyer_nick|body|string| 是 |none|
|» trade_create_time|body|string| 是 |none|
|» is_invoice|body|string| 是 |none|
|» delivery_method|body|integer| 是 |none|
|» orderType|body|integer| 是 |none|
|» stagePayStatus|body|string| 是 |none|
|» subsidy_area|body|string| 是 |none|
|» subsidy_type|body|integer| 是 |none|
|» presale_deposit_time|body|string| 是 |none|
|» items|body|[object]| 是 |none|
|»» num|body|integer| 否 |none|
|»» num_iid|body|string| 否 |none|
|»» delivery_time|body|string| 否 |none|
|»» oid|body|string| 否 |none|
|»» title|body|string| 否 |none|
|»» discount_fee|body|integer| 否 |none|
|»» tid|body|string| 否 |none|
|»» is_present|body|string| 否 |none|
|»» gift_relation|body|string| 否 |none|
|»» price|body|integer| 否 |none|
|»» total_fee|body|integer| 否 |none|
|»» payment|body|integer| 否 |none|
|»» outer_sku_id|body|string| 否 |none|
|»» reserve_decimal01|body|integer| 否 |none|
|»» integral|body|integer| 否 |none|
|»» pic_path|body|string| 否 |none|
|»» sku_id|body|string| 否 |none|
|»» sku_properties_name|body|string| 否 |none|
|»» outerIid|body|string| 否 |none|
|»» sku_tax_fee|body|integer| 否 |none|
|»» salesType|body|integer| 否 |none|
|»» subsidy_fee|body|integer| 否 |none|
|»» product_code|body|string| 否 |none|
|»» estimate_con_time|body|string| 否 |none|
|» invoice_info|body|object| 是 |none|
|»» invoice_title_type|body|string| 是 |none|
|»» invoice_title|body|string| 是 |none|
|»» invoice_type|body|integer| 是 |none|
|»» invoice_amount|body|integer| 是 |none|
|»» invoice_identifier|body|string| 是 |none|
|»» invoice_bank|body|string| 是 |none|
|»» invoice_account|body|string| 是 |none|
|»» invoice_address|body|string| 是 |none|
|»» invoice_telephone|body|string| 是 |none|
|»» invoice_email|body|string| 是 |none|
|» payment_info|body|object| 是 |none|
|»» pay_way|body|string| 是 |none|
|»» pay_way_name|body|string| 是 |none|
|»» transaction_no|body|string| 是 |none|
|»» pay_amount|body|integer| 是 |none|
|»» pay_time|body|string| 是 |none|
|»» remark|body|string| 是 |none|
|» promotion_info|body|object| 是 |none|
|»» type_name|body|string| 是 |none|
|»» type|body|integer| 是 |none|
|»» amount|body|integer| 是 |none|
|»» apportion_detail|body|[object]| 是 |none|
|»»» oid|body|string| 否 |none|
|»»» sku_id|body|string| 否 |none|
|»»» qty|body|integer| 否 |none|
|»»» sku_type|body|integer| 否 |none|
|»»» apportion_type|body|integer| 否 |none|
|»»» apportion_amount|body|integer| 否 |none|
|»»» apportion_rate|body|number| 否 |none|

> 返回示例

> 200 Response

```json
{}
```

### 返回结果

|状态码|状态码含义|说明|数据模型|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|none|Inline|

### 返回数据结构

# 数据模型

