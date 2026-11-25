# -*- mode: python ; coding: utf-8 -*-
"""
Windows x64 命令行 EXE（--onedir 目录模式）PyInstaller 构建配置。

设计原则：
1. 放置于独立的 packaging/ 目录，避免与仓库根目录业务模块 config.py 同名冲突。
2. 动态定位当前解释器下的 rapidocr 安装目录，不写死开发机盘符或虚拟环境路径。
3. 构建前对照 rapidocr/default_models.yaml 逐一校验目标四个 ONNX 模型的 SHA-256，
   缺失或摘要不符时指出具体文件并立即中止构建。
4. 明确白名单打包所需四个模型，排除开发机缓存的其他模型（如 PP-OCRv6）及开发头文件。
5. 显式补齐延迟导入模块与 Windows 离线运行所需的原生库（含 MSVC 2015-2022 运行库）。
"""

import hashlib
import os
from pathlib import Path

import rapidocr
import yaml
from PyInstaller.utils.hooks import collect_dynamic_libs

# 基于 SPECPATH 定位仓库根目录（不依赖当前工作目录）
ROOT_DIR = Path(SPECPATH).resolve().parent
ENTRY_SCRIPT = ROOT_DIR / "main.py"

if not ENTRY_SCRIPT.is_file():
    raise SystemExit(f"[构建中止] 未找到应用入口文件: {ENTRY_SCRIPT}")

# 定位当前 Python 环境中的 rapidocr 包目录
RAPIDOCR_DIR = Path(rapidocr.__file__).resolve().parent
RAPIDOCR_CONFIG_YAML = RAPIDOCR_DIR / "config.yaml"
RAPIDOCR_MODELS_YAML = RAPIDOCR_DIR / "default_models.yaml"
RAPIDOCR_MODELS_DIR = RAPIDOCR_DIR / "models"

for required_cfg in (RAPIDOCR_CONFIG_YAML, RAPIDOCR_MODELS_YAML):
    if not required_cfg.is_file():
        raise SystemExit(f"[构建中止] 缺少 RapidOCR 必需配置文件: {required_cfg}")

# 四个目标 ONNX 模型白名单：(OCR版本, 任务类型, default_models.yaml 键名, 文件名)
REQUIRED_MODELS = (
    ("PP-OCRv4", "det", "ch_PP-OCRv4_det_mobile", "ch_PP-OCRv4_det_mobile.onnx"),
    ("PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
    ("PP-OCRv4", "rec", "ch_PP-OCRv4_rec_mobile", "ch_PP-OCRv4_rec_mobile.onnx"),
    ("PP-OCRv5", "rec", "ch_PP-OCRv5_rec_server", "ch_PP-OCRv5_rec_server.onnx"),
)


def _sha256_file(file_path: Path) -> str:
    """流式计算文件的 SHA-256 十六进制摘要。"""
    digest = hashlib.sha256()
    with file_path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _collect_and_verify_rapidocr_assets() -> list[tuple[str, str]]:
    """收集 RapidOCR 配置文件及经 SHA-256 校验通过的四个目标 ONNX 模型。"""
    with RAPIDOCR_MODELS_YAML.open("r", encoding="utf-8") as fp:
        models_manifest = yaml.safe_load(fp)

    ort_manifest = (models_manifest or {}).get("onnxruntime", {})
    assets: list[tuple[str, str]] = [
        (str(RAPIDOCR_CONFIG_YAML), "rapidocr"),
        (str(RAPIDOCR_MODELS_YAML), "rapidocr"),
    ]

    for ocr_ver, task_type, model_key, file_name in REQUIRED_MODELS:
        model_path = RAPIDOCR_MODELS_DIR / file_name
        if not model_path.is_file():
            raise SystemExit(
                f"[构建中止] 缺少必需模型文件: {model_path}\n"
                f"请先从已校验的本地模型副本补齐，或在联网构建机按锁定版本下载。"
            )

        try:
            expected_sha = str(ort_manifest[ocr_ver][task_type][model_key]["SHA256"]).strip().lower()
        except (KeyError, TypeError) as exc:
            raise SystemExit(
                f"[构建中止] default_models.yaml 中未找到模型校验条目: "
                f"onnxruntime.{ocr_ver}.{task_type}.{model_key}"
            ) from exc

        actual_sha = _sha256_file(model_path)
        if actual_sha != expected_sha:
            raise SystemExit(
                f"[构建中止] 模型文件 SHA-256 校验失败: {model_path}\n"
                f"  期望摘要: {expected_sha}\n"
                f"  实际摘要: {actual_sha}"
            )

        assets.append((str(model_path), "rapidocr/models"))

    return assets


def _collect_windows_msvc_runtime() -> list[tuple[str, str]]:
    """
    收集 Windows 离线目标机加载 ONNX Runtime / PyMuPDF / PyClipper 所需的 MSVC 运行库。

    仅收集标准非调试版本，确保无 VC++ Redistributable 的干净离线机也可正常启动推理。
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    system32 = system_root / "System32"
    msvc_dlls = (
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
    )
    collected: list[tuple[str, str]] = []
    for dll_name in msvc_dlls:
        dll_path = system32 / dll_name
        if dll_path.is_file():
            collected.append((str(dll_path), "."))
    return collected


# 1. 数据文件：RapidOCR 配置与四个白名单 ONNX 模型
datas: list[tuple[str, str]] = _collect_and_verify_rapidocr_assets()

# 2. 原生动态库：显式补齐 onnxruntime / pymupdf 动态库与 MSVC 运行库（PyInstaller 钩子会合并去重）
binaries: list[tuple[str, str]] = []
binaries += collect_dynamic_libs("onnxruntime")
binaries += collect_dynamic_libs("pymupdf")
binaries += _collect_windows_msvc_runtime()

# 3. 延迟导入与隐式导入清单
hiddenimports: list[str] = [
    # RapidOCR 通过 __getattr__ 与 importlib.import_module 延迟加载的入口
    "rapidocr.main",
    "rapidocr.inference_engine.onnxruntime",
    "rapidocr.inference_engine.onnxruntime.main",
    "rapidocr.inference_engine.onnxruntime.provider_config",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    # 项目内按需局部导入模块（双保险）
    "ocr_engine",
    "excel_exporter",
    "patches",
    "patches.no_review_layout_patch",
]

# 4. 排除测试框架与未使用的重型推理后端，避免无效体积与无关告警
excludes: list[str] = [
    "pytest",
    "_pytest",
    "torch",
    "paddle",
    "openvino",
    "tensorrt",
    "MNN",
    "pymupdf4llm",
    "pymupdf_layout",
]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(ROOT_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# 过滤 PyMuPDF 2.x 钩子附带的仅开发用头文件/静态库（mupdf-devel）及未使用的版面分析模型（layout/resources/onnx）
a.datas = [
    item
    for item in a.datas
    if "mupdf-devel" not in item[0].replace("\\", "/").lower()
    and "pymupdf/layout/resources/onnx" not in item[0].replace("\\", "/").lower()
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ocr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ocr",
)
