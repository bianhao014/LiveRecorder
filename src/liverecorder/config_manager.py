import sqlite3
import os
from pathlib import Path
from typing import Dict, List, Any, Callable

class ConfigManager:
    """配置管理器，使用SQLite存储配置"""

    def __init__(self):
        self.config_dir = os.path.join(os.path.expanduser('~'), '.liverecorder')
        os.makedirs(self.config_dir, exist_ok=True)

        self.log_dir = os.path.join(self.config_dir, 'logs')
        os.makedirs(self.log_dir, exist_ok=True)  # 确保日志目录存在
        self.db_path = os.path.join(self.config_dir, 'config.db')
        self.conn = sqlite3.connect(self.db_path)
        self.observers: List[Callable[[str, Any], None]] = []  # 在构造方法中初始化
        self._ensure_db()

    
    def _ensure_db(self):
        cursor = self.conn.cursor()
        # 创建用户表（修正后的表结构）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                name TEXT NOT NULL,
                interval INTEGER DEFAULT 60,
                duration INTEGER DEFAULT 120,
                duration_unit TEXT DEFAULT 'minutes'
            )
        ''')
        # 创建全局配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        self.conn.commit()
           

    def add_observer(self, observer: Callable[[str, Any], None]):
        """添加配置变更观察者

        Args:
            observer: 配置变更回调函数，接收key和value参数
        """
        self.observers.append(observer)

    def remove_observer(self, observer: Callable[[str, Any], None]):
        """移除配置变更观察者"""
        if observer in self.observers:
            self.observers.remove(observer)

    def _notify_observers(self, key: str, value: Any):
        """通知所有观察者配置已变更"""
        for observer in self.observers:
            observer(key, value)

    def set_global_config(self, key: str, value: str):
        """设置全局配置

        Args:
            key: 配置键
            value: 配置值
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO global_config (key, value)
                VALUES (?, ?)
            ''', (key, value))
            conn.commit()
            self._notify_observers(key, value)
        finally:
            conn.close()

    def get_user_by_id(self, user_id: int) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('SELECT * FROM users WHERE id =?', (user_id,))
            result = c.fetchone()

            if result:
                # 将查询结果转换为字典
                return {
                    col[0]: value 
                    for col, value in zip(c.description, result)
                }
            return None
        except sqlite3.Error as e:
            print(f"数据库查询错误: {e}")
            return None

    def get_global_config(self, key: str, default: str = '') -> str:
        """获取全局配置

        Args:
            key: 配置键
            default: 默认值

        Returns:
            配置值
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('SELECT value FROM global_config WHERE key = ?', (key,))
            result = c.fetchone()
            return result[0] if result else default
        finally:
            conn.close()

    def get_all_global_config(self) -> Dict[str, str]:
        """获取所有全局配置

        Returns:
            配置字典
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('SELECT key, value FROM global_config')
            return dict(c.fetchall())
        finally:
            conn.close()

    def add_user(self, user: Dict[str, Any]):
        
        """添加用户配置

        Args:
            user: 用户配置字典
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('''
                INSERT INTO users (platform, name, interval, duration, duration_unit)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                user['platform'], 
                user['name'], 
                user['interval'],
                user.get('duration'),
                user.get('duration_unit')
            ))
            user_id = c.lastrowid
            conn.commit()
            self._notify_observers('users', self.get_all_users())
            return user_id
        finally:
            conn.close()

    def update_user(self, user: Dict[str, Any]):
        """更新用户配置

        Args:
            user: 用户配置字典，必须包含id字段
        """
        if 'id' not in user:
            raise ValueError("User dictionary must contain 'id' field for updates")
            
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('''
                UPDATE users 
                SET platform = ?, name = ?, interval = ?, duration = ?, duration_unit = ?
                WHERE id = ?
            ''', (
                user['platform'], 
                user['name'], 
                user['interval'],
                user.get('duration'),
                user.get('duration_unit'),
                user['id']
            ))
            conn.commit()
            self._notify_observers('users', self.get_all_users())
        finally:
            conn.close()

    def delete_user(self, user_id: int):
        """删除用户配置

        Args:
            user_id: 用户ID（整数）
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()
            self._notify_observers('users', self.get_all_users())
        finally:
            conn.close()

    def get_all_users(self) -> List[Dict[str, Any]]:
        """获取所有用户配置

        Returns:
            用户配置列表
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute('SELECT id, platform, name, interval, duration, duration_unit FROM users')
            users = []
            for row in c.fetchall():
                users.append({
                    'id': row[0],  # 现在是整数类型
                    'platform': row[1],
                    'name': row[2],
                    'interval': row[3],
                    'duration': row[4],
                    'duration_unit': row[5]
                })
            return users
        finally:
            conn.close()

    def export_config(self) -> Dict[str, Any]:
        """导出完整配置

        Returns:
            配置字典
        """
        return {
            **self.get_all_global_config(),
            'user': self.get_all_users()
        }

    def import_config(self, config: Dict[str, Any]):
        """导入完整配置

        Args:
            config: 配置字典
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            # 清空现有配置
            c.execute('DELETE FROM global_config')
            c.execute('DELETE FROM users')

            # 导入全局配置
            for key, value in config.items():
                if key != 'user':
                    c.execute('''
                        INSERT INTO global_config (key, value)
                        VALUES (?, ?)
                    ''', (key, str(value)))

            # 导入用户配置
            for user in config.get('user', []):
                c.execute('''
                    INSERT INTO users (id, platform, name, interval)
                    VALUES (?, ?, ?, ?)
                ''', (user['id'], user['platform'], user['name'], user['interval']))

            conn.commit()
            self._notify_observers('config_imported', self.export_config())
        finally:
            conn.close()


    def close_connection(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None

