"""
FR-06 数据库表格导出模块

负责将只读查询得到的全部文件处理记录（List[ExportRow]）按 FR-06 规范写入标准 .xlsx 工作簿。
与 OCR 识别、字段提取、两阶段复核、版式补丁完全物理隔离。
"""
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font

from config import DEFAULT_DB_PATH, EXPORT_HEADERS, EXPORT_SHEET_NAME
from data_repository import DataRepository
from models import ExportRow, ExportSummary


class ExportError(Exception):
    """FR-06 导出失败统一异常，承载明确中文原因与路径上下文。"""

    pass


# 匹配 XML 1.0 非法控制字符（防止 openpyxl IllegalCharacterError）
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ISO 8601 标准日期格式：YYYY-MM-DD
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# 灵活日期格式：YYYY-MM-DD / YYYY/M/D / YYYY.M.D / YYYY年M月D日
_FLEX_DATE_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")


def derive_export_output_path(db_path: Path) -> Path:
    """
    推导导出工作簿的默认输出路径。

    规则：数据库所在同一目录，沿用数据库主文件名并将扩展名设为 .xlsx。
    防覆盖守卫：若源数据库文件本身后缀已为 .xlsx，则追加 .xlsx 生成 <db_name>.xlsx。

    Args:
        db_path: 数据库文件路径

    Returns:
        推导的 .xlsx 工作簿路径
    """
    resolved = db_path.resolve()
    if resolved.suffix.lower() == ".xlsx":
        # 防止覆盖源数据库文件（若数据库本身后缀意外为 .xlsx）
        return resolved.with_name(f"{resolved.name}.xlsx")
    return resolved.with_suffix(".xlsx")


def _sanitize_xml_text(text: str) -> str:
    """
    清洗文本中的 XML 1.0 非法控制字符。

    Args:
        text: 原始文本

    Returns:
        移除非法控制字符后的文本
    """
    return _ILLEGAL_XML_RE.sub("", text)


