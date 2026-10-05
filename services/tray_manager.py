from __future__ import annotations
# -*- coding: utf-8 -*-
"""
系统托盘管理模组：负责系统托盘图标和菜单管理。

功能特性:
- 系统托盘图标显示
- 托盘右键菜单
- 窗口最小化到托盘
- 托盘点击恢复窗口

错误处理策略:
- 托盘相关操作使用 try-except 捕获异常
- 所有操作记录完整的错误上下文
"""
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except Exception:
    # 除缺少依赖外，pystray 在无显示环境（headless Linux 无 DISPLAY）导入时
    # 会抛 Xlib.error.DisplayNameError，这里同样降级为“托盘不可用”。
    PYSTRAY_AVAILABLE = False
    pystray = None
    Image = None
    ImageDraw = None

IS_DARWIN = sys.platform == "darwin"

# macOS 使用 pyobjc 原生状态栏（pystray 的 darwin 后端要求在主线程创建图标，
# 与 pywebview 的 Cocoa 事件循环冲突，实测会抛
# "NSWindow should only be instantiated on the main thread!"）。
if IS_DARWIN:
    try:
        import objc
        from AppKit import (
            NSImage,
            NSMenu,
            NSMenuItem,
            NSStatusBar,
            NSVariableStatusItemLength,
        )
        from Foundation import NSObject
        from PyObjCTools import AppHelper

        MACOS_TRAY_AVAILABLE = True
    except Exception:  # pragma: no cover - 取决于运行环境是否有 pyobjc
        MACOS_TRAY_AVAILABLE = False
        objc = None
        NSImage = NSMenu = NSMenuItem = NSStatusBar = None
        NSVariableStatusItemLength = None
        NSObject = object
        AppHelper = None
else:
    MACOS_TRAY_AVAILABLE = False
    objc = None
    NSImage = NSMenu = NSMenuItem = NSStatusBar = None
    NSVariableStatusItemLength = None
    NSObject = object
    AppHelper = None

from utils.logger import get_logger

log = get_logger(__name__)

if MACOS_TRAY_AVAILABLE:

    class _MacStatusBarHandler(NSObject):
        """状态栏菜单回调：统一在 Cocoa 主线程上被调用。"""

        def init(self):
            self = objc.super(_MacStatusBarHandler, self).init()
            if self is None:
                return None
            self._callbacks = {}
            self._item_callbacks = {}
            return self

        @objc.python_method
        def bind(self, callbacks, item_callbacks):
            self._callbacks = dict(callbacks or {})
            self._item_callbacks = dict(item_callbacks or {})

        @objc.python_method
        def _dispatch(self, callback):
            if not callback:
                return

            def _run():
                try:
                    callback()
                except Exception as e:
                    log.error(f"托盘菜单回调失败: {e}")

            # NSStatusItem 的菜单动作在 Cocoa 主线程执行；而 pywebview 的
            # evaluate_js / destroy 等接口会等待主线程处理，在主线程里调用会自死锁
            # （表现为菜单点“退出”后进程卡住、系统提示.app无响应）。
            # 因此统一转交后台线程执行，与 Windows/Linux 的 pystray 行为保持一致。
            if threading.current_thread() is threading.main_thread():
                threading.Thread(
                    target=_run, name="MacTrayMenuAction", daemon=True
                ).start()
                return
            _run()

        def showWindow_(self, sender):
            self._dispatch(self._callbacks.get("show"))

        def hideWindow_(self, sender):
            self._dispatch(self._callbacks.get("hide"))

        def quitApp_(self, sender):
            self._dispatch(self._callbacks.get("quit"))

        def menuItemClicked_(self, sender):
            try:
                index = int(sender.tag())
            except Exception:
                return
            self._dispatch(self._item_callbacks.get(index))


