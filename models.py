"""
数据契约模块

定义跨模块流转的标准数据结构（仅依赖 Python 标准库）。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from config import FIELD_KEYS

# 固定九个字段的标准键值字典类型
FieldDict = Dict[str, Optional[str]]

# 证件联合主键类型：(证号, 作业类别)
CertificateKey = Tuple[str, str]


def create_empty_fields() -> FieldDict:
    """创建固定九个键、值全为 None 的标准字段字典。"""
    return {key: None for key in FIELD_KEYS}


def count_non_empty_fields(fields: FieldDict) -> int:
    """统计固定九个字段中非空值的数量。"""
    return sum(1 for key in FIELD_KEYS if fields.get(key) is not None)


def extract_certificate_key(fields: FieldDict) -> Optional[CertificateKey]:
    """
    从字段字典中提取证件联合主键。

    仅当"证号"与"作业类别"均非空且去空白后非空串时，返回 (证号, 作业类别) 元组。
    否则返回 None（表示无法建立证件关联）。
    """
    cert_no = fields.get("证号")
    work_category = fields.get("作业类别")

    if cert_no is None or work_category is None:
        return None

    cert_no_clean = cert_no.strip()
    work_category_clean = work_category.strip()

    if not cert_no_clean or not work_category_clean:
        return None

    return (cert_no_clean, work_category_clean)


class OCRStatus(str, Enum):
    """OCR 识别状态枚举。"""

    OK = "ok"  # 已取得有效文本块
    UNSUPPORTED = "unsupported"  # 不支持的媒体（如多页 PDF）
    FAILED = "failed"  # 解码、校验或识别失败


class FileStatus(str, Enum):
    """单文件处理结果状态枚举。"""

    SUCCESS = "success"  # 至少一个字段可信并已保存
    UNSUPPORTED = "unsupported"  # 不支持的文件（如多页 PDF）
    FAILED = "failed"  # 读取、解码、OCR 或字段提取失败


@dataclass
class FileInfo:
    """源文件基础信息契约。"""

    file_name: str  # 原始文件名
    file_path: str  # 规范化绝对路径
    file_ext: str  # 小写扩展名（.png / .jpg / .jpeg / .pdf）
    file_hash: Optional[str] = None  # SHA-256 哈希，仅在未取得文件内容时允许为空


@dataclass
class TextBlock:
    """单个 OCR 识别文本块（轴对齐包围框，坐标原点位于纠偏后的整页左上角）。"""

    text: str  # 识别文本
    confidence: float  # 识别置信度
    x: float  # 左上角 x 坐标（向右为正）
    y: float  # 左上角 y 坐标（向下为正）
    width: float  # 包围框宽度
    height: float  # 包围框高度

    @property
    def x_max(self) -> float:
        """右边界 x 坐标。"""
        return self.x + self.width

    @property
    def y_max(self) -> float:
        """下边界 y 坐标。"""
        return self.y + self.height

    @property
    def x_center(self) -> float:
        """水平中心 x 坐标。"""
        return self.x + self.width / 2.0

    @property
    def y_center(self) -> float:
        """垂直中心 y 坐标。"""
        return self.y + self.height / 2.0


@dataclass
class OCRResult:
    """单文件 OCR 识别结果契约。"""

    status: OCRStatus  # 识别状态
    text_blocks: List[TextBlock] = field(default_factory=list)  # 有效文本块列表
    image_width: int = 0  # 纠偏后的整页图像宽度
    image_height: int = 0  # 纠偏后的整页图像高度
    raw_text: Optional[str] = None  # 整页 OCR 原文
    error_message: Optional[str] = None  # 错误或不支持原因


@dataclass
class ProcessResult:
    """按 file_id 查询返回的持久化记录视图。"""

    file_id: str  # UUID4 文件唯一标识
    file_info: FileInfo  # 源文件信息
    status: FileStatus  # 处理状态
    raw_text: Optional[str] = None  # 整页 OCR 原文（若已取得）
    fields: FieldDict = field(default_factory=dict)  # 成功时为九键字典，失败/不支持时为空字典
    error_message: Optional[str] = None  # 失败或不支持原因
    created_at: Optional[str] = None  # ISO 8601 创建时间
    certificate_key: Optional[CertificateKey] = None  # 证件联合主键（若已建立关联）


@dataclass
class CertificateRecord:
    """按联合主键查询返回的证件归并记录。"""

    cert_no: str  # 证号（联合主键第一部分）
    work_category: str  # 作业类别（联合主键第二部分）
    file_ids: List[str]  # 关联的全部源文件 file_id 列表（按关联时间升序）
    created_at: str  # 首次建档时间（ISO 8601）
    updated_at: str  # 最近关联更新时间（ISO 8601）


@dataclass
class ExportRow:
    """FR-06 只读导出单行视图（严格限定为单份 files 记录及其本文件识别字段和可选证件关联）。"""

    file_id: str  # 源文件唯一标识
    file_name: str  # 原始文件名
    file_path: str  # 规范化绝对路径
    error_message: Optional[str] = None  # 失败或不支持原因
    fields: FieldDict = field(default_factory=dict)  # 本文件识别字段（success 时为九键字典，其余为空）
    certificate_key: Optional[CertificateKey] = None  # 证件联合主键（若已建立关联）


@dataclass
class ExportSummary:
    """FR-06 导出完成后的结构化摘要。"""

    db_path: str  # 实际读取的数据库路径
    output_path: str  # 实际落盘的 .xlsx 工作簿路径
    row_count: int  # 导出的数据行数（不含表头，0 条时为 0）
