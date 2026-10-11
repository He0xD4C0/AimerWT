#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a self-contained macOS vgmstream runtime for the AimerWT app bundle.

The distributed macOS application must not rely on the user's Homebrew prefix.
This helper builds a pinned vgmstream CLI on the current runner architecture,
copies its complete non-system dylib closure into one directory, rewrites all
load commands to @loader_path, and writes an auditable third-party manifest.

It deliberately does not sign the result. scripts/build.py installs this
runtime into the final .app and performs the existing ad-hoc bundle signing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VGMSTREAM_REPOSITORY = "https://github.com/vgmstream/vgmstream.git"
VGMSTREAM_REF = "r2117"
VGMSTREAM_COMMIT = "71e2361042531fe767fb98300cf8c1ee95e539a0"
MINIMUM_MACOS_VERSION = "15.0"

CLI_NAME = "vgmstream-cli"
MANIFEST_NAME = "third-party-manifest.json"
LICENSES_DIR_NAME = "licenses"

# Keep this list aligned with the pinned upstream macOS workflow. The
# dependencies are installed only in macOS CI or when a developer explicitly
# passes --install-dependencies; normal application builds never call brew.
HOMEBREW_BUILD_DEPENDENCIES = (
    "autoconf",
    "automake",
    "cmake",
    "pkgconfig",
    "ffmpeg",
    "libao",
    "libvorbis",
    "mpg123",
    "libtool",
)

ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
FORBIDDEN_PREFIXES = ("/opt/homebrew/", "/usr/local/")
SYSTEM_PREFIXES = ("/System/Library/", "/usr/lib/")
LICENSE_NAME_RE = re.compile(r"(?:copying|license|licence|notice|copyright)", re.IGNORECASE)
VERSION_RE = re.compile(r"^\d+(?:\.\d+)*")


class RuntimeBuildError(RuntimeError):
    """Raised when the runtime cannot be safely made self-contained."""


