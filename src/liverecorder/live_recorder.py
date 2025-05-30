import asyncio
import json
import os
import re
import time
import threading

import warnings
import urllib3
from urllib3.exceptions import InsecureRequestWarning
from .config_manager import ConfigManager
# 禁用所有urllib3的警告
urllib3.disable_warnings()
# 特别禁用 InsecureRequestWarning 警告
warnings.filterwarnings('ignore', category=InsecureRequestWarning)
from loguru import logger
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, Tuple, Union
from urllib.parse import parse_qs
import requests
import hashlib
import time
import urllib.parse
# 配置requests禁用SSL验证警告
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

import anyio
import ffmpeg
import httpx
import jsengine
import streamlink
from httpx_socks import AsyncProxyTransport
from jsonpath_ng.ext import parse
from streamlink.options import Options
from streamlink.stream import StreamIO, HTTPStream, HLSStream
from streamlink_cli.main import open_stream
from streamlink_cli.output import FileOutput
from streamlink_cli.streamrunner import StreamRunner

recording: Dict[str, Tuple[StreamIO, FileOutput]] = {}


class LiveRecorder:
    def __init__(self, config: dict, user: dict = None, proxies=None, gui_timeout_callback=None):
        """初始化直播录制器

        Args:
            config (dict): 配置字典，包含全局配置
            user (dict, optional): 用户特定的配置。如果提供，将立即初始化录制器配置
            proxies (dict, optional): 代理配置字典
            gui_timeout_callback (callable, optional): GUI超时回调函数
        """
        # 保存完整配置
        self.config = config
        self.proxies = proxies
        self.gui_timeout_callback = gui_timeout_callback

        # 初始化录制状态
        self.running = False
        self.tasks = []
        logger.add(
            os.path.join(ConfigManager().log_dir, 'log_{time:YYYY-MM-DD}.log'),
            rotation="00:00",
            retention="3 days",
            encoding='utf-8'
        )
        # logger.add(
        #     sink='logs/log_{time:YYYY-MM-DD}.log',
        #     rotation='00:00',
        #     retention='3 days',
        #     level='INFO',
        #     encoding='utf-8',
        #     format='[{time:YYYY-DD-MM HH:mm:ss}][{level}][{name}][{function}:{line}]{message}'
        # )

        # 如果提供了用户配置，立即初始化录制器
        if user is not None:
            self._init_recorder(user)

    def update_config(self, config: dict):
        """更新配置

        Args:
            config (dict): 新的配置字典
        """
        logger.info("更新录制器配置")
        self.config = config

        # 更新全局配置
        if 'proxy' in config:
            self.proxy = config.get('proxy')
            logger.info(f"更新代理设置: {self.proxy}")

        if 'output' in config:
            self.output = config.get('output')
            logger.info(f"更新输出目录: {self.output}")

        # 如果有用户配置更新，需要重新初始化对应的录制器
        # 这里只是更新配置，不会重启录制任务

    def add_user(self, user: dict):
        """动态添加新用户监控

        Args:
            user (dict): 新用户配置字典
        """
        if not self.running:
            logger.warning("录制器未运行，无法添加用户")
            return

        try:
            # 创建对应平台的录制实例
            platform_class = globals()[user['platform']]
            recorder = platform_class(self.config, user, gui_timeout_callback=self.gui_timeout_callback)

            # 启动新的录制任务
            task = self.loop.create_task(self._record_task(recorder))
            self.tasks.append(task)
            logger.info(f"成功添加用户监控: {user['name']}")

        except Exception as e:
            logger.exception("添加用户监控失败")
            raise e

    def stop_user(self, user_id: int):
        """停止指定用户的录制

        Args:
            user_id (int): 用户ID
        """
        if not self.running:
            logger.warning("录制器未运行")
            return

        try:
            # 查找并取消对应用户的任务
            tasks_to_remove = []
            for task in self.tasks:
                # 检查任务是否对应指定用户
                if hasattr(task, '_recorder') and hasattr(task._recorder, 'id'):
                    if task._recorder.id == user_id:
                        logger.info(f"取消用户 {user_id} 的录制任务")
                        task.cancel()
                        tasks_to_remove.append(task)

            # 从任务列表中移除已取消的任务
            for task in tasks_to_remove:
                self.tasks.remove(task)

            logger.info(f"成功停止用户 {user_id} 的录制")

        except Exception as e:
            logger.exception(f"停止用户 {user_id} 录制失败")
            raise e

    def _init_recorder(self, user: dict):
        """初始化单个录制实例的配置

        Args:
            user (dict): 用户配置字典
        """
        self.id = user['id']  # 数据库中的自增ID
        self.name = user['name']  # 用户名
        platform = user['platform']
        self.flag = f'[{platform}][{self.name}]'

        self.interval = user.get('interval', 10)
        self.crypto_js_url = user.get('crypto_js_url', '')
        self.headers = user.get('headers', {'User-Agent': 'Chrome'})
        self.cookies = user.get('cookies')
        self.format = user.get('format')
        self.proxy = user.get('proxy', self.config.get('proxy'))
        self.output = user.get('output', self.config.get('output', 'output'))
        if not self.crypto_js_url:
            self.crypto_js_url = 'https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js'
        self.get_cookies()
        self.client = self.get_client()

    def start(self):
        """启动录制

        初始化并启动所有配置的直播录制任务
        """
        if self.running:
            logger.warning("录制已经在运行中")
            return

        logger.debug("设置录制状态为运行中")
        self.running = True
        self.ssl = True
        self.mState = 0

        # 创建事件循环但不在此方法中运行
        logger.debug("创建新的事件循环")
        self.loop = asyncio.new_event_loop()
        logger.debug(f"事件循环创建成功: {self.loop!r}")

        # 返回一个函数，该函数将在单独的线程中运行
        def run_loop():
            try:
                logger.debug(f"进入run_loop函数，当前线程ID: {threading.get_ident()}")
                logger.debug("run_loop函数开始执行")
                logger.debug(f"设置事件循环: {self.loop!r}")
                asyncio.set_event_loop(self.loop)
                logger.debug("事件循环已设置")

                try:
                    logger.debug(f"用户配置: {self.config['user']}")
                    for user in self.config['user']:
                        # 创建对应平台的录制实例
                        logger.debug(f"为用户 {user['name']} 创建 {user['platform']} 平台的录制实例")
                        try:
                            platform_class = globals()[user['platform']]
                            logger.debug(f"成功获取平台类: {platform_class.__name__}")
                            recorder = platform_class(self.config, user, gui_timeout_callback=self.gui_timeout_callback)
                            logger.debug(f"成功创建录制实例: {recorder}")

                            # 启动录制任务
                            logger.debug(f"为用户 {user['name']} 创建录制任务")
                            task = self.loop.create_task(self._record_task(recorder))
                            self.tasks.append(task)
                            logger.debug(f"成功创建录制任务: {task}")
                        except KeyError as e:
                            logger.error(f"找不到平台类 {user['platform']}: {e}")
                        except Exception as e:
                            logger.error(f"创建录制实例失败: {e}", exc_info=True)

                    # 运行事件循环
                    logger.debug("开始运行事件循环")
                    self.loop.run_forever()
                    logger.debug("事件循环已结束")
                except Exception as e:
                    logger.exception(f"启动录制失败: {e}")
                    self.stop()
                    raise e
            except Exception as e:
                logger.error(f"run_loop函数执行出错: {str(e)}", exc_info=True)
            finally:
                logger.debug("run_loop函数执行完毕")

        logger.debug("返回run_loop函数")
        return run_loop  # 返回函数而不是直接执行

    def stop(self):
        """停止录制

        取消所有录制任务并关闭事件循环
        """
        if not self.running:
            logger.warning("录制未运行")
            return

        logger.debug("开始停止录制")
        self.running = False

        # 取消所有任务 - 使用简化的线程安全方式
        if self.tasks:
            logger.debug(f"取消 {len(self.tasks)} 个录制任务")
            if hasattr(self, 'loop') and self.loop and self.loop.is_running():
                # 在事件循环线程中安全地取消任务
                def cancel_tasks():
                    for task in self.tasks:
                        if not task.done():
                            logger.debug(f"取消任务: {task}")
                            task.cancel()
                
                self.loop.call_soon_threadsafe(cancel_tasks)
                
                # 简单等待，避免复杂的事件循环操作
                import time
                time.sleep(0.2)  # 给事件循环时间处理取消请求
                
                # 清空任务列表
                self.tasks = []
                logger.debug("任务取消请求已发送，继续执行")
            else:
                # 如果事件循环不在运行，直接清空任务列表
                self.tasks = []

        # 关闭所有直播流
        for stream_fd, output in recording.copy().values():
            logger.debug(f"关闭直播流: {stream_fd}")
            try:
                stream_fd.close()
                output.close()
            except Exception as e:
                logger.warning(f"关闭流时出错: {e}")

        # 停止事件循环
        if hasattr(self, 'loop') and self.loop and self.loop.is_running():
            logger.debug("停止事件循环")
            self.loop.call_soon_threadsafe(self.loop.stop)
            logger.debug("事件循环停止信号已发送")

        logger.debug("录制已完全停止")

    async def _record_task(self, recorder):
        """录制任务

        Args:
            recorder: 录制实例
        """
        logger.debug(f"开始录制任务: {recorder.flag}")

        # 为任务添加录制器引用，以便能够通过用户ID识别任务
        current_task = asyncio.current_task()
        current_task._recorder = recorder

        # 创建任务特定的运行标志，允许单独停止此任务
        task_running = True

        while self.running and task_running:
            try:
                logger.debug(f"执行录制: {recorder.flag}")
                logger.debug(f"录制器类型: {recorder.__class__.__name__}")

                # 设置超时保护
                try:
                    # 直接调用异步的录制方法
                    logger.debug(f"开始执行录制器的run方法: {recorder.flag}")
                    # 直接调用异步的run方法，设置超时
                    await asyncio.wait_for(
                        recorder.run(),
                        timeout=300  # 增加超时时间到5分钟，给录制更多时间
                    )
                    logger.debug(f"录制器run方法执行完成: {recorder.flag}")
                except asyncio.TimeoutError:
                    logger.error(f"录制器run方法执行超时(300秒): {recorder.flag}，可能是网络问题或直播源获取失败")
                    # 继续执行，等待下一次检查
                except asyncio.CancelledError:
                    logger.info(f"录制任务被取消: {recorder.flag}")
                    task_running = False
                    break
                except Exception as run_error:
                    logger.error(f"录制器run方法执行出错: {recorder.flag}, 错误: {run_error}", exc_info=True)

                logger.debug(f"录制执行完成，等待间隔: {recorder.interval}秒")
                # 等待指定的间隔时间
                for i in range(recorder.interval):
                    if not self.running or not task_running:
                        logger.debug(f"录制已停止，退出等待循环: {recorder.flag}")
                        break
                    if i % 10 == 0:  # 每10秒记录一次日志
                        logger.debug(f"等待中: {recorder.flag}, 已等待{i}秒, 总计{recorder.interval}秒")
                    await asyncio.sleep(1)  # 每秒检查一次是否需要停止
            except asyncio.CancelledError:
                logger.info(f"录制任务被取消: {recorder.flag}")
                task_running = False
                break
            except Exception as e:
                logger.error(f"录制任务异常: {e}", exc_info=True)
                # 添加短暂休眠，避免因持续错误导致CPU占用过高
                await asyncio.sleep(5)
        logger.debug(f"录制任务结束: {recorder.flag}")

    async def run(self):
        pass

    async def request(self, method, url, **kwargs):
        try:
            response = await self.client.request(method, url, **kwargs)
            return response
        except httpx.ProtocolError as error:
            raise ConnectionError(f'{self.flag}直播检测请求协议错误\n{error}')
        except httpx.HTTPStatusError as error:
            raise ConnectionError(
                f'{self.flag}直播检测请求状态码错误\n{error}\n{response.text}')
        except anyio.EndOfStream as error:
            raise ConnectionError(f'{self.flag}直播检测代理错误\n{error}')
        except httpx.HTTPError as error:
            logger.error(f'网络异常 重试...')
            raise ConnectionError(f'{self.flag}直播检测请求错误\n{repr(error)}')

    def get_client(self):
        client_kwargs = {
            'http2': True,
            'timeout': self.interval,
            'limits': httpx.Limits(max_keepalive_connections=100, keepalive_expiry=self.interval * 2),
            'headers': self.headers,
            'cookies': self.cookies
        }
        # 检查是否有设置代理
        if self.proxy:
            if 'socks' in self.proxy:
                client_kwargs['transport'] = AsyncProxyTransport.from_url(self.proxy)
            else:
                client_kwargs['proxy'] = self.proxy
        return httpx.AsyncClient(**client_kwargs)

    def get_cookies(self):
        if self.cookies:
            cookies = SimpleCookie()
            cookies.load(self.cookies)
            self.cookies = {k: v.value for k, v in cookies.items()}

    def get_filename(self, title, format):
        live_time = time.strftime('%Y.%m.%d %H.%M.%S')
        # 文件名特殊字符转换为全角字符
        char_dict = {
            '"': '＂',
            '*': '＊',
            ':': '：',
            '<': '＜',
            '>': '＞',
            '?': '？',
            '/': '／',
            '\\': '＼',
            '|': '｜'
        }
        for half, full in char_dict.items():
            title = title.replace(half, full)
        filename = f'[{live_time}]{self.flag}{title[:50]}.{format}'
        return filename

    def get_streamlink(self):
        session = streamlink.session.Streamlink({
            'stream-segment-timeout': 60,
            'hls-segment-queue-threshold': 10
        })
        ssl = self.ssl
        logger.info(f'是否验证SSL：{ssl}')
        session.set_option('http-ssl-verify', ssl)
        # 添加streamlink的http相关选项
        if proxy := self.proxy:
            # 代理为socks5时，streamlink的代理参数需要改为socks5h，防止部分直播源获取失败
            if 'socks' in proxy:
                proxy = proxy.replace('://', 'h://')
            session.set_option('http-proxy', proxy)
        if self.headers:
            session.set_option('http-headers', self.headers)
        if self.cookies:
            session.set_option('http-cookies', self.cookies)
        return session

    def run_record(self, stream: Union[StreamIO, HTTPStream], url, title, format, duration=None, duration_unit=None, timeout_callback=None):
        # 默认使用MP4格式，除非有特殊配置
        output_format = self.format if self.format else 'mp4'
        # 获取输出文件名
        filename = self.get_filename(title, output_format)

        if not stream:
            logger.error(f'{self.flag}无可用直播源：{filename}')
            return

        try:
            logger.debug(f'{self.flag}开始录制：{filename}')
            logger.debug(f'{self.flag}录制参数：format={format}, duration={duration}, duration_unit={duration_unit}')

            # 确保输出目录存在
            output_dir = Path(self.output)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / filename
            logger.debug(f'{self.flag}输出路径：{output_path}')

            # 初始化变量
            stream_fd = None
            prebuffer = None
            
            # 打开流并获取prebuffer
            logger.debug(f'{self.flag}打开流并获取prebuffer')
            stream_fd, prebuffer = open_stream(stream)
            
            # 创建输出文件
            logger.debug(f'{self.flag}创建输出文件')
            output = FileOutput(output_path)
            logger.debug(f'{self.flag}输出文件创建成功')

            # 记录录制信息
            recording[url] = (stream_fd, output)
            logger.debug(f'{self.flag}已添加到录制列表，当前录制数量：{len(recording)}')

            # 设置录制时长
            if duration and duration_unit:
                logger.debug(f'{self.flag}设置录制时长：{duration} {duration_unit}')
                if duration_unit == 'seconds':
                    timeout = duration
                elif duration_unit == 'minutes':
                    timeout = duration * 60
                elif duration_unit == 'hours':
                    timeout = duration * 3600
                else:
                    timeout = None
                logger.debug(f'{self.flag}录制超时设置为：{timeout}秒')
            else:
                timeout = None
                logger.debug(f'{self.flag}未设置录制时长，将持续录制')
            # 使用超时控制录制时长
            start_time = time.time()
            output_opened = False
            try:
                output.open()
                output_opened = True
                logger.debug(f'{self.flag}输出文件已打开')
                
                # 创建并启动流运行器
                logger.debug(f'{self.flag}创建StreamRunner')
                stream_runner = StreamRunner(stream_fd, output)
                logger.debug(f'{self.flag}开始运行StreamRunner')
                if timeout:
                    logger.debug(f'{self.flag}使用超时控制：{timeout}秒')
                    # StreamRunner.run()需要prebuffer参数，需要在外部控制时长
                    
                    
                    def timeout_handler():
                        logger.debug(f'{self.flag}录制已达到设定时长，准备结束录制')
                        # 通过关闭流来结束录制，而不是直接关闭输出
                        try:
                            if hasattr(stream_fd, 'close') and not stream_fd.closed:
                                stream_fd.close()
                        except:
                            pass
                        
                        # 调用回调函数处理GUI状态
                        if timeout_callback and hasattr(self,'id'):
                            try:
                                timeout_callback(self.id)
                                logger.debug(f'{self.flag}已调用超时回调函数')
                            except Exception as e:
                                logger.warning(f'{self.flag}执行超时回调时出错: {e}')
                    
                    # 设置定时器
                    timer = threading.Timer(timeout, timeout_handler)
                    timer.start()
                    
                    try:
                        stream_runner.run(prebuffer)
                        logger.debug(f'{self.flag}录制已结束')
                    finally:
                        timer.cancel()  # 取消定时器
                else:
                    logger.debug(f'{self.flag}无超时控制，持续录制')
                    stream_runner.run(prebuffer)
                    logger.debug(f'{self.flag}录制已结束')
            except KeyboardInterrupt:
                logger.info(f'{self.flag}录制被用户中断')
            except Exception as e:
                logger.error(f'{self.flag}录制过程中出错：{e}', exc_info=True)
            finally:
                end_time = time.time()
                duration = end_time - start_time
                logger.info(f'{self.flag}录制完成，持续时间：{duration:.2f}秒')

                # 安全关闭资源
                try:
                    if output_opened and hasattr(output, 'close') and not getattr(output, 'closed', True):
                        output.close()
                        logger.debug(f'{self.flag}输出文件已关闭')
                except Exception as close_err:
                    logger.warning(f'{self.flag}关闭输出文件时出错: {close_err}')
                
                try:
                    if hasattr(stream_fd, 'close') and not getattr(stream_fd, 'closed', True):
                        stream_fd.close()
                        logger.debug(f'{self.flag}流已关闭')
                except Exception as close_err:
                    logger.warning(f'{self.flag}关闭流时出错: {close_err}')

                # 清理资源
                if url in recording:
                    logger.debug(f'{self.flag}清理录制资源')
                    del recording[url]
                    logger.debug(f'{self.flag}录制资源已清理，当前录制数量：{len(recording)}')
                
                # 如果有用户ID，清理用户录制状态（类似stop_user_recording的逻辑）
                if hasattr(self, 'user_id') and self.user_id:
                    logger.debug(f'{self.flag}清理用户录制状态: {self.user_id}')
                    # 这里需要通知GUI更新状态，但由于这是在LiveRecorder中，
                    # 实际的状态清理应该在GUI层面处理
        except Exception as e:
            logger.error(f'{self.flag}录制失败：{e}', exc_info=True)
            # 确保清理资源
            if url in recording:
                try:
                    # 安全地获取并关闭资源
                    resources = recording[url]
                    if isinstance(resources, tuple) and len(resources) == 2:
                        res_stream_fd, res_output = resources
                        if res_stream_fd:
                            try:
                                res_stream_fd.close()
                            except Exception as close_err:
                                logger.warning(f'{self.flag}关闭流时出错: {close_err}')
                        if res_output:
                            try:
                                res_output.close()
                            except Exception as close_err:
                                logger.warning(f'{self.flag}关闭输出时出错: {close_err}')
                    del recording[url]
                    logger.debug(f'{self.flag}异常情况下清理录制资源完成')
                except Exception as cleanup_error:
                    logger.error(f'{self.flag}清理录制资源失败：{cleanup_error}', exc_info=True)

    def stop_recording(self, url):
        """停止指定URL的录制并清理相关线程"""
        if url not in recording:
            logger.debug(f'{self.flag}未找到录制任务: {url}')
            return

        # 获取流和输出对象
        stream_fd, output = recording.get(url, (None, None))
        if stream_fd is None or output is None:
            logger.warning(f'{self.flag}无效的录制资源')
            return

        # 关闭文件描述符和输出
        close_errors = []
        try:
            if hasattr(stream_fd, 'closed') and not stream_fd.closed:
                stream_fd.close()
                logger.debug(f'{self.flag}已关闭流文件')
        except Exception as close_error:
            close_errors.append(f'流文件关闭错误: {close_error}')
            # 尝试强制关闭
            try:
                if hasattr(stream_fd, '_close'):
                    stream_fd._close()
            except:
                pass

        try:
            if hasattr(output, 'opened') and output.opened:
                output.close()
                logger.debug(f'{self.flag}已关闭输出文件')
        except Exception as close_error:
            close_errors.append(f'输出文件关闭错误: {close_error}')
            # 尝试强制关闭
            try:
                if hasattr(output, '_close'):
                    output._close()
            except:
                pass

        if close_errors:
            logger.warning(f'{self.flag}关闭资源时出错: {" | ".join(close_errors)}')

        # 终止相关线程
        recording_threads = [t for t in threading.enumerate()
                           if getattr(t, 'name', '').startswith(f"recording_thread_{url}") or
                              getattr(t, '_target', None).__name__ == 'run' and
                              getattr(getattr(t, '_target', None), '__self__', None).__class__.__name__ == 'StreamRunner']

        for thread in recording_threads:
            try:
                if thread.is_alive():
                    # 首先尝试优雅地停止线程
                    if hasattr(thread, '_stop_event'):
                        thread._stop_event.set()

                    # 给线程一些时间来清理
                    thread.join(timeout=2.0)

                    # 如果线程仍然活着，使用更激进的方式终止
                    if thread.is_alive():
                        logger.warning(f'{self.flag}强制终止录制线程：{thread.name}')
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
                                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                                    ctypes.c_long(thread.ident),
                                    ctypes.py_object(SystemError)
                                )
                        except Exception as force_error:
                            logger.error(f'{self.flag}强制终止线程失败: {force_error}')
                    else:
                        logger.info(f'{self.flag}成功终止录制线程：{thread.name}')
            except Exception as thread_error:
                logger.error(f'{self.flag}终止线程时出错: {thread_error}')

        # 更新录制状态
        if hasattr(self, 'recording_status'):
            try:
                self.recording_status[url] = False
                logger.debug(f'{self.flag}已更新录制状态')
            except Exception as status_error:
                logger.warning(f'{self.flag}更新状态时出错: {status_error}')

        # 清理录制记录
        try:
            if url in recording:
                recording.pop(url, None)
                logger.info(f'{self.flag}已完全停止录制：{url}')
        except Exception as cleanup_error:
            logger.error(f'{self.flag}清理录制记录时出错: {cleanup_error}')
            # 尝试强制清理
            try:
                recording.pop(url, None)
            except:
                pass

    def stream_writer(self, stream, url, filename):
        logger.info(f'{self.flag}获取到直播流链接：{filename}\n{stream.url}')
        output = FileOutput(Path(f'{self.output}/{filename}'))
        output_opened = False
        try:
            stream_fd, prebuffer = open_stream(stream)
            output.open()
            output_opened = True
            recording[url] = (stream_fd, output)
            logger.info(f'{self.flag}正在录制：{filename}')
            StreamRunner(stream_fd, output).run(prebuffer)
            return True
        except Exception as error:
            if 'timeout' in str(error):
                logger.warning(f'{self.flag}直播录制超时，请检查主播是否正常开播或网络连接是否正常：{filename}\n{error}')
            elif re.search(f'SSL: CERTIFICATE_VERIFY_FAILED', str(error)):
                logger.warning(f'{self.flag}SSL错误，将取消SSL验证：{filename}\n{error}')
                self.ssl = False
            elif re.search(f'(Unable to open URL|No data returned from stream)', str(error)):
                logger.warning(f'{self.flag}直播流打开错误，请检查主播是否正常开播：{filename}\n{error}')
            else:
                logger.exception(f'{self.flag}直播录制错误：{filename}\n{error}')
        finally:
            if output_opened:
                try:
                    output.close()
                except Exception as close_err:
                    logger.warning(f'{self.flag}关闭输出文件时出错: {close_err}')

    def run_ffmpeg(self, filename, format):
        logger.info(f'{self.flag}开始ffmpeg封装：{filename}')
        temp_filename = filename.replace(f'.{format}', f'_temp.{self.format}')
        new_filename = filename.replace(f'.{format}', f'.{self.format}')

        # 先转换到临时文件
        ffmpeg.input(f'{self.output}/{filename}').output(
            f'{self.output}/{temp_filename}',
            codec='copy',
            map_metadata='-1',
            movflags='faststart'
        ).global_args('-hide_banner', '-y').run()

        # 删除原文件
        os.remove(f'{self.output}/{filename}')

        # 重命名临时文件为最终文件名
        os.rename(f'{self.output}/{temp_filename}', f'{self.output}/{new_filename}')

