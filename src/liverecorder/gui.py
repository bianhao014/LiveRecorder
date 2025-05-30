import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW, CENTER, LEFT
import os
import asyncio
import threading
import re
import logging
from datetime import datetime

# 预先导入可能在子线程中使用的模块，避免在子线程中首次导入
from .live_recorder import LiveRecorder
# 预先导入streamlink相关模块，避免在子线程中导入时的signal错误
import streamlink
from streamlink_cli.main import open_stream
from streamlink_cli.output import FileOutput
from streamlink_cli.streamrunner import StreamRunner

from .config_manager import ConfigManager

# 配置日志
logger = logging.getLogger('liverecorder.gui')

class GuiLogHandler(logging.Handler):
    """将日志重定向到GUI的处理器"""
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                          datefmt='%H:%M:%S'))

    def emit(self, record):
        try:
            msg = self.format(record)
            print(f"CONSOLE LOG: {msg}")  # 添加控制台日志输出
            # 使用asyncio.create_task替代已弃用的add_background_task
            asyncio.create_task(self._update_log(msg))
        except Exception as e:
            print(f"Error emitting log: {e}")
            self.handleError(record)

    async def _update_log(self, msg):
        """异步更新日志"""
        try:
            self.app.add_log(msg)
        except Exception as e:
            print(f"Error updating log: {e}")

def validate_proxy(proxy):
    """验证代理格式"""
    if not proxy:  # 允许为空
        return True
    proxy_pattern = r'^(http|socks[45]?)://([a-zA-Z0-9.-]+):(\d+)$'
    return bool(re.match(proxy_pattern, proxy))

def validate_interval(interval):
    """验证间隔时间"""
    try:
        value = int(interval)
        return value > 0
    except ValueError:
        return False

