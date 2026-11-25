"""
Windows EXE（dist/ocr/ocr.exe）产物校验与命令行对照验收测试。

覆盖范围：
- 产物目录结构、RapidOCR 配置文件、四个目标 ONNX 模型白名单与 SHA-256 校验
- 原生运行库核查（ONNX Runtime、OpenCV、PyMuPDF、Shapely、NumPy、PyClipper、SQLite、MSVC 运行库）
- 打包计划第四节第 01～08 项：源码入口（uv run main.py）与成品 EXE 的全量命令行对照验收

运行方式：
    uv run --with pytest pytest tests/test_exe_packaging.py -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import openpyxl
import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIST_DIR = ROOT_DIR / "dist" / "ocr"
TEMPLATE_SET1_DIR = ROOT_DIR / "template" / "set1"

# 四个必需 ONNX 模型清单：(OCR版本, 任务类型, default_models.yaml 键名, 文件名, 预期字节大小)
EXPECTED_MODELS: Tuple[Tuple[str, str, str, str, int], ...] = (
    ("PP-OCRv4", "det", "ch_PP-OCRv4_det_mobile", "ch_PP-OCRv4_det_mobile.onnx", 4_745_517),
    ("PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile", "ch_ppocr_mobile_v2.0_cls_mobile.onnx", 585_532),
    ("PP-OCRv4", "rec", "ch_PP-OCRv4_rec_mobile", "ch_PP-OCRv4_rec_mobile.onnx", 10_857_958),
    ("PP-OCRv5", "rec", "ch_PP-OCRv5_rec_server", "ch_PP-OCRv5_rec_server.onnx", 84_577_022),
)

# 固定对照样本（来自仓库既有样本，包含单页 PDF 与清晰图片）
SAMPLE_IMAGE_1 = TEMPLATE_SET1_DIR / "模板1.png"
SAMPLE_IMAGE_2 = TEMPLATE_SET1_DIR / "模板2.jpg"
SAMPLE_PDF_1 = TEMPLATE_SET1_DIR / "A44130044321008304苏志冲登高证.pdf"


@dataclass(frozen=True)
class CmdResult:
    """子进程执行结果。"""

    returncode: int
    stdout: str
    stderr: str


def _resolve_dist_dir() -> Path:
    """定位待验收的 dist/ocr 目录（支持通过 OCR_DIST_DIR 环境变量覆盖）。"""
    env_dir = os.environ.get("OCR_DIST_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return DEFAULT_DIST_DIR


def _sha256_file(path: Path) -> str:
    """流式计算文件的 SHA-256 十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _decode_output(raw: bytes) -> str:
    """优先按 UTF-8 解码，兼容 Windows 本地代码页（GBK/cp936）。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _run_cmd(args: List[str], cwd: Path, timeout: int = 180) -> CmdResult:
    """在指定工作目录运行子进程并采集返回码与标准流。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    return CmdResult(
        returncode=completed.returncode,
        stdout=_decode_output(completed.stdout),
        stderr=_decode_output(completed.stderr),
    )


def _run_source(cli_args: List[str], cwd: Path) -> CmdResult:
    """调用源码入口：uv run --no-sync --project <ROOT> <ROOT>/main.py ..."""
    cmd = [
        "uv",
        "run",
        "--no-sync",
        "--project",
        str(ROOT_DIR),
        str(ROOT_DIR / "main.py"),
        *cli_args,
    ]
    return _run_cmd(cmd, cwd=cwd)


def _run_exe(exe_path: Path, cli_args: List[str], cwd: Path) -> CmdResult:
    """调用打包后的 ocr.exe。"""
    return _run_cmd([str(exe_path), *cli_args], cwd=cwd)


def _snapshot_db(db_path: Path) -> Dict[str, dict]:
    """
    提取数据库中按文件名归集的业务数据快照（剥离自动生成的 UUID 与时间戳），
    便于逐文件比对源码与 EXE 的识别一致性。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT file_id, file_name, status, raw_text, error_message
            FROM files
            ORDER BY rowid ASC
            """
        ).fetchall()

        fields_map: Dict[str, Dict[str, Optional[str]]] = {}
        for fid, fkey, fval in conn.execute(
            "SELECT file_id, field_key, field_value FROM extracted_fields ORDER BY id ASC"
        ):
            fields_map.setdefault(fid, {})[fkey] = fval

        cert_map: Dict[str, Tuple[str, str]] = {}
        for fid, cno, wcat in conn.execute(
            "SELECT file_id, cert_no, work_category FROM certificate_files"
        ):
            cert_map[fid] = (cno, wcat)

        result: Dict[str, dict] = {}
        for fid, fname, status, raw_text, err_msg in rows:
            result[fname] = {
                "file_id": fid,
                "status": status,
                "raw_text": raw_text,
                "error_message": err_msg,
                "fields": fields_map.get(fid, {}),
                "certificate_key": cert_map.get(fid),
            }
        return result
    finally:
        conn.close()


