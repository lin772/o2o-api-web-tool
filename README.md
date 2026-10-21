# O2O API 测试工具（集成版）

## 简介

O2O API 测试工具是一个集成了订单中间表复制、调整库存与公域造单功能的桌面应用。

**集成来源**：
- `VF_O2O_AoTo_Interface/O2O_API工具_分发包` - 基础 API 测试功能、S4/预发环境
- `VF_O2O_AoTo_UI/O2O_API工具_分发包` - UI版本环境(18980)、数据库订单复制功能

## 功能特性

### 订单复制
- **平台单号录入**：输入要复制的订单 tid
- **店铺下拉选择**：从 `CP_C_SHOP` 表查询店铺列表，支持下拉选择
- **商品SKU输入**：支持更换商品SKU（从 `PS_C_SKU` 表查询）
- **自动ID自增**：查询当前最大id值，自动+1避免冲突
- **自动TID生成**：生成新的唯一tid
- **Message字段更新**：自动更新message字段中的tid、platform、sellernick、outer_sku_id
- **字段映射**：根据选择的店铺自动更新相关字段

## 环境配置

环境、OMS、数据库连接信息集中配置在 `config/config.yaml` 中（S4 / TEST），在工具内点击「刷新配置」或重启即可生效。

## 字段映射规则

当选择店铺后，以下字段会自动更新：

| 字段名 | 取值来源 |
|--------|----------|
| platform_id | CP_C_SHOP.cp_c_platform_id |
| cp_c_shop_id | CP_C_SHOP.id |
| cp_c_shop_ecode | CP_C_SHOP.ecode |
| cp_c_shop_title | CP_C_SHOP.cp_c_shop_title |
| seller_nick | CP_C_SHOP.seller_nick |
| message.platform | CP_C_SHOP.cp_c_platform_id |
| message.sellernick | CP_C_SHOP.seller_nick |

当输入SKU后：

| 字段名 | 取值来源 |
|--------|----------|
| message.outer_sku_id | PS_C_SKU.ecode |

## 文件说明

| 文件/目录 | 说明 |
|-----------|------|
| `platform_order_gui.py` | 公域造单 UI 模块（已集成进 `api_tool.py`） |
| `config/config.yaml` | 环境、OMS、数据库、平台元数据（可手工修改） |
| `settings.py` | 配置加载（每次读取 yaml，改配置后点「刷新配置」） |
| `message_service.py` | 报文解析、SKU 槽位识别、预览构建 |
| `order_service.py` | 店铺/SKU 查询、入库、转单 |
| `oms_client.py` | OMS 登录与转单 API |
| `公域报文汇总/` | 各平台场景报文（自动扫描平台与场景） |
| `api_tool.py` | O2O API 测试 + 订单中间表复制 |
| `requirements.txt` | Python 依赖 |
| `启动工具.bat` | **统一启动入口**（O2O API / 订单复制 / 公域造单） |
| `安装依赖.bat` | 首次使用或发给别人前：安装 `requirements.txt` |
| `启动工具_调试.bat` | 带控制台启动，打不开时用此查看报错 |

### 包内自包含目录结构

```
O2O_API工具/
├── config/
│   └── config.yaml          # 所有可配置项（带中文注释）
├── 公域报文汇总/             # 各平台场景报文 .txt
├── platform_order_gui.py    # 可视化造单
├── settings.py / message_service.py / order_service.py / oms_client.py
└── requirements.txt
```

**不需要**再从仓库其他目录拷贝 `test-workspace/config`、`公域报文汇总`（根目录）等文件。

## 公域平台订单造单（可视化）

已集成到主工具左侧导航 **「公域平台造单」**，与 O2O API 测试、订单复制共用同一窗口。

```bash
pip install -r requirements.txt
python api_tool.py
# 或直接打开公域造单页：python api_tool.py --tab platform_order
```

或双击 `启动工具.bat`。

### 操作流程

