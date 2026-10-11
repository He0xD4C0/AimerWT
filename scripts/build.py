# -*- coding: utf-8 -*-
import json
import os
import runpy
import shutil
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_macos_vgmstream_runtime import (
    RuntimeBuildError,
    normalize_architecture,
    refresh_runtime_manifest_hashes,
    validate_runtime_directory,
)
from utils.logger import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXE_DISPLAY_NAME = "AimerWT V3.1 Beta"
APP_VERSION = "3.1.0"
APP_VERSION_TUPLE = (3, 1, 0, 0)


REQUIRED_BUILD_ENV_VARS = (
    "REPORT_URL",
    "TELEMETRY_CLIENT_SECRET",
    "TELEMETRY_SALT",
)

# 只供 fork 的开发 tag CI 显式开启。占位 URL 会被 telemetry_manager
# 识别为禁用遥测，不能替代任何正式发布环境的真实配置。
ALLOW_MISSING_BUILD_SECRETS_ENV = "AIMERWT_ALLOW_MISSING_BUILD_SECRETS"
DEVELOPMENT_BUILD_SECRET_DEFAULTS = {
    "REPORT_URL": "https://api.example.com/telemetry",
    "TELEMETRY_CLIENT_SECRET": "",
    "TELEMETRY_SALT": "",
}

REQUIRED_UNTRACKED_WEB_FILES = (
    "components/ResourceLibraryLoading.js",
)

MACOS_VGMSTREAM_RUNTIME_ENV = "AIMERWT_MACOS_VGMSTREAM_RUNTIME"


def calculate_checksum(file_path, algorithm='sha256'):
    """计算文件的校验和"""
    hash_func = getattr(hashlib, algorithm)()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_func.update(chunk)
    return hash_func.hexdigest()


def calculate_directory_checksum(directory, algorithm='sha256'):
    """对目录（macOS .app bundle）计算稳定校验和：按相对路径排序后逐个文件累加。"""
    hash_func = getattr(hashlib, algorithm)()
    root = Path(directory)
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: str(p.relative_to(root))):
        hash_func.update(str(path.relative_to(root)).replace(os.sep, "/").encode("utf-8"))
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hash_func.update(chunk)
    return hash_func.hexdigest()


def build_macos_icns(work_dir: Path) -> Path:
    """由 web/assets/app_icon.ico 生成 .icns（Pillow + iconutil，不新增二进制资源）。"""
    source = PROJECT_ROOT / "web" / "assets" / "app_icon.ico"
    if not source.is_file():
        raise RuntimeError(f"缺少 macOS 图标源文件: {source}")

    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - Pillow 是运行依赖
        raise RuntimeError(f"生成 .icns 需要 Pillow: {exc}") from exc

    iconset_dir = work_dir / f"{EXE_DISPLAY_NAME}.iconset"
    iconset_dir.mkdir(parents=True, exist_ok=True)
    icon_specs = (
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    )
    with Image.open(source) as image:
        base = image.convert("RGBA")
        for size, name in icon_specs:
            resized = base.resize((size, size), Image.LANCZOS)
            resized.save(iconset_dir / name, format="PNG")

    icns_path = work_dir / f"{EXE_DISPLAY_NAME}.icns"
    try:
        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("macOS 构建环境缺少 iconutil 命令") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"生成 .icns 失败: {(exc.stderr or exc.stdout or '').strip()}") from exc

    if not icns_path.is_file():
        raise RuntimeError("生成 .icns 失败：输出文件不存在")
    return icns_path