class LiveRecorderApp(toga.App):
    def __init__(self):
        super().__init__(
            formal_name='Kwai直播录制工具',
            app_id='com.example.liverecorder',
            app_name='LiveRecorder',
            description='实时检测录制各大平台直播流',
            on_exit=self.on_exit  # 正确设置退出处理程序
        )
        self.recorder = None
        self.recorder_threads = []  # 初始化录制线程列表
        self.recording = False

        # 用户录制状态跟踪
        self.user_recorders = {}  # {user_id: recorder_instance}
        self.user_recording_status = {}  # {user_id: True/False}
        self.config_manager = ConfigManager()
        self.current_user = None  # 当前选中的用户
        self._is_exiting = False  # 标记应用是否正在退出

    def on_exit(self, widget):
        # 标记应用正在退出
        self._is_exiting = True
        logger.info("应用正在退出，开始清理资源...")
        
        # 终止所有录制器
        try:
            # 停止全局录制器
            if hasattr(self, 'recorder') and self.recorder:
                logger.debug("停止全局录制器")
                self.recorder.stop()
                self.recorder = None
            
            # 停止所有用户录制器
            if hasattr(self, 'user_recorders'):
                logger.debug(f"停止 {len(self.user_recorders)} 个用户录制器")
                for user_id, recorder in list(self.user_recorders.items()):
                    try:
                        recorder.stop()
                    except Exception as e:
                        logger.error(f"停止用户录制器失败: {e}")
                self.user_recorders.clear()
            
            # 清理所有待处理的异步任务
            try:
                import asyncio
                logger.debug("开始清理所有待处理的异步任务")
                # 获取所有待处理的任务
                pending_tasks = [task for task in asyncio.all_tasks() if not task.done()]
                if pending_tasks:
                    logger.debug(f"发现 {len(pending_tasks)} 个待处理的异步任务")
                    # 取消所有待处理的任务
                    for task in pending_tasks:
                        logger.debug(f"取消异步任务: {task.get_name()}")
                        task.cancel()
                    
                    # 等待任务取消完成
                    try:
                        # 创建一个短暂的事件循环来处理任务取消
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        # 等待所有任务取消完成，设置较短的超时时间
                        wait_task = loop.create_task(asyncio.gather(*pending_tasks, return_exceptions=True))
                        loop.run_until_complete(asyncio.wait_for(wait_task, timeout=3.0))
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        logger.warning("等待异步任务取消超时")
                    except Exception as e:
                        logger.error(f"清理异步任务时出错: {e}")
                    finally:
                        # 关闭事件循环
                        loop.close()
                else:
                    logger.debug("没有发现待处理的异步任务")
            except Exception as e:
                logger.error(f"清理异步任务失败: {e}")
            
            # 等待所有录制线程结束
            if hasattr(self, 'recorder_threads'):
                logger.debug(f"等待 {len(self.recorder_threads)} 个录制线程结束")
                for thread in self.recorder_threads:
                    if thread.is_alive():
                        logger.debug(f"等待线程结束: {thread.name}")
                        thread.join(timeout=5)
                        
                        # 如果线程仍然活着，使用更激进的方式终止
                        if thread.is_alive():
                            logger.warning(f"强制终止线程：{thread.name}")
                            try:
                                import ctypes
                                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                                    ctypes.c_long(thread.ident),
                                    ctypes.py_object(SystemExit)
                                )
                                # 再次等待确保线程已终止
                                thread.join(timeout=1.0)
                                if thread.is_alive():
                                    # 如果仍然活着，尝试最后的强制终止
                                    logger.warning(f"线程仍然活着，尝试最后的强制终止: {thread.name}")
                                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                                        ctypes.c_long(thread.ident),
                                        ctypes.py_object(SystemError)
                                    )
                                    thread.join(timeout=1.0)
                            except Exception as force_error:
                                logger.error(f"强制终止线程失败: {force_error}")
                        else:
                            logger.info(f"成功终止线程：{thread.name}")
                
                # 检查是否还有活着的线程
                alive_threads = [t for t in self.recorder_threads if t.is_alive()]
                if alive_threads:
                    logger.warning(f"仍有 {len(alive_threads)} 个线程未能终止")
                else:
                    logger.info("所有录制线程已成功终止")
                        
        except Exception as e:
            logger.error(f"退出时清理资源失败: {e}")

        logger.info("资源清理完成，应用退出")
        # 直接返回True允许正常退出
        return True


    def add_log(self, message):
        """添加日志到日志输出区域"""
        if hasattr(self, 'log_output'):
            # 直接更新日志，因为emit方法已经确保在UI线程中调用
            current_text = self.log_output.value
            timestamp = datetime.now().strftime("%H:%M:%S")
            new_log = f"[{timestamp}] {message}\n"
            self.log_output.value = current_text + new_log
            # 滚动到底部
            self.log_output.scroll_to_bottom()

    def startup(self):




        # 创建主窗口
        self.main_window = toga.MainWindow(title=self.formal_name)

        # 创建设置页内容
        settings_content = toga.Box(style=Pack(direction=COLUMN, margin=10))
        settings_scroll = toga.ScrollContainer(style=Pack(flex=1))
        settings_scroll.content = settings_content

        # 创建控制页内容
        control_content = toga.Box(style=Pack(direction=COLUMN, margin=10))

        # 录制控制
        record_box = toga.Box(style=Pack(direction=COLUMN, margin=5))
        record_box.add(toga.Label('录制控制', style=Pack(margin=(0, 0, 5, 0))))

        # 录制按钮
        self.record_button = toga.Button(
            '开始全部录制',
            on_press=self.toggle_recording,
            style=Pack(flex=1, margin=2)
        )
        record_box.add(self.record_button)


        # 日志输出
        log_box = toga.Box(style=Pack(direction=COLUMN, margin=5))
        log_box.add(toga.Label('日志打印', style=Pack(margin=(0, 0, 5, 0))))

        self.log_output = toga.MultilineTextInput(
            readonly=True,
            style=Pack(flex=1, height=200)
        )
        log_box.add(self.log_output)

        control_content.add(record_box)
        control_content.add(log_box)

        # 包装控制页内容
        control_scroll = toga.ScrollContainer(style=Pack(flex=1))
        control_scroll.content = control_content

        # 创建选项容器(替代标签页)
        option_container = toga.OptionContainer(
            style=Pack(flex=1),
            content=[
                ('录制设置', settings_scroll),
                ('录制控制', control_scroll)
            ]
        )

        # 全局配置
        global_config_box = toga.Box(style=Pack(direction=COLUMN, margin=5))
        global_config_box.add(toga.Label('全局设置', style=Pack(margin=(0, 0, 5, 0))))

        # Proxy输入
        proxy_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.proxy_input = toga.TextInput(
            style=Pack(flex=1),
            on_confirm=self.on_proxy_changed,
            on_lose_focus=self.on_proxy_changed
        )
        proxy_box.add(toga.Label('代理设置:', style=Pack(width=100)))
        proxy_box.add(self.proxy_input)
        global_config_box.add(proxy_box)

        # Output目录选择
        output_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.output_input = toga.TextInput(
            style=Pack(flex=1),
            on_change=self.on_output_changed
        )
        self.output_button = toga.Button(
            '浏览',
            on_press=self.select_output_directory,
            style=Pack(width=80)
        )
        output_box.add(toga.Label('输出目录:', style=Pack(width=100)))
        output_box.add(self.output_input)
        output_box.add(self.output_button)
        global_config_box.add(output_box)

        settings_content.add(global_config_box)

        # 用户配置
        user_config_box = toga.Box(style=Pack(direction=COLUMN, margin=5))
        user_config_box.add(toga.Label('录制用户设置', style=Pack(margin=(10, 0, 5, 0))))

        # Platform选择
        platform_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.platform_select = toga.Selection(
            items=['Kwai'],
            style=Pack(flex=1)
        )
        platform_box.add(toga.Label('平台:', style=Pack(width=100)))
        platform_box.add(self.platform_select)
        user_config_box.add(platform_box)

        # Name输入
        name_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.name_input = toga.TextInput(style=Pack(flex=1))
        name_box.add(toga.Label('用户昵称:', style=Pack(width=100)))
        name_box.add(self.name_input)
        user_config_box.add(name_box)

        # Interval输入
        interval_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.interval_input = toga.TextInput(style=Pack(flex=1))
        interval_box.add(toga.Label('轮询时间间隔:', style=Pack(width=100)))
        interval_box.add(self.interval_input)
        user_config_box.add(interval_box)

        # Duration输入
        duration_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.duration_input = toga.TextInput(style=Pack(flex=1))
        duration_box.add(toga.Label('录制时长:', style=Pack(width=100)))
        duration_box.add(self.duration_input)
        user_config_box.add(duration_box)

        # Duration单位选择
        unit_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.unit_select = toga.Selection(
            items=['seconds', 'minutes', 'hours'],
            style=Pack(flex=1)
        )
        unit_box.add(toga.Label('时长单位:', style=Pack(width=100)))
        unit_box.add(self.unit_select)
        user_config_box.add(unit_box)

        # 用户操作按钮
        user_buttons_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.add_user_button = toga.Button(
            '新增用户',
            on_press=self.add_user,
            style=Pack(flex=1, margin=2)
        )
        self.update_user_button = toga.Button(
            '修改用户',
            on_press=self.update_user,
            style=Pack(flex=1, margin=2)
        )
        self.delete_user_button = toga.Button(
            '删除用户',
            on_press=self.delete_user,
            style=Pack(flex=1, margin=2)
        )
        user_buttons_box.add(self.add_user_button)
        user_buttons_box.add(self.update_user_button)
        user_buttons_box.add(self.delete_user_button)
        user_config_box.add(user_buttons_box)

        # 单个用户录制控制按钮
        user_record_buttons_box = toga.Box(style=Pack(direction=ROW, margin=2))
        self.start_user_record_button = toga.Button(
            '开始录制选中用户',
            on_press=self.start_selected_user_recording,
            style=Pack(flex=1, margin=2)
        )
        self.stop_user_record_button = toga.Button(
            '停止录制选中用户',
            on_press=self.stop_selected_user_recording,
            style=Pack(flex=1, margin=2)
        )
        user_record_buttons_box.add(self.start_user_record_button)
        user_record_buttons_box.add(self.stop_user_record_button)
        user_config_box.add(user_record_buttons_box)

        settings_content.add(user_config_box)

        # 用户列表
        users_box = toga.Box(style=Pack(direction=COLUMN, margin=5))
        users_box.add(toga.Label('用户列表', style=Pack(margin=(10, 0, 5, 0))))

        # 创建并初始化表格
        self.users_table = toga.Table(
            headings=['ID', '平台', '用户昵称', '轮询间隔', '录制时长', '时长单位', '状态', '操作'],
            accessors=['id', 'platform', 'name', 'interval', 'duration', 'duration_unit', 'status', 'action'],
            data=[],
            style=Pack(flex=1,width=1150,height=300),
            on_select=self.on_user_selected,
            multiple_select=False,
            missing_value=''
        )
        users_box.add(self.users_table)

        # 立即刷新表格数据
        self.refresh_users_table()

        settings_content.add(users_box)

        # 设置主窗口的内容
        self.main_window.content = option_container
        self.main_window.size = (1200, 800)

        # 加载配置并更新显示
        self.update_config_display()

        # 显示主窗口
        self.main_window.show()

    def on_proxy_changed(self, widget):
        """代理设置变更处理"""
        try:
            proxy = widget.value.strip()
            if proxy and not validate_proxy(proxy):
                self.main_window.info_dialog(
                    'Error',
                    'Invalid proxy format. Use http://host:port or socks5://host:port'
                )
                return

            self.config_manager.set_global_config('proxy', proxy)
            self.update_recorder_config()
        except Exception as e:
            print(f"Error updating proxy: {e}")

    def on_output_changed(self, widget):
        """输出目录变更处理"""
        try:
            output = widget.value.strip()
            self.config_manager.set_global_config('output', output)
            self.update_recorder_config()
        except Exception as e:
            print(f"Error updating output directory: {e}")

    def update_recorder_config(self):
        """更新录制器配置"""
        try:
            if self.recording and self.recorder:
                config = self.config_manager.export_config()
                self.recorder.update_config(config)
        except Exception as e:
            print(f"Error updating recorder config: {e}")

    async def show_info_message(self, title, message):
        """安全地显示信息对话框"""
        try:
            # 使用异步对话框API
            dialog = toga.InfoDialog(title=title, message=message)
            await self.main_window.dialog(dialog)
        except Exception as e:
            print(f"Error showing message: {e}")
            logger.error(f'Failed to show message: {str(e)}')

    def update_config_display(self):
        """更新界面显示"""
        try:
            # 更新全局配置
            self.proxy_input.value = self.config_manager.get_global_config('proxy', '')
            self.output_input.value = self.config_manager.get_global_config('output', 'output')

            # 更新用户列表
            self.refresh_users_table()
        except Exception as e:
            print(f"Error updating display: {e}")

    def refresh_users_table(self):
        """刷新用户列表"""
        try:
            users = self.config_manager.get_all_users()
            print(f"Refreshing tree with {len(users)} users")  # 调试信息

            # 为Table组件准备数据
            table_data = []
            for user in users:
                user_id = user.get('id')
                is_recording = self.user_recording_status.get(user_id, False)

                # 创建表格行数据（使用字典格式配合accessors）
                row_data = {
                    'id': str(user.get('id', '')),
                    'platform': str(user.get('platform', 'Kwai')),
                    'name': str(user.get('name', '')),
                    'interval': str(user.get('interval', 10)),
                    'duration': str(user.get('duration', '')) if user.get('duration') else '',
                    'duration_unit': str(user.get('duration_unit', 'seconds')),
                    'status': '录制中' if is_recording else '未录制',
                    'action': '停止录制' if is_recording else '开始录制'
                }
                table_data.append(row_data)
                print(f"Added row: {row_data}")  # 调试信息

            print(f"Setting table data with {len(table_data)} rows")  # 调试信息
            self.users_table.data = table_data

            # 如果当前没有选中的用户且有数据，自动选择第一个用户
            if not self.current_user and table_data and len(users) > 0:
                print("No current user, updating form with first user")  # 调试信息
                first_user = users[0]
                self.platform_select.value = first_user.get('platform', 'Kwai')
                self.name_input.value = first_user.get('name', '')
                self.interval_input.value = str(first_user.get('interval', 30))
                self.current_user = first_user
                print(f"Form updated with first user: {first_user}")  # 调试信息

        except Exception as e:
            print(f"Error refreshing users tree: {e}")
            import traceback
            print(traceback.format_exc())  # 打印完整的错误堆栈

    async def select_output_directory(self, widget):
        """选择输出目录"""
        try:
            # 使用新的对话框API
            dialog = toga.SelectFolderDialog(title="Select Output Directory")
            output_dir = await self.main_window.dialog(dialog)

            if output_dir is not None:  # 检查是否选择了目录
                # 将 WindowsPath 转换为字符串
                output_dir_str = str(output_dir)
                self.output_input.value = output_dir_str
                self.config_manager.set_global_config('output', output_dir_str)
                self.update_recorder_config()
        except Exception as e:
            print(f"Error selecting directory: {e}")
            logger.error(f'Failed to select directory: {str(e)}')

    def validate_user_input(self):
        """验证用户输入"""
        try:
            name = self.name_input.value.strip()
            if not name:
                self.main_window.info_dialog(
                    'Error',
                    'Name is required'
                )
                return False

            interval = self.interval_input.value.strip()
            if not validate_interval(interval):
                self.main_window.info_dialog(
                    'Error',
                    'Interval must be a positive number'
                )
                return False

            duration = self.duration_input.value.strip()
            if duration and not validate_interval(duration):
                self.main_window.info_dialog(
                    'Error',
                    'Duration must be a positive number'
                )
                return False

            return True
        except Exception as e:
            print(f"Error validating user input: {e}")
            return False

    def clear_user_form(self):
        """清空用户表单"""
        try:
            self.platform_select.value = 'Kwai'
            self.name_input.value = ''
            self.interval_input.value = '30'
            self.duration_input.value = ''
            self.unit_select.value = 'seconds'
            if self.current_user:
                logger.debug(f"清空表单，原用户ID: {self.current_user.get('id', '')}")
            self.current_user = None
            print("Form cleared")  # 添加调试信息
        except Exception as e:
            print(f"Error clearing form: {e}")

    def add_user(self, widget):
        """添加新用户到配置"""
        try:
            logger.info("开始添加新用户流程")

            # 1. 验证输入
            if not self.validate_user_input():
                logger.warning("用户输入验证失败")
                return

            # 2. 准备新用户数据
            duration = self.duration_input.value.strip()
            duration = int(duration) if duration else None
            duration_unit = self.unit_select.value if duration else None

            new_user = {
                'platform': self.platform_select.value,
                'name': self.name_input.value.strip(),
                'interval': int(self.interval_input.value.strip()),
                'duration': duration,
                'duration_unit': duration_unit,
                'created_at': datetime.now().isoformat()
            }
            logger.debug(f"准备添加的用户数据: {new_user}")

            # 3. 检查用户是否已存在
            existing_users = self.config_manager.get_all_users()
            for user in existing_users:
                if (user['name'].lower() == new_user['name'].lower() and
                        user['platform'].lower() == new_user['platform'].lower()):
                    logger.warning(f"用户已存在: {new_user['name']}@{new_user['platform']}")
                    self.main_window.info_dialog(
                        '错误',
                        f'用户 {new_user["name"]} 已在平台 {new_user["platform"]} 存在'
                    )
                    return

            # 4. 保存到配置文件
            self.config_manager.add_user(new_user)
            logger.info(f"成功添加用户: {new_user['name']}")

            # 5. 刷新界面
            self.refresh_users_table()
            self.clear_user_form()

            # 6. 提供成功反馈
            self.main_window.info_dialog(
                '成功',
                f'用户 {new_user["name"]} 添加成功'
            )

        except ValueError as ve:
            logger.error(f"数值转换错误: {str(ve)}")
            self.main_window.info_dialog(
                '输入错误',
                '请输入有效的数字值'
            )
        except Exception as e:
            logger.error(f"添加用户失败: {str(e)}", exc_info=True)
            self.main_window.info_dialog(
                '错误',
                f'添加用户失败: {str(e)}'
            )

    def update_user(self, widget):
        """更新当前选中的用户信息"""
        try:
            logger.info("开始更新用户流程")

            # 1. 检查是否有选中的用户
            if not self.current_user:
                logger.warning("没有选中的用户")
                self.main_window.info_dialog(
                    '错误',
                    '请先选择一个要修改的用户'
                )
                return

            # 2. 验证输入
            if not self.validate_user_input():
                logger.warning("用户输入验证失败")
                return

            # 3. 准备更新后的用户数据
            duration = self.duration_input.value.strip()
            duration = int(duration) if duration else None
            duration_unit = self.unit_select.value if duration else None

            updated_user = {
                'id': self.current_user.get('id'),  # 保留原有ID
                'platform': self.platform_select.value,
                'name': self.name_input.value.strip(),
                'interval': int(self.interval_input.value.strip()),
                'duration': duration,
                'duration_unit': duration_unit,
                # 'updated_at': datetime.now().isoformat()
            }
            logger.debug(f"准备更新的用户数据: {updated_user}")

            # 4. 检查用户是否已存在(名称或平台变更时)
            if (updated_user['name'].lower() != self.current_user['name'].lower() or
                    updated_user['platform'].lower() != self.current_user['platform'].lower()):
                existing_users = self.config_manager.get_all_users()
                for user in existing_users:
                    if (user['name'].lower() == updated_user['name'].lower() and
                            user['platform'].lower() == updated_user['platform'].lower()):
                        logger.warning(f"用户已存在: {updated_user['name']}@{updated_user['platform']}")
                        self.main_window.info_dialog(
                            '错误',
                            f'用户 {updated_user["name"]} 已在平台 {updated_user["platform"]} 存在'
                        )
                        return

            # 5. 更新配置文件
            old_name = self.current_user["name"]
            self.config_manager.update_user(updated_user)
            logger.info(f"成功更新用户: {old_name} -> {updated_user['name']}")

            # 6. 刷新界面
            self.refresh_users_table()
            self.current_user = updated_user

            # 7. 提供成功反馈
            self.main_window.info_dialog(
                '成功',
                f'用户 {old_name} 更新成功'
            )

        except ValueError as ve:
            logger.error(f"数值转换错误: {str(ve)}")
            self.main_window.info_dialog(
                '输入错误',
                '请输入有效的数字值'
            )
        except Exception as e:
            logger.error(f"更新用户失败: {str(e)}", exc_info=True)
            self.main_window.info_dialog(
                '错误',
                f'更新用户失败: {str(e)}'
            )

    def delete_user(self, widget):
        """删除当前选中的用户"""
        try:
            logger.info("开始删除用户流程")

            # 1. 检查是否有选中的用户
            if not self.current_user:
                logger.warning("没有选中的用户")
                self.main_window.info_dialog(
                    '错误',
                    '请先选择一个要删除的用户'
                )
                return

            # 2. 删除用户
            user_id = self.current_user.get('id', '')
            user_name = self.current_user['name']
            self.config_manager.delete_user(user_id)
            logger.info(f"成功删除用户: {user_name} (ID: {user_id})")

            # 3. 刷新界面
            self.refresh_users_table()
            self.clear_user_form()

            # 4. 提供成功反馈
            self.main_window.info_dialog(
                '成功',
                f'用户 {user_name} (ID: {user_id}) 删除成功'
            )

        except Exception as e:
            logger.error(f"删除用户失败: {str(e)}", exc_info=True)
            self.main_window.info_dialog(
                '错误',
                f'删除用户失败: {str(e)}'
            )

    def on_user_selected(self, table):
        """处理用户选择事件"""
        print(f"User selected: {table.selection}")

        row = table.selection
        self.current_user = None

        # 处理空选择
        if not row:
            self.clear_user_form()
            self.update_user_button.enabled = False
            self.delete_user_button.enabled = False
            self.start_user_record_button.enabled = False
            self.stop_user_record_button.enabled = False
            return

        # 获取选中行的用户ID
        user_id = int(row.id)

        # 加载完整用户数据
        user_data = self.config_manager.get_user_by_id(user_id)
        self.current_user = user_data

        if user_data:
            # 更新表单字段
            self.name_input.value = user_data['name']
            self.platform_select.value = user_data['platform']
            self.interval_input.value = str(user_data['interval'])
            self.duration_input.value = str(user_data['duration']) if user_data['duration'] else ''
            self.unit_select.value = user_data['duration_unit'] if user_data['duration_unit'] else 'seconds'

            # 启用操作按钮
            self.update_user_button.enabled = True
            self.delete_user_button.enabled = True

            # 根据录制状态启用/禁用单个用户录制按钮
            is_recording = self.user_recording_status.get(user_id, False)
            self.start_user_record_button.enabled = not is_recording
            self.stop_user_record_button.enabled = is_recording

    async def toggle_user_recording(self, user_id):
        """切换单个用户的录制状态"""
        try:
            user_data = self.config_manager.get_user_by_id(user_id)
            if not user_data:
                await self.show_info_message('错误', '用户不存在')
                return

            is_recording = self.user_recording_status.get(user_id, False)

            if not is_recording:
                # 开始录制该用户
                await self.start_user_recording(user_id, user_data)
            else:
                # 停止录制该用户
                await self.stop_user_recording(user_id)

            # 刷新表格显示
            self.refresh_users_table()

            # 如果当前选中的用户状态发生变化，更新按钮状态
            if self.current_user and self.current_user['id'] == user_id:
                is_recording = self.user_recording_status.get(user_id, False)
                self.start_user_record_button.enabled = not is_recording
                self.stop_user_record_button.enabled = is_recording

        except Exception as e:
            logger.error(f"切换用户录制状态失败: {str(e)}", exc_info=True)
            await self.show_info_message('错误', f'操作失败: {str(e)}')

    async def start_user_recording(self, user_id, user_data):
        """开始录制指定用户"""
        try:
            # 验证输出目录
            output_dir = self.config_manager.get_global_config('output', '')
            # 如果GUI中没有设置输出目录，则不传递output到LiveRecorder，让其使用默认值
            if not output_dir:
                logger.info("GUI中未设置输出目录，将使用LiveRecorder的默认输出目录。")
            elif not os.path.isdir(output_dir):
                await self.show_info_message('错误', '请先设置有效的输出目录')
                return

            # 创建单用户配置
            config = {
                'proxy': self.config_manager.get_global_config('proxy', ''),
                'output': output_dir,
                'user': [user_data]  # 只包含当前用户
            }

            # 使用asyncio.create_task在后台启动录制，避免阻塞GUI
            await asyncio.create_task(self._start_user_recording_task(user_id, user_data, config))

            logger.info(f"正在启动用户 {user_data['name']} 的录制...")

        except Exception as e:
            logger.error(f"启动用户录制失败: {str(e)}", exc_info=True)
            await self.show_info_message('错误', f'启动录制失败: {str(e)}')

    async def _start_user_recording_task(self, user_id, user_data, config):
        """在后台线程中启动用户录制的异步任务"""
        try:
            # 直接在后台线程中创建录制器，不使用线程池等待
            def create_recorder_and_start():
                """在独立线程中创建录制器并启动录制循环"""
                try:
                    # 创建GUI超时回调函数
                    def gui_timeout_callback(recorder_user_id):
                        """处理录制超时时的GUI状态更新"""
                        logger.debug(f"全局录制中用户 {recorder_user_id} 定时录制结束，更新GUI状态")
                        try:
                            # 对于全局录制，需要停止全局录制器
                            if hasattr(self, 'recorder') and self.recorder:
                                logger.debug(f"停止全局录制器")
                                self.recorder.stop()
                                # 等待一小段时间确保资源释放
                                import time
                                time.sleep(0.5)
                                # 清理全局录制器引用
                                self.recorder = None
                                # 更新录制状态
                                self.recording = False
                                
                                # 更新UI状态（在主线程中执行）
                                try:
                                    # 更新按钮状态
                                    self.record_button.text = "开始全部录制"
                                    self.record_button.enabled = True
                                    if hasattr(self.record_button.style, 'color'):
                                        del self.record_button.style.color
                                except Exception as e:
                                    logger.warning(f"更新按钮状态时出错: {e}")
                        except Exception as e:
                            logger.error(f"停止全局录制器时出错: {e}")
                        
                        # 更新用户录制状态
                        if recorder_user_id in self.user_recording_status:
                            self.user_recording_status[recorder_user_id] = False
                        
                        # 刷新界面（直接在当前线程中执行，避免使用asyncio）
                        try:
                            # 直接调用非异步方法刷新表格
                            self.refresh_users_table()
                            
                            # 如果当前选中的用户录制状态发生变化，更新按钮状态
                            if self.current_user and self.current_user['id'] == recorder_user_id:
                                self.start_user_record_button.enabled = True
                                self.stop_user_record_button.enabled = False
                            
                            logger.debug("定时录制结束后UI状态已更新")
                        except Exception as e:
                            logger.warning(f"刷新UI时出错: {e}")
                    
                    # 不再需要导入LiveRecorder，因为已在主线程中导入
                    # from .live_recorder import LiveRecorder
                    recorder = LiveRecorder(config, gui_timeout_callback=gui_timeout_callback)
                    run_loop = recorder.start()

                    # 保存录制器引用（在主线程中访问需要线程安全）
                    self.user_recorders[user_id] = recorder

                    # 直接在此线程中运行录制循环
                    if run_loop:
                        logger.info(f"用户 {user_data['name']} 录制循环开始运行")
                        run_loop()  # 这会阻塞当前线程，但不会阻塞GUI
                        logger.info(f"用户 {user_data['name']} 录制循环结束")

                except Exception as e:
                    logger.error(f"用户 {user_data['name']} 录制线程执行失败: {str(e)}", exc_info=True)
                finally:
                    # 清理录制状态
                    self.user_recording_status[user_id] = False
                    if user_id in self.user_recorders:
                        del self.user_recorders[user_id]
                    if user_id in self.user_recording_status:
                        self.user_recording_status[user_id] = False

            # 创建并启动录制线程
            record_thread = threading.Thread(
                target=create_recorder_and_start,
                daemon=True,
                name=f"recording_user_{user_id}"
            )
            record_thread.start()

            # 保存线程引用
            self.recorder_threads.append(record_thread)

            # 更新用户录制状态
            self.user_recording_status[user_id] = True

            # 刷新界面
            self.refresh_users_table()
            if self.current_user and self.current_user['id'] == user_id:
                self.start_user_record_button.enabled = False
                self.stop_user_record_button.enabled = True

            logger.info(f"用户 {user_data['name']} 录制任务已启动")
            await self.show_info_message('成功', f"用户 {user_data['name']} 录制任务已启动")

        except Exception as e:
            logger.error(f"启动用户录制失败: {str(e)}", exc_info=True)
            await self.show_info_message('错误', f'启动录制失败: {str(e)}')

    async def _start_global_recording_task(self, config):
        """在后台线程中启动全局录制的异步任务"""
        try:
            # 直接在后台线程中创建录制器，不使用线程池等待
            def create_recorder_and_start():
                """在独立线程中创建录制器并启动录制循环"""
                try:
                    # 创建GUI超时回调函数
                    def gui_timeout_callback(recorder_user_id):
                        """处理录制超时时的GUI状态更新"""
                        logger.debug(f"全局录制中用户 {recorder_user_id} 定时录制结束，更新GUI状态")
                        try:
                            # 对于全局录制，需要停止全局录制器
                            if hasattr(self, 'recorder') and self.recorder:
                                logger.debug(f"停止全局录制器")
                                self.recorder.stop()
                                # 等待一小段时间确保资源释放
                                import time
                                time.sleep(0.5)
                                # 清理全局录制器引用
                                self.recorder = None
                                # 更新录制状态
                                self.recording = False
                                
                                # 更新UI状态（在主线程中执行）
                                try:
                                    # 更新按钮状态
                                    self.record_button.text = "开始全部录制"
                                    self.record_button.enabled = True
                                    if hasattr(self.record_button.style, 'color'):
                                        del self.record_button.style.color
                                except Exception as e:
                                    logger.warning(f"更新按钮状态时出错: {e}")
                        except Exception as e:
                            logger.error(f"停止全局录制器时出错: {e}")
                        
                        # 更新用户录制状态
                        if recorder_user_id in self.user_recording_status:
                            self.user_recording_status[recorder_user_id] = False
                        
                        # 刷新界面（直接在当前线程中执行，避免使用asyncio）
                        try:
                            # 直接调用非异步方法刷新表格
                            self.refresh_users_table()
                            
                            # 如果当前选中的用户录制状态发生变化，更新按钮状态
                            if self.current_user and self.current_user['id'] == recorder_user_id:
                                self.start_user_record_button.enabled = True
                                self.stop_user_record_button.enabled = False
                            
                            logger.debug("定时录制结束后UI状态已更新")
                        except Exception as e:
                            logger.warning(f"刷新UI时出错: {e}")
                    
                    # 不再需要导入LiveRecorder，因为已在主线程中导入
                    # from .live_recorder import LiveRecorder
                    recorder = LiveRecorder(config, gui_timeout_callback=gui_timeout_callback)
                    run_loop = recorder.start()

                    # 保存录制器引用
                    self.recorder = recorder

                    # 直接在此线程中运行录制循环
                    if run_loop:
                        logger.info("全局录制循环开始运行")
                        run_loop()  # 这会阻塞当前线程，但不会阻塞GUI
                        logger.info("全局录制循环结束")

                except Exception as e:
                    logger.error(f"全局录制线程执行失败: {str(e)}", exc_info=True)
                finally:
                    # 清理录制状态
                    self.recording = False
                    if hasattr(self, 'recorder'):
                        self.recorder = None
                    
                    # 清理所有用户录制状态（类似stop_user_recording的逻辑）
                    for user_id in list(self.user_recorders.keys()):
                        recorder = self.user_recorders[user_id]
                        if recorder:
                            try:
                                recorder.stop()
                            except Exception as e:
                                logger.warning(f"停止用户录制器时出错: {e}")
                        del self.user_recorders[user_id]
                        self.user_recording_status[user_id] = False
                    
                    # 更新UI状态
                    try:
                        asyncio.create_task(self._async_update_ui_after_recording_stop())
                    except Exception as e:
                        logger.warning(f"更新UI状态时出错: {e}")

            # 创建并启动录制线程
            record_thread = threading.Thread(
                target=create_recorder_and_start,
                daemon=True,
                name="recording_main_thread"
            )
            record_thread.start()

            # 保存线程引用
            self.recorder_threads.append(record_thread)

            # 重新启用按钮
            self.record_button.enabled = True

            logger.info("全局录制任务已启动")
            await self.show_info_message('成功', '全局录制任务已启动')

        except Exception as e:
            logger.error(f"启动全局录制失败: {str(e)}", exc_info=True)

            # 启动失败，重置状态
            self.recording = False
            self.record_button.text = "开始全部录制"
            self.record_button.enabled = True
            del self.record_button.style.color

            await self.show_info_message('错误', f'启动全局录制失败: {str(e)}')

    async def stop_user_recording(self, user_id):
        """停止录制指定用户"""
        try:
            recorder = self.user_recorders.get(user_id)
            if recorder:
                # 在后台线程中停止录制器，避免阻塞GUI
                def stop_recorder_thread():
                    try:
                        logger.debug(f"在后台线程中停止用户 {user_id} 的录制器")
                        recorder.stop()
                        logger.debug(f"用户 {user_id} 的录制器已停止")
                    except Exception as e:
                        logger.error(f"停止录制器线程中出错: {e}")
                
                # 创建并启动停止线程
                stop_thread = threading.Thread(
                    target=stop_recorder_thread,
                    daemon=True,
                    name=f"stop_recording_user_{user_id}"
                )
                stop_thread.start()
                
                # 立即从字典中删除录制器引用，避免重复停止
                del self.user_recorders[user_id]

            # 立即更新录制状态，不等待线程完成
            self.user_recording_status[user_id] = False

            user_data = self.config_manager.get_user_by_id(user_id)
            user_name = user_data['name'] if user_data else f'用户{user_id}'

            logger.info(f"用户 {user_name} 停止录制")
            await self.show_info_message('成功', f"用户 {user_name} 停止录制")

        except Exception as e:
            logger.error(f"停止用户录制失败: {str(e)}", exc_info=True)
            await self.show_info_message('错误', f'停止录制失败: {str(e)}')

    async def start_selected_user_recording(self, widget):
        """开始录制选中的用户"""
        if not self.current_user:
            await self.show_info_message('错误', '请先选择一个用户')
            return

        user_id = self.current_user['id']
        await self.toggle_user_recording(user_id)

    async def stop_selected_user_recording(self, widget):
        """停止录制选中的用户"""
        if not self.current_user:
            await self.show_info_message('错误', '请先选择一个用户')
            return

        user_id = self.current_user['id']
        await self.toggle_user_recording(user_id)

    def toggle_recording(self, widget):
        """切换录制状态"""
        try:
            if not self.recording:
                # 开始录制
                logger.debug("开始验证录制条件")
                if not self.validate_recording_conditions():
                    logger.warning("录制条件验证失败")
                    return

                self.recording = True
                self.record_button.text = "停止全部录制"
                self.record_button.style.color = "red"
                self.record_button.enabled = False  # 暂时禁用按钮防止重复点击

                # 导出配置
                exported_config = self.config_manager.export_config()
                
                # 准备传递给 LiveRecorder 的配置
                live_recorder_config = {}
                if 'proxy' in exported_config:
                    live_recorder_config['proxy'] = exported_config['proxy']
                if 'user' in exported_config:
                    live_recorder_config['user'] = exported_config['user']
                
                # 只有当 output_dir 在导出配置中存在且有效时才添加到config
                output_dir_from_config = exported_config.get('output', '')
                if output_dir_from_config and os.path.isdir(output_dir_from_config):
                    live_recorder_config['output'] = output_dir_from_config
                else:
                    logger.info("全局配置中未设置有效输出目录，将使用LiveRecorder的默认输出目录。")

                # 创建后台线程来处理录制
                def record_task():
                    try:
                        self.recorder = LiveRecorder(live_recorder_config) # 使用处理过的配置
                        run_loop = self.recorder.start()
                        if run_loop:
                            logger.info("录制循环开始运行")
                            run_loop()
                    except Exception as e:
                        logger.error(f"录制线程执行失败: {str(e)}", exc_info=True)
                        # 使用主线程安全的方式更新UI
                        asyncio.create_task(self._async_handle_recording_error())

                # 启动录制线程
                record_thread = threading.Thread(
                    target=record_task,
                    daemon=True,
                    name="recording_main_thread"
                )
                record_thread.start()
                self.recorder_threads.append(record_thread)

                logger.info("录制任务已启动")
                self.record_button.enabled = True  # 重新启用按钮

            else:
                # 停止录制
                logger.debug("开始停止录制过程")
                self.recording = False
                self.record_button.text = "开始全部录制"
                self.record_button.enabled = True
                del self.record_button.style.color

                # 停止全局录制器
                if self.recorder:
                    logger.debug("正在停止全局录制器")
                    self.recorder.stop()
                    logger.debug("全局录制器已停止")

                # 停止所有单个用户录制
                for user_id in list(self.user_recorders.keys()):
                    recorder = self.user_recorders[user_id]
                    if recorder:
                        recorder.stop()
                    del self.user_recorders[user_id]
                    self.user_recording_status[user_id] = False

                # 清理录制线程（不阻塞主线程）
                def cleanup_threads():
                    for thread in self.recorder_threads:
                        if thread.is_alive():
                            thread.join(timeout=0.1)
                    self.recorder_threads = [t for t in self.recorder_threads if t.is_alive()]
                    logger.debug(f"剩余活动线程数: {len(self.recorder_threads)}")
                    # 在主线程中刷新表格
                    asyncio.create_task(self._async_refresh_users_table())

                cleanup_thread = threading.Thread(
                    target=cleanup_threads,
                    daemon=True,
                    name="cleanup_thread"
                )
                cleanup_thread.start()
                
                # 将清理线程也添加到recorder_threads列表中，确保在退出时能够正确终止
                self.recorder_threads.append(cleanup_thread)

                logger.info("录制已停止")

        except Exception as e:
            logger.error(f"切换录制状态失败: {str(e)}", exc_info=True)
            self.main_window.info_dialog(
                '错误',
                f'切换录制状态失败: {str(e)}'
            )
            # 确保状态重置
            self.recording = False
            self.record_button.text = "开始全部录制"
            self.record_button.enabled = True
            del self.record_button.style.color

    def _handle_recording_error(self, *args):
        """处理录制错误的回调函数"""
        self.recording = False
        self.record_button.text = "开始全部录制"
        self.record_button.enabled = True
        del self.record_button.style.color
        self.main_window.info_dialog(
            '错误',
            '录制过程发生错误，已停止录制'
        )

    async def _async_handle_recording_error(self):
        """异步处理录制错误的包装器"""
        self._handle_recording_error()

    async def _async_refresh_users_table(self):
        """异步刷新用户表格的包装器"""
        self.refresh_users_table()
        
    async def _async_update_ui_after_recording_stop(self):
        """异步更新UI状态（在录制停止后）"""
        # 更新按钮状态
        self.record_button.text = "开始全部录制"
        self.record_button.enabled = True
        del self.record_button.style.color
        
        # 刷新用户表格
        self.refresh_users_table()
        
        # 显示通知
        await self.show_info_message('成功', "录制已停止")
    
    async def _async_refresh_ui_after_timeout(self):
        """定时录制结束后异步刷新UI状态"""
        try:
            # 刷新用户表格
            self.refresh_users_table()
            
            # 如果当前选中的用户录制状态发生变化，更新按钮状态
            if self.current_user:
                user_id = self.current_user['id']
                is_recording = self.user_recording_status.get(user_id, False)
                self.start_user_record_button.enabled = not is_recording
                self.stop_user_record_button.enabled = is_recording
            
            # 显示通知
            await self.show_info_message('提示', '定时录制已结束')
            
            logger.debug("定时录制结束后UI状态已更新")
        except Exception as e:
            logger.error(f"刷新UI状态时出错: {e}", exc_info=True)

    def validate_recording_conditions(self):
        """验证录制条件"""
        try:
            # 检查输出目录
            output_dir = self.config_manager.get_global_config('output', '')
            if not output_dir or not os.path.isdir(output_dir):
                self.main_window.info_dialog(
                    '错误',
                    '请先设置有效的输出目录'
                )
                return False

            # 检查至少有一个用户
            users = self.config_manager.get_all_users()
            if not users:
                self.main_window.info_dialog(
                    '错误',
                    '请先添加至少一个录制用户'
                )
                return False

            return True
        except Exception as e:
            logger.error(f"验证录制条件失败: {str(e)}", exc_info=True)
            return False

# 修改main函数启动方式
def main():
    app = LiveRecorderApp()
    app.main_loop()