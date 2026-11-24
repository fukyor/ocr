"""
最小化配置模块

集中保存支持的文件扩展名、单页 PDF 渲染分辨率、正式 OCR 参数以及固定九个字段与标签定义。
几何匹配规则属于提取器内部逻辑，不在此模块配置。
"""
from typing import Dict, FrozenSet, Tuple

# 支持的文件扩展名（统一小写）
SUPPORTED_EXTENSIONS: FrozenSet[str] = frozenset({".pdf", ".png", ".jpg", ".jpeg"})

# 单页 PDF 渲染分辨率
PDF_RENDER_DPI: int = 250

# 默认 SQLite 数据库路径（正式高精度模式）
DEFAULT_DB_PATH: str = "./data/ocr.db"

# 快速联调默认 SQLite 数据库路径（与正式库隔离，避免快速档成功记录导致正式识别被跳过）
DEFAULT_FAST_DB_PATH: str = "./data/ocr_fast.db"

# 固定九个标准输出字段键（按输出顺序）
FIELD_KEYS: Tuple[str, ...] = (
    "证号",
    "姓名",
    "性别",
    "作业类别",
    "操作项目",
    "初领日期",
    "有效期开始日期",
    "有效期截止日期",
    "签发机关",
)

# 有效期限逻辑槽位键（用于标签映射，避免与两个独立输出字段键冲突）
VALIDITY_SLOT_KEY: str = "有效期限"

# 目标字段可识别标签（标准键 -> 可识别标签集合，含已知别名与样本中已观察到的错字）
# 注意："有效期限"作为逻辑槽位，由提取器解析后同时填充"有效期开始日期"与"有效期截止日期"两个独立字段
FIELD_LABEL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "证号": ("证号",),
    "姓名": ("姓名",),
    "性别": ("性别",),
    "作业类别": ("作业类别", "作业类型"),
    "操作项目": ("操作项目", "操作类型"),
    "初领日期": ("初领日期", "初次领取日期", "初领目期"),
    VALIDITY_SLOT_KEY: ("有效期限", "有效期", "有效期至"),
    "签发机关": ("签发机关", "发证机关"),
}

# 仅用于正面局部区域辅助定位的标签（不作为九个标准输出键）
AUX_LABEL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "应复审日期": ("应复审日期", "复审日期"),
}

# 明确表示“有效期至”单日期语义的标签
VALID_UNTIL_EXACT_LABELS: FrozenSet[str] = frozenset({"有效期至"})

# 正式高精度 OCR 参数（RapidOCR 3.x + ONNX Runtime CPU 后端）
# 检测模型：ch_PP-OCRv4_det_mobile.onnx
# 识别模型：ch_PP-OCRv5_rec_server.onnx
OCR_PRODUCTION_CONFIG: Dict[str, object] = {
    "engine_type": "onnxruntime",
    "det_lang": "ch",
    "det_ocr_version": "PP-OCRv4",
    "det_model_type": "mobile",
    "rec_lang": "ch",
    "rec_ocr_version": "PP-OCRv5",
    "rec_model_type": "server",
    "global_max_side_len": 4600,
    "det_limit_type": "max",
    "det_limit_side_len": 3200,
    "use_cuda": False,
    "use_dml": False,
}

# 开发联调快速档 OCR 参数（仅用于独立测试库开发联调）
# 识别模型：ch_PP-OCRv4_rec_mobile.onnx
OCR_FAST_CONFIG: Dict[str, object] = {
    **OCR_PRODUCTION_CONFIG,
    "rec_ocr_version": "PP-OCRv4",
    "rec_model_type": "mobile",
}

# FR-06 导出工作表配置（数据库记录导出为 XLSX 表格）
EXPORT_SHEET_NAME: str = "识别结果"

# FR-06 导出工作表 13 列中文表头（固定顺序）
EXPORT_HEADERS: Tuple[str, ...] = (
    "编号",
    "姓名",
    "性别",
    "证号",
    "作业类型",
    "操作项目",
    "初领日期",
    "有效期开始日期",
    "有效期截止日期",
    "签发机关",
    "文件路径",
    "文件名",
    "错误原因",
)
