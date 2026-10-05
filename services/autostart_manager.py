# -*- coding: utf-8 -*-
"""
开机自启动管理模组：负责 Windows（注册表）与 macOS（LaunchAgent）开机自启动设置。

功能特性:
- 设置/取消开机自启动
- 支持静默启动（只显示托盘）
- Windows: 注册表 Run 项；macOS: ~/Library/LaunchAgents/com.aimer.wt.plist

错误处理策略:
- 注册表/plist 操作使用 try-except 捕获异常
- 所有操作记录完整的错误上下文
"""
import os
import plistlib
import sys
import platform
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_DARWIN = platform.system() == "Darwin"

if IS_WINDOWS:
    import winreg

# 注册表路径
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_APP_NAME = "AimerWT"

# macOS LaunchAgent 标识
LAUNCH_AGENT_LABEL = "com.aimer.wt"


class AutostartManager:
    """
    开机自启动管理器：管理 Windows 开机自启动设置。
    
    属性:
        _app_name: 注册表中显示的应用名称
        _registry_key: 注册表键路径
    """

    def __init__(self, app_name: str = REGISTRY_APP_NAME):
        """
        初始化 AutostartManager。
        
        Args:
            app_name: 注册表中显示的应用名称
        """
        self._app_name = app_name
        self._registry_key = REGISTRY_KEY

    # --- macOS LaunchAgent 辅助 ---

    @staticmethod
    def _macos_app_bundle() -> Optional[Path]:
        """打包运行时返回所在 .app bundle 路径。"""
        if not getattr(sys, "frozen", False):
            return None
        executable = Path(sys.executable)
        for candidate in (executable, *executable.parents):
            if candidate.suffix == ".app":
                return candidate
        return None

    @staticmethod
    def _launch_agent_path() -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    def _macos_program_arguments(self, silent: bool) -> list[str] | None:
        """构造 LaunchAgent 的启动命令。"""
        bundle = self._macos_app_bundle()
        if bundle is not None:
            args = ["/usr/bin/open", "-a", str(bundle)]
            if silent:
                args.extend(["--args", "--silent"])
            return args

        main_script = Path(__file__).resolve().parent.parent / "main.py"
        if not main_script.is_file():
            return None
        args = [sys.executable, str(main_script)]
        if silent:
            args.append("--silent")
        return args

    def _macos_is_enabled(self) -> bool:
        return self._launch_agent_path().is_file()

    def _macos_enable(self, silent: bool) -> bool:
        try:
            arguments = self._macos_program_arguments(silent)
            if not arguments:
                log.error("启用开机自启动失败：找不到可执行入口")
                return False

            plist_path = self._launch_agent_path()
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": LAUNCH_AGENT_LABEL,
                "ProgramArguments": arguments,
                "RunAtLoad": True,
                "KeepAlive": False,
            }
            # 先写临时文件再替换，避免留下半截 plist
            temp_path = plist_path.with_suffix(".plist.tmp")
            with open(temp_path, "wb") as file_obj:
                plistlib.dump(payload, file_obj)
            os.replace(temp_path, plist_path)

            mode_str = "静默模式" if silent else "正常模式"
            log.info(f"已启用开机自启动 ({mode_str}): {plist_path}")
            return True
        except PermissionError as e:
            log.error(f"启用开机自启动失败（权限不足）: {e}")
            return False
        except Exception as e:
            log.error(f"启用开机自启动失败: {e}")
            return False

    def _macos_disable(self) -> bool:
        try:
            plist_path = self._launch_agent_path()
            if plist_path.exists():
                plist_path.unlink()
            log.info("已禁用开机自启动")
            return True
        except Exception as e:
            log.error(f"禁用开机自启动失败: {e}")
            return False

    def _get_executable_path(self, silent: bool = False) -> str:
        """
        获取可执行文件路径。
        
        Args:
            silent: 是否静默启动（只显示托盘）
            
        Returns:
            可执行文件完整路径，包含参数
        """
        if getattr(sys, 'frozen', False):
            # 打包后的 exe
            exe_path = sys.executable
        else:
            # 开发环境，使用 main.py
            exe_path = f'"{sys.executable}" "{Path(__file__).parent.parent / "main.py"}"'
            if silent:
                exe_path += ' --silent'
            return exe_path

        # 打包环境，添加参数
        if silent:
            return f'"{exe_path}" --silent'
        return f'"{exe_path}"'

    def is_enabled(self) -> bool:
        """
        检查开机自启动是否已启用。
        
        Returns:
            是否已启用
        """
        if IS_DARWIN:
            return self._macos_is_enabled()
        if not IS_WINDOWS:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._registry_key, 0, winreg.KEY_READ) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, self._app_name)
                    return value is not None and value != ""
                except FileNotFoundError:
                    return False
        except Exception as e:
            log.error(f"检查开机自启动状态失败: {e}")
            return False

    def enable(self, silent: bool = True) -> bool:
        """
        启用开机自启动。
        
        Args:
            silent: 是否静默启动（只显示托盘，不显示主窗口）
            
        Returns:
            是否设置成功
        """
        if IS_DARWIN:
            return self._macos_enable(silent)
        if not IS_WINDOWS:
            log.warning("非 Windows 平台，不支持注册表方式的开机自启动")
            return False
        try:
            exe_path = self._get_executable_path(silent)
            
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._registry_key, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, self._app_name, 0, winreg.REG_SZ, exe_path)
            
            mode_str = "静默模式" if silent else "正常模式"
            log.info(f"已启用开机自启动 ({mode_str})")
            return True
            
        except PermissionError as e:
            log.error(f"启用开机自启动失败（权限不足）: {e}")
            return False
        except Exception as e:
            log.error(f"启用开机自启动失败: {e}")
            return False

    def disable(self) -> bool:
        """
        禁用开机自启动。
        
        Returns:
            是否禁用成功
        """
        if IS_DARWIN:
            return self._macos_disable()
        if not IS_WINDOWS:
            return True
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._registry_key, 0, winreg.KEY_WRITE) as key:
                try:
                    winreg.DeleteValue(key, self._app_name)
                    log.info("已禁用开机自启动")
                    return True
                except FileNotFoundError:
                    # 本来就不存在
                    return True
                    
        except PermissionError as e:
            log.error(f"禁用开机自启动失败（权限不足）: {e}")
            return False
        except Exception as e:
            log.error(f"禁用开机自启动失败: {e}")
            return False

    def toggle(self, enabled: bool, silent: bool = True) -> bool:
        """
        切换开机自启动状态。
        
        Args:
            enabled: 是否启用
            silent: 是否静默启动
            
        Returns:
            操作是否成功
        """
        if enabled:
            return self.enable(silent)
        else:
            return self.disable()

    def get_current_value(self) -> Optional[str]:
        """
        获取当前注册表值。
        
        Returns:
            注册表值，如果不存在返回 None
        """
        if IS_DARWIN:
            if not self._macos_is_enabled():
                return None
            try:
                with open(self._launch_agent_path(), "rb") as file_obj:
                    payload = plistlib.load(file_obj)
                arguments = payload.get("ProgramArguments") or []
                return " ".join(str(item) for item in arguments)
            except Exception as e:
                log.error(f"读取 LaunchAgent 失败: {e}")
                return None
        if not IS_WINDOWS:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._registry_key, 0, winreg.KEY_READ) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, self._app_name)
                    return value
                except FileNotFoundError:
                    return None
        except Exception as e:
            log.error(f"获取开机自启动值失败: {e}")
            return None

    def is_silent_mode(self) -> bool:
        """
        检查当前是否是静默启动模式。
        
        Returns:
            是否是静默模式
        """
        value = self.get_current_value()
        if value is None:
            return False
        return '--silent' in value or '--tray-only' in value


# 全局自启动管理器实例
autostart_manager = AutostartManager()
