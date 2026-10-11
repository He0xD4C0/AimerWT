#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the self-contained vgmstream runtime inside a signed macOS app."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_macos_vgmstream_runtime import (  # noqa: E402
    CLI_NAME,
    RuntimeBuildError,
    validate_runtime_directory,
)


RUNTIME_RELATIVE_PATH = Path("Contents/Frameworks/AimerWTVgmstream")
LEGACY_RUNTIME_RELATIVE_PATH = Path("Contents/Frameworks/tools/vgmstream/macos")


def verify_app(app_path: Path, architecture: str) -> None:
    app_path = Path(app_path).resolve()
    if not app_path.is_dir() or app_path.suffix != ".app":
        raise RuntimeBuildError(f"不是有效的 macOS app bundle: {app_path}")

    legacy_runtime = app_path / LEGACY_RUNTIME_RELATIVE_PATH
    if legacy_runtime.exists():
        raise RuntimeBuildError(
            f"app 不应包含旧 tools/vgmstream/macos 副本: {legacy_runtime}"
        )

    runtime_dir = app_path / RUNTIME_RELATIVE_PATH
    validate_runtime_directory(runtime_dir, expected_arch=architecture, require_manifest=True)

    try:
        result = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--all-architectures",
                "--deep",
                "--strict",
                str(app_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeBuildError("未找到 codesign；该校验必须在 macOS 上运行") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeBuildError(f"app 严格签名校验失败: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="验证 AimerWT macOS app 内的自包含 vgmstream 运行时"
    )
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64", "x86_64"))
    args = parser.parse_args(argv)

    try:
        verify_app(args.app, args.arch)
    except RuntimeBuildError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Verified macOS vgmstream app runtime: {args.app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
