import re
import os
from datetime import datetime

def sanitize_filename(text):
    """移除文件名中的非法字符"""
    return re.sub(r'[\\/*?:"<>|\[\] ]', '_', str(text))

def generate_output_path(base_dir, platform, title, filename):
    """生成安全的输出路径"""
    # 确保目录存在
    os.makedirs(base_dir, exist_ok=True)

    # 处理文件名
    safe_title = sanitize_filename(title)[:50]  # 限制长度
    safe_filename = sanitize_filename(filename)[:50]

    # 生成带时间戳的文件名
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return os.path.normpath(
        os.path.join(
            base_dir,
            f"live_{timestamp}_{sanitize_filename(platform)}_{safe_title}_{safe_filename}.mp4"
        )
    )

# 新增路径验证逻辑
def validate_write_permission(path):
    try:
        with tempfile.TemporaryFile(dir=os.path.dirname(path)):
            pass
        return True
    except PermissionError:
        return False