class Kwai(LiveRecorder):
    def __init__(self, config: dict, user: dict, gui_timeout_callback=None):
        """初始化快手录制器

        Args:
            config (dict): 配置字典
            user (dict): 用户配置
            gui_timeout_callback (callable, optional): GUI超时回调函数
        """
        super().__init__(config, user)
        self.mState = 0  # 初始化直播状态
        self.ssl = False  # 设置SSL验证状态
        self.proxy = config['proxy']  # 设置代理
        self.duration = user.get('duration', None)  # 录制时长
        self.duration_unit = user.get('duration_unit', None)  # 时长单位
        self.gui_timeout_callback = gui_timeout_callback  # 保存GUI回调函数
        self.config_manager = type('ConfigManager', (), {
            'get_global_config': lambda self, key: config.get(key)
        })()  # 创建配置管理器对象

    async def create_signature(self, query_str: str, post_dict: dict) -> str:
        # 解析 query_str 为字典
        query_obj = {}
        for pair in query_str.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                query_obj[k] = v
            else:
                query_obj[pair] = ''

        # 合并参数
        map_object = {**query_obj, **{k: str(v) for k, v in post_dict.items()}}

        # 按 key 排序
        sorted_items = sorted(map_object.items(), key=lambda x: x[0])

        # 拼接字符串，value 需 decodeURIComponent
        url_string = ""
        for k, v in sorted_items:
            if k in ['sig', '__NS_sig3', '__NStokensig']:
                continue
            url_string += f"{k}={urllib.parse.unquote(v)}"

        # 拼接密钥
        url_string += "382700b563f4"

        # 计算 md5
        return hashlib.md5(url_string.encode('utf-8')).hexdigest()

    async def user_search(self) -> dict:
        url = "https://az2-api-akpro.kwai-pro.com/rest/o/user/search?"
        query = f"mod=unknown%28unknown%29&lon=0&countryInfo=USA&abi=armeabi-v7a&country_code=us&bucket=us&netScore=1&kpn=KWAI&timestamp={int(time.time() * 1000)}&kwai_tiny_type=2&ds=100&oc=UNKNOWN&egid=&appver=10.3.30.535003&session_id=&mcc=724&pkg=com.kwai.video&__NS_sig3=&kpf=ANDROID_PHONE&did=ANDROID_8ec206c37f1c89a8&app=1&net=WIFI&ud=0&c=GOOGLE_PLAY&time_zone=UTC%20America%2FNew_York&sys=KWAI_ANDROID_5.1.1&language=en-us&lat=0&ver=10.3"

        data = {
            'user_name': self.name,  # 使用name而不是id
            'page': '1',
            'source': 'USER_INPUT',
            'searchSubQueryID': 'd1bdf8ac-b292-43bf-ac52-6b51b622843d',
            'adExtInfo': '{"gaid":"02481f56-ceac-4a1c-9d6f-ed7fbbfbbbed","userAgent":"Dalvik\\/2.1.0 (Linux; U; Android 5.1.1; ASUS_I005DA Build\\/LMY48Z)"}',
            'client_key': '3c2cd3f3',
            'os': 'android',
        }

        sig = await self.create_signature(query, data)
        data['sig'] = sig

        headers = {
            'user-agent': 'kwai-android aegon/3.12.1-2-ge5f58c20-nodiag-nolto',
            'content-type': 'application/x-www-form-urlencoded',
            'x-client-info': 'model=ASUS_I005DA;os=Android;nqe-score=8;network=WIFI;signal-strength=4;'
        }

        # response = await self.request(
        #     method='POST',
        #     url=url + query,
        #     data=data,
        #     headers=headers
        # )


        # 禁用SSL验证警告
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', InsecureRequestWarning)
            # 从配置获取代理设置并支持SOCKS
            proxy_config = self.config_manager.get_global_config('proxy')
            use_proxies = None
            if proxy_config:
                # 自动补全协议前缀
                if not proxy_config.startswith(('http://', 'https://', 'socks5://')):
                    proxy_config = 'http://' + proxy_config

                # 根据协议类型设置proxies
                if proxy_config.startswith('socks5://'):
                    try:
                        import socks
                        use_proxies = {
                            'http': proxy_config,
                            'https': proxy_config
                        }
                    except ImportError:
                        print("Warning: PySocks not installed, SOCKS proxy disabled")
                        use_proxies = None
                else:
                    use_proxies = {
                        'http': proxy_config,
                        'https': proxy_config
                    }

            resp = requests.post(url + query, data=data, headers=headers,
                               proxies=use_proxies, verify=False, timeout=10)
        resp.raise_for_status()

        response_json = resp.json()
        if not response_json.get('users'):
            return None

        user = response_json['users'][0]
        live_info = user.get('liveInfo')
        if not live_info:
            return None

        return {
            'live_stream_id': live_info.get('liveStreamId'),
            'exp_tag': live_info.get('exp_tag'),
            'user_id': live_info.get('user', {}).get('user_id'),
            'zt_play_config': json.loads(live_info.get('playInfo', {}).get('ztPlayConfig', '{}'))
        }

    async def get_live_url(self) -> str:
        info = await self.user_search()
        if not info:
            return None
        url = info['zt_play_config']['liveAdaptiveManifest'][-1]['adaptationSet']['representation'][-1]['url']
        # live_url = re.match(r'(.*?auth_key=[^&]+)', url).group(1)
        # live_url = "httpstream://"+live_url
        return url

    async def run(self):
        url = f''
        if url not in recording:
            try:
                logger.debug(f"{self.flag} 开始获取直播URL")
                live_url = await self.get_live_url()
                logger.debug(f"{self.flag} 获取到直播URL: {live_url}")
                if live_url:
                    self.mState = "1"  # 设置为直播中状态
                    title = self.name.replace(" ", "")  # 使用name作为标题
                    logger.debug(f"{self.flag} 创建HTTPStream")
                    try:
                        # 修复超时处理，使用asyncio.wait_for替代asyncio.timeout
                        async def create_stream_with_timeout():
                            logger.debug(f"{self.flag} 开始创建HTTPStream对象")
                            stream = HTTPStream(
                                self.get_streamlink(),
                                live_url
                            )  # HTTPStream[flv]
                            logger.debug(f"{self.flag} HTTPStream对象创建成功: {stream}")
                            return stream
                        
                        # 使用asyncio.wait_for设置30秒超时
                        stream = await asyncio.wait_for(create_stream_with_timeout(), timeout=30.0)
                        url = live_url
                        # 明确指定文件格式为flv，确保正确转换
                        logger.debug(f"{self.flag} 准备开始录制，格式: flv, 时长: {self.duration} {self.duration_unit}")

                        # 使用超时保护的方式调用run_record
                        try:
                            logger.debug(f"{self.flag} 开始调用run_record")
                            
                            # 创建超时回调函数，用于处理GUI状态更新
                            def timeout_callback(user_id=None):
                                logger.debug(f"{self.flag} 定时录制结束，准备清理状态")
                                # 调用GUI传递的回调函数
                                if self.gui_timeout_callback:
                                    try:
                                        self.gui_timeout_callback(self.id)
                                        logger.debug(f"{self.flag} 已调用GUI超时回调函数")
                                    except Exception as e:
                                        logger.warning(f"{self.flag} 调用GUI超时回调时出错: {e}")
                            
                            await asyncio.to_thread(
                                self.run_record,
                                stream,
                                url,
                                title,
                                'flv',  # 明确指定输入格式
                                self.duration,
                                self.duration_unit,
                                timeout_callback  # 传递超时回调函数
                            )
                            logger.debug(f"{self.flag} run_record调用完成")
                        except Exception as e:
                            logger.error(f"{self.flag} run_record执行失败: {e}", exc_info=True)
                    except asyncio.TimeoutError:
                        logger.error(f"{self.flag} 创建或运行HTTPStream超时")
                    except Exception as e:
                        logger.error(f"{self.flag} 创建或运行HTTPStream失败: {e}", exc_info=True)
                else:
                    self.mState = "2"  # 设置为未开播状态
                    logger.info(f"{self.flag} 未获取到直播URL，可能未开播")
            except Exception as e:
                logger.error(f"{self.flag} 运行录制任务失败: {e}", exc_info=True)
                # 添加短暂休眠，避免立即重试导致CPU占用过高
                await asyncio.sleep(5)


async def run():
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    try:
        tasks = []
        for item in config['user']:
            platform_class = globals()[item['platform']]
            coro = platform_class(config, item).start()
            tasks.append(asyncio.create_task(coro))
        await asyncio.wait(tasks)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        logger.warning('用户中断录制，正在关闭直播流')
        for stream_fd, output in recording.copy().values():
            stream_fd.close()
            output.close()


if __name__ == '__main__':
    asyncio.run(run())

# streamlink "httpstream://http://ali-pro-origin-pull.kwai.net/livecloud/kszt_rn7FUC__WeU_snma450.flv?auth_key=1750825651-0-0-a3ddb20e72ea41217b250329e071c7ac" best
