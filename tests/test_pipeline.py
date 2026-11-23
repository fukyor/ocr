"""
集成测试 - 主流程编排、单文件异常隔离、幂等与重试、PDF/媒体校验与批次数据库故障
"""
import sqlite3
from pathlib import Path
from typing import Dict

import pymupdf
from PIL import Image

import main as main_module
from config import OCR_FAST_CONFIG, OCR_PRODUCTION_CONFIG
from data_repository import DataRepository
from main import OCRProcessor
from models import (
    FileStatus,
    OCRResult,
    OCRStatus,
    TextBlock,
    create_empty_fields,
)
from ocr_engine import OCREngine


class _StubOCREngine:
    """用于快速测试编排逻辑的替身 OCR 引擎，不加载重型 ONNX 模型。"""

    def __init__(self, responses: Dict[str, OCRResult]):
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    def recognize(self, file_ext: str, file_content: bytes) -> OCRResult:
        self.calls.append((file_ext, len(file_content)))
        key = file_content.decode("utf-8", errors="ignore")
        if key in self.responses:
            return self.responses[key]
        return OCRResult(status=OCRStatus.FAILED, error_message="未配置替身响应")


def _sample_ok_ocr() -> OCRResult:
    blocks = [
        TextBlock("姓名", 0.99, 100.0, 100.0, 40.0, 20.0),
        TextBlock("作业类别", 0.99, 260.0, 100.0, 80.0, 20.0),
        TextBlock("廖浩庄", 0.99, 100.0, 125.0, 60.0, 22.0),
        TextBlock("高处作业", 0.99, 260.0, 125.0, 80.0, 22.0),
    ]
    return OCRResult(
        status=OCRStatus.OK,
        text_blocks=blocks,
        image_width=600,
        image_height=400,
        raw_text="姓名 作业类别\n廖浩庄 高处作业",
    )


def test_mixed_batch_isolation_idempotency_and_retry(tmp_path: Path, monkeypatch):
    """
    验证混合批次处理：
    - 不可读文件 -> failed、空哈希、空原文、零字段，不中断后续文件；
    - 多页 PDF（unsupported）-> unsupported、有哈希、空原文、零字段；
    - 仅含背面/干扰（零可信字段）-> failed、保留原文、零字段；
    - 正常文件 -> success、完整原文、七条字段；
    - 再次运行同一目录 -> 成功文件跳过并保留原 file_id，失败文件可重试。
    """
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    db_path = tmp_path / "db" / "ocr.db"

    f_unreadable = in_dir / "1_unreadable.png"
    f_multi_pdf = in_dir / "2_multi.pdf"
    f_back_only = in_dir / "3_back.jpg"
    f_good = in_dir / "4_good.png"

    f_unreadable.write_bytes(b"UNREADABLE")
    f_multi_pdf.write_bytes(b"MULTI_PDF")
    f_back_only.write_bytes(b"BACK_ONLY")
    f_good.write_bytes(b"GOOD_CERT")

    stub_ocr = _StubOCREngine(
        {
            "MULTI_PDF": OCRResult(status=OCRStatus.UNSUPPORTED, error_message="不支持多页 PDF（共 2 页）"),
            "BACK_ONLY": OCRResult(
                status=OCRStatus.OK,
                text_blocks=[TextBlock("中华人民共和国", 0.99, 100.0, 100.0, 200.0, 30.0)],
                image_width=600,
                image_height=400,
                raw_text="中华人民共和国",
            ),
            "GOOD_CERT": _sample_ok_ocr(),
        }
    )

    real_open = open

    def flaky_open(path, *args, **kwargs):
        if Path(path).name == "1_unreadable.png":
            raise PermissionError("模拟文件被占用")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    processor = OCRProcessor(db_path=str(db_path), ocr_engine=stub_ocr)
    assert processor.process_folder(str(in_dir)) is True
    assert processor.stats == {
        "scanned": 4,
        "success": 1,
        "skipped": 0,
        "unsupported": 1,
        "failed": 2,
    }

    repo = DataRepository(str(db_path))
    records = {r.file_info.file_name: r for r in repo.query_all_files()}
    assert len(records) == 4

    assert records["1_unreadable.png"].status == FileStatus.FAILED
    assert records["1_unreadable.png"].file_info.file_hash is None
    assert records["1_unreadable.png"].raw_text is None
    assert records["1_unreadable.png"].fields == {}

    assert records["2_multi.pdf"].status == FileStatus.UNSUPPORTED
    assert records["2_multi.pdf"].file_info.file_hash is not None
    assert records["2_multi.pdf"].raw_text is None
    assert records["2_multi.pdf"].fields == {}

    assert records["3_back.jpg"].status == FileStatus.FAILED
    assert records["3_back.jpg"].raw_text == "中华人民共和国"
    assert records["3_back.jpg"].fields == {}

    good_first_id = records["4_good.png"].file_id
    assert records["4_good.png"].status == FileStatus.SUCCESS
    assert len(records["4_good.png"].fields) == 7
    assert records["4_good.png"].fields["姓名"] == "廖浩庄"
    assert records["4_good.png"].fields["初领日期"] is None

    # 第二次运行：恢复 1_unreadable.png 可读，且让 3_back.jpg 改为可识别，验证：
    # - 4_good.png 被跳过并保留原 file_id
    # - 1_unreadable.png 与 3_back.jpg（原失败文件）均可重试
    monkeypatch.setattr("builtins.open", real_open)
    stub_ocr.responses["UNREADABLE"] = _sample_ok_ocr()
    stub_ocr.responses["BACK_ONLY"] = _sample_ok_ocr()

    assert processor.process_folder(str(in_dir)) is True
    assert processor.stats == {
        "scanned": 4,
        "success": 2,
        "skipped": 1,
        "unsupported": 1,
        "failed": 0,
    }
    assert repo.find_existing_success_id(str(f_good.resolve()), records["4_good.png"].file_info.file_hash) == good_first_id