1. 选择 **环境**（S4 / TEST）→ 同时切换 OMS 地址与数据库
2. 选择 **平台**、**场景报文**（来自 `公域报文汇总/` 子目录）
3. **查询并选择店铺** → 自动替换报文 `sellernick` / `seller_nick`
4. 按报文明细数量选择 **SKU**（多主品/主+主 会自动出现多个下拉框）
5. 点击 **仅预览** → 下方显示完整报文（可手工修改）
6. 点击 **生成订单** → 以当前预览区报文写入并触发转单

### 修改配置

编辑 `config/config.yaml` 后，在工具内点击 **刷新配置** 或重启即可生效。

重点配置段：

- `environments` — OMS 地址、登录账号、order_db / base_db
- `platform_meta` — 各平台 `platform_id`、`method`、`message_format`
- `transfer` — 是否自动报文转单、平台订单转零售单
- `sku_field_keys` — 识别报文中商品条码的字段名顺序

新增平台：在 `公域报文汇总/新平台名/` 放报文，并在 `platform_meta` 补充平台 ID。

### 命令行（可选）

```bash
python platform_order_cli.py --env s4 --platform 抖音 --seller-nick "VF台湾官旗店铺001" --sku VN0009PVDOR105000M
```

## 快速开始

### 方式一：桌面版（推荐）

```bash
pip install -r requirements.txt
python api_tool.py
```

或双击 `启动工具.bat`。左侧导航可切换：
- **基础数据** — 逻辑仓 / 逻辑仓库存查询
- **平台订单中间表复制** — 按 TID 复制订单
- **调整库存** — 调整库存
- **公域平台正向造单** — 按场景报文造单并转单
- **公域平台逆向造单** — 逆向造单

### 方式二：打包为 EXE

```bash
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --windowed --name "O2O_API_Tool" api_tool.py
```

## 使用说明

### 订单复制

1. 切换到"订单复制"标签页
2. 输入**平台单号 TID**：要复制的订单tid
3. **选择店铺**（可选）：从下拉框选择目标店铺
4. **输入商品SKU**（可选）：填写新的SKU编码
5. 点击"复制订单"按钮
6. 在"操作结果"区域查看执行结果

## 发给别人使用（分发说明）

本工具是 **Python 源码包**，不是独立 exe。对方电脑需要：

1. **安装 Python 3.8+**（安装时勾选 **Add python.exe to PATH**）
2. 解压整个 `O2O_API工具` 文件夹（不要只拷单个 bat 或 py 文件）
3. 先双击 **`安装依赖.bat`**，再双击 **`启动工具.bat`**
4. 若仍打不开：双击 **`启动工具_调试.bat`**，把控制台里的报错发给维护人

常见「打不开」原因：

| 现象 | 原因 |
|------|------|
| 双击 bat 闪一下没窗口 | 未装 Python / 未装依赖；原 bat 用 `pythonw` 静默运行，报错也看不见 |
| 提示找不到 pythonw | 只装了 Python 但未加入 PATH，或未装 Python |
| 窗口出来又秒关 | 缺 `pymysql`、`pyyaml` 等，先运行 `安装依赖.bat` |
| 能开但查库失败 | 需连公司 VPN / 能访问 `config.yaml` 里的 OceanBase 地址 |

不想让对方装 Python：在本机执行 `build_exe.bat` 打包，把 `dist\O2O_API_Tool.exe` 和 `config`、`公域报文汇总` 一并发给对方。

## 注意事项

1. 数据库功能需要安装 pymysql：`pip install pymysql`（或运行 `安装依赖.bat`）
2. 数据库连接信息已迁移到 `config/config.yaml`，如需修改请直接编辑该文件
3. 确保数据库用户有相关表的读写权限

---

**版本**: 4.0.0（集成版）  
**更新日期**: 2026-05-09  
**集成来源**: 
- Interface 版本 (`VF_O2O_AoTo_Interface/O2O_API工具_分发包`)
- UI 版本 (`VF_O2O_AoTo_UI/O2O_API工具_分发包`)