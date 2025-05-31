#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志配置模块
提供统一的日志配置和管理功能
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional
from datetime import datetime

class LoggerConfig:
    """日志配置管理器"""
    
    _instance: Optional['LoggerConfig'] = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self.config_dir = os.path.join(os.path.expanduser('~'), '.liverecorder')
            self.log_dir = os.path.join(self.config_dir, 'logs')
            os.makedirs(self.log_dir, exist_ok=True)
            
            # 生成包含日期的日志文件名
            current_date = datetime.now().strftime('%Y-%m-%d')
            log_filename = f'log_{current_date}.log'
            self.log_file = os.path.join(self.log_dir, log_filename)
            self.logger = None
            self._initialized = True
    
    def setup_logger(self, log_level: str = 'INFO') -> logging.Logger:
        """设置日志配置
        
        Args:
            log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            
        Returns:
            配置好的logger实例
        """
        if self.logger is not None:
            # 如果logger已存在，只更新日志级别
            level = getattr(logging, log_level.upper(), logging.INFO)
            self.logger.setLevel(level)
            for handler in self.logger.handlers:
                handler.setLevel(level)
            return self.logger
        
        # 创建logger
        self.logger = logging.getLogger('liverecorder')
        level = getattr(logging, log_level.upper(), logging.INFO)
        self.logger.setLevel(level)
        
        # 清除现有的处理器
        self.logger.handlers.clear()
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 文件处理器 - 使用RotatingFileHandler避免日志文件过大
        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        
        # 控制台处理器 - 只在DEBUG级别时输出到控制台
        if log_level.upper() == 'DEBUG':
            console_handler = logging.StreamHandler()
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
        
        # 防止日志重复
        self.logger.propagate = False
        
        return self.logger
    
    def get_logger(self) -> logging.Logger:
        """获取logger实例
        
        Returns:
            logger实例，如果未初始化则使用默认配置
        """
        if self.logger is None:
            return self.setup_logger()
        return self.logger
    
    def update_log_level(self, log_level: str):
        """更新日志级别
        
        Args:
            log_level: 新的日志级别
        """
        if self.logger is not None:
            level = getattr(logging, log_level.upper(), logging.INFO)
            self.logger.setLevel(level)
            for handler in self.logger.handlers:
                handler.setLevel(level)

# 全局日志配置实例
logger_config = LoggerConfig()

def get_logger() -> logging.Logger:
    """获取全局logger实例
    
    Returns:
        配置好的logger实例
    """
    return logger_config.get_logger()

def setup_logging(log_level: str = 'INFO') -> logging.Logger:
    """设置全局日志配置
    
    Args:
        log_level: 日志级别
        
    Returns:
        配置好的logger实例
    """
    return logger_config.setup_logger(log_level)

def update_log_level(log_level: str):
    """更新全局日志级别
    
    Args:
        log_level: 新的日志级别
    """
    logger_config.update_log_level(log_level)