def test_extractor_exception_preserves_raw_text(tmp_path: Path):
    """验证提取器抛出异常时仍保存 failed、已取得原文及原因，关联字段零行。"""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a.png").write_bytes(b"OK_IMG")

    stub_ocr = _StubOCREngine({"OK_IMG": _sample_ok_ocr()})

    def broken_extractor(blocks, w, h):
        raise RuntimeError("模拟提取器内部崩溃")

    db_path = tmp_path / "ocr.db"
    processor = OCRProcessor(
        db_path=str(db_path),
        ocr_engine=stub_ocr,
        extractor=broken_extractor,
        enable_two_stage=False,  # 禁用两阶段以测试提取器异常
    )
    assert processor.process_folder(str(in_dir)) is True
    assert processor.stats["failed"] == 1

    repo = DataRepository(str(db_path))
    recs = repo.query_all_files()
    assert len(recs) == 1
    assert recs[0].status == FileStatus.FAILED
    assert recs[0].raw_text == "姓名 作业类别\n廖浩庄 高处作业"
    assert "模拟提取器内部崩溃" in (recs[0].error_message or "")
    assert recs[0].fields == {}


def test_database_failure_aborts_batch_and_keeps_committed(tmp_path: Path):
    """验证数据库写入故障属于批次错误：返回 False、停止后续文件、保留已提交记录。"""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "1_ok.png").write_bytes(b"OK1")
    (in_dir / "2_boom.png").write_bytes(b"OK2")
    (in_dir / "3_never.png").write_bytes(b"OK3")

    stub_ocr = _StubOCREngine(
        {"OK1": _sample_ok_ocr(), "OK2": _sample_ok_ocr(), "OK3": _sample_ok_ocr()}
    )
    db_path = tmp_path / "ocr.db"
    repo = DataRepository(str(db_path))

    # 第一次 save_success 正常提交，第二次模拟数据库只读/磁盘异常
    original_save = repo.save_success
    call_count = {"n": 0}

    def flaky_save_success(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return original_save(*args, **kwargs)

    repo.save_success = flaky_save_success  # type: ignore[method-assign]

    processor = OCRProcessor(db_path=str(db_path), ocr_engine=stub_ocr, repository=repo)
    ok = processor.process_folder(str(in_dir))
    assert ok is False
    # 仅第一份已提交，第三份未继续处理
    assert processor.stats["success"] == 1
    assert len(stub_ocr.calls) == 2

    # 校验第一份已提交记录完好可查
    saved = DataRepository(str(db_path)).query_all_files()
    assert len(saved) == 1
    assert saved[0].file_info.file_name == "1_ok.png"


def test_ocr_engine_media_validation_without_model_load():
    """验证损坏图片、多页 PDF、损坏 PDF 与空白单页在内存校验阶段的正确返回状态。"""
    # 直接构造未触发底层推理的轻量校验实例，确保快速验证媒体解码分支
    engine = OCREngine.__new__(OCREngine)
    engine.ocr = lambda img: type("EmptyOut", (), {"boxes": [], "txts": [], "scores": []})()

    # 1. 损坏图片
    bad_img = engine.recognize(".png", b"not-a-valid-png")
    assert bad_img.status == OCRStatus.FAILED

    # 2. 损坏 PDF
    bad_pdf = engine.recognize(".pdf", b"%PDF-1.4 corrupted")
    assert bad_pdf.status == OCRStatus.FAILED

    # 3. 多页 PDF -> unsupported（跳过渲染与 OCR）
    doc_multi = pymupdf.open()
    doc_multi.new_page()
    doc_multi.new_page()
    multi_bytes = doc_multi.tobytes()
    doc_multi.close()

    res_multi = engine.recognize(".pdf", multi_bytes)
    assert res_multi.status == OCRStatus.UNSUPPORTED

    # 4. 空白单页 PDF -> 未识别到有效文字 -> failed
    doc_blank = pymupdf.open()
    doc_blank.new_page(width=200, height=200)
    blank_bytes = doc_blank.tobytes()
    doc_blank.close()

    res_blank = engine.recognize(".pdf", blank_bytes)
    assert res_blank.status == OCRStatus.FAILED

    # 5. 空白图片 -> 未识别到有效文字 -> failed
    import io

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color="white").save(buf, format="PNG")
    res_blank_img = engine.recognize(".png", buf.getvalue())
    assert res_blank_img.status == OCRStatus.FAILED