def finalize_macos_bundle(bundle_path: Path, runtime_dir: Path | None = None) -> None:
    """写入版本号并重新做 ad-hoc 签名（PyInstaller CLI 无法直接设置 CFBundleVersion）。"""
    bundle_path = Path(bundle_path)
    info_plist = bundle_path / "Contents" / "Info.plist"
    if not info_plist.is_file():
        raise RuntimeError(f"未找到 macOS Info.plist: {info_plist}")

    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        try:
            subprocess.run(
                ["/usr/bin/plutil", "-replace", key, "-string", APP_VERSION, str(info_plist)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"写入 Info.plist 失败({key}): {(exc.stderr or '').strip()}") from exc

    # Info.plist 变更会使原签名失效，需要重新签名并校验。先深度签名
    # 所有嵌套代码；其中的 Mach-O 会新增/更新 LC_CODE_SIGNATURE。
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(bundle_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"macOS 重新签名失败: {(exc.stderr or '').strip()}") from exc

    if runtime_dir is not None:
        try:
            # 嵌套代码签名改变 CLI/dylib 字节，先刷新随包第三方清单的哈希，
            # 再深度签名一次以重新封存 Frameworks 内的清单。ad-hoc 签名对
            # 已签名且未变的嵌套 Mach-O 是稳定的；随后再次校验清单哈希。
            refresh_runtime_manifest_hashes(runtime_dir)
            validate_runtime_directory(
                runtime_dir,
                expected_arch=normalize_architecture(),
                require_manifest=True,
            )
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(bundle_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            validate_runtime_directory(
                runtime_dir,
                expected_arch=normalize_architecture(),
                require_manifest=True,
            )
        except RuntimeBuildError as exc:
            raise RuntimeError(f"刷新 macOS 试听运行时清单失败: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"macOS bundle 重新签名失败: {(exc.stderr or '').strip()}") from exc

    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--all-architectures", "--deep", "--strict", str(bundle_path)],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise RuntimeError(f"macOS 签名校验失败: {(verify.stderr or verify.stdout or '').strip()}")


def resolve_macos_vgmstream_runtime() -> Path:
    """读取并预检由 macOS 专用构建器生成的自包含试听运行时。"""
    raw_runtime_dir = os.environ.get(MACOS_VGMSTREAM_RUNTIME_ENV, "").strip()
    if not raw_runtime_dir:
        raise RuntimeError(
            f"macOS 打包需要 {MACOS_VGMSTREAM_RUNTIME_ENV}。"
            "请先运行 scripts/build_macos_vgmstream_runtime.py 生成当前架构运行时。"
        )

    runtime_dir = Path(raw_runtime_dir).expanduser().resolve()
    try:
        validate_runtime_directory(
            runtime_dir,
            expected_arch=normalize_architecture(),
            require_manifest=True,
        )
    except RuntimeBuildError as exc:
        raise RuntimeError(f"macOS vgmstream 运行时无效: {exc}") from exc
    return runtime_dir


def stage_macos_tools_without_legacy_vgmstream(tools_dir: Path, destination: Path) -> Path:
    """复制原有工具，但不让旧的 Homebrew macOS CLI 进入 .app。"""
    source = Path(tools_dir).resolve()
    legacy_parent = source / "vgmstream"

    def ignore_legacy(current_dir: str, names: list[str]) -> set[str]:
        if Path(current_dir).resolve() == legacy_parent:
            return {"macos"} & set(names)
        return set()

    shutil.copytree(source, destination, ignore=ignore_legacy)
    legacy_copy = destination / "vgmstream" / "macos"
    if legacy_copy.exists():
        raise RuntimeError(f"旧 macOS vgmstream CLI 不应被打包: {legacy_copy}")
    return destination


def install_macos_vgmstream_runtime(bundle_path: Path, runtime_dir: Path) -> Path:
    """安装已验证的运行时到 app Frameworks；随后由现有流程统一 ad-hoc 签名。"""
    contents_dir = Path(bundle_path) / "Contents"
    frameworks_dir = contents_dir / "Frameworks"
    if not contents_dir.is_dir():
        raise RuntimeError(f"未找到 macOS app Contents 目录: {contents_dir}")

    destination = frameworks_dir / "AimerWTVgmstream"
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink():
            raise RuntimeError(f"不安全的 macOS 运行时目标路径: {destination}")
        shutil.rmtree(destination)
    shutil.copytree(runtime_dir, destination, copy_function=shutil.copy2)

    try:
        validate_runtime_directory(
            destination,
            expected_arch=normalize_architecture(),
            require_manifest=True,
        )
    except RuntimeBuildError as exc:
        raise RuntimeError(f"安装到 app 后的 macOS vgmstream 运行时无效: {exc}") from exc
    return destination


def clean_build_artifacts():
    """清理构建临时文件"""
    log.info("🧹 正在清理临时文件...")

    # 删除 build 文件夹
    build_dir = PROJECT_ROOT / "build"
    if build_dir.exists():
        try:
            shutil.rmtree(build_dir)
            log.info("   - 已删除 build 文件夹")
        except Exception as e:
            log.warning(f"   ! 删除 build 文件夹失败: {e}")

    # 删除 spec 文件
    for spec_name in ('WT_Aimer_Voice.spec', 'AimerWT V3 Beta.spec', 'AimerWT V3.1 Beta.spec'):
        spec_path = PROJECT_ROOT / spec_name
        if spec_path.exists():
            try:
                spec_path.unlink()
                log.info(f'   - 已删除 spec 文件: {spec_name}')
            except Exception as e:
                log.warning(f'   ! 删除 spec 文件失败: {e}')


def load_dotenv(path=".env"):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        except Exception as e:
            print(f"   ! 加载 .env 失败: {e}")


def copy_tracked_web_files(target_dir: Path) -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "web"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_files = [item for item in result.stdout.split("\0") if item]
    if not tracked_files:
        raise RuntimeError("未找到 Git 跟踪的 web 文件")

    copied = 0
    for rel_path in tracked_files:
        source = PROJECT_ROOT / rel_path
        if not source.is_file():
            continue
        web_rel_path = Path(rel_path).relative_to("web")
        destination = target_dir / web_rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    return copied


def load_secrets_from_app_secrets() -> dict[str, str]:
    """环境变量缺失时，回退读取本地已有的 app_secrets.py。"""
    secrets_file = PROJECT_ROOT / "app_secrets.py"
    if not secrets_file.is_file():
        return {}
    namespace = runpy.run_path(str(secrets_file))
    values: dict[str, str] = {}
    for key in REQUIRED_BUILD_ENV_VARS:
        value = str(namespace.get(key, "") or "").strip()
        if value:
            values[key] = value
    return values


def load_local_theme_unlock():
    """加载本地的可选主题口令模块。

    该模块和主题文件有意不进入公开仓库。公开 GitHub 构建应当产出可用的
    开源版本；只有本地存在该模块时，才要求完整校验并打包支持者主题。
    """
    unlock_service = PROJECT_ROOT / "services" / "theme_unlock" / "service.py"
    if not unlock_service.is_file():
        return None

    try:
        from services.theme_unlock.service import HIDDEN_THEME_RULES, ThemeUnlockService
    except Exception as exc:
        raise RuntimeError("无法加载本地主题口令模块") from exc
    return HIDDEN_THEME_RULES, ThemeUnlockService


def _read_theme_meta(theme_path: Path) -> dict:
    data = json.loads(theme_path.read_text(encoding="utf-8"))
    meta = data.get("meta") if isinstance(data, dict) else {}
    return meta if isinstance(meta, dict) else {}


def verify_hidden_theme_pack() -> tuple[str, ...]:
    """校验可选的本地主题包；公开构建没有它时使用 GitHub 回退模式。"""
    theme_unlock = load_local_theme_unlock()
    if theme_unlock is None:
        log.info("   - 未发现本地主题口令模块，构建公开 GitHub 版本")
        return ()

    hidden_theme_rules, theme_unlock_service = theme_unlock
    theme_dir = PROJECT_ROOT / "web" / "themes"
    filenames = tuple(hidden_theme_rules.keys())
    missing = [name for name in filenames if not (theme_dir / name).is_file()]
    if missing:
        raise RuntimeError("分发版隐藏主题缺失: " + ", ".join(missing))

    required_theme_files = ("chifeng.json", "DaiSongSong.json")
    for filename in required_theme_files:
        if filename not in hidden_theme_rules:
            raise RuntimeError(f"口令模块缺少隐藏主题: {filename}")

    chifeng_author = str(_read_theme_meta(theme_dir / "chifeng.json").get("author") or "")
    if "赤风" not in chifeng_author:
        raise RuntimeError(f"赤风主题作者异常: {chifeng_author}")
    songsong_author = str(_read_theme_meta(theme_dir / "DaiSongSong.json").get("author") or "")
    if "大松松" not in songsong_author:
        raise RuntimeError(f"大松松主题作者异常: {songsong_author}")

    class _UnlockProbe:
        def get_unlocked_themes(self):
            return []

        def set_unlocked_themes(self, unlocked):
            self.unlocked = list(unlocked)
            return True

    probe = theme_unlock_service(_UnlockProbe())
    checked = []
    for filename, rule in hidden_theme_rules.items():
        if not isinstance(rule, dict) or rule.get("admin_only"):
            continue
        code = str(rule.get("code") or "").strip()
        if not code:
            continue
        result = probe.redeem_theme_code(code)
        if result.get("theme_file") != filename or not result.get("success"):
            raise RuntimeError(f"{filename} 口令未能解锁对应主题")
        checked.append(filename)
    if not checked:
        raise RuntimeError("口令模块没有可校验的隐藏主题口令")

    invalid_result = probe.redeem_theme_code("__aimerwt_invalid_theme_code__")
    if invalid_result.get("success"):
        raise RuntimeError("无效口令不应解锁任何主题")

    log.info("   - 已校验隐藏主题口令模块: " + ", ".join(checked))
    return filenames


def _copy_untracked_build_assets(web_pack_dir: Path) -> int:
    """复制被 .gitignore 排除但分发版必需的 web 文件。"""
    themes_src = PROJECT_ROOT / "web" / "themes"
    themes_dst = web_pack_dir / "themes"
    themes_dst.mkdir(parents=True, exist_ok=True)
    hidden_theme_files = verify_hidden_theme_pack()

    copied = 0
    missing_web_files = []
    for relative_path in REQUIRED_UNTRACKED_WEB_FILES:
        source = PROJECT_ROOT / "web" / relative_path
        if not source.is_file():
            missing_web_files.append(relative_path)
            continue
        destination = web_pack_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source, destination)
            copied += 1
    if missing_web_files:
        raise RuntimeError("分发版必需文件缺失: " + ", ".join(missing_web_files))

    for filename in hidden_theme_files:
        source = themes_src / filename
        dst = themes_dst / filename
        if not dst.exists():
            shutil.copy2(source, dst)
            copied += 1
    if hidden_theme_files:
        packed_themes = sorted(path.name for path in themes_dst.glob("*.json"))
        log.info("   - 已纳入本地主题文件: " + ", ".join(packed_themes))
    return copied


def write_version_info(version_file: Path) -> None:
    version_file.write_text(
        f"""# UTF-8
# PyInstaller Windows 版本资源文件
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={APP_VERSION_TUPLE},
    prodvers={APP_VERSION_TUPLE},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'080404B0',
          [
            StringStruct(u'CompanyName', u'Aimer'),
            StringStruct(u'FileDescription', u'{EXE_DISPLAY_NAME}'),
            StringStruct(u'FileVersion', u'{APP_VERSION}'),
            StringStruct(u'InternalName', u'{EXE_DISPLAY_NAME}'),
            StringStruct(u'LegalCopyright', u'Copyright (c) 2026 Aimer. All rights reserved.'),
            StringStruct(u'OriginalFilename', u'{EXE_DISPLAY_NAME}.exe'),
            StringStruct(u'ProductName', u'{EXE_DISPLAY_NAME}'),
            StringStruct(u'ProductVersion', u'{APP_VERSION}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct(u'Translation', [0x0804, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def require_build_env() -> dict[str, str]:
    """校验生产打包所需的关键环境变量。"""
    missing = []
    values: dict[str, str] = {}
    file_secrets = load_secrets_from_app_secrets()
    for key in REQUIRED_BUILD_ENV_VARS:
        value = os.environ.get(key, "").strip() or file_secrets.get(key, "")
        if not value:
            missing.append(key)
            continue
        values[key] = value

    allow_missing = os.environ.get(ALLOW_MISSING_BUILD_SECRETS_ENV, "").strip().lower()
    if missing:
        if allow_missing not in {"1", "true", "yes"}:
            raise RuntimeError(
                "缺少必填环境变量: " + ", ".join(missing)
            )
        for key in missing:
            values[key] = DEVELOPMENT_BUILD_SECRET_DEFAULTS[key]
        log.warning(
            "开发构建跳过遥测密钥校验，发布包将禁用遥测：" + ", ".join(missing)
        )

    report_url_error = "REPORT_URL 必须是包含主机名的绝对 HTTPS URL"
    try:
        report_url = urlsplit(values["REPORT_URL"])
        report_url_port = report_url.port
        report_url_hostname = report_url.hostname
    except ValueError:
        raise RuntimeError(report_url_error) from None

    if (
        report_url.scheme != "https"
        or not report_url_hostname
        or (
            report_url_port is not None
            and not 1 <= report_url_port <= 65535
        )
    ):
        raise RuntimeError(report_url_error)

    return values


def build_exe():
    """执行打包任务"""
    log.info("🚀 开始打包程序...")

    # 确保 dist 目录存在 (PyInstaller 会自动创建，但为了保险)
    dist_dir = PROJECT_ROOT / "dist"
    dist_dir.mkdir(exist_ok=True)

    load_dotenv(PROJECT_ROOT / ".env")

    try:
        build_env = require_build_env()
    except RuntimeError as exc:
        log.error(f"[X] 打包终止: {exc}")
        sys.exit(1)

    # 在打包前，从打包环境的环境变量中读取遥测配置。
    salt = build_env["TELEMETRY_SALT"]
    url = build_env["REPORT_URL"]
    client_secret = build_env["TELEMETRY_CLIENT_SECRET"]

    # 生成临时的 app_secrets.py 供编译使用
    # 注意：该文件已被加入 .gitignore，不会被上传到 GitHub
    secrets_file = PROJECT_ROOT / "app_secrets.py"
    with open(secrets_file, "w", encoding="utf-8") as f:
        f.write("# 由 build.py 自动生成 - 不要把它提交到github\n")
        f.write(f"TELEMETRY_SALT = {repr(salt)}\n")
        f.write(f"REPORT_URL = {repr(url)}\n")
        f.write(f"TELEMETRY_CLIENT_SECRET = {repr(client_secret)}\n")

    # Os specific separator
    sep = ';' if os.name == 'nt' else ':'

    with tempfile.TemporaryDirectory(prefix="aimerwt_web_pack_") as tmp_dir:
        web_pack_dir = Path(tmp_dir) / "web"
        copied_web_files = copy_tracked_web_files(web_pack_dir)
        log.info(f"   - 已准备 web 打包文件: {copied_web_files} 个")

        # 补充复制被 .gitignore 排除但分发版必需的文件
        extra_count = _copy_untracked_build_assets(web_pack_dir)
        if extra_count:
            log.info(f"   - 已补充非 Git 跟踪文件: {extra_count} 个")
        version_file = Path(tmp_dir) / "version_info.txt"
        write_version_info(version_file)

        is_macos = sys.platform == "darwin"
        macos_runtime_dir = resolve_macos_vgmstream_runtime() if is_macos else None

        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconsole",
            # macOS 上 onefile 与 .app bundle 冲突（PyInstaller 7 直接报错），改用 onedir
            "--onedir" if is_macos else "--onefile",
            "--add-data", f"{web_pack_dir}{sep}web",
            "--name", EXE_DISPLAY_NAME,
            "--clean",
            # hidden imports：确保 pywebview 后端、pystray 等均被打包
            "--hidden-import", "PIL._imaging",
            "--hidden-import", "PIL.Image",
            "--hidden-import", "PIL.IcoImagePlugin",
            "--hidden-import", "requests",
            "--hidden-import", "certifi",
            "--hidden-import", "charset_normalizer",
            "--hidden-import", "bottle",
            "--collect-all", "webview",
            "--collect-all", "pystray",
            "main.py"
        ]

        # 支持者主题口令模块不属于公开仓库；主程序会在其缺失时走 GitHub
        # 回退服务，因此公开 CI 不能把它当成强制 hidden import。
        if load_local_theme_unlock() is not None:
            cmd.extend([
                "--hidden-import", "services.theme_unlock",
                "--hidden-import", "services.theme_unlock.service",
            ])

        if is_macos:
            cmd.extend([
                "--hidden-import", "webview.platforms.cocoa",
                "--hidden-import", "pystray._darwin",
                "--hidden-import", "objc",
                "--hidden-import", "AppKit",
                "--hidden-import", "Foundation",
                "--hidden-import", "PyObjCTools",
            ])
        else:
            cmd.extend([
                "--hidden-import", "webview.platforms.edgechromium",
                "--hidden-import", "webview.platforms.winforms",
                "--hidden-import", "webview.platforms.cef",
                "--hidden-import", "webview.platforms.gtk",
                "--hidden-import", "clr",
                "--hidden-import", "clr_loader",
                "--hidden-import", "pystray._win32",
            ])

        catalog_json = PROJECT_ROOT / "services" / "sight_vehicle_catalog.json"
        if not catalog_json.is_file():
            raise RuntimeError("缺少 services/sight_vehicle_catalog.json，无法打包炮镜车辆目录")
        cmd.extend(["--add-data", f"{catalog_json}{sep}services"])

        # 可选打包 tools 目录。macOS 的旧 vgmstream-cli 使用 Homebrew
        # 绝对依赖，因此只能由专用自包含运行时替代，不能随 tools 原样复制。
        tools_dir = PROJECT_ROOT / "tools"
        if tools_dir.is_dir():
            tools_pack_dir = tools_dir
            if is_macos:
                tools_pack_dir = stage_macos_tools_without_legacy_vgmstream(
                    tools_dir,
                    Path(tmp_dir) / "tools",
                )
            cmd.extend(["--add-data", f"{tools_pack_dir}{sep}tools"])
        else:
            log.warning("未发现 tools 目录，跳过工具文件打包")

        if os.name == 'nt':
            cmd.extend(["--icon", str(PROJECT_ROOT / "web" / "assets" / "app_icon.ico")])
            cmd.extend(["--version-file", str(version_file)])
            log.info(f"已生成版本资源文件: {version_file}")
        elif is_macos:
            icns_path = build_macos_icns(Path(tmp_dir))
            cmd.extend(["--icon", str(icns_path)])
            cmd.extend(["--osx-bundle-identifier", "com.aimer.wt"])
            log.info(f"已生成 macOS 图标: {icns_path}")
        else:
            cmd.append("--strip")

        log.info(f"执行命令: {' '.join(cmd)}")

        run_env = os.environ.copy()
        if is_macos:
            # 让 PyInstaller 校验 ad-hoc 签名，避免产出无法启动的 .app
            run_env["PYINSTALLER_VERIFY_BUNDLE_SIGNATURE"] = "1"

        try:
            # shell=False ensures arguments are passed correctly on Linux without manual escaping
            result = subprocess.run(
                cmd, cwd=PROJECT_ROOT, check=True, capture_output=True, text=True, env=run_env
            )
            if result.stdout:
                log.debug(result.stdout)
            if result.stderr:
                log.debug(result.stderr)
        except subprocess.CalledProcessError as e:
            log.error(f"[X] 打包失败！错误: {e}", exc_info=True)
            log.error("--- PyInstaller stdout ---")
            if e.stdout:
                log.error(e.stdout)
            log.error("--- PyInstaller stderr ---")
            if e.stderr:
                log.error(e.stderr)
            sys.exit(1)
        except Exception as e:
            log.exception(f"[X] 打包失败！错误: {e}")
            sys.exit(1)
        else:
            if os.name == 'nt':
                exe_name = f"{EXE_DISPLAY_NAME}.exe"
            elif is_macos:
                exe_name = f"{EXE_DISPLAY_NAME}.app"
            else:
                exe_name = EXE_DISPLAY_NAME
            exe_path = dist_dir / exe_name
            if is_macos:
                # 运行时进入 Frameworks 后再统一做现有 ad-hoc 签名，确保每个
                # dylib、CLI、清单和许可证都在最终签名覆盖范围内。
                installed_runtime = install_macos_vgmstream_runtime(exe_path, macos_runtime_dir)
                finalize_macos_bundle(exe_path, runtime_dir=installed_runtime)
                log.info("已安装自包含 macOS 试听运行时，写入版本号并完成 ad-hoc 签名校验")
            log.info("[OK] 打包成功！")
            log.info(f"输出文件: {exe_path}")
            return True


def main():
    # 1. 执行打包
    if not build_exe():
        return

    # 2. 生成校验文件
    if os.name == 'nt':
        exe_name = f"{EXE_DISPLAY_NAME}.exe"
    elif sys.platform == "darwin":
        exe_name = f"{EXE_DISPLAY_NAME}.app"
    else:
        exe_name = EXE_DISPLAY_NAME
    dist_dir = PROJECT_ROOT / "dist"
    exe_path = dist_dir / exe_name

    if not exe_path.exists():
        log.error(f"❌ 未找到生成的 exe 文件！: {exe_path}")
        return

    log.info("🔐 正在生成校验文件...")
    if exe_path.is_dir():
        checksum = calculate_directory_checksum(exe_path, 'sha256')
    else:
        checksum = calculate_checksum(exe_path, 'sha256')
    checksum_file = dist_dir / Path("checksum.txt")

    with open(checksum_file, 'w', encoding='utf-8') as f:
        f.write(f"File: {exe_path.name}\n")
        f.write(f"SHA256: {checksum}\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    log.info(f"✅ 校验文件已生成: {checksum_file}")
    log.info(f"   SHA256: {checksum}")

    # 3. 清理临时文件
    clean_build_artifacts()

    log.info("\n🎉 所有任务完成！可执行文件位于 dist 目录。")


if __name__ == "__main__":
    main()