def _extract_stats_lines(stdout: str) -> List[str]:
    """提取终端输出中的统计段落，忽略入口名和自动生成的 file_id。"""
    keep_prefixes = ("  扫描总数:", "  成功:", "  跳过:", "  不支持:", "  失败:")
    return [line.rstrip() for line in stdout.splitlines() if line.startswith(keep_prefixes)]


def _read_xlsx_snapshot(xlsx_path: Path) -> Tuple[str, List[List[Tuple[object, str, str]]]]:
    """读取工作簿名称及所有单元格的 (value, data_type, number_format) 快照。"""
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=False)
    try:
        ws = wb.active
        sheet_data: List[List[Tuple[object, str, str]]] = []
        for row in ws.iter_rows(values_only=False):
            sheet_data.append([(cell.value, cell.data_type, cell.number_format) for cell in row])
        return ws.title, sheet_data
    finally:
        wb.close()


def _copy_samples(dst_dir: Path, files: Iterable[Path]) -> None:
    """将指定样本文件复制到临时测试目录。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        assert src.is_file(), f"缺少固定测试样本: {src}"
        shutil.copy2(src, dst_dir / src.name)


@pytest.fixture(scope="session")
def dist_dir() -> Path:
    d = _resolve_dist_dir()
    assert d.is_dir(), f"未找到候选发布目录: {d}，请先运行 PyInstaller 构建。"
    return d


@pytest.fixture(scope="session")
def exe_path(dist_dir: Path) -> Path:
    exe = dist_dir / "ocr.exe"
    assert exe.is_file(), f"未找到可执行文件: {exe}"
    return exe


@pytest.fixture(scope="session")
def baseline_fast_databases(
    tmp_path_factory: pytest.TempPathFactory, exe_path: Path
) -> Dict[str, Path]:
    """
    第 03 项基准运行（session 级复用，避免后续导出测试重复执行 OCR）。
    在隔离目录中分别运行源码与 EXE 的 --fast 默认库识别，供第 03 / 06 项核验。
    """
    base = tmp_path_factory.mktemp("baseline_fast")
    sample_dir = base / "samples"
    _copy_samples(sample_dir, (SAMPLE_IMAGE_1, SAMPLE_PDF_1))

    src_cwd = base / "src_cwd"
    exe_cwd = base / "exe_cwd"
    src_cwd.mkdir()
    exe_cwd.mkdir()

    src_res = _run_source(["--input", str(sample_dir), "--fast"], cwd=src_cwd)
    exe_res = _run_exe(exe_path, ["--input", str(sample_dir), "--fast"], cwd=exe_cwd)

    assert src_res.returncode == 0, f"源码快速识别失败:\n{src_res.stdout}\n{src_res.stderr}"
    assert exe_res.returncode == 0, f"EXE 快速识别失败:\n{exe_res.stdout}\n{exe_res.stderr}"

    src_db = src_cwd / "data" / "ocr_fast.db"
    exe_db = exe_cwd / "data" / "ocr_fast.db"
    assert src_db.is_file(), "源码未生成默认快速库 ./data/ocr_fast.db"
    assert exe_db.is_file(), "EXE 未生成默认快速库 ./data/ocr_fast.db"

    return {
        "sample_dir": sample_dir,
        "src_db": src_db,
        "exe_db": exe_db,
        "src_stdout": src_res.stdout,
        "exe_stdout": exe_res.stdout,
    }


def test_00_artifact_structure_models_and_native_libs(dist_dir: Path, exe_path: Path) -> None:
    """核对发布目录结构、四个模型白名单与 SHA-256、原生运行库及无多余开发资源。"""
    internal = dist_dir / "_internal"
    assert internal.is_dir(), "缺少 _internal 目录"

    # 1. RapidOCR 配置文件
    rapidocr_dir = internal / "rapidocr"
    cfg_yaml = rapidocr_dir / "config.yaml"
    models_yaml = rapidocr_dir / "default_models.yaml"
    models_dir = rapidocr_dir / "models"
    assert cfg_yaml.is_file(), f"缺少 {cfg_yaml}"
    assert models_yaml.is_file(), f"缺少 {models_yaml}"
    assert models_dir.is_dir(), f"缺少 {models_dir}"

    # 2. 精确核对四个模型白名单（不得混入 PP-OCRv6 等多余模型）及 SHA-256
    manifest = yaml.safe_load(models_yaml.read_text(encoding="utf-8"))
    ort_manifest = manifest["onnxruntime"]

    actual_onnx_files = sorted(p.name for p in models_dir.glob("*.onnx"))
    expected_onnx_files = sorted(item[3] for item in EXPECTED_MODELS)
    assert actual_onnx_files == expected_onnx_files, (
        f"模型白名单不匹配！实际: {actual_onnx_files}，期望: {expected_onnx_files}"
    )

    for ocr_ver, task_type, model_key, file_name, expected_size in EXPECTED_MODELS:
        model_file = models_dir / file_name
        assert model_file.stat().st_size == expected_size, (
            f"模型大小不符: {file_name} 实际={model_file.stat().st_size}, 期望={expected_size}"
        )
        expected_sha = str(ort_manifest[ocr_ver][task_type][model_key]["SHA256"]).strip().lower()
        actual_sha = _sha256_file(model_file)
        assert actual_sha == expected_sha, (
            f"模型 SHA-256 不符: {file_name} 实际={actual_sha}, 期望={expected_sha}"
        )

    # 3. 核心原生运行库与数据文件检查
    required_exact = [
        internal / "onnxruntime" / "capi" / "onnxruntime.dll",
        internal / "onnxruntime" / "capi" / "onnxruntime_providers_shared.dll",
        internal / "onnxruntime" / "capi" / "onnxruntime_pybind11_state.pyd",
        internal / "cv2" / "config.py",
        internal / "cv2" / "config-3.py",
        internal / "pymupdf" / "mupdfcpp64.dll",
        internal / "pymupdf" / "_mupdf.pyd",
        internal / "pymupdf" / "_extra.pyd",
        internal / "sqlite3.dll",
        internal / "_sqlite3.pyd",
        internal / "vcruntime140.dll",
        internal / "vcruntime140_1.dll",
        internal / "msvcp140.dll",
        internal / "msvcp140_1.dll",
    ]
    missing = [str(p.relative_to(dist_dir)) for p in required_exact if not p.is_file()]
    assert not missing, f"发布包缺少关键原生库或配置文件: {missing}"

    # 4. 通配核对 OpenCV 扩展、Shapely GEOS、NumPy OpenBLAS、PyClipper
    assert list((internal / "cv2").glob("cv2*.pyd")), "缺少 cv2*.pyd"
    assert list((internal / "pyclipper").glob("_pyclipper*.pyd")), "缺少 _pyclipper*.pyd"

    shapely_libs = next(
        (p for p in internal.iterdir() if p.is_dir() and p.name.lower() == "shapely.libs"),
        None,
    )
    assert shapely_libs is not None and list(shapely_libs.glob("geos_c*.dll")), (
        "缺少 shapely.libs/geos_c*.dll"
    )
    numpy_libs = next(
        (p for p in internal.iterdir() if p.is_dir() and p.name.lower() == "numpy.libs"),
        None,
    )
    assert numpy_libs is not None and list(numpy_libs.glob("*openblas*.dll")), (
        "缺少 numpy.libs/*openblas*.dll"
    )

    # 5. 确保不含开发/业务敏感内容、mupdf-devel 及 PyMuPDF 冗余 ONNX 模型
    assert not (internal / "pymupdf" / "mupdf-devel").exists(), "不应打包 pymupdf/mupdf-devel"
    assert not list((internal / "pymupdf").rglob("*.onnx")), "不应打包 PyMuPDF 未使用的版面 ONNX 模型"
    for forbidden in ("data", "template", ".venv", "debug", "tests"):
        assert not (dist_dir / forbidden).exists(), f"发布包不应包含目录: {forbidden}"


def test_01_help_and_invalid_cli_args(exe_path: Path, tmp_path: Path) -> None:
    """第 01 项：-h / --help 返回 0 且显示 ocr.exe；四种错误调用返回 2 且有 stderr。"""
    for flag in ("-h", "--help"):
        src_res = _run_source([flag], cwd=tmp_path)
        exe_res = _run_exe(exe_path, [flag], cwd=tmp_path)
        assert src_res.returncode == 0
        assert exe_res.returncode == 0
        first_line = exe_res.stdout.splitlines()[0]
        assert "ocr.exe" in first_line, f"帮助首行未显示 ocr.exe: {first_line}"
        for opt in ("-export", "--export", "--input", "--db", "--fast"):
            assert opt in src_res.stdout
            assert opt in exe_res.stdout

    invalid_cases = [
        [],
        ["--input", "   "],
        ["--input"],
        ["--unknown-option-xyz"],
    ]
    for case in invalid_cases:
        src_res = _run_source(case, cwd=tmp_path)
        exe_res = _run_exe(exe_path, case, cwd=tmp_path)
        assert src_res.returncode == 2, f"源码用例 {case} 期望退出码 2，实际 {src_res.returncode}"
        assert exe_res.returncode == 2, f"EXE 用例 {case} 期望退出码 2，实际 {exe_res.returncode}"
        assert src_res.stderr.strip(), f"源码用例 {case} 缺少错误输出"
        assert exe_res.stderr.strip(), f"EXE 用例 {case} 缺少错误输出"


def test_02_production_empty_dir_and_v5_server_session(
    dist_dir: Path, exe_path: Path, tmp_path: Path
) -> None:
    """第 02 项：省略 --fast 以空目录执行（默认库/指定库），并单独验证成品 V5 Server 模型可加载。"""
    empty_dir = tmp_path / "empty_in"
    empty_dir.mkdir()

    # 2.1 默认正式库
    cwd_default = tmp_path / "cwd_default"
    cwd_default.mkdir()
    res_default = _run_exe(exe_path, ["--input", str(empty_dir)], cwd=cwd_default)
    assert res_default.returncode == 0, res_default.stderr
    assert "正式高精度 (PP-OCRv5 Server)" in res_default.stdout
    assert "发现 0 个候选文件" in res_default.stdout
    assert (cwd_default / "data" / "ocr.db").is_file()

    # 2.2 指定数据库路径
    cwd_custom = tmp_path / "cwd_custom"
    cwd_custom.mkdir()
    custom_db = cwd_custom / "custom_dir" / "prod_empty.db"
    res_custom = _run_exe(
        exe_path,
        ["--input", str(empty_dir), "--db", str(custom_db)],
        cwd=cwd_custom,
    )
    assert res_custom.returncode == 0, res_custom.stderr
    assert "正式高精度 (PP-OCRv5 Server)" in res_custom.stdout
    assert "发现 0 个候选文件" in res_custom.stdout
    assert custom_db.is_file()

    # 2.3 使用锁定版本 ONNX Runtime 直接对成品包中的 V5 Server 模型建立 CPU 推理会话
    import onnxruntime as ort

    v5_model = dist_dir / "_internal" / "rapidocr" / "models" / "ch_PP-OCRv5_rec_server.onnx"
    assert v5_model.is_file()
    sess = ort.InferenceSession(str(v5_model), providers=["CPUExecutionProvider"])
    assert len(sess.get_inputs()) >= 1
    assert len(sess.get_outputs()) >= 1
    meta = sess.get_modelmeta().custom_metadata_map
    assert "character" in meta and len(meta["character"].splitlines()) > 10_000


def test_03_fast_recognition_source_vs_exe_parity(
    baseline_fast_databases: Dict[str, Path], exe_path: Path, tmp_path: Path
) -> None:
    """第 03 项：固定样本在默认快速库与独立自定义库下，源码与 EXE 逐文件结果完全一致。"""
    # 3.1 比对默认快速库结果
    src_snap = _snapshot_db(baseline_fast_databases["src_db"])
    exe_snap = _snapshot_db(baseline_fast_databases["exe_db"])

    assert set(src_snap.keys()) == {SAMPLE_IMAGE_1.name, SAMPLE_PDF_1.name}
    assert set(exe_snap.keys()) == set(src_snap.keys())

    for fname in src_snap:
        assert src_snap[fname]["status"] == "success"
        assert exe_snap[fname]["status"] == "success"
        assert len(exe_snap[fname]["fields"]) == 9
        assert exe_snap[fname]["fields"] == src_snap[fname]["fields"], f"{fname} 字段提取不一致"
        assert exe_snap[fname]["raw_text"] == src_snap[fname]["raw_text"], f"{fname} OCR 原文不一致"
        assert exe_snap[fname]["certificate_key"] == src_snap[fname]["certificate_key"]

    assert _extract_stats_lines(baseline_fast_databases["exe_stdout"]) == _extract_stats_lines(
        baseline_fast_databases["src_stdout"]
    )

    # 3.2 以独立自定义库再运行一次
    custom_db = tmp_path / "custom_fast" / "my_fast.db"
    res = _run_exe(
        exe_path,
        ["--input", str(baseline_fast_databases["sample_dir"]), "--fast", "--db", str(custom_db)],
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert custom_db.is_file()
    custom_snap = _snapshot_db(custom_db)
    for fname in src_snap:
        assert custom_snap[fname]["status"] == "success"
        assert custom_snap[fname]["fields"] == src_snap[fname]["fields"]
        assert custom_snap[fname]["raw_text"] == src_snap[fname]["raw_text"]


def test_04_chinese_space_cwd_and_idempotent_skip(exe_path: Path, tmp_path: Path) -> None:
    """第 04 项：含中文与空格的 CWD 交叉相对/绝对路径、--db=路径与不同参数顺序，子目录不计入且复跑幂等跳过。"""
    work_dir = tmp_path / "中文 工作目录 04"
    input_dir = work_dir / "待识别 样本"
    nested_dir = input_dir / "嵌套子目录"
    _copy_samples(input_dir, (SAMPLE_IMAGE_2,))
    _copy_samples(nested_dir, (SAMPLE_IMAGE_1,))

    # 第一次：参数打乱顺序、相对 --input、--db=相对路径（含中文与空格）
    rel_db = "结果 库/识别 记录.db"
    res1 = _run_exe(
        exe_path,
        ["--fast", f"--db={rel_db}", "--input", "待识别 样本"],
        cwd=work_dir,
    )
    assert res1.returncode == 0, f"{res1.stdout}\n{res1.stderr}"
    db_file = work_dir / rel_db
    assert db_file.is_file(), f"未按调用者工作目录创建数据库: {db_file}"

    snap1 = _snapshot_db(db_file)
    assert list(snap1.keys()) == [SAMPLE_IMAGE_2.name], "子目录样本不应被计入扫描范围"
    assert snap1[SAMPLE_IMAGE_2.name]["status"] == "success"
    first_file_id = snap1[SAMPLE_IMAGE_2.name]["file_id"]
    assert "  扫描总数: 1" in res1.stdout
    assert "  成功: 1" in res1.stdout

    # 第二次：相同库与相同样本，改为绝对路径与另一参数顺序，确认跳过且 file_id 不变
    res2 = _run_exe(
        exe_path,
        ["--input", str(input_dir), "--db", str(db_file), "--fast"],
        cwd=work_dir,
    )
    assert res2.returncode == 0, f"{res2.stdout}\n{res2.stderr}"
    assert "  扫描总数: 1" in res2.stdout
    assert "  跳过: 1" in res2.stdout
    assert "  成功: 0" in res2.stdout

    snap2 = _snapshot_db(db_file)
    assert len(snap2) == 1
    assert snap2[SAMPLE_IMAGE_2.name]["file_id"] == first_file_id


def test_05_fast_mode_prod_db_guard(exe_path: Path, tmp_path: Path) -> None:
    r"""第 05 项：--fast 显式指定 .\data\ocr.db 及其等价绝对路径均被拒绝（返回 1 且正式库未被写入）。"""
    sample_dir = tmp_path / "samples"
    _copy_samples(sample_dir, (SAMPLE_IMAGE_2,))

    cwd = tmp_path / "guard_cwd"
    cwd.mkdir()
    prod_db_abs = (cwd / "data" / "ocr.db").resolve()

    for db_arg in (r".\data\ocr.db", str(prod_db_abs)):
        src_res = _run_source(["--input", str(sample_dir), "--fast", "--db", db_arg], cwd=cwd)
        exe_res = _run_exe(exe_path, ["--input", str(sample_dir), "--fast", "--db", db_arg], cwd=cwd)
        assert src_res.returncode == 1
        assert exe_res.returncode == 1
        assert "禁止写入正式默认数据库" in exe_res.stderr
        assert not prod_db_abs.exists(), f"正式默认库不应被写入: {prod_db_abs}"


def test_06_export_parity_readonly_and_ocr_independence(
    baseline_fast_databases: Dict[str, Path],
    dist_dir: Path,
    exe_path: Path,
    tmp_path: Path,
) -> None:
    """第 06 项：-export / --export 互换、表头/行数/单元格源码一致、源库只读不变、移开模型独立导出。"""
    # 准备正式默认库位置与快速库位置（复用真实识别生成的数据库内容）
    src_cwd = tmp_path / "export_src"
    exe_cwd = tmp_path / "export_exe"
    for d in (src_cwd / "data", exe_cwd / "data"):
        d.mkdir(parents=True)

    src_prod_db = src_cwd / "data" / "ocr.db"
    exe_prod_db = exe_cwd / "data" / "ocr.db"
    src_fast_db = src_cwd / "data" / "ocr_fast.db"
    exe_fast_db = exe_cwd / "data" / "ocr_fast.db"

    shutil.copy2(baseline_fast_databases["src_db"], src_prod_db)
    shutil.copy2(baseline_fast_databases["exe_db"], exe_prod_db)
    shutil.copy2(baseline_fast_databases["src_db"], src_fast_db)
    shutil.copy2(baseline_fast_databases["exe_db"], exe_fast_db)

    prod_sha_before = _sha256_file(exe_prod_db)
    fast_sha_before = _sha256_file(exe_fast_db)

    # 6.1 正式默认库：-export 与 --export 交换验证
    for flag in ("-export", "--export"):
        s_res = _run_source([flag], cwd=src_cwd)
        e_res = _run_exe(exe_path, [flag], cwd=exe_cwd)
        assert s_res.returncode == 0, s_res.stderr
        assert e_res.returncode == 0, e_res.stderr
        assert "共 2 条记录" in e_res.stdout
        assert str((exe_cwd / "data" / "ocr.xlsx").resolve()) in e_res.stdout

        src_sheet, src_cells = _read_xlsx_snapshot(src_cwd / "data" / "ocr.xlsx")
        exe_sheet, exe_cells = _read_xlsx_snapshot(exe_cwd / "data" / "ocr.xlsx")
        assert exe_sheet == src_sheet == "识别结果"
        assert len(exe_cells[0]) == 13
        assert exe_cells == src_cells

    # 6.2 快速库：--export --db 与 -export --db 交换验证
    for flag in ("--export", "-export"):
        s_res = _run_source([flag, "--db", r".\data\ocr_fast.db"], cwd=src_cwd)
        e_res = _run_exe(exe_path, [flag, "--db", r".\data\ocr_fast.db"], cwd=exe_cwd)
        assert s_res.returncode == 0, s_res.stderr
        assert e_res.returncode == 0, e_res.stderr
        assert "共 2 条记录" in e_res.stdout
        assert str((exe_cwd / "data" / "ocr_fast.xlsx").resolve()) in e_res.stdout

        src_sheet, src_cells = _read_xlsx_snapshot(src_cwd / "data" / "ocr_fast.xlsx")
        exe_sheet, exe_cells = _read_xlsx_snapshot(exe_cwd / "data" / "ocr_fast.xlsx")
        assert exe_sheet == src_sheet
        assert exe_cells == src_cells

    # 6.3 源库字节零修改（只读验证）
    assert _sha256_file(exe_prod_db) == prod_sha_before, "导出后正式库内容被修改！"
    assert _sha256_file(exe_fast_db) == fast_sha_before, "导出后快速库内容被修改！"

    # 6.4 移开模型文件的临时发布包副本复测导出，确认导出完全独立于 OCR
    no_model_dist = tmp_path / "dist_without_models"
    shutil.copytree(
        dist_dir,
        no_model_dist,
        ignore=shutil.ignore_patterns("*.onnx"),
    )
    assert not list((no_model_dist / "_internal" / "rapidocr" / "models").glob("*.onnx"))

    no_model_cwd = tmp_path / "no_model_export_cwd"
    (no_model_cwd / "data").mkdir(parents=True)
    shutil.copy2(exe_fast_db, no_model_cwd / "data" / "ocr_fast.db")

    nm_res = _run_exe(
        no_model_dist / "ocr.exe",
        ["-export", "--db", r".\data\ocr_fast.db"],
        cwd=no_model_cwd,
    )
    assert nm_res.returncode == 0, f"移除模型后导出失败:\n{nm_res.stdout}\n{nm_res.stderr}"
    nm_xlsx = no_model_cwd / "data" / "ocr_fast.xlsx"
    assert nm_xlsx.is_file()
    _, nm_cells = _read_xlsx_snapshot(nm_xlsx)
    _, ref_cells = _read_xlsx_snapshot(exe_cwd / "data" / "ocr_fast.xlsx")
    assert nm_cells == ref_cells


def test_07_export_and_input_error_cases(exe_path: Path, tmp_path: Path) -> None:
    """第 07 项：四种非法组合/缺路径均返回 1 且 stderr 含原因，导出缺库不新建库或工作簿。"""
    existing_dir = tmp_path / "exist_dir"
    existing_dir.mkdir()
    missing_db = tmp_path / "missing_dir" / "not_exist.db"
    missing_dir = tmp_path / "not_exist_input_dir"

    error_cases = [
        (["-export", "--fast"], "不接受"),
        (["--export", "--input", str(existing_dir)], "不接受"),
        (["-export", "--db", str(missing_db)], "不存在"),
        (["--input", str(missing_dir), "--fast"], "扫描目录失败"),
    ]
    for cli_args, expected_keyword in error_cases:
        src_cwd = tmp_path / f"src_{abs(hash(tuple(cli_args)))}"
        exe_cwd = tmp_path / f"exe_{abs(hash(tuple(cli_args)))}"
        src_cwd.mkdir(exist_ok=True)
        exe_cwd.mkdir(exist_ok=True)

        src_res = _run_source(cli_args, cwd=src_cwd)
        exe_res = _run_exe(exe_path, cli_args, cwd=exe_cwd)
        assert src_res.returncode == 1, f"源码用例 {cli_args} 期望 1，实际 {src_res.returncode}"
        assert exe_res.returncode == 1, f"EXE 用例 {cli_args} 期望 1，实际 {exe_res.returncode}"
        assert expected_keyword in exe_res.stderr, (
            f"EXE 用例 {cli_args} stderr 缺少关键词 '{expected_keyword}': {exe_res.stderr}"
        )

    assert not missing_db.exists(), "导出不存在的库时不应新建数据库文件"
    assert not missing_db.with_suffix(".xlsx").exists(), "导出不存在的库时不应新建工作簿"


def test_08_mixed_valid_and_corrupt_image_batch(exe_path: Path, tmp_path: Path) -> None:
    """第 08 项：有效样本 + 损坏图片混合批次返回 0，有效样本成功且损坏文件记失败。"""
    mixed_dir = tmp_path / "mixed_samples"
    _copy_samples(mixed_dir, (SAMPLE_IMAGE_2,))
    corrupt_img = mixed_dir / "corrupt_broken.jpg"
    corrupt_img.write_bytes(b"NOT_A_VALID_JPEG_FILE_HEADER_1234567890")

    src_cwd = tmp_path / "src_mixed"
    exe_cwd = tmp_path / "exe_mixed"
    src_cwd.mkdir()
    exe_cwd.mkdir()

    src_res = _run_source(["--input", str(mixed_dir), "--fast"], cwd=src_cwd)
    exe_res = _run_exe(exe_path, ["--input", str(mixed_dir), "--fast"], cwd=exe_cwd)

    assert src_res.returncode == 0, src_res.stderr
    assert exe_res.returncode == 0, exe_res.stderr
    assert _extract_stats_lines(exe_res.stdout) == [
        "  扫描总数: 2",
        "  成功: 1",
        "  跳过: 0",
        "  不支持: 0",
        "  失败: 1",
    ]
    assert _extract_stats_lines(exe_res.stdout) == _extract_stats_lines(src_res.stdout)

    src_snap = _snapshot_db(src_cwd / "data" / "ocr_fast.db")
    exe_snap = _snapshot_db(exe_cwd / "data" / "ocr_fast.db")

    assert exe_snap[SAMPLE_IMAGE_2.name]["status"] == "success"
    assert exe_snap[SAMPLE_IMAGE_2.name]["fields"] == src_snap[SAMPLE_IMAGE_2.name]["fields"]
    assert exe_snap[corrupt_img.name]["status"] == "failed"
    assert exe_snap[corrupt_img.name]["error_message"]
    assert exe_snap[corrupt_img.name]["fields"] == {}
