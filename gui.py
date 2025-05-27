import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
import json
import os
from pathlib import Path
import asyncio
import threading
from live_recorder import LiveRecorder

class LiveRecorderApp(toga.App):
    def __init__(self):
        super().__init__(
            formal_name='LiveRecorder',
            app_id='com.example.liverecorder',
            app_name='LiveRecorder',
            description='A GUI application for recording live streams'
        )
        self.recorder = None
        self.recording = False
        self.config = None

    def startup(self):
        # 创建主窗口
        self.main_window = toga.MainWindow(title=self.formal_name)

        # 创建主容器
        main_box = toga.Box(style=Pack(direction=COLUMN, margin=10))

        # 创建按钮容器
        button_box = toga.Box(style=Pack(direction=ROW, margin=5))

        # 创建配置按钮
        load_config_button = toga.Button(
            'Load Config',
            on_press=self.load_config,
            style=Pack(margin=5)
        )
        save_config_button = toga.Button(
            'Save Config',
            on_press=self.save_config,
            style=Pack(margin=5)
        )

        # 创建录制按钮
        self.record_button = toga.Button(
            'Start Recording',
            on_press=self.toggle_recording,
            style=Pack(margin=5)
        )

        # 创建配置显示区域
        self.config_display = toga.MultilineTextInput(
            readonly=True,
            style=Pack(flex=1, margin=5)
        )

        # 添加按钮到按钮容器
        button_box.add(load_config_button)
        button_box.add(save_config_button)
        button_box.add(self.record_button)

        # 添加所有元素到主容器
        main_box.add(button_box)
        main_box.add(self.config_display)

        # 设置主窗口的内容
        self.main_window.content = main_box
        self.main_window.size = (600, 400)

        # 尝试加载默认配置
        self.load_default_config()

        # 显示主窗口
        self.main_window.show()

    def load_default_config(self):
        """尝试加载默认配置文件"""
        config_path = Path('config.json')
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                self.update_config_display()
            except Exception as e:
                self.main_window.error_dialog(
                    'Error',
                    f'Failed to load default config: {str(e)}'
                )

    def load_config(self, widget):
        """加载配置文件"""
        try:
            config_file = self.main_window.open_file_dialog(
                title="Select Config File",
                file_types=['json']
            )
            if config_file:
                with open(config_file, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                self.update_config_display()
        except Exception as e:
            self.main_window.error_dialog(
                'Error',
                f'Failed to load config: {str(e)}'
            )

    def save_config(self, widget):
        """保存配置文件"""
        try:
            if not self.config:
                self.main_window.error_dialog(
                    'Error',
                    'No configuration to save'
                )
                return

            save_path = self.main_window.save_file_dialog(
                title="Save Config File",
                suggested_filename="config.json"
            )
            if save_path:
                with open(save_path, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                self.main_window.dialog(
                    toga.InfoDialog(
                        title='Success',
                        message='Configuration saved successfully'
                    )
                )
        except Exception as e:
            self.main_window.error_dialog(
                'Error',
                f'Failed to save config: {str(e)}'
            )

    def update_config_display(self):
        """更新配置显示区域"""
        if self.config:
            self.config_display.value = json.dumps(self.config, indent=4, ensure_ascii=False)
        else:
            self.config_display.value = "No configuration loaded"

    def toggle_recording(self, widget):
        """切换录制状态"""
        if not self.recording:
            if not self.config:
                self.main_window.error_dialog(
                    'Error',
                    'Please load a configuration first'
                )
                return

            # 开始录制
            try:
                self.recorder = LiveRecorder(self.config)
                threading.Thread(target=self.recorder.start, daemon=True).start()
                self.recording = True
                self.record_button.label = 'Stop Recording'
                self.main_window.dialog(
                    toga.InfoDialog(
                        title='Success',
                        message='Recording started'
                    )
                )
            except Exception as e:
                self.main_window.error_dialog(
                    'Error',
                    f'Failed to start recording: {str(e)}'
                )
        else:
            # 停止录制
            try:
                if self.recorder:
                    self.recorder.stop()
                self.recording = False
                self.record_button.label = 'Start Recording'
                self.main_window.info_dialog(
                    'Success',
                    'Recording stopped'
                )
            except Exception as e:
                self.main_window.error_dialog(
                    'Error',
                    f'Failed to stop recording: {str(e)}'
                )

def main():
    return LiveRecorderApp()

if __name__ == '__main__':
    app = main()
    app.main_loop()