class TrayManager:
    """
    系统托盘管理器：管理托盘图标和菜单。
    
    属性:
        _icon: pystray 图标实例
        _window: pywebview 窗口实例
        _on_show: 显示窗口回调
        _on_exit: 退出程序回调
        _menu_items: 自定义菜单项列表
    """

    def __init__(self):
        """初始化 TrayManager。"""
        self._icon: Optional[pystray.Icon] = None
        self._window = None
        self._on_show: Optional[Callable] = None
        self._on_exit: Optional[Callable] = None
        self._menu_items: list = []
        self._lock = threading.Lock()
        # macOS 原生状态栏对象
        self._status_item = None
        self._status_handler = None
        self._status_image_path: Optional[str] = None

    def is_available(self) -> bool:
        """检查托盘功能是否可用。"""
        if IS_DARWIN:
            return bool(MACOS_TRAY_AVAILABLE and Image is not None)
        return PYSTRAY_AVAILABLE

    def setup(self, window, on_show: Callable, on_exit: Callable, 
              menu_items: Optional[list] = None) -> bool:
        """
        设置托盘管理器。
        
        Args:
            window: pywebview 窗口实例
            on_show: 显示窗口回调函数
            on_exit: 退出程序回调函数
            menu_items: 可选的自定义菜单项列表
            
        Returns:
            是否设置成功
        """
        if not self.is_available():
            log.warning("pystray 不可用，托盘功能无法启用")
            return False

        with self._lock:
            self._window = window
            self._on_show = on_show
            self._on_exit = on_exit
            self._menu_items = menu_items or []

        return True

    def create_icon_image(self, width: int = 64, height: int = 64):
        """
        创建托盘图标图片。
        
        Args:
            width: 图标宽度
            height: 图标高度
            
        Returns:
            PIL Image 对象，如果 PIL 不可用则返回 None
        """
        if Image is None:
            log.warning("PIL 不可用，无法创建托盘图标")
            return None

        icon_path = Path(__file__).resolve().parent.parent / "web" / "assets" / "app_icon.ico"
        try:
            with Image.open(icon_path) as icon:
                return icon.convert("RGBA").resize((width, height), Image.LANCZOS)
        except Exception as e:
            log.warning(f"加载托盘图标失败，使用默认图标: {e}")

        if ImageDraw is None:
            return None

        # 创建一个简单的橙色圆形图标
        image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        dc = ImageDraw.Draw(image)
        
        # 绘制橙色圆形背景
        margin = 4
        dc.ellipse(
            [margin, margin, width - margin, height - margin],
            fill=(255, 153, 0, 255)  # 橙色
        )
        
        # 绘制白色字母 "A"
        try:
            from PIL import ImageFont
            font_size = int(height * 0.5)
            font = ImageFont.truetype("arial.ttf", font_size)
        except:
            font = ImageFont.load_default()
        
        # 计算文字位置使其居中
        bbox = dc.textbbox((0, 0), "A", font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) / 2
        y = (height - text_height) / 2 - 2
        
        dc.text((x, y), "A", fill=(255, 255, 255, 255), font=font)
        
        return image

    def _create_menu(self) -> pystray.Menu:
        """创建托盘菜单。"""
        menu_items = []
        
        # 显示窗口
        menu_items.append(pystray.MenuItem(
            "显示窗口",
            self._on_show_clicked,
            default=True  # 双击托盘图标触发
        ))
        
        menu_items.append(pystray.Menu.SEPARATOR)
        
        # 添加自定义菜单项
        for item in self._menu_items:
            if item.get('separator'):
                menu_items.append(pystray.Menu.SEPARATOR)
            else:
                menu_items.append(pystray.MenuItem(
                    item['text'],
                    item['callback'],
                    checked=item.get('checked'),
                    radio=item.get('radio')
                ))
        
        if self._menu_items:
            menu_items.append(pystray.Menu.SEPARATOR)
        
        # 退出程序
        menu_items.append(pystray.MenuItem(
            "退出",
            self._on_exit_clicked
        ))
        
        return pystray.Menu(*menu_items)

    def _on_show_clicked(self, icon, item):
        """处理显示窗口菜单点击。"""
        if self._on_show:
            try:
                self._on_show()
            except Exception as e:
                log.error(f"托盘显示窗口回调失败: {e}")

    def _on_exit_clicked(self, icon, item):
        """处理退出菜单点击。"""
        self.stop()
        if self._on_exit:
            try:
                self._on_exit()
            except Exception as e:
                log.error(f"托盘退出回调失败: {e}")

    def start(self) -> bool:
        """
        启动托盘图标。
        
        Returns:
            是否启动成功
        """
        if not self.is_available():
            return False

        if IS_DARWIN:
            return self._start_macos()

        with self._lock:
            if self._icon is not None:
                return True  # 已经启动

            try:
                self._icon = pystray.Icon(
                    "aimer_wt",
                    self.create_icon_image(),
                    "AimerWT - 战雷工具箱",
                    self._create_menu()
                )
                
                # 在后台线程运行托盘
                self._icon.run_detached()
                log.info("系统托盘已启动")
                return True
                
            except Exception as e:
                log.error(f"启动系统托盘失败: {e}")
                self._icon = None
                return False

    # --- macOS 原生菜单栏实现 ---

    def _start_macos(self) -> bool:
        """在 Cocoa 主线程上创建 NSFStatusItem（pystray 在 macOS 上不可用）。"""
        with self._lock:
            if self._status_item is not None:
                return True
            callbacks = {
                "show": self._macos_show_window,
                "hide": self._macos_hide_window,
                "quit": self._macos_exit_app,
            }
            menu_items = [
                dict(item) for item in (self._menu_items or []) if isinstance(item, dict)
            ]

        menu_item_callbacks = {
            index: item["callback"]
            for index, item in enumerate(menu_items)
            if callable(item.get("callback"))
        }

        result: dict = {}
        done = threading.Event()
        try:
            AppHelper.callAfter(
                self._create_macos_status_item,
                callbacks,
                menu_items,
                menu_item_callbacks,
                result,
                done,
            )
        except Exception as e:
            log.error(f"启动系统托盘失败: {e}")
            return False

        # 菜单栏对象必须在主线程创建；启动早期主事件循环可能还没跑起来，
        # 此时排队调用依然会执行，只是拿不到同步结果。
        if not done.wait(3.0):
            log.warning("[TRAY] macOS 菜单栏图标改为异步创建（主事件循环尚未就绪）")
            return True

        if result.get("ok"):
            log.info("系统托盘已启动 (macOS 菜单栏)")
            return True
        return False

    def _create_macos_status_item(
        self,
        callbacks: dict,
        menu_items: list,
        menu_item_callbacks: dict,
        result: dict,
        done: threading.Event,
    ) -> None:
        """必须是 Cocoa 主线程：创建状态栏图标与菜单。"""
        status_item = None
        try:
            status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength
            )
            icon_path = self._write_macos_icon_file()
            if icon_path:
                image = NSImage.alloc().initWithContentsOfFile_(icon_path)
                if image is not None:
                    image.setSize_((22.0, 22.0))
                    try:
                        image.setTemplate_(False)
                    except Exception:
                        pass
                    status_item.button().setImage_(image)
            try:
                status_item.button().setToolTip_("AimerWT - 战雷工具箱")
            except Exception:
                pass

            handler = _MacStatusBarHandler.alloc().init()
            handler.bind(callbacks, menu_item_callbacks)

            menu = NSMenu.alloc().init()
            try:
                menu.setAutoenablesItems_(False)
            except Exception:
                pass

            for title, action in (("显示窗口", "showWindow:"), ("隐藏窗口", "hideWindow:")):
                entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, action, ""
                )
                entry.setTarget_(handler)
                menu.addItem_(entry)

            custom_items = [item for item in menu_items if not item.get("separator")]
            if custom_items:
                menu.addItem_(NSMenuItem.separatorItem())
                for index, item in enumerate(menu_items):
                    if item.get("separator"):
                        menu.addItem_(NSMenuItem.separatorItem())
                        continue
                    entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        str(item.get("text") or ""), "menuItemClicked:", ""
                    )
                    entry.setTag_(index)
                    entry.setTarget_(handler)
                    if item.get("checked"):
                        entry.setState_(1)
                    menu.addItem_(entry)

            menu.addItem_(NSMenuItem.separatorItem())
            quit_entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "退出", "quitApp:", ""
            )
            quit_entry.setTarget_(handler)
            menu.addItem_(quit_entry)

            status_item.setMenu_(menu)

            with self._lock:
                self._status_item = status_item
                self._status_handler = handler
            result["ok"] = True
        except Exception as e:
            log.error(f"启动系统托盘失败: {e}")
            if status_item is not None:
                try:
                    NSStatusBar.systemStatusBar().removeStatusItem_(status_item)
                except Exception:
                    pass
            result["ok"] = False
        finally:
            done.set()

    def _write_macos_icon_file(self) -> Optional[str]:
        """把现有 logo 转成 22pt（44px，含 Retina）PNG 供状态栏使用。"""
        try:
            image = self.create_icon_image(44, 44)
            if image is None:
                return None
            path = Path(tempfile.gettempdir()) / "aimerwt_status_icon.png"
            image.save(path, format="PNG")
            self._status_image_path = str(path)
            return self._status_image_path
        except Exception as e:
            log.warning(f"生成 macOS 菜单栏图标失败: {e}")
            return None

    def _macos_show_window(self) -> None:
        if self._on_show:
            try:
                self._on_show()
            except Exception as e:
                log.error(f"托盘显示窗口回调失败: {e}")

    def _macos_hide_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.hide()
        except Exception as e:
            log.error(f"托盘隐藏窗口失败: {e}")

    def _macos_exit_app(self) -> None:
        self.stop()
        if self._on_exit:
            try:
                self._on_exit()
            except Exception as e:
                log.error(f"托盘退出回调失败: {e}")

    def stop(self):
        """停止托盘图标。"""
        if IS_DARWIN:
            self._stop_macos()
            return

        with self._lock:
            if self._icon is not None:
                try:
                    self._icon.stop()
                    log.info("系统托盘已停止")
                except Exception as e:
                    log.error(f"停止系统托盘失败: {e}")
                finally:
                    self._icon = None

    def _stop_macos(self) -> None:
        with self._lock:
            status_item = self._status_item
            self._status_item = None
            self._status_handler = None

        if status_item is None:
            return

        def _remove():
            try:
                NSStatusBar.systemStatusBar().removeStatusItem_(status_item)
                log.info("系统托盘已停止")
            except Exception as e:
                log.error(f"停止系统托盘失败: {e}")

        try:
            AppHelper.callAfter(_remove)
        except Exception:
            _remove()

    def notify(self, title: str, message: str, duration: int = 3):
        """
        显示托盘通知。
        
        Args:
            title: 通知标题
            message: 通知内容
            duration: 显示时长（秒）
        """
        if IS_DARWIN:
            # macOS 通知中心需要 bundle 与用户授权，这里只记录日志
            log.info(f"[TRAY] 通知: {title} - {message}")
            return

        if not self.is_available() or self._icon is None:
            return

        try:
            self._icon.notify(message, title)
        except Exception as e:
            log.error(f"显示托盘通知失败: {e}")

    def is_running(self) -> bool:
        """检查托盘是否正在运行。"""
        if IS_DARWIN:
            with self._lock:
                return self._status_item is not None
        with self._lock:
            return self._icon is not None


# 全局托盘管理器实例
tray_manager = TrayManager()
