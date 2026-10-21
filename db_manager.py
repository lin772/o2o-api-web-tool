# -*- coding: utf-8 -*-
"""
数据库操作模块 - 用于 IP_B_PLATFORM_ORDER_SOURCE 表的数据复制和插入

来源: VF_O2O_AoTo_UI/O2O_API工具_分发包/db_manager.py
功能: 订单数据复制、店铺/SKU信息查询
"""

import json
import re
from typing import Optional, Dict, Any

try:
    import pymysql
except ImportError:
    print("警告: 未安装 pymysql，数据库功能不可用")
    pymysql = None


class DBOrderSourceManager:
    """订单源数据管理器"""

    def __init__(self, config: Dict[str, Any], base_config: Dict[str, Any] = None):
        """
        初始化数据库连接

        Args:
            config: 数据库配置字典（订单库）
            base_config: 基础库配置字典（包含店铺表）
        """
        self.config = config
        self.base_config = base_config if base_config else config
        self.connection = None
        self.base_connection = None

    def connect(self) -> bool:
        """连接数据库"""
        if not pymysql:
            print("错误: pymysql 未安装")
            return False
        try:
            self.connection = pymysql.connect(
                host=self.config['host'],
                user=self.config['user'],
                password=self.config['password'],
                database=self.config['database'],
                port=self.config['port'],
                charset=self.config.get('charset', 'utf8mb4'),
                cursorclass=pymysql.cursors.DictCursor
            )
            return True
        except Exception as e:
            print(f"数据库连接失败: {e}")
            return False

    def connect_base(self) -> bool:
        """连接基础库"""
        if not pymysql:
            print("错误: pymysql 未安装")
            return False
        try:
            self.base_connection = pymysql.connect(
                host=self.base_config['host'],
                user=self.base_config['user'],
                password=self.base_config['password'],
                database=self.base_config['database'],
                port=self.base_config['port'],
                charset=self.base_config.get('charset', 'utf8mb4'),
                cursorclass=pymysql.cursors.DictCursor
            )
            return True
        except Exception as e:
            print(f"基础库连接失败: {e}")
            return False

    def close(self):
        """关闭数据库连接"""
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.base_connection:
            self.base_connection.close()
            self.base_connection = None

    def get_all_shops(self) -> list:
        """获取所有店铺列表"""
        try:
            if not self.base_connection:
                if not self.connect_base():
                    return []
            with self.base_connection.cursor() as cursor:
                sql = "SELECT id, ecode, seller_nick, cp_c_platform_id, cp_c_shop_title FROM cp_c_shop ORDER BY id"
                cursor.execute(sql)
                results = cursor.fetchall()
                return results
        except Exception as e:
            print(f"获取店铺列表失败: {e}")
            return []

    def get_shop_by_id(self, shop_id: int) -> Optional[Dict[str, Any]]:
        """根据店铺ID查询店铺信息"""
        try:
            if not self.base_connection:
                if not self.connect_base():
                    return None
            with self.base_connection.cursor() as cursor:
                sql = "SELECT * FROM cp_c_shop WHERE id = %s"
                cursor.execute(sql, (shop_id,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询店铺信息失败: {e}")
            return None

    def get_sku_by_id(self, sku_id: int) -> Optional[Dict[str, Any]]:
        """根据SKU ID查询商品信息"""
        try:
            if not self.base_connection:
                if not self.connect_base():
                    return None
            with self.base_connection.cursor() as cursor:
                sql = "SELECT * FROM ps_c_sku WHERE id = %s"
                cursor.execute(sql, (sku_id,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询SKU信息失败: {e}")
            return None

    def get_sku_by_ecode(self, ecode: str) -> Optional[Dict[str, Any]]:
        """根据SKU ecode查询商品信息"""
        try:
            if not self.base_connection:
                if not self.connect_base():
                    return None
            with self.base_connection.cursor() as cursor:
                sql = "SELECT * FROM ps_c_sku WHERE ecode = %s"
                cursor.execute(sql, (ecode,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询SKU信息失败: {e}")
            return None

    def get_all_skus(self) -> list:
        """获取所有SKU列表"""
        try:
            if not self.base_connection:
                if not self.connect_base():
                    return []
            with self.base_connection.cursor() as cursor:
                sql = "SELECT id, ecode, pro_name FROM ps_c_sku ORDER BY id"
                cursor.execute(sql)
                results = cursor.fetchall()
                return results
        except Exception as e:
            print(f"获取SKU列表失败: {e}")
            return []

    def get_max_id(self) -> Optional[int]:
        """获取当前表中最大的 id 值"""
        try:
            with self.connection.cursor() as cursor:
                sql = "SELECT MAX(id) as max_id FROM IP_B_PLATFORM_ORDER_SOURCE"
                cursor.execute(sql)
                result = cursor.fetchone()
                max_id = result.get('max_id') if result else None
                return max_id if max_id is not None else 0
        except Exception as e:
            print(f"获取最大ID失败: {e}")
            return None

    def get_max_tid(self) -> Optional[str]:
        """获取当前表中最大的 tid 值"""
        try:
            with self.connection.cursor() as cursor:
                sql = "SELECT MAX(tid) as max_tid FROM IP_B_PLATFORM_ORDER_SOURCE"
                cursor.execute(sql)
                result = cursor.fetchone()
                return result.get('max_tid') if result else None
        except Exception as e:
            print(f"获取最大TID失败: {e}")
            return None

    def get_order_by_tid(self, tid: str) -> Optional[Dict[str, Any]]:
        """根据 tid 查询订单数据"""
        try:
            with self.connection.cursor() as cursor:
                sql = "SELECT * FROM IP_B_PLATFORM_ORDER_SOURCE WHERE tid = %s"
                cursor.execute(sql, (tid,))
                result = cursor.fetchone()
                return result
        except Exception as e:
            print(f"查询订单数据失败: {e}")
            return None

    def update_message_tid(self, message: str, old_tid: str, new_tid: str) -> str:
        """
        更新 message 字段中的 tid 值

        Args:
            message: 原始 message 字符串
            old_tid: 旧的 tid 值
            new_tid: 新的 tid 值

        Returns:
            更新后的 message 字符串
        """
        if not message:
            return message

        updated_message = message.replace(old_tid, new_tid)
        return updated_message

    def insert_order_copy(self, source_tid: str, shop_id: int = None, sku_ecode: str = None) -> Dict[str, Any]:
        """
        根据 tid 复制数据并插入新记录

        Args:
            source_tid: 源 tid（要复制的数据）
            shop_id: 目标店铺ID
            sku_ecode: 商品SKU编码

        Returns:
            操作结果字典
        """
        result = {
            'success': False,
            'message': '',
            'data': None
        }

        try:
            if not self.connection:
                if not self.connect():
                    result['message'] = '数据库连接失败'
                    return result

            source_data = self.get_order_by_tid(source_tid)
            if not source_data:
                result['message'] = f'未找到 tid 为 {source_tid} 的数据'
                return result

            shop_info = None
            if shop_id:
                shop_info = self.get_shop_by_id(shop_id)
                if not shop_info:
                    result['message'] = f'未找到 ID 为 {shop_id} 的店铺信息'
                    return result

            sku_info = None
            if sku_ecode:
                sku_info = self.get_sku_by_ecode(sku_ecode)
                if not sku_info:
                    result['message'] = f'未找到 SKU编码为 {sku_ecode} 的商品信息'
                    return result

            max_id = self.get_max_id()
            max_tid = self.get_max_tid()

            if max_id is None or max_tid is None:
                result['message'] = '获取最大 ID 或 TID 失败'
                return result

            new_id = max_id + 1
            new_tid = str(int(max_tid) + 1) if max_tid and max_tid.isdigit() else str(new_id)

            new_data = source_data.copy()
            new_data['id'] = new_id
            new_data['tid'] = new_tid

            if shop_info:
                platform_id = shop_info.get('cp_c_platform_id')
                new_data['platform_id'] = platform_id
                new_data['cp_c_shop_id'] = shop_info.get('id')
                new_data['cp_c_shop_ecode'] = shop_info.get('ecode')
                new_data['cp_c_shop_title'] = shop_info.get('cp_c_shop_title')
                new_data['seller_nick'] = shop_info.get('seller_nick')

                if 'message' in new_data and new_data['message']:
                    message = new_data['message']
                    if platform_id:
                        message = re.sub(r'"platform"\s*:\s*"[^"]*"', f'"platform": "{platform_id}"', message)
                    seller_nick = shop_info.get('seller_nick')
                    if seller_nick:
                        message = re.sub(r'"sellernick"\s*:\s*"[^"]*"', f'"sellernick": "{seller_nick}"', message, flags=re.IGNORECASE)
                    new_data['message'] = message

            if sku_info and 'message' in new_data and new_data['message']:
                sku_ecode_value = sku_info.get('ecode')
                if sku_ecode_value:
                    new_data['message'] = re.sub(r'"outer_sku_id"\s*:\s*"[^"]*"', f'"outer_sku_id": "{sku_ecode_value}"', new_data['message'])

            columns = ', '.join(new_data.keys())
            placeholders = ', '.join(['%s'] * len(new_data))
            sql = f"INSERT INTO IP_B_PLATFORM_ORDER_SOURCE ({columns}) VALUES ({placeholders})"

            with self.connection.cursor() as cursor:
                cursor.execute(sql, tuple(new_data.values()))
                self.connection.commit()

            result['success'] = True
            result['message'] = f'复制成功！新记录 tid: {new_tid}'
            result['data'] = {'new_id': new_id, 'new_tid': new_tid}

            return result
        except Exception as e:
            result['message'] = f'复制失败: {str(e)}'
            return result


def test_db_connection(config: Dict[str, Any]) -> bool:
    """测试数据库连接"""
    manager = DBOrderSourceManager(config)
    try:
        success = manager.connect()
        manager.close()
        return success
    except Exception as e:
        print(f"连接测试失败: {e}")
        return False
