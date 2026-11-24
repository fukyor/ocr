"""
FR-06 数据库表格导出模块单元测试

覆盖混合状态、同证件多文件、缺项兼容、证号文本保真、日期格式、
0 条记录空库、防公式注入、非法字符清洗及 CLI 参数守卫等全部边界场景。
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from config import EXPORT_HEADERS, EXPORT_SHEET_NAME, FIELD_KEYS
from data_repository import DataRepository
from excel_exporter import ExportError, export_database_to_xlsx
from main import main
from models import FileInfo, FileStatus


class TestExcelExporter:
    """FR-06 表格导出核心功能测试。"""

    @pytest.fixture
    def temp_db(self):
        """创建临时测试数据库。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        # 清理数据库文件
        if os.path.exists(db_path):
            os.remove(db_path)
        # 清理可能生成的 .xlsx 文件
        xlsx_path = Path(db_path).with_suffix(".xlsx")
        if xlsx_path.exists():
            xlsx_path.unlink()

    def test_mixed_status_and_column_order(self, temp_db):
        """验收场景 1：混合状态与列序验收。"""
        repo = DataRepository(temp_db)

        # 写入 1 条 success、1 条 failed、1 条 unsupported
        success_fields = {key: None for key in FIELD_KEYS}
        success_fields["姓名"] = "张三"
        success_fields["证号"] = "T441322199001010001"
        success_fields["作业类别"] = "电工作业"

        file_info_success = FileInfo(
            file_name="test_success.png",
            file_path="/tmp/test_success.png",
            file_ext=".png",
            file_hash="abc123",
        )
        repo.save_success(file_info_success, "整页原文", success_fields)

        file_info_failed = FileInfo(
            file_name="test_failed.png",
            file_path="/tmp/test_failed.png",
            file_ext=".png",
            file_hash="def456",
        )
        repo.save_non_success(
            file_info_failed,
            FileStatus.FAILED.value,
            "解码失败",
            raw_text=None,
        )

        file_info_unsupported = FileInfo(
            file_name="test_unsupported.pdf",
            file_path="/tmp/test_unsupported.pdf",
            file_ext=".pdf",
            file_hash="ghi789",
        )
        repo.save_non_success(
            file_info_unsupported,
            FileStatus.UNSUPPORTED.value,
            "多页 PDF 不支持",
            raw_text=None,
        )

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)

        # 验证摘要
        assert summary.row_count == 3
        assert Path(summary.output_path).exists()

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active
        assert ws.title == EXPORT_SHEET_NAME

        # 验证表头（第 1 行 13 列）
        header_row = [ws.cell(1, col).value for col in range(1, 14)]
        assert header_row == list(EXPORT_HEADERS)

        # 验证数据行数（表头 + 3 条数据）
        assert ws.max_row == 4

        # 验证编号列
        assert ws.cell(2, 1).value == 1
        assert ws.cell(3, 1).value == 2
        assert ws.cell(4, 1).value == 3

        # 验证第一行（success）
        assert ws.cell(2, 2).value == "张三"  # 姓名
        assert ws.cell(2, 4).value == "T441322199001010001"  # 证号
        assert ws.cell(2, 5).value == "电工作业"  # 作业类型
        assert ws.cell(2, 11).value == "/tmp/test_success.png"  # 文件路径
        assert ws.cell(2, 13).value is None  # 错误原因为空

        # 验证第二行（failed）
        assert ws.cell(3, 2).value is None  # 姓名为空
        assert ws.cell(3, 11).value == "/tmp/test_failed.png"  # 文件路径
        assert ws.cell(3, 13).value == "解码失败"  # 错误原因

        # 验证第三行（unsupported）
        assert ws.cell(4, 11).value == "/tmp/test_unsupported.pdf"  # 文件路径
        assert ws.cell(4, 13).value == "多页 PDF 不支持"  # 错误原因

    def test_same_certificate_multiple_files(self, temp_db):
        """验收场景 2：同证件多文件独立成行。"""
        repo = DataRepository(temp_db)

        cert_no = "T441322199001010001"
        work_category = "电工作业"

        # 第一份文件
        fields1 = {key: None for key in FIELD_KEYS}
        fields1["证号"] = cert_no
        fields1["姓名"] = "张三"
        fields1["作业类别"] = work_category
        fields1["初领日期"] = "2020-01-01"

        file_info1 = FileInfo(
            file_name="cert_v1.png",
            file_path="/tmp/cert_v1.png",
            file_ext=".png",
            file_hash="hash1",
        )
        repo.save_success(file_info1, "原文1", fields1)

        # 第二份文件（同证号同类别，不同路径）
        fields2 = {key: None for key in FIELD_KEYS}
        fields2["证号"] = cert_no
        fields2["姓名"] = "张三"
        fields2["作业类别"] = work_category
        fields2["初领日期"] = "2020-01-01"
        fields2["有效期开始日期"] = "2023-05-10"  # 不同字段值

        file_info2 = FileInfo(
            file_name="cert_v2.jpg",
            file_path="/tmp/cert_v2.jpg",
            file_ext=".jpg",
            file_hash="hash2",
        )
        repo.save_success(file_info2, "原文2", fields2)

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)
        assert summary.row_count == 2

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        # 验证两行数据
        assert ws.max_row == 3  # 表头 + 2 条数据

        # 两行都有同一证号和作业类别（来自 certificate_key）
        assert ws.cell(2, 4).value == cert_no
        assert ws.cell(2, 5).value == work_category
        assert ws.cell(3, 4).value == cert_no
        assert ws.cell(3, 5).value == work_category

        # 但文件路径和部分字段不同
        assert ws.cell(2, 11).value == "/tmp/cert_v1.png"
        assert ws.cell(3, 11).value == "/tmp/cert_v2.jpg"

        # 有效期开始日期第一行为空，第二行有值
        assert ws.cell(2, 8).value is None
        assert ws.cell(3, 8).value == "2023-05-10"

    def test_missing_fields_and_legacy_compatibility(self, temp_db):
        """验收场景 3：缺项与历史 7 字段库兼容。"""
        # 手动构建旧库结构（无 certificate_files 表，只有部分字段）
        conn = sqlite3.connect(temp_db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT,
                status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'unsupported')),
                raw_text TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE extracted_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                field_key TEXT NOT NULL,
                field_value TEXT,
                FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE
            )
            """
        )

        # 插入一条只有作业类别、缺少证号的记录（无法建证件关联）
        conn.execute(
            """
            INSERT INTO files (file_id, file_name, file_path, file_hash, status, raw_text, error_message, created_at)
            VALUES ('file1', 'old.png', '/tmp/old.png', 'hash1', 'success', '原文', NULL, '2023-01-01T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO extracted_fields (file_id, field_key, field_value) VALUES ('file1', '作业类别', '高处作业')"
        )
        conn.commit()
        conn.close()

        # 导出（应兼容无 certificate_files 表的情况）
        summary = export_database_to_xlsx(db_path=temp_db)
        assert summary.row_count == 1

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        # 作业类型列（第 5 列）应有值
        assert ws.cell(2, 5).value == "高处作业"

        # 证号列（第 4 列）为空
        assert ws.cell(2, 4).value is None

        # 有效期开始日期列（第 8 列）为空（历史库无此字段）
        assert ws.cell(2, 8).value is None

    def test_cert_no_text_fidelity(self, temp_db):
        """验收场景 4：证号高精度文本保真。"""
        repo = DataRepository(temp_db)

        test_cases = [
            ("00441300123456", "前导零证号"),
            ("441322199507034550", "18 位纯数字证号"),
            ("T445221199507034550", "字母数字混合证号"),
            ("44162319930920031X", "末尾 X 证号"),
        ]

        for cert_no, description in test_cases:
            fields = {key: None for key in FIELD_KEYS}
            fields["证号"] = cert_no
            fields["姓名"] = description
            fields["作业类别"] = "电工作业"

            file_info = FileInfo(
                file_name=f"{cert_no}.png",
                file_path=f"/tmp/{cert_no}.png",
                file_ext=".png",
                file_hash=cert_no,
            )
            repo.save_success(file_info, "原文", fields)

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)
        assert summary.row_count == len(test_cases)

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        for idx, (expected_cert_no, _) in enumerate(test_cases, start=2):
            cell = ws.cell(idx, 4)  # 证号列
            # 验证单元格值与格式
            assert isinstance(cell.value, str), f"证号 {expected_cert_no} 应为字符串类型"
            assert cell.value == expected_cert_no, f"证号 {expected_cert_no} 内容不匹配"
            assert cell.data_type == "s", f"证号 {expected_cert_no} 应为纯文本类型"
            assert cell.number_format == "@", f"证号 {expected_cert_no} 应强制文本格式"

    def test_date_format_normalization(self, temp_db):
        """验收场景 5：三日期显示格式 YYYY-MM-DD。"""
        repo = DataRepository(temp_db)

        fields = {key: None for key in FIELD_KEYS}
        fields["证号"] = "T441322199001010001"
        fields["作业类别"] = "电工作业"
        fields["初领日期"] = "2020-01-15"
        fields["有效期开始日期"] = "2023-06-20"
        fields["有效期截止日期"] = "2029-06-19"

        file_info = FileInfo(
            file_name="test.png",
            file_path="/tmp/test.png",
            file_ext=".png",
            file_hash="hash1",
        )
        repo.save_success(file_info, "原文", fields)

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        # 验证三个日期列（7, 8, 9）
        init_date = ws.cell(2, 7)
        start_date = ws.cell(2, 8)
        end_date = ws.cell(2, 9)

        assert init_date.value == "2020-01-15"
        assert init_date.data_type == "s"
        assert init_date.number_format == "@"

        assert start_date.value == "2023-06-20"
        assert start_date.data_type == "s"
        assert start_date.number_format == "@"

        assert end_date.value == "2029-06-19"
        assert end_date.data_type == "s"
        assert end_date.number_format == "@"

    def test_empty_database(self, temp_db):
        """验收场景 6：0 条记录空库。"""
        # 初始化空库
        DataRepository(temp_db)

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)
        assert summary.row_count == 0

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        # 仅有表头
        assert ws.max_row == 1

        # 验证表头完整
        header_row = [ws.cell(1, col).value for col in range(1, 14)]
        assert header_row == list(EXPORT_HEADERS)

    def test_formula_injection_and_illegal_chars(self, temp_db):
        """验收场景 7：防公式与非法字符。"""
        repo = DataRepository(temp_db)

        # 测试公式注入和非法字符
        file_info = FileInfo(
            file_name="test\x00_name.png",  # 非法字符 \x00
            file_path="/tmp/test.png",
            file_ext=".png",
            file_hash="hash1",
        )
        repo.save_non_success(
            file_info,
            FileStatus.FAILED.value,
            "=SUM(A1:A2)\x0b测试",  # 公式注入 + 非法字符 \x0b
            raw_text=None,
        )

        # 导出
        summary = export_database_to_xlsx(db_path=temp_db)

        # 回读验证
        wb = load_workbook(summary.output_path)
        ws = wb.active

        # 文件名列（第 12 列）：非法字符应被清洗
        file_name_cell = ws.cell(2, 12)
        assert "\x00" not in file_name_cell.value
        assert file_name_cell.data_type == "s"

        # 错误原因列（第 13 列）：公式应被当作纯文本，非法字符应被清洗
        error_cell = ws.cell(2, 13)
        assert error_cell.value == "=SUM(A1:A2)测试"  # \x0b 被清洗
        assert error_cell.data_type == "s"  # 纯文本，不是公式

    def test_database_not_found(self, temp_db):
        """异常场景：数据库文件不存在。"""
        # 删除数据库文件
        if os.path.exists(temp_db):
            os.remove(temp_db)

        # 导出应抛出 ExportError
        with pytest.raises(ExportError) as exc_info:
            export_database_to_xlsx(db_path=temp_db)

        assert "数据库文件不存在" in str(exc_info.value)

    def test_database_missing_tables(self, temp_db):
        """异常场景：数据库缺少必需表。"""
        # 创建空数据库（无任何表）
        conn = sqlite3.connect(temp_db)
        conn.close()

        # 导出应抛出 ExportError
        with pytest.raises(ExportError) as exc_info:
            export_database_to_xlsx(db_path=temp_db)

        assert "表结构不完整" in str(exc_info.value)

    def test_cli_export_mode_with_fast_conflict(self):
        """CLI 参数守卫：-export 混传 --fast。"""
        exit_code = main(["-export", "--fast"])
        assert exit_code == 1

    def test_cli_export_mode_with_input_conflict(self):
        """CLI 参数守卫：-export 混传 --input。"""
        exit_code = main(["-export", "--input", "./test"])
        assert exit_code == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
