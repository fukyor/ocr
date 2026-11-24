"""
两阶段识别与复核模块

整页识别 → 字段提取 → 检测缺失/疑点 → 正面定位 → 局部复识 → 合并结果
"""
from typing import Dict, List, Optional, Set, Tuple

from config import VALID_UNTIL_EXACT_LABELS
from models import FieldDict, TextBlock, create_empty_fields
from field_extractor import extract_fields, _is_noise_text


class TwoStageProcessor:
    """两阶段识别与复核处理器"""

    def __init__(self):
        pass

    def process_with_review(
        self,
        text_blocks: List[TextBlock],
        image_width: int,
        image_height: int,
        ocr_engine,
        file_ext: str,
        file_content: bytes,
    ) -> Tuple[FieldDict, List[TextBlock], str]:
        """
        执行两阶段识别与复核。

        Args:
            text_blocks: 整页识别的文本块
            image_width: 整页图像宽度
            image_height: 整页图像高度
            ocr_engine: OCR 引擎实例
            file_ext: 文件扩展名
            file_content: 文件字节内容

        Returns:
            (最终字段, 整页文本块, 处理说明)
        """
        # 第一阶段：整页识别与字段提取（让异常传播出去）
        fields_stage1 = extract_fields(text_blocks, image_width, image_height)

        # 检查是否需要触发第二阶段
        needs_review, review_reason = self._check_needs_review(fields_stage1, text_blocks)

        if not needs_review:
            return fields_stage1, text_blocks, "整页识别完成，无需复核"

        # 尝试定位正面卡区
        roi_box = self._estimate_front_card_roi(
            text_blocks, fields_stage1, image_width, image_height
        )

        if roi_box is None:
            return fields_stage1, text_blocks, f"触发复核({review_reason})但无法定位正面区域"

        # 第二阶段：局部复识
        try:
            roi_result = ocr_engine.recognize_roi(file_ext, file_content, roi_box)

            if roi_result.status.value != 'ok' or not roi_result.text_blocks:
                return fields_stage1, text_blocks, f"触发复核({review_reason})但局部识别失败"

            # 提取局部字段
            fields_stage2 = extract_fields(
                roi_result.text_blocks,
                image_width,  # 使用整页尺寸，因为坐标已映射回整页
                image_height,
            )

            # 合并两阶段结果
            merged_fields = self._merge_fields(fields_stage1, fields_stage2)

            return merged_fields, text_blocks, f"两阶段复核完成({review_reason})"

        except Exception as exc:
            # 局部识别失败不影响整页结果
            return fields_stage1, text_blocks, f"触发复核({review_reason})但发生异常: {exc}"

    @staticmethod
    def _check_needs_review(
        fields: FieldDict, text_blocks: List[TextBlock]
    ) -> Tuple[bool, str]:
        """
        检查是否需要触发第二阶段。

        触发条件：
        1. 存在缺失字段（但排除"有效期至"单止日证件仅缺起日的合法情况）
        2. 操作项目缺"高"字（高处安装、维护、拆除作业）
        """
        # 检查缺失
        missing_fields = [k for k, v in fields.items() if v is None]

        # 特殊情况：若仅缺失"有效期开始日期"且已确定"有效期截止日期"，检查是否为"有效期至"单止日证件
        if missing_fields == ["有效期开始日期"] and fields.get("有效期截止日期") is not None:
            # 检查文本块中是否存在"有效期至"标签
            for block in text_blocks:
                normalized = block.text.strip().replace("：", "").replace(":", "")
                if any(normalized.startswith(label) for label in VALID_UNTIL_EXACT_LABELS):
                    # 合法的单止日证件，免于无效复核
                    missing_fields = []
                    break

        if missing_fields:
            return True, f"缺失字段: {', '.join(missing_fields[:3])}"

        # 检查操作项目是否完整
        op_item = fields.get("操作项目")
        if op_item and "处安装" in op_item and "高" not in op_item:
            return True, "操作项目疑似截字"

        return False, ""

    @staticmethod
    def _estimate_front_card_roi(
        text_blocks: List[TextBlock],
        fields: FieldDict,
        image_width: int,
        image_height: int,
    ) -> Optional[Tuple[float, float, float, float]]:
        """
        估算正面卡片区域（像素坐标）。

        基于已识别字段的文本块位置，推算卡片边界并扩展。
        """
        # 收集已确认字段对应的文本块
        field_blocks: List[TextBlock] = []

        # 简单策略：收集所有文本块中与已知字段值匹配的块
        known_values = {v for v in fields.values() if v and len(v) >= 2}

        for block in text_blocks:
            # 跳过卡外噪声块，避免备注等干扰 ROI 估算
            if _is_noise_text(block.text):
                continue
            for val in known_values:
                if val in block.text or block.text in val:
                    field_blocks.append(block)
                    break

        if len(field_blocks) < 2:
            return None

        # 计算包围框
        min_x = min(b.x for b in field_blocks)
        min_y = min(b.y for b in field_blocks)
        max_x = max(b.x + b.width for b in field_blocks)
        max_y = max(b.y + b.height for b in field_blocks)

        # 估算行高并扩展边界
        heights = [b.height for b in field_blocks if b.height > 0]
        if not heights:
            return None

        avg_h = sum(heights) / len(heights)

        # 向外扩展
        expand_x = avg_h * 3
        expand_y = avg_h * 2

        roi_x_min = max(0, min_x - expand_x)
        roi_y_min = max(0, min_y - expand_y)
        roi_x_max = min(image_width, max_x + expand_x)
        roi_y_max = min(image_height, max_y + expand_y)

        # 检查区域是否有效
        roi_w = roi_x_max - roi_x_min
        roi_h = roi_y_max - roi_y_min

        if roi_w < 100 or roi_h < 100:
            return None

        # 避免返回接近整页的区域
        if roi_w > image_width * 0.95 and roi_h > image_height * 0.95:
            return None

        return (roi_x_min, roi_y_min, roi_x_max, roi_y_max)

    @staticmethod
    def _merge_fields(
        fields1: FieldDict, fields2: FieldDict
    ) -> FieldDict:
        """
        合并两阶段识别结果。

        优先级：
        1. 两次一致 → 保留
        2. 只有一次取得值且归属明确 → 保留该值
        3. 两次冲突 → 留空（需要额外佐证，当前简化为留空）

        特殊校验：合并后若有效期起止日期均非空但逻辑倒置（start > end），双端置空
        """
        merged = create_empty_fields()

        for key in merged.keys():
            val1 = fields1.get(key)
            val2 = fields2.get(key)

            if val1 == val2:
                # 一致（包括都为 None）
                merged[key] = val1
            elif val1 is not None and val2 is None:
                # 仅第一次取得
                merged[key] = val1
            elif val1 is None and val2 is not None:
                # 仅第二次取得
                merged[key] = val2
            else:
                # 冲突 → 留空（简化策略）
                merged[key] = None

        # 跨阶段有效期起止日期一致性校验
        start_date = merged.get("有效期开始日期")
        end_date = merged.get("有效期截止日期")
        if start_date is not None and end_date is not None:
            if start_date > end_date:
                # 逻辑倒置，双端置空
                merged["有效期开始日期"] = None
                merged["有效期截止日期"] = None

        return merged


# 全局实例
_two_stage_processor = TwoStageProcessor()


def process_with_two_stage_review(
    text_blocks: List[TextBlock],
    image_width: int,
    image_height: int,
    ocr_engine,
    file_ext: str,
    file_content: bytes,
) -> Tuple[FieldDict, List[TextBlock], str]:
    """两阶段识别与复核的入口函数"""
    return _two_stage_processor.process_with_review(
        text_blocks, image_width, image_height, ocr_engine, file_ext, file_content
    )