def test_fast_mode_selects_fast_config_and_isolates_default_db(tmp_path: Path, monkeypatch, capsys):
    """验证 --fast 参数使用 OCR_FAST_CONFIG、默认写入独立测试库且禁止写入正式默认库。"""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    (in_dir / "sample.png").write_bytes(b"IMG")

    prod_db = tmp_path / "data" / "ocr.db"
    fast_db = tmp_path / "data" / "ocr_fast.db"
    custom_db = tmp_path / "data" / "custom_fast.db"
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", str(prod_db))
    monkeypatch.setattr(main_module, "DEFAULT_FAST_DB_PATH", str(fast_db))

    seen_configs = []

    class _RecordingEngine(_StubOCREngine):
        def __init__(self, ocr_config=None):
            seen_configs.append(ocr_config)
            super().__init__({"IMG": _sample_ok_ocr()})

    monkeypatch.setattr(main_module, "OCREngine", _RecordingEngine)

    # 1. --fast 未指定 --db 时，自动写入独立测试库并加载 OCR_FAST_CONFIG
    exit_code = main_module.main(["--input", str(in_dir), "--fast"])
    assert exit_code == 0
    assert seen_configs[-1] == OCR_FAST_CONFIG
    assert fast_db.exists()
    assert not prod_db.exists()
    assert len(DataRepository(str(fast_db)).query_all_files()) == 1

    # 2. --fast 指定自定义 --db 时，写入指定测试库
    exit_code = main_module.main(["--input", str(in_dir), "--fast", "--db", str(custom_db)])
    assert exit_code == 0
    assert seen_configs[-1] == OCR_FAST_CONFIG
    assert custom_db.exists()
    assert not prod_db.exists()

    # 3. --fast 显式指定正式默认库时，拒绝执行并返回非零退出码
    before_calls = len(seen_configs)
    exit_code = main_module.main(["--input", str(in_dir), "--fast", "--db", str(prod_db)])
    assert exit_code == 1
    assert len(seen_configs) == before_calls
    assert not prod_db.exists()
    captured = capsys.readouterr()
    assert "禁止写入正式默认数据库" in captured.err

    # 4. 不传 --fast 时，默认写入正式库并加载 OCR_PRODUCTION_CONFIG
    exit_code = main_module.main(["--input", str(in_dir)])
    assert exit_code == 0
    assert seen_configs[-1] == OCR_PRODUCTION_CONFIG
    assert prod_db.exists()
    assert len(DataRepository(str(prod_db)).query_all_files()) == 1
