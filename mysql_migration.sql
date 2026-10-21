-- ============================================================================
-- O2O_API_Web_MVP: SQLite -> MySQL 8.0 建表 DDL（含表注释 / 字段注释）
-- 目标库：o2o_api_db
-- 说明：由 SQLite 原生表结构自动映射生成，共 5 张业务数据表。
--       sqlite INTEGER(pk) -> INT AUTO_INCREMENT PRIMARY KEY
--       sqlite TIMESTAMP DEFAULT CURRENT_TIMESTAMP -> DATETIME DEFAULT CURRENT_TIMESTAMP
--       带 DEFAULT 字面量的 TEXT 列(type) -> VARCHAR(255)（MySQL TEXT 不支持默认值）
-- ============================================================================

CREATE DATABASE IF NOT EXISTS o2o_api_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE o2o_api_db;

SET NAMES utf8mb4;

-- ----------------------------------------------------------------------------
-- 1. 调用历史记录
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_call_history (
    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    request_method  TEXT COMMENT '请求方法',
    request_url     TEXT COMMENT '请求URL',
    request_headers TEXT COMMENT '请求头',
    request_body    TEXT COMMENT '请求体',
    response_code   INT COMMENT '响应状态码',
    response_body   TEXT COMMENT '响应体',
    cost_ms         INT COMMENT '耗时(毫秒)',
    error_msg       TEXT COMMENT '错误信息',
    create_time     TEXT COMMENT '创建时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调用历史记录表';

-- ----------------------------------------------------------------------------
-- 2. 报文主数据（原 SQLite 中 type 列由 ALTER 追加，故位于表末尾）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_main_data (
    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    platform        TEXT NOT NULL COMMENT '平台',
    message_name    TEXT NOT NULL COMMENT '报文名称',
    message_detail  TEXT NOT NULL COMMENT '报文详情',
    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
    type            VARCHAR(255) NOT NULL DEFAULT '正向' COMMENT '类型(正向/逆向)',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报文主数据表';

-- ----------------------------------------------------------------------------
-- 3. 环境配置
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS env_config (
    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    project_name    TEXT NOT NULL COMMENT '项目名称',
    env             TEXT NOT NULL COMMENT '环境',
    page_url        TEXT COMMENT '页面URL',
    account         TEXT COMMENT '账号',
    password        TEXT COMMENT '密码',
    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='环境配置表';

-- ----------------------------------------------------------------------------
-- 4. 数据库配置
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS db_config (
    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    project_name    TEXT NOT NULL COMMENT '项目名称',
    env             TEXT NOT NULL COMMENT '环境',
    db_address      TEXT COMMENT '数据库地址',
    port            TEXT COMMENT '端口',
    db_name         TEXT COMMENT '数据库名',
    username        TEXT COMMENT '用户名',
    password        TEXT COMMENT '密码',
    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据库配置表';

-- ----------------------------------------------------------------------------
-- 5. XXL-Job 配置
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS xxl_config (
    id              INT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    project_name    TEXT NOT NULL COMMENT '项目名称',
    env             TEXT NOT NULL COMMENT '环境',
    address         TEXT COMMENT 'XXL-Job调度中心地址',
    account         TEXT COMMENT '账号',
    password        TEXT COMMENT '密码',
    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='XXL-Job配置表';