@dataclass(frozen=True)
class Dependency:
    """A non-system Mach-O load command and the physical dylib it resolves to."""

    raw: str
    resolved: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: Iterable[object],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [str(part) for part in command]
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeBuildError(f"找不到构建所需命令: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeBuildError(f"命令超时: {' '.join(args)}") from exc

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeBuildError(f"命令失败 ({' '.join(args)}): {detail}")
    return result


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeBuildError("macOS vgmstream 运行时只能在 Darwin 主机上构建或校验")


def normalize_architecture(value: str | None = None) -> str:
    """Return Mach-O's architecture spelling for a matrix or host value."""

    candidate = (value or platform.machine()).strip().lower()
    normalized = ARCH_ALIASES.get(candidate)
    if not normalized:
        supported = ", ".join(sorted(ARCH_ALIASES))
        raise RuntimeBuildError(f"不支持的 macOS 架构 {candidate!r}（支持: {supported}）")
    return normalized


def _version_tuple(value: str) -> tuple[int, ...]:
    match = VERSION_RE.match(value.strip())
    if not match:
        raise RuntimeBuildError(f"无法解析 macOS 最低版本: {value!r}")
    return tuple(int(part) for part in match.group(0).split("."))


def _is_system_library(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIXES)


def _is_forbidden_homebrew_reference(name: str) -> bool:
    return name.startswith(FORBIDDEN_PREFIXES)


def _otool_dependencies(binary: Path) -> list[str]:
    result = _run(("/usr/bin/otool", "-L", binary))
    dependencies: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        dependency = line.split(" (", 1)[0].strip()
        if dependency:
            dependencies.append(dependency)
    return dependencies


def _otool_rpaths(binary: Path) -> list[str]:
    result = _run(("/usr/bin/otool", "-l", binary))
    rpaths: list[str] = []
    awaiting_path = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("cmd "):
            awaiting_path = stripped == "cmd LC_RPATH"
            continue
        if awaiting_path and stripped.startswith("path "):
            rpath = stripped[5:].split(" (offset", 1)[0].strip()
            if rpath:
                rpaths.append(rpath)
            awaiting_path = False
    return rpaths


def _minimum_macos_version(binary: Path) -> str | None:
    """Read LC_BUILD_VERSION / legacy LC_VERSION_MIN_MACOSX from Mach-O."""

    result = _run(("/usr/bin/otool", "-l", binary))
    current_command: str | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("cmd "):
            current_command = stripped[4:]
            continue
        if current_command in {"LC_BUILD_VERSION", "LC_VERSION_MIN_MACOSX"}:
            if stripped.startswith("minos "):
                return stripped[6:].strip()
            if stripped.startswith("version "):
                return stripped[8:].strip()
    return None


def mach_o_architectures(binary: Path) -> set[str]:
    result = _run(("/usr/bin/lipo", "-archs", binary))
    architectures = set(re.findall(r"\b(?:arm64|x86_64)\b", f"{result.stdout}\n{result.stderr}"))
    if not architectures:
        raise RuntimeBuildError(f"无法读取 Mach-O 架构: {binary}")
    return architectures


def _expand_dyld_token(value: str, binary: Path, executable: Path) -> Path | None:
    if value.startswith("@loader_path/"):
        return binary.parent / value.removeprefix("@loader_path/")
    if value.startswith("@executable_path/"):
        return executable.parent / value.removeprefix("@executable_path/")
    if value.startswith("/"):
        return Path(value)
    return None


def _resolve_dependency(raw: str, binary: Path, executable: Path) -> Path | None:
    """Resolve a non-system otool dependency in the source/Homebrew layout."""

    if _is_system_library(raw):
        return None

    if raw.startswith("@rpath/"):
        suffix = raw.removeprefix("@rpath/")
        candidates: list[Path] = []
        for rpath in _otool_rpaths(binary):
            resolved_rpath = _expand_dyld_token(rpath, binary, executable)
            if resolved_rpath is not None:
                candidates.append(resolved_rpath / suffix)
        # A few Homebrew kegs use a local rpath. This fallback is deliberately
        # restricted to the binary's source directory and not global DYLD paths.
        candidates.extend((binary.parent / suffix, executable.parent / suffix))
    else:
        direct = _expand_dyld_token(raw, binary, executable)
        if direct is None:
            raise RuntimeBuildError(
                f"无法解析第三方动态库引用 {raw!r}（来自 {binary}）"
            )
        candidates = [direct]

    for candidate in candidates:
        if candidate.is_file():
            resolved = candidate.resolve()
            if _is_system_library(str(resolved)):
                return None
            return resolved

    raise RuntimeBuildError(f"无法解析第三方动态库 {raw!r}（来自 {binary}）")


def _homebrew_command() -> str:
    brew = shutil.which("brew")
    if not brew:
        raise RuntimeBuildError("未找到 Homebrew（brew）；无法构建自包含 macOS 运行时")
    return brew


def _homebrew_output(brew: str, *args: str) -> str:
    return _run((brew, *args)).stdout.strip()


def _homebrew_formula_metadata(
    brew: str,
    formula: str,
    installed_version: str,
    *,
    cache: dict[str, dict],
) -> dict:
    if formula in cache:
        return cache[formula]

    result = _run((brew, "info", "--json=v2", formula))
    try:
        records = json.loads(result.stdout).get("formulae", [])
    except json.JSONDecodeError as exc:
        raise RuntimeBuildError(f"Homebrew 无法返回 {formula} 的元数据") from exc
    record = next(
        (
            item
            for item in records
            if item.get("name") == formula or item.get("full_name", "").endswith(f"/{formula}")
        ),
        None,
    )
    if not record:
        raise RuntimeBuildError(f"Homebrew 未返回 {formula} 的元数据")

    stable = (record.get("urls") or {}).get("stable") or {}
    source_commit = record.get("tap_git_head") or stable.get("revision")
    if not source_commit:
        raise RuntimeBuildError(f"无法确定 Homebrew 配方 {formula} 的源提交")

    prefix = Path(_homebrew_output(brew, "--prefix", formula)).resolve()
    if not prefix.is_dir():
        raise RuntimeBuildError(f"Homebrew 配方目录不存在: {formula} ({prefix})")

    metadata = {
        "formula": formula,
        "version": installed_version,
        "source": {
            "url": stable.get("url"),
            "revision": stable.get("revision"),
            "checksum": stable.get("checksum"),
            "formula_tap": record.get("tap"),
            "source_commit": source_commit,
            "formula_path": record.get("ruby_source_path"),
            "formula_sha256": (record.get("ruby_source_checksum") or {}).get("sha256"),
        },
        "license_spdx": record.get("license"),
        "prefix": prefix,
    }
    cache[formula] = metadata
    return metadata


def _formula_for_binary(binary: Path, cellar: Path) -> tuple[str, str]:
    try:
        parts = binary.resolve().relative_to(cellar.resolve()).parts
    except ValueError as exc:
        raise RuntimeBuildError(
            f"第三方动态库不在 Homebrew Cellar 内，拒绝封入发布包: {binary}"
        ) from exc
    if len(parts) < 3:
        raise RuntimeBuildError(f"无法从 Homebrew 路径识别配方和版本: {binary}")
    return parts[0], parts[1]


def _license_candidates(prefix: Path) -> list[Path]:
    candidates: list[Path] = []
    for parent, directories, filenames in os.walk(prefix):
        # A VCS source cache can contain a very large .git database. License
        # text belongs to the checked-out tree, never to Git internals.
        directories[:] = [directory for directory in directories if directory != ".git"]
        for filename in filenames:
            path = Path(parent) / filename
            if not LICENSE_NAME_RE.search(path.name):
                continue
            try:
                if path.stat().st_size <= 0:
                    continue
                sample = path.read_bytes()[:4096]
            except OSError:
                continue
            if b"\0" not in sample:
                candidates.append(path)
    return sorted(set(candidates), key=lambda item: str(item))


def _archive_license_texts(archive: Path) -> list[tuple[str, bytes]]:
    """Read license-like text directly from a Homebrew source archive."""

    texts: list[tuple[str, bytes]] = []
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    name = Path(member.filename).name
                    if member.is_dir() or not LICENSE_NAME_RE.search(name):
                        continue
                    data = source.read(member)
                    if data and b"\0" not in data[:4096]:
                        texts.append((name, data))
        else:
            with tarfile.open(archive, mode="r:*") as source:
                for member in source.getmembers():
                    name = Path(member.name).name
                    if not member.isfile() or not LICENSE_NAME_RE.search(name):
                        continue
                    extracted = source.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read()
                    if data and b"\0" not in data[:4096]:
                        texts.append((name, data))
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RuntimeBuildError(f"无法读取许可证源归档: {archive}") from exc
    return texts


def _fetch_homebrew_source_cache(brew: str, formula: str) -> Path:
    """Fetch formula source (not a bottle) and return Homebrew's cache path."""

    _run((brew, "fetch", "--build-from-source", "--retry", formula))
    source_cache = Path(
        _homebrew_output(brew, "--cache", "--build-from-source", formula)
    )
    if not source_cache.exists():
        raise RuntimeBuildError(
            f"Homebrew 未保存 {formula} 的源归档，无法提取许可证文本"
        )
    return source_cache


def _git_cache_license_texts(source_cache: Path) -> list[tuple[str, bytes]]:
    """Read license text from a Homebrew VCS cache, including bare clones."""

    probe = _run(
        ("git", "-C", source_cache, "rev-parse", "--is-inside-work-tree"),
        check=False,
    )
    bare_probe = _run(
        ("git", "-C", source_cache, "rev-parse", "--is-bare-repository"),
        check=False,
    )
    if probe.returncode != 0 and bare_probe.returncode != 0:
        return []

    tree = _run(
        ("git", "-C", source_cache, "ls-tree", "-r", "--name-only", "HEAD"),
        check=False,
    )
    if tree.returncode != 0:
        return []

    texts: list[tuple[str, bytes]] = []
    for relative_path in tree.stdout.splitlines():
        relative_path = relative_path.strip()
        if not relative_path or not LICENSE_NAME_RE.search(Path(relative_path).name):
            continue
        content = _run(
            ("git", "-C", source_cache, "show", f"HEAD:{relative_path}"),
            check=False,
        )
        if content.returncode != 0:
            continue
        data = content.stdout.encode("utf-8")
        if data and b"\0" not in data[:4096]:
            texts.append((Path(relative_path).name, data))
    return texts


def _copy_license_texts(
    prefix: Path,
    destination_root: Path,
    component: str,
    *,
    brew: str,
    formula: str,
) -> list[str]:
    """Copy installed or source-archive license text for one component."""

    sources = _license_candidates(prefix)
    archive_texts: list[tuple[str, bytes]] = []
    if not sources:
        source_cache = _fetch_homebrew_source_cache(brew, formula)
        if source_cache.is_dir():
            sources = _license_candidates(source_cache)
            if not sources:
                archive_texts = _git_cache_license_texts(source_cache)
        elif source_cache.is_file():
            archive_texts = _archive_license_texts(source_cache)
        else:
            raise RuntimeBuildError(f"Homebrew 源缓存类型无效: {source_cache}")

    if not sources and not archive_texts:
        raise RuntimeBuildError(
            f"无法提取 {component} 的许可证文本（安装目录和源归档均未提供）"
        )

    copied: list[str] = []
    component_dir = destination_root / component
    component_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()

    def copy_text(name: str, data: bytes | None = None, source: Path | None = None) -> None:
        if data is None and source is None:
            raise RuntimeBuildError(f"许可证复制来源无效: {component}/{name}")
        if name in used_names:
            digest = hashlib.sha256(data if data is not None else source.read_bytes()).hexdigest()[:12]
            source_name = Path(name)
            name = f"{source_name.stem}-{digest}{source_name.suffix}"
        used_names.add(name)
        target = component_dir / name
        if data is not None:
            target.write_bytes(data)
        else:
            shutil.copy2(source, target)
        copied.append(str(target.relative_to(destination_root.parent)))

    for source in sources:
        copy_text(source.name, source=source)
    for name, data in archive_texts:
        copy_text(name, data=data)
    return copied


def _copy_vgmstream_license(source_dir: Path, licenses_dir: Path) -> list[str]:
    copying = source_dir / "COPYING"
    if not copying.is_file() or copying.stat().st_size <= 0:
        raise RuntimeBuildError(f"固定 vgmstream 源码缺少 COPYING: {copying}")
    target_dir = licenses_dir / "vgmstream"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "COPYING"
    shutil.copy2(copying, target)
    return [str(target.relative_to(licenses_dir.parent))]


def _clean_runtime_env() -> dict[str, str]:
    """Use no Homebrew/DYLD state when checking the packaged CLI."""

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def _run_cli_help(cli: Path) -> None:
    result = _run((cli, "-h"), env=_clean_runtime_env(), timeout=30, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeBuildError(f"干净环境下 vgmstream-cli -h 失败: {detail}")


def _assert_minimum_macos_compatibility(binary: Path, *, is_cli: bool) -> str:
    minimum = _minimum_macos_version(binary)
    if not minimum:
        raise RuntimeBuildError(f"无法读取 {binary} 的 macOS deployment target")
    required = _version_tuple(MINIMUM_MACOS_VERSION)
    actual = _version_tuple(minimum)
    if is_cli and actual != required:
        raise RuntimeBuildError(
            f"vgmstream-cli 的 deployment target 为 {minimum}，预期为 {MINIMUM_MACOS_VERSION}"
        )
    if actual > required:
        raise RuntimeBuildError(
            f"{binary.name} 最低需要 macOS {minimum}，超过发行承诺的 {MINIMUM_MACOS_VERSION}"
        )
    return minimum


def _validate_load_commands(runtime_dir: Path, binaries: Iterable[Path]) -> None:
    runtime_dir = runtime_dir.resolve()
    for binary in binaries:
        rpaths = _otool_rpaths(binary)
        if rpaths:
            raise RuntimeBuildError(
                f"{binary.name} 仍包含 rpath，不能证明不依赖 Homebrew: {rpaths}"
            )

        for dependency in _otool_dependencies(binary):
            if _is_system_library(dependency):
                continue
            if _is_forbidden_homebrew_reference(dependency):
                raise RuntimeBuildError(f"{binary.name} 仍依赖 Homebrew 路径: {dependency}")
            if not dependency.startswith("@loader_path/"):
                raise RuntimeBuildError(
                    f"{binary.name} 存在未封装的第三方依赖: {dependency}"
                )
            resolved = (binary.parent / dependency.removeprefix("@loader_path/")).resolve()
            try:
                resolved.relative_to(runtime_dir)
            except ValueError as exc:
                raise RuntimeBuildError(
                    f"{binary.name} 的依赖逃离运行时目录: {dependency}"
                ) from exc
            if not resolved.is_file():
                raise RuntimeBuildError(
                    f"{binary.name} 的封装依赖不存在: {dependency}"
                )


def _verify_manifest(runtime_dir: Path, expected_arch: str | None) -> dict:
    manifest_path = runtime_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeBuildError(f"运行时缺少第三方清单: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeBuildError(f"第三方清单不是有效 JSON: {manifest_path}") from exc

    if manifest.get("schema_version") != 1:
        raise RuntimeBuildError("第三方清单 schema_version 不受支持")
    source = manifest.get("vgmstream_source") or {}
    if (
        source.get("repository") != VGMSTREAM_REPOSITORY
        or source.get("ref") != VGMSTREAM_REF
        or source.get("commit") != VGMSTREAM_COMMIT
    ):
        raise RuntimeBuildError("第三方清单中的 vgmstream 固定源信息不正确")
    if expected_arch and manifest.get("architecture") != expected_arch:
        raise RuntimeBuildError(
            f"运行时清单架构为 {manifest.get('architecture')!r}，预期为 {expected_arch!r}"
        )
    if manifest.get("minimum_macos_version") != MINIMUM_MACOS_VERSION:
        raise RuntimeBuildError("第三方清单未声明 macOS 15.0 最低版本")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeBuildError("第三方清单缺少 artifacts")

    expected_paths = {CLI_NAME, *(path.name for path in runtime_dir.glob("*.dylib"))}
    listed_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeBuildError("第三方清单包含无效 artifact 条目")
        relative_path = artifact.get("path")
        digest = artifact.get("sha256")
        licenses = artifact.get("license_files")
        if not isinstance(relative_path, str) or relative_path in listed_paths:
            raise RuntimeBuildError("第三方清单中 artifact 路径无效或重复")
        listed_paths.add(relative_path)
        path = runtime_dir / relative_path
        if path.parent != runtime_dir or not path.is_file():
            raise RuntimeBuildError(f"第三方清单指向不存在的 artifact: {relative_path}")
        if not isinstance(digest, str) or _sha256(path) != digest:
            raise RuntimeBuildError(f"第三方 artifact SHA-256 不匹配: {relative_path}")
        if not isinstance(licenses, list) or not licenses:
            raise RuntimeBuildError(f"第三方 artifact 缺少许可证文本: {relative_path}")
        for license_path in licenses:
            if not isinstance(license_path, str):
                raise RuntimeBuildError(f"许可证路径无效: {relative_path}")
            license_file = runtime_dir / license_path
            try:
                license_file.resolve().relative_to(runtime_dir.resolve())
            except ValueError as exc:
                raise RuntimeBuildError(f"许可证路径逃离运行时目录: {license_path}") from exc
            if not license_file.is_file() or license_file.stat().st_size <= 0:
                raise RuntimeBuildError(f"许可证文本不存在或为空: {license_path}")
        if artifact.get("kind") == "executable":
            if artifact.get("source_commit") != VGMSTREAM_COMMIT:
                raise RuntimeBuildError(f"vgmstream CLI 清单缺少固定源提交: {relative_path}")
        elif artifact.get("kind") == "dylib":
            source_info = artifact.get("source") or {}
            if not artifact.get("formula") or not artifact.get("version"):
                raise RuntimeBuildError(f"动态库清单缺少配方或版本: {relative_path}")
            if not artifact.get("source_commit") or not source_info.get("source_commit"):
                raise RuntimeBuildError(f"动态库清单缺少源提交: {relative_path}")
        else:
            raise RuntimeBuildError(f"第三方清单包含未知 artifact 类型: {relative_path}")

    if listed_paths != expected_paths:
        raise RuntimeBuildError(
            "第三方清单与运行时文件不一致: "
            f"expected={sorted(expected_paths)}, listed={sorted(listed_paths)}"
        )
    return manifest


def refresh_runtime_manifest_hashes(runtime_dir: Path) -> None:
    """Refresh artifact hashes after codesign has updated embedded Mach-O files.

    The final app signer writes an LC_CODE_SIGNATURE into every bundled CLI and
    dylib. Their SHA-256 values therefore must be refreshed between nested-code
    signing and the final outer-bundle signature.
    """

    runtime_dir = Path(runtime_dir).resolve()
    manifest_path = runtime_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeBuildError(f"运行时缺少第三方清单: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeBuildError(f"第三方清单不是有效 JSON: {manifest_path}") from exc

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeBuildError("第三方清单缺少 artifacts，无法刷新 SHA-256")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise RuntimeBuildError("第三方清单包含无效 artifact，无法刷新 SHA-256")
        artifact_path = runtime_dir / artifact["path"]
        if artifact_path.parent != runtime_dir or not artifact_path.is_file():
            raise RuntimeBuildError(
                f"第三方清单指向不存在的 artifact，无法刷新 SHA-256: {artifact['path']}"
            )
        artifact["sha256"] = _sha256(artifact_path)

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_runtime_directory(
    runtime_dir: Path,
    *,
    expected_arch: str | None = None,
    require_manifest: bool = True,
) -> dict | None:
    """Validate a staged runtime exactly as it will live inside Contents/Frameworks."""

    _require_macos()
    runtime_dir = Path(runtime_dir).resolve()
    if not runtime_dir.is_dir():
        raise RuntimeBuildError(f"macOS vgmstream 运行时目录不存在: {runtime_dir}")

    cli = runtime_dir / CLI_NAME
    if not cli.is_file():
        raise RuntimeBuildError(f"macOS vgmstream 运行时缺少 CLI: {cli}")
    if cli.is_symlink():
        raise RuntimeBuildError("macOS vgmstream CLI 不能是符号链接")
    if not os.access(cli, os.X_OK):
        raise RuntimeBuildError(f"macOS vgmstream CLI 不可执行: {cli}")

    normalized_arch = normalize_architecture(expected_arch) if expected_arch else None
    binaries = [cli, *sorted(runtime_dir.glob("*.dylib"))]
    if len(binaries) == 1:
        raise RuntimeBuildError("macOS vgmstream 运行时未收集到任何第三方 dylib")

    for binary in binaries:
        if binary.is_symlink():
            raise RuntimeBuildError(f"运行时不能包含 dylib 符号链接: {binary}")
        architectures = mach_o_architectures(binary)
        if normalized_arch and architectures != {normalized_arch}:
            raise RuntimeBuildError(
                f"{binary.name} 架构为 {sorted(architectures)}，预期仅 {normalized_arch}"
            )
        _assert_minimum_macos_compatibility(binary, is_cli=binary == cli)

    _validate_load_commands(runtime_dir, binaries)
    _run_cli_help(cli)
    return _verify_manifest(runtime_dir, normalized_arch) if require_manifest else None


def _clone_or_update_vgmstream(work_dir: Path) -> Path:
    source_dir = work_dir / "vgmstream"
    if source_dir.exists():
        if not (source_dir / ".git").is_dir():
            raise RuntimeBuildError(f"构建目录已有非 Git vgmstream 路径: {source_dir}")
        _run(("git", "-C", source_dir, "fetch", "--tags", "--force", "origin"))
    else:
        _run(("git", "clone", VGMSTREAM_REPOSITORY, source_dir))

    _run(("git", "-C", source_dir, "checkout", "--detach", VGMSTREAM_COMMIT))
    actual_commit = _run(("git", "-C", source_dir, "rev-parse", "HEAD")).stdout.strip()
    if actual_commit != VGMSTREAM_COMMIT:
        raise RuntimeBuildError(
            f"vgmstream 固定提交不匹配: expected={VGMSTREAM_COMMIT}, actual={actual_commit}"
        )
    return source_dir


def _configure_and_build_cli(
    source_dir: Path,
    build_dir: Path,
    *,
    brew: str,
    architecture: str,
) -> Path:
    brew_prefix = Path(_homebrew_output(brew, "--prefix"))
    env = os.environ.copy()
    env["LIBRARY_PATH"] = str(brew_prefix / "lib")
    env["PKG_CONFIG_PATH"] = os.pathsep.join(
        (
            str(brew_prefix / "lib" / "pkgconfig"),
            str(brew_prefix / "share" / "pkgconfig"),
            env.get("PKG_CONFIG_PATH", ""),
        )
    ).rstrip(os.pathsep)
    env["CMAKE_PREFIX_PATH"] = str(brew_prefix)
    env["MACOSX_DEPLOYMENT_TARGET"] = MINIMUM_MACOS_VERSION

    _run(
        (
            "cmake",
            "-S",
            source_dir,
            "-B",
            build_dir,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_CLI=ON",
            "-DBUILD_V123=OFF",
            "-DBUILD_AUDACIOUS:BOOL=OFF",
            f"-DCMAKE_OSX_ARCHITECTURES={architecture}",
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MINIMUM_MACOS_VERSION}",
        ),
        env=env,
    )
    _run(
        ("cmake", "--build", build_dir, "--config", "Release", "--target", "vgmstream_cli", "--parallel"),
        env=env,
    )
    cli = build_dir / "cli" / CLI_NAME
    if not cli.is_file():
        raise RuntimeBuildError(f"CMake 没有产生 vgmstream CLI: {cli}")
    return cli.resolve()


def _collect_dependency_closure(cli: Path) -> tuple[dict[Path, list[Dependency]], dict[Path, tuple[str, str]]]:
    """Return all source binaries and Homebrew ownership for each dylib."""

    brew = _homebrew_command()
    cellar = Path(_homebrew_output(brew, "--cellar")).resolve()
    if not cellar.is_dir():
        raise RuntimeBuildError(f"Homebrew Cellar 不存在: {cellar}")

    records: dict[Path, list[Dependency]] = {}
    ownership: dict[Path, tuple[str, str]] = {}
    pending: deque[Path] = deque([cli.resolve()])
    while pending:
        binary = pending.popleft().resolve()
        if binary in records:
            continue
        if not binary.is_file():
            raise RuntimeBuildError(f"动态库闭包中出现不存在的文件: {binary}")

        dependencies: list[Dependency] = []
        for raw in _otool_dependencies(binary):
            resolved = _resolve_dependency(raw, binary, cli)
            if resolved is None or resolved == binary:
                continue
            formula, version = _formula_for_binary(resolved, cellar)
            ownership[resolved] = (formula, version)
            dependencies.append(Dependency(raw=raw, resolved=resolved))
            if resolved not in records:
                pending.append(resolved)
        records[binary] = dependencies

    if len(records) <= 1:
        raise RuntimeBuildError("vgmstream CLI 未发现需要随包的非系统 dylib")
    return records, ownership


def _destination_map(records: dict[Path, list[Dependency]], cli: Path) -> dict[Path, str]:
    destinations: dict[Path, str] = {}
    used_names: dict[str, Path] = {}
    for source in records:
        name = CLI_NAME if source == cli else source.name
        other = used_names.get(name)
        if other is not None and other != source:
            raise RuntimeBuildError(
                f"动态库闭包中存在同名文件，拒绝不安全扁平化: {name} ({other}, {source})"
            )
        used_names[name] = source
        destinations[source] = name
    return destinations


def _copy_and_rewrite_closure(
    output_dir: Path,
    records: dict[Path, list[Dependency]],
    destinations: dict[Path, str],
    cli: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for source, name in destinations.items():
        target = output_dir / name
        shutil.copy2(source, target)
        if source == cli:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    for source, dependencies in records.items():
        target = output_dir / destinations[source]
        for dependency in dependencies:
            replacement = f"@loader_path/{destinations[dependency.resolved]}"
            if dependency.raw == replacement:
                continue
            _run(
                (
                    "/usr/bin/install_name_tool",
                    "-change",
                    dependency.raw,
                    replacement,
                    target,
                )
            )

        if target.suffix == ".dylib":
            _run(("/usr/bin/install_name_tool", "-id", f"@loader_path/{target.name}", target))

        # No remaining dependency needs an rpath after flattening the closure.
        # Removing all rpaths also makes an accidental Homebrew lookup impossible.
        for rpath in dict.fromkeys(_otool_rpaths(target)):
            _run(("/usr/bin/install_name_tool", "-delete_rpath", rpath, target))


def _adhoc_sign_relocated_binaries(output_dir: Path, destinations: dict[Path, str]) -> None:
    """Make the rewritten standalone Mach-O files executable on current macOS.

    Homebrew bottles are already signed.  ``install_name_tool`` changes their
    load commands, which invalidates that embedded signature; recent macOS
    versions can then kill the binary before its ``main`` function runs.  The
    final app is signed again by ``scripts/build.py``, but the runtime builder
    must be able to run its own clean-environment smoke test before that step.
    Re-sign every rewritten file ad-hoc here, after all load-command edits.
    """

    binaries = sorted(
        (output_dir / name for name in destinations.values()),
        key=lambda path: path.name,
    )
    for binary in binaries:
        _run(("/usr/bin/codesign", "--force", "--sign", "-", binary))


def _write_manifest(
    output_dir: Path,
    *,
    architecture: str,
    source_dir: Path,
    destinations: dict[Path, str],
    ownership: dict[Path, tuple[str, str]],
) -> None:
    brew = _homebrew_command()
    licenses_dir = output_dir / LICENSES_DIR_NAME
    vgmstream_licenses = _copy_vgmstream_license(source_dir, licenses_dir)

    formula_cache: dict[str, dict] = {}
    licenses_by_formula: dict[str, list[str]] = {}
    for source, (formula, version) in sorted(ownership.items(), key=lambda item: str(item[0])):
        if formula in licenses_by_formula:
            continue
        metadata = _homebrew_formula_metadata(brew, formula, version, cache=formula_cache)
        licenses_by_formula[formula] = _copy_license_texts(
            metadata["prefix"],
            licenses_dir,
            formula,
            brew=brew,
            formula=formula,
        )

    artifacts: list[dict] = [
        {
            "path": CLI_NAME,
            "kind": "executable",
            "sha256": _sha256(output_dir / CLI_NAME),
            "version": VGMSTREAM_REF,
            "source_commit": VGMSTREAM_COMMIT,
            "source": {
                "repository": VGMSTREAM_REPOSITORY,
                "ref": VGMSTREAM_REF,
                "commit": VGMSTREAM_COMMIT,
            },
            "license_files": vgmstream_licenses,
            "minimum_macos_version": _minimum_macos_version(output_dir / CLI_NAME),
        }
    ]
    for source, (formula, _version) in sorted(ownership.items(), key=lambda item: destinations[item[0]]):
        target = output_dir / destinations[source]
        metadata = formula_cache[formula]
        artifacts.append(
            {
                "path": target.name,
                "kind": "dylib",
                "sha256": _sha256(target),
                "formula": formula,
                "version": metadata["version"],
                "source_commit": metadata["source"]["source_commit"],
                "source": metadata["source"],
                "license_spdx": metadata["license_spdx"],
                "license_files": licenses_by_formula[formula],
                "minimum_macos_version": _minimum_macos_version(target),
            }
        )

    manifest = {
        "schema_version": 1,
        "architecture": architecture,
        "minimum_macos_version": MINIMUM_MACOS_VERSION,
        "vgmstream_source": {
            "repository": VGMSTREAM_REPOSITORY,
            "ref": VGMSTREAM_REF,
            "commit": VGMSTREAM_COMMIT,
        },
        "artifacts": artifacts,
    }
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_runtime(
    work_dir: Path,
    output_dir: Path,
    *,
    architecture: str | None = None,
    install_dependencies: bool = False,
) -> Path:
    """Build, relocate, document, and validate one native-architecture runtime."""

    _require_macos()
    expected_arch = normalize_architecture(architecture)
    work_dir = Path(work_dir).resolve()
    output_dir = Path(output_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeBuildError(
            f"运行时输出目录必须为空，避免混入旧 dylib: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    brew = _homebrew_command()
    if install_dependencies:
        _run((brew, "install", *HOMEBREW_BUILD_DEPENDENCIES))

    source_dir = _clone_or_update_vgmstream(work_dir)
    cli = _configure_and_build_cli(
        source_dir,
        work_dir / "build",
        brew=brew,
        architecture=expected_arch,
    )
    if mach_o_architectures(cli) != {expected_arch}:
        raise RuntimeBuildError(
            f"刚构建的 vgmstream CLI 架构不匹配: {sorted(mach_o_architectures(cli))}"
        )
    _assert_minimum_macos_compatibility(cli, is_cli=True)

    records, ownership = _collect_dependency_closure(cli)
    destinations = _destination_map(records, cli)
    _copy_and_rewrite_closure(output_dir, records, destinations, cli)
    _adhoc_sign_relocated_binaries(output_dir, destinations)
    validate_runtime_directory(output_dir, expected_arch=expected_arch, require_manifest=False)
    _write_manifest(
        output_dir,
        architecture=expected_arch,
        source_dir=source_dir,
        destinations=destinations,
        ownership=ownership,
    )
    validate_runtime_directory(output_dir, expected_arch=expected_arch, require_manifest=True)
    return output_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建固定版本、自包含的 macOS vgmstream 运行时"
    )
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--arch",
        choices=("amd64", "arm64", "x86_64"),
        help="目标架构；默认使用当前 runner 架构",
    )
    parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="通过 Homebrew 安装固定构建依赖（CI 使用）",
    )
    args = parser.parse_args(argv)
    try:
        output = build_runtime(
            args.work_dir,
            args.output_dir,
            architecture=args.arch,
            install_dependencies=args.install_dependencies,
        )
    except RuntimeBuildError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Built self-contained macOS vgmstream runtime: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