def _format_export_date(raw_value: Optional[str]) -> Optional[str]:
    """
    规范化导出日期为 YYYY-MM-DD 文本格式。

    若原值已为标准 ISO 8601 格式（YYYY-MM-DD）且为合法公历日期，原样返回。
    若为其他可解析格式（YYYY/M/D、YYYY.M.D、YYYY年M月D日），规范化为 YYYY-MM-DD。
    若无法解析或为空，原样返回（包括 None）。

    Args:
        raw_value: 原始日期字符串或 None

    Returns:
        规范化后的 YYYY-MM-DD 文本或原值或 None
    """
    if raw_value is None:
        return None

    cleaned = raw_value.strip()
    if not cleaned:
        return None

    # 已为标准 ISO 8601 格式
    match = _ISO_DATE_RE.match(cleaned)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if _is_valid_date(year, month, day):
            return cleaned

    # 尝试灵活格式解析
    match = _FLEX_DATE_RE.match(cleaned)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if _is_valid_date(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"

    # 兜底原样文本保留，不丢历史数据
    return cleaned


def _is_valid_date(year: int, month: int, day: int) -> bool:
    """
    校验是否为合法公历日期。

    Args:
        year: 年份
        month: 月份
        day: 日

    Returns:
        True 表示合法，False 表示非法
    """
    try:
        datetime(year, month, day)
        return True
    except ValueError:
        return False


def _write_text_cell(
    ws,
    row_idx: int,
    col_idx: int,
    value: Optional[str],
    force_text_format: bool = False,
) -> None:
    """
    安全写入文本单元格（清洗非法字符、防公式注入、可选强制文本格式）。

    Args:
        ws: openpyxl 工作表对象
        row_idx: 行号（从 1 开始）
        col_idx: 列号（从 1 开始）
        value: 文本值或 None
        force_text_format: 是否强制设置单元格数字格式为 "@"（纯文本）
    """
    if value is None or value == "":
        return  # 空值与历史缺失字段在单元格中留空

    clean = _sanitize_xml_text(str(value))
    if not clean:
        return

    cell = ws.cell(row=row_idx, column=col_idx)
    cell.value = clean
    cell.data_type = "s"  # 显式强制纯文本（防 15 位以上数字失真、防前导零丢失、防 = 公式注入）

    if force_text_format:
        cell.number_format = "@"


def _extract_row_columns(index: int, row: ExportRow) -> Tuple:
    """
    从 ExportRow 提取 13 列导出数据。

    证号与作业类型：优先取 certificate_files 关联值，无关联时分别回退至本文件识别字段。

    Args:
        index: 导出行编号（从 1 开始）
        row: ExportRow 对象

    Returns:
        包含 13 列值的元组
    """
    # 证号与作业类型：优先取 certificate_files 关联值，无关联时分别回退至本文件识别字段
    cert_no = None
    work_type = None

    if row.certificate_key is not None:
        linked_cert_no, linked_work_cat = row.certificate_key
        if linked_cert_no and linked_cert_no.strip():
            cert_no = linked_cert_no
        if linked_work_cat and linked_work_cat.strip():
            work_type = linked_work_cat

    if cert_no is None:
        cert_no = row.fields.get("证号")
    if work_type is None:
        work_type = row.fields.get("作业类别")

    return (
        index,  # 1. 编号
        row.fields.get("姓名"),  # 2. 姓名
        row.fields.get("性别"),  # 3. 性别
        cert_no,  # 4. 证号
        work_type,  # 5. 作业类型
        row.fields.get("操作项目"),  # 6. 操作项目
        _format_export_date(row.fields.get("初领日期")),  # 7. 初领日期
        _format_export_date(row.fields.get("有效期开始日期")),  # 8. 有效期开始日期
        _format_export_date(row.fields.get("有效期截止日期")),  # 9. 有效期截止日期
        row.fields.get("签发机关"),  # 10. 签发机关
        row.file_path,  # 11. 文件路径
        row.file_name,  # 12. 文件名
        row.error_message,  # 13. 错误原因
    )


def export_database_to_xlsx(
    db_path: str = DEFAULT_DB_PATH,
    output_path: Optional[str] = None,
) -> ExportSummary:
    """
    FR-06 核心导出函数：将数据库记录导出为 13 列 XLSX 工作簿。

    执行流程：
    1. 只读查询数据库（不存在/无权限/损坏/缺表立即抛出具体原因）
    2. 确定目标工作簿路径并校验非目录
    3. 构建 13 列标准工作簿（冻结首行、表头加粗、证号/日期强制文本格式）
    4. 临时文件 + 原子替换写入目标工作簿（防 Windows 占用破坏旧文件与半截脏文件）

    Args:
        db_path: 数据库文件路径（默认 ./data/ocr.db）
        output_path: 可选的输出工作簿路径（默认推导为数据库同目录同名 .xlsx）

    Returns:
        ExportSummary 对象（包含实际数据库路径、输出路径和数据行数）

    Raises:
        ExportError: 数据库读取失败或工作簿写入失败，包含具体中文原因
    """
    # 1. 只读查询数据库（不存在/无权限/损坏/缺表立即抛出具体原因）
    try:
        rows = DataRepository.query_export_rows_readonly(db_path)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError, sqlite3.Error) as exc:
        raise ExportError(f"无法读取数据库 {db_path}: {exc}")

    # 2. 确定目标工作簿路径并校验非目录
    resolved_db = Path(db_path).resolve()
    if output_path is not None:
        target_path = Path(output_path).resolve()
    else:
        target_path = derive_export_output_path(resolved_db)

    if target_path.exists() and target_path.is_dir():
        raise ExportError(f"无法写入工作簿，目标路径是目录: {target_path}")

    # 3. 构建 13 列标准工作簿
    wb = Workbook()
    ws = wb.active
    ws.title = EXPORT_SHEET_NAME
    ws.freeze_panes = "A2"

    # 写入第 1 行中文表头（严格 13 列）
    for col_idx, title in enumerate(EXPORT_HEADERS, start=1):
        _write_text_cell(ws, 1, col_idx, title)
        ws.cell(row=1, column=col_idx).font = Font(bold=True)

    # 写入数据行（0 条时循环不执行，仅保留表头）
    for idx, row in enumerate(rows, start=1):
        excel_row = idx + 1
        values = _extract_row_columns(idx, row)

        # 第 1 列：编号（整数）
        num_cell = ws.cell(row=excel_row, column=1, value=values[0])
        num_cell.number_format = "0"

        # 第 2~13 列：文本安全写入；证号(4)与三日期(7,8,9)强制文本格式 "@"
        for col_idx in range(2, 14):
            _write_text_cell(
                ws,
                excel_row,
                col_idx,
                values[col_idx - 1],
                force_text_format=(col_idx in {4, 7, 8, 9}),
            )

    # 按列预设并微调列宽（含最大宽度上限）
    _adjust_column_widths(ws)

    # 4. 临时文件 + 原子替换写入目标工作簿
    tmp_path = None
    try:
        # 在 target_path.parent 创建临时文件（后缀 .tmp.xlsx）
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=f".{target_path.stem}_",
            suffix=".tmp.xlsx",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name

        wb.save(tmp_path)
        os.replace(tmp_path, target_path)
    except PermissionError as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExportError(
            f"无法写入工作簿 {target_path}（文件可能正被其他程序占用或无写入权限）: {exc}"
        )
    except OSError as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExportError(f"写入工作簿失败 {target_path}: {exc}")

    return ExportSummary(
        db_path=str(resolved_db),
        output_path=str(target_path),
        row_count=len(rows),
    )


def _adjust_column_widths(ws) -> None:
    """
    按列语义预设基础列宽并随内容长度适度自适应（带合理上限）。

    Args:
        ws: openpyxl 工作表对象
    """
    # 预设基础列宽（单位：Excel 字符宽度）
    base_widths = {
        1: 8,  # 编号
        2: 12,  # 姓名
        3: 6,  # 性别
        4: 22,  # 证号
        5: 15,  # 作业类型
        6: 30,  # 操作项目
        7: 14,  # 初领日期
        8: 18,  # 有效期开始日期
        9: 18,  # 有效期截止日期
        10: 25,  # 签发机关
        11: 50,  # 文件路径
        12: 30,  # 文件名
        13: 40,  # 错误原因
    }

    # 最大宽度上限
    max_widths = {
        11: 60,  # 文件路径
        12: 40,  # 文件名
        13: 60,  # 错误原因
    }

    for col_idx in range(1, 14):
        base_width = base_widths.get(col_idx, 15)
        max_width = max_widths.get(col_idx, base_width)

        # 根据内容长度微调（简化计算，仅取最大单元格文本长度）
        max_len = base_width
        for row_idx in range(1, ws.max_row + 1):
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            if cell_value:
                cell_len = len(str(cell_value))
                # 中文字符按 1.5 倍宽度估算
                adjusted_len = sum(1.5 if ord(c) > 127 else 1 for c in str(cell_value))
                max_len = max(max_len, adjusted_len)

        # 应用宽度（带上限保护）
        ws.column_dimensions[chr(64 + col_idx)].width = min(max_len + 2, max_width)
