"""
单元测试 - 数据契约与文件扫描模块
"""
from pathlib import Path
import pytest

from config import FIELD_KEYS
from file_scanner import scan_folder
from models import (
    FileInfo,
    OCRResult,
    OCRStatus,
    TextBlock,
    count_non_empty_fields,
    create_empty_fields,
)


def test_models_and_field_helpers():
    """验证数据契约字段及固定七键辅助函数。"""
    info = FileInfo(
        file_name="cert.png",
        file_path="E:/tmp/cert.png",
        file_ext=".png",
        file_hash=None,
    )
    assert info.file_hash is None

    block = TextBlock(text="姓名", confidence=0.99, x=10.0, y=20.0, width=40.0, height=16.0)
    assert block.x_max == 50.0
    assert block.y_max == 36.0
    assert block.x_center == 30.0
    assert block.y_center == 28.0

    fields = create_empty_fields()
    assert tuple(fields.keys()) == FIELD_KEYS
    assert count_non_empty_fields(fields) == 0

    fields["姓名"] = "廖浩庄"
    fields["性别"] = "男"
    assert count_non_empty_fields(fields) == 2

    ocr_res = OCRResult(status=OCRStatus.OK, image_width=100, image_height=200, raw_text="原文")
    assert ocr_res.status == OCRStatus.OK


def test_scan_folder_empty_and_filtering(tmp_path: Path):
    """验证空目录、大小写扩展名、忽略其他格式与跳过子目录。"""
    assert scan_folder(str(tmp_path)) == []

    # 创建大小写混合的候选文件、不支持的文件和子目录
    (tmp_path / "c.PDF").write_bytes(b"pdf")
    (tmp_path / "a.PNG").write_bytes(b"png")
    (tmp_path / "b.Jpg").write_bytes(b"jpg")
    (tmp_path / "d.jpeg").write_bytes(b"jpeg")
    (tmp_path / "ignore.txt").write_text("txt", encoding="utf-8")
    (tmp_path / "ignore.bmp").write_bytes(b"bmp")

    subdir = tmp_path / "nested"
    subdir.mkdir()
    (subdir / "inside.png").write_bytes(b"inside")

    result = scan_folder(str(tmp_path))
    names = [Path(p).name for p in result]
    assert names == ["a.PNG", "b.Jpg", "c.PDF", "d.jpeg"]
    # 验证均返回规范化绝对路径
    assert all(Path(p).is_absolute() for p in result)


def test_scan_folder_invalid_directory(tmp_path: Path):
    """验证不存在目录与文件路径均报错。"""
    with pytest.raises(FileNotFoundError):
        scan_folder(str(tmp_path / "not_exists"))

    file_path = tmp_path / "sample.png"
    file_path.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        scan_folder(str(file_path))
