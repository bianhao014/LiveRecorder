import os
import ffmpeg
from datetime import datetime
from utils.path_utils import sanitize_filename, generate_output_path

class LiveRecorder:
    def __init__(self, output_base="D:/111"):
        self.output_base = output_base
        os.makedirs(output_base, exist_ok=True)

    def _validate_path(self, path):
        """验证路径是否合法"""
        try:
            return os.path.normpath(path)
        except (TypeError, AttributeError):
            raise ValueError(f"Invalid path: {path}")

    def start_recording(self, stream_url, platform, title, filename=None):
        """启动直播录制"""
        filename = filename or f"live_{datetime.now().strftime('%H%M%S')}"

        try:
            output_path = generate_output_path(
                self.output_base,
                sanitize_filename(platform),
                sanitize_filename(title),
                sanitize_filename(filename)
            )

            output_path = self._validate_path(output_path)
            print(f"开始录制到: {output_path}")

            (
                ffmpeg
                .input(stream_url)
                .output(output_path, c='copy')
                .global_args('-loglevel', 'error')
                .global_args('-y')  # 允许覆盖
                .run()
            )
            return output_path

        except ffmpeg.Error as e:
            error_msg = f"FFmpeg错误: {e.stderr.decode('utf-8')}"
            if os.path.exists(output_path):
                os.remove(output_path)
            raise RuntimeError(error_msg)
        except Exception as e:
            raise RuntimeError(f"录制失败: {str(e)}")
