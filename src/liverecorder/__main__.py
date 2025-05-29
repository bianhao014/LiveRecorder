"""
LiveRecorder 主程序入口点
使用 GUI 界面启动应用程序
"""
import os
from .gui import main

# 确保必要的目录存在
os.makedirs('output', exist_ok=True)
# os.makedirs('logs', exist_ok=True)


if __name__ == "__main__":
    main()
