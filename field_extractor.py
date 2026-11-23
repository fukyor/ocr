"""
字段提取模块

依据特种作业操作证正面卡内两个局部区域（人像右侧、卡片底部）的无量纲相对几何关系，
定位并提取固定七个字段。所有几何判定均基于文字框实际行高自适应缩放，不依赖绝对像素坐标。
"""
import re
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Dict, List, Optional, Sequence, Set, Tuple

from config import (
    AUX_LABEL_ALIASES,
    FIELD_LABEL_ALIASES,
    VALID_UNTIL_EXACT_LABELS,
)
from models import FieldDict, TextBlock, create_empty_fields

# 证号格式：以 T 开头后接 18 位身份证号码（末位可为 X/x）
_CERT_NO_RE = re.compile(r"^T\d{17}[\dXx]$")

# 单日期匹配：支持 YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / YYYY年MM月DD日
_SINGLE_DATE_PATTERN = r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?"
_SINGLE_DATE_RE = re.compile(_SINGLE_DATE_PATTERN)

# 有效期区间匹配：两个完整日期及其中间的区间连接符
_DATE_RANGE_RE = re.compile(
    _SINGLE_DATE_PATTERN
    + r"\s*(?:至|到|[-—–~～]+)\s*"
    + _SINGLE_DATE_PATTERN
)

# 中文姓名正则：仅含汉字与常见间隔号，至少两个汉字
_CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fff]+(?:[·•・][\u4e00-\u9fff]+)*$")

# 明确排除在正面定位及字段值之外的卡外干扰关键词
_NOISE_KEYWORDS: Tuple[str, ...] = (
    "档案编码",
    "有效证书",
    "无效证书",
    "主办单位",
    "承办单位",
    "备案号",
    "监制",
    "法律效力",
    "备注",
    "国家安全生产考试",
    "特种作业操作证",
    "中华人民共和国",
    "mem.gov.cn",
)

# 签发机关在无标签恢复时要求具备的明确结尾关键词
_ORG_SUFFIXES: Tuple[str, ...] = (
    "应急管理局",
    "安全生产监督管理局",
    "应急管理厅",
    "应急管理部",
)

# 区域槽位定义
_UPPER_SLOTS: Dict[str, Tuple[int, int]] = {
    "证号": (0, 0),  # 辅助定位，位于人像右侧第一行左栏上方
    "姓名": (1, 0),
    "作业类别": (1, 1),
    "性别": (2, 0),
    "操作项目": (2, 1),
}

_BOTTOM_SLOTS: Dict[str, Tuple[int, int]] = {
    "初领日期": (1, 0),
    "有效期截止日期": (1, 1),
    "应复审日期": (2, 0),
    "签发机关": (2, 1),
}


@dataclass(frozen=True)
class _LabelToken:
    """标准化后的标签锚点（包含原块索引、语义键、命中的原始标签文本及可能的同行粘连值）。"""

    block_idx: int
    field_key: str  # 标准键或辅助键（证号 / 应复审日期）
    matched_alias: str  # 命中的具体别名，如 '有效期至'、'有效期限'
    block: TextBlock  # 标签包围框
    inline_value: Optional[str] = None  # 若标签与值粘在同块中，此处为剥离出的剩余文本
    inline_block: Optional[TextBlock] = None  # 粘连值对应的估算包围框


@dataclass(frozen=True)
class _RangeEvidence:
    """在页面中识别到的完整有效期区间（可由单个或多个同行碎块组成）。"""

    block_indices: Tuple[int, ...]
    bbox: TextBlock
    start_date: str
    end_date: str


@dataclass(frozen=True)
class _RegionAnchor:
    """用于区域确认的统一证据节点。"""

    slot_key: str  # 槽位字段名（同一字段只计作一项证据）
    row: int  # 0=证号, 1=第一行, 2=第二行
    col: int  # 0=左栏, 1=右栏
    bbox: TextBlock
    is_value_anchor: bool = False  # True 表示该节点来自值行（证号值或有效期区间），而非标签行
    label_token: Optional[_LabelToken] = None
    range_evidence: Optional[_RangeEvidence] = None


def _normalize_label_text(text: str) -> str:
    """去除所有空白和尾部冒号，用于标签匹配。"""
    cleaned = re.sub(r"\s+", "", text)
    return cleaned.rstrip(":：")


def _is_noise_text(text: str) -> bool:
    """判断文本是否属于明确的卡外页眉/背面/备注/页脚干扰。"""
    compact = re.sub(r"\s+", "", text)
    return any(keyword in compact for keyword in _NOISE_KEYWORDS)


def _validate_calendar_date(year: int, month: int, day: int) -> Optional[str]:
    """用日历规则严格校验日期并格式化为 YYYY-MM-DD。"""
    if not (1900 <= year <= 2100):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_single_date(text: str) -> Optional[str]:
    """
    从字符串中解析唯一的合法单日期（YYYY-MM-DD）。
    若包含多个日期、包含区间连接符、或存在非法日期（如 2031-55-12），返回 None。
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    # 若明显包含区间关键词或类似“至/到”连接两个日期片段，拒绝作为单日期
    if "至" in compact or "到" in compact:
        return None

    matches = list(_SINGLE_DATE_RE.finditer(compact))
    if len(matches) != 1:
        return None

    m = matches[0]
    # 拒绝在日期之外混杂过多无关长文本（允许尾部如 "前" 等极短字符前先剔除？初领日期不应含“前”）
    rem = (compact[: m.start()] + compact[m.end() :]).strip()
    if rem:
        return None

    return _validate_calendar_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_date_range(text: str) -> Optional[Tuple[str, str]]:
    """
    解析完整有效期区间（两个合法日期及其间的 '至'/区间横线）。
    任一端缺失、非法或截止日期早于起始日期时返回 None。
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None

    match = _DATE_RANGE_RE.fullmatch(compact)
    if match is None:
        return None

    start_iso = _validate_calendar_date(
        int(match.group(1)), int(match.group(2)), int(match.group(3))
    )
    end_iso = _validate_calendar_date(
        int(match.group(4)), int(match.group(5)), int(match.group(6))
    )
    if start_iso is None or end_iso is None:
        return None
    if end_iso < start_iso:
        return None
    return start_iso, end_iso


def _union_bbox(blocks: Sequence[TextBlock], text: str = "") -> TextBlock:
    """计算多个文本块的轴对齐并集包围框。"""
    x_min = min(b.x for b in blocks)
    y_min = min(b.y for b in blocks)
    x_max = max(b.x_max for b in blocks)
    y_max = max(b.y_max for b in blocks)
    avg_conf = sum(b.confidence for b in blocks) / len(blocks)
    return TextBlock(
        text=text or "".join(b.text for b in blocks),
        confidence=avg_conf,
        x=x_min,
        y=y_min,
        width=max(0.0, x_max - x_min),
        height=max(0.0, y_max - y_min),
    )


def _is_same_row(a: TextBlock, b: TextBlock) -> bool:
    """按垂直重叠与中心距离无量纲判断两个文本块是否处于同一视觉行。"""
    # 若两块在水平方向明显重叠且上下错开，属于上下两行（如折行字段），不是同一行
    x_overlap = min(a.x_max, b.x_max) - max(a.x, b.x)
    min_w = max(1.0, min(a.width, b.width))
    min_h = max(1.0, min(a.height, b.height))
    max_h = max(1.0, max(a.height, b.height))
    y_overlap = min(a.y_max, b.y_max) - max(a.y, b.y)

    if x_overlap >= min_w * 0.4 and abs(a.y_center - b.y_center) >= min_h * 0.55:
        return False

    if y_overlap >= min_h * 0.45 and abs(a.y_center - b.y_center) <= max_h * 0.65:
        return True
    return abs(a.y_center - b.y_center) <= min_h * 0.45


def _is_left_aligned(a: TextBlock, b: TextBlock, ref_h: float) -> bool:
    """按无量纲行高判断两个文本块是否左边缘对齐。"""
    return abs(a.x - b.x) <= max(1.8 * ref_h, 0.35 * max(a.width, b.width, 1.0))


def _ref_height(anchors: Sequence[_RegionAnchor]) -> float:
    """计算锚点组的特征行高（中位数）。"""
    heights = [a.bbox.height for a in anchors if a.bbox.height > 0]
    return max(1.0, float(median(heights))) if heights else 16.0


class FieldExtractor:
    """特种作业操作证字段提取器。"""

    def __init__(self) -> None:
        # 汇总所有已知标签别名，按别名长度降序排列以优先匹配更精确的长标签（如“有效期至”优先于“有效期”）
        alias_pairs: List[Tuple[str, str]] = []
        for field_key, aliases in FIELD_LABEL_ALIASES.items():
            for alias in aliases:
                alias_pairs.append((alias, field_key))
        for aux_key, aliases in AUX_LABEL_ALIASES.items():
            for alias in aliases:
                alias_pairs.append((alias, aux_key))

        alias_pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._alias_pairs: Tuple[Tuple[str, str], ...] = tuple(alias_pairs)
        self._exact_alias_map: Dict[str, str] = {
            alias: key for alias, key in alias_pairs
        }

    def extract_fields(
        self,
        text_blocks: List[TextBlock],
        image_width: int,
        image_height: int,
    ) -> FieldDict:
        """
        从整页 OCR 文本块中定位正面两个局部区域并提取固定七个字段。

        Args:
            text_blocks: OCR 文本块列表
            image_width: 整页图像宽度
            image_height: 整页图像高度

        Returns:
            固定七键的字段字典，未确定项为 None
        """
        fields = create_empty_fields()
        if not text_blocks:
            return fields

        # 1. 识别所有标签锚点（含标签与值粘连分离）
        label_tokens = self._find_label_tokens(text_blocks)
        label_block_indices: Set[int] = {tok.block_idx for tok in label_tokens}

        # 2. 识别辅助值证据：证号值（T + 18位）和完整有效期区间（单块或同行碎块）
        cert_no_anchors = self._find_cert_no_anchors(text_blocks, label_tokens)
        range_evidences = self._find_date_range_evidences(text_blocks, label_block_indices)

        # 3. 分别独立确认人像右侧区域和卡片底部区域
        upper_anchors = self._confirm_region(
            slot_layout=_UPPER_SLOTS,
            label_tokens=label_tokens,
            extra_anchors=cert_no_anchors,
        )
        bottom_anchors = self._confirm_region(
            slot_layout=_BOTTOM_SLOTS,
            label_tokens=label_tokens,
            extra_anchors=[
                _RegionAnchor(
                    slot_key="有效期截止日期",
                    row=1,
                    col=1,
                    bbox=ev.bbox,
                    is_value_anchor=True,
                    range_evidence=ev,
                )
                for ev in range_evidences
            ],
        )

        # 4. 在已确认的人像右侧区域提取：姓名、性别、作业类别、操作项目
        if upper_anchors:
            bottom_top_y = (
                min(a.bbox.y for a in bottom_anchors.values())
                if bottom_anchors
                else None
            )
            self._extract_upper_region(
                anchors=upper_anchors,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                bottom_top_y=bottom_top_y,
                fields=fields,
            )

        # 5. 在已确认的卡片底部区域提取：初领日期、有效期截止日期、签发机关
        if bottom_anchors:
            self._extract_bottom_region(
                anchors=bottom_anchors,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                range_evidences=range_evidences,
                fields=fields,
            )

        return fields

    # ------------------------------------------------------------------
    # 第一阶段：标签识别与辅助值证据发现
    # ------------------------------------------------------------------

    def _find_label_tokens(self, text_blocks: List[TextBlock]) -> List[_LabelToken]:
        """扫描所有文本块，识别纯标签或“标签+值”粘连块。"""
        tokens: List[_LabelToken] = []
        for idx, block in enumerate(text_blocks):
            raw_text = block.text.strip()
            if not raw_text or _is_noise_text(raw_text):
                continue

            normalized = _normalize_label_text(raw_text)
            if not normalized:
                continue

            # 情况 A：整块精确匹配已知标签（短标签与长标签均优先走精确匹配）
            if normalized in self._exact_alias_map:
                tokens.append(
                    _LabelToken(
                        block_idx=idx,
                        field_key=self._exact_alias_map[normalized],
                        matched_alias=normalized,
                        block=block,
                    )
                )
                continue

            # 情况 B：标签与值粘连在同一文本块中（例如 "姓名：廖浩庄"、"有效期至2028-08-28"）
            split_token = self._try_split_inline_label(idx, block, raw_text)
            if split_token is not None:
                tokens.append(split_token)

        return tokens

    def _try_split_inline_label(
        self,
        block_idx: int,
        block: TextBlock,
        raw_text: str,
    ) -> Optional[_LabelToken]:
        """尝试从“标签+分隔符/值”的粘连块中分离标签前缀与剩余值。"""
        stripped = raw_text.lstrip()
        for alias, field_key in self._alias_pairs:
            # 允许标签字符间夹带空格，后接可选冒号/空白
            pattern = r"^\s*" + r"\s*".join(re.escape(ch) for ch in alias) + r"\s*([:：]?)\s*(.+)$"
            m = re.match(pattern, stripped)
            if not m:
                continue

            has_colon = bool(m.group(1))
            rest = m.group(2).strip()
            if not rest:
                continue

            # 若没有冒号分隔，要求剩余值符合该字段的合理前缀特征，避免把普通词误拆
            if not has_colon and not self._looks_like_plausible_inline_value(field_key, rest):
                continue

            total_len = max(1, len(stripped))
            prefix_len = max(1, len(stripped) - len(rest))
            label_w = block.width * (prefix_len / total_len)
            val_x = block.x + label_w
            val_w = max(1.0, block.width - label_w)

            label_box = TextBlock(
                text=alias,
                confidence=block.confidence,
                x=block.x,
                y=block.y,
                width=max(1.0, label_w),
                height=block.height,
            )
            inline_box = TextBlock(
                text=rest,
                confidence=block.confidence,
                x=val_x,
                y=block.y,
                width=val_w,
                height=block.height,
            )
            return _LabelToken(
                block_idx=block_idx,
                field_key=field_key,
                matched_alias=alias,
                block=label_box,
                inline_value=rest,
                inline_block=inline_box,
            )

        return None

    @staticmethod
    def _looks_like_plausible_inline_value(field_key: str, rest: str) -> bool:
        """无冒号粘连拆分时的合理性守卫，防止普通正文被误判为标签+值。"""
        compact = re.sub(r"\s+", "", rest)
        if field_key == "证号":
            return bool(_CERT_NO_RE.match(compact))
        if field_key == "性别":
            return compact in ("男", "女")
        if field_key == "姓名":
            return bool(_CHINESE_NAME_RE.match(compact)) and 2 <= len(compact) <= 8
        if field_key in ("初领日期", "应复审日期"):
            return bool(_SINGLE_DATE_RE.search(compact))
        if field_key == "有效期截止日期":
            return bool(_SINGLE_DATE_RE.search(compact))
        if field_key in ("作业类别", "操作项目", "签发机关"):
            return len(compact) >= 2 and not _is_noise_text(compact)
        return False

    @staticmethod
    def _find_cert_no_anchors(
        text_blocks: List[TextBlock],
        label_tokens: List[_LabelToken],
    ) -> List[_RegionAnchor]:
        """查找 T + 18 位身份证号格式的证号值作为人像右侧区域的辅助锚点。"""
        anchors: List[_RegionAnchor] = []
        seen_indices: Set[int] = set()

        for tok in label_tokens:
            if tok.field_key == "证号" and tok.inline_value:
                compact = re.sub(r"\s+", "", tok.inline_value)
                if _CERT_NO_RE.match(compact):
                    anchors.append(
                        _RegionAnchor(
                            slot_key="证号",
                            row=0,
                            col=0,
                            bbox=tok.block,
                            is_value_anchor=True,
                        )
                    )
                    seen_indices.add(tok.block_idx)

        for idx, block in enumerate(text_blocks):
            if idx in seen_indices:
                continue
            compact = re.sub(r"\s+", "", block.text)
            if _CERT_NO_RE.match(compact):
                anchors.append(
                    _RegionAnchor(
                        slot_key="证号",
                        row=0,
                        col=0,
                        bbox=block,
                        is_value_anchor=True,
                    )
                )
        return anchors

    @staticmethod
    def _find_date_range_evidences(
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
    ) -> List[_RangeEvidence]:
        """
        在整页文本块中查找所有完整有效期区间（支持单个文本块或同行相邻碎块组合）。
        """
        evidences: List[_RangeEvidence] = []
        used_single: Set[int] = set()

        # 1. 先检查单个文本块直接构成完整有效期区间
        for idx, block in enumerate(text_blocks):
            if idx in label_block_indices or _is_noise_text(block.text):
                continue
            parsed = _parse_date_range(block.text)
            if parsed is not None:
                evidences.append(
                    _RangeEvidence(
                        block_indices=(idx,),
                        bbox=block,
                        start_date=parsed[0],
                        end_date=parsed[1],
                    )
                )
                used_single.add(idx)

        # 2. 再检查同行相邻的 2~3 个文本块拼接构成完整有效期区间（如 ["2022-08-29", "至", "2028-08-28"]）
        non_label_items = [
            (idx, b)
            for idx, b in enumerate(text_blocks)
            if idx not in label_block_indices
            and idx not in used_single
            and not _is_noise_text(b.text)
        ]

        # 按视觉行分组
        rows: List[List[Tuple[int, TextBlock]]] = []
        for idx, block in sorted(non_label_items, key=lambda item: (item[1].y_center, item[1].x)):
            placed = False
            for row in rows:
                if _is_same_row(row[0][1], block):
                    row.append((idx, block))
                    placed = True
                    break
            if not placed:
                rows.append([(idx, block)])

        for row in rows:
            row.sort(key=lambda item: item[1].x)
            n = len(row)
            for start_i in range(n):
                for span in (2, 3):
                    end_i = start_i + span
                    if end_i > n:
                        continue
                    chunk = row[start_i:end_i]
                    blocks = [item[1] for item in chunk]
                    avg_h = max(1.0, sum(b.height for b in blocks) / len(blocks))

                    # 相邻碎块之间不能有过大水平间隙
                    contiguous = True
                    for left_b, right_b in zip(blocks, blocks[1:]):
                        gap = right_b.x - left_b.x_max
                        if gap < -0.6 * avg_h or gap > 2.2 * avg_h:
                            contiguous = False
                            break
                    if not contiguous:
                        continue

                    combined_text = "".join(b.text.strip() for b in blocks)
                    parsed = _parse_date_range(combined_text)
                    if parsed is not None:
                        evidences.append(
                            _RangeEvidence(
                                block_indices=tuple(item[0] for item in chunk),
                                bbox=_union_bbox(blocks, combined_text),
                                start_date=parsed[0],
                                end_date=parsed[1],
                            )
                        )

        return evidences

    # ------------------------------------------------------------------
    # 第二阶段：无量纲几何一致性校验与双区域独立确认
    # ------------------------------------------------------------------

    def _confirm_region(
        self,
        slot_layout: Dict[str, Tuple[int, int]],
        label_tokens: List[_LabelToken],
        extra_anchors: List[_RegionAnchor],
    ) -> Dict[str, _RegionAnchor]:
        """
        确认单个正面局部区域（人像右侧或卡片底部）。

        规则：
        - 必须包含至少 2 个不同槽位的证据（同一字段的标签和值仅计作 1 项证据）；
        - 任意一对不同槽位证据必须满足无量纲相对行/列几何关系；
        - 存在多组候选时，选取包含不同槽位最多且几何紧凑度最佳的一组。
        """
        candidates: List[_RegionAnchor] = []
        for tok in label_tokens:
            if tok.field_key in slot_layout:
                row, col = slot_layout[tok.field_key]
                candidates.append(
                    _RegionAnchor(
                        slot_key=tok.field_key,
                        row=row,
                        col=col,
                        bbox=tok.block,
                        is_value_anchor=False,
                        label_token=tok,
                    )
                )
        candidates.extend(extra_anchors)

        if len({c.slot_key for c in candidates}) < 2:
            return {}

        best_group: Dict[str, _RegionAnchor] = {}
        best_score: Tuple[int, int, float] = (0, 0, -1e9)

        # 以每个候选作为种子，贪心聚合所有与其几何兼容的不同槽位候选
        for seed in candidates:
            group: Dict[str, _RegionAnchor] = {seed.slot_key: seed}

            # 按是否标签优先排序（同一槽位同时有标签与值证据时，先放标签，但有效期区间另行关联）
            ordered = sorted(
                candidates,
                key=lambda c: (c.is_value_anchor, abs(c.bbox.y - seed.bbox.y) + abs(c.bbox.x - seed.bbox.x)),
            )

            for cand in ordered:
                if cand.slot_key == seed.slot_key:
                    continue
                if cand.slot_key in group:
                    continue

                if all(self._anchors_compatible(existing, cand) for existing in group.values()):
                    group[cand.slot_key] = cand

            if len(group) < 2:
                continue

            # 统计覆盖的标签数、总槽位数及紧凑度
            label_count = sum(1 for a in group.values() if not a.is_value_anchor)
            span_score = -self._group_dispersion(list(group.values()))
            score = (len(group), label_count, span_score)
            if score > best_score:
                best_score = score
                best_group = group

        return best_group

    @staticmethod
    def _anchors_compatible(a: _RegionAnchor, b: _RegionAnchor) -> bool:
        """判断同一区域内两个不同槽位证据的相对几何位置是否符合卡内结构。"""
        if a.slot_key == b.slot_key:
            return False

        # 尺度参考采用两框高度均值，完全具备缩放与平移不变性
        ref_h = max(1.0, (a.bbox.height + b.bbox.height) / 2.0)

        # 同一槽位行高差异过大时拒绝（例如把大号标题和卡内小字强行配对）
        ratio_h = max(a.bbox.height, b.bbox.height) / max(1.0, min(a.bbox.height, b.bbox.height))
        if ratio_h > 2.8:
            return False

        # 情形 1：同一行（左栏 vs 右栏）
        if a.row == b.row and a.col != b.col:
            left_a, right_a = (a, b) if a.col < b.col else (b, a)
            # 若一侧是标签行、另一侧是值行（如初领日期标签 vs 有效期区间值），垂直偏移略大但仍紧邻
            if left_a.is_value_anchor == right_a.is_value_anchor:
                if not _is_same_row(left_a.bbox, right_a.bbox):
                    return False
            else:
                label_a = left_a if not left_a.is_value_anchor else right_a
                val_a = right_a if not left_a.is_value_anchor else left_a
                dy_lv = val_a.bbox.y - label_a.bbox.y
                if not (-0.5 * ref_h <= dy_lv <= 2.4 * ref_h):
                    return False

            dx = right_a.bbox.x - left_a.bbox.x
            return (
                2.2 * ref_h <= dx <= 22.0 * ref_h
                and right_a.bbox.x >= left_a.bbox.x_max - 0.3 * ref_h
            )

        # 情形 2：同一列（上下行）
        if a.col == b.col and a.row != b.row:
            top_a, bot_a = (a, b) if a.row < b.row else (b, a)
            if not _is_left_aligned(top_a.bbox, bot_a.bbox, ref_h):
                return False

            row_diff = bot_a.row - top_a.row
            dy = bot_a.bbox.y - top_a.bbox.y

            if top_a.row == 0:
                # 证号（标签或证号值）在姓名/性别正上方
                min_dy = (0.6 if top_a.is_value_anchor else 1.2) * ref_h
                max_dy = (5.0 if row_diff == 1 else 9.5) * ref_h
                return min_dy <= dy <= max_dy

            # row 1 -> row 2
            if top_a.is_value_anchor and not bot_a.is_value_anchor:
                # 例如 Row1 的有效期区间值 -> Row2 的签发机关标签
                return 0.5 * ref_h <= dy <= 4.5 * ref_h
            return 1.2 * ref_h <= dy <= 7.5 * ref_h

        # 情形 3：对角关系（不同行且不同列）
        top_a, bot_a = (a, b) if a.row < b.row else (b, a)
        left_a, right_a = (a, b) if a.col < b.col else (b, a)

        dx = right_a.bbox.x - left_a.bbox.x
        dy = bot_a.bbox.y - top_a.bbox.y
        row_diff = bot_a.row - top_a.row

        if not (2.2 * ref_h <= dx <= 22.0 * ref_h):
            return False

        if top_a.row == 0:
            min_dy = (0.6 if top_a.is_value_anchor else 1.2) * ref_h
            max_dy = (5.5 if row_diff == 1 else 9.5) * ref_h
            return min_dy <= dy <= max_dy

        if top_a.is_value_anchor and not bot_a.is_value_anchor:
            return 0.5 * ref_h <= dy <= 4.5 * ref_h
        return 1.2 * ref_h <= dy <= 7.5 * ref_h

    @staticmethod
    def _group_dispersion(anchors: Sequence[_RegionAnchor]) -> float:
        """无量纲紧凑度度量，用于多组候选时优选更紧密的真实卡片区域。"""
        ref_h = _ref_height(anchors)
        xs = [a.bbox.x for a in anchors]
        ys = [a.bbox.y for a in anchors]
        return ((max(xs) - min(xs)) + (max(ys) - min(ys))) / ref_h

    # ------------------------------------------------------------------
    # 第三阶段：区域内行列边界推导与字段组装
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_column_bounds(
        anchors: Dict[str, _RegionAnchor],
        ref_h: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        确定本区域内左列起点与右列起点（仅在本区域内借用另一行的已确认列起点）。
        """
        left_xs = [a.bbox.x for a in anchors.values() if a.col == 0]
        right_xs = [a.bbox.x for a in anchors.values() if a.col == 1]

        left_col_x = min(left_xs) if left_xs else None
        right_col_x = min(right_xs) if right_xs else None

        # 安全核查：两列起点必须保持合理的左右顺序
        if left_col_x is not None and right_col_x is not None:
            if right_col_x - left_col_x < 1.8 * ref_h:
                return left_col_x, None

        return left_col_x, right_col_x

    def _extract_upper_region(
        self,
        anchors: Dict[str, _RegionAnchor],
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
        bottom_top_y: Optional[float],
        fields: FieldDict,
    ) -> None:
        """在已确认的人像右侧区域提取：姓名、作业类别、性别、操作项目。"""
        ref_h = _ref_height(list(anchors.values()))
        left_col_x, right_col_x = self._resolve_column_bounds(anchors, ref_h)

        row1_anchors = [a for a in anchors.values() if a.row == 1 and not a.is_value_anchor]
        row2_anchors = [a for a in anchors.values() if a.row == 2 and not a.is_value_anchor]

        row2_label_y = min(a.bbox.y for a in row2_anchors) if row2_anchors else None

        # 计算各行向下的垂直边界
        row1_y_limit = row2_label_y - 0.1 * ref_h if row2_label_y is not None else None
        if row1_y_limit is None and bottom_top_y is not None:
            row1_y_limit = bottom_top_y - 0.1 * ref_h

        row2_y_limit = bottom_top_y - 0.1 * ref_h if bottom_top_y is not None else None

        # 1. 姓名（必须有本字段标签）
        name_anchor = anchors.get("姓名")
        if name_anchor is not None and name_anchor.label_token is not None:
            raw_name = self._collect_labeled_field_value(
                token=name_anchor.label_token,
                col_start_x=name_anchor.bbox.x,
                col_right_limit=right_col_x,
                y_bottom_limit=row1_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=False,
                is_left_col=True,
            )
            fields["姓名"] = self._validate_name(raw_name)

        # 2. 作业类别（必须有本字段标签）
        cat_anchor = anchors.get("作业类别")
        if cat_anchor is not None and cat_anchor.label_token is not None:
            raw_cat = self._collect_labeled_field_value(
                token=cat_anchor.label_token,
                col_start_x=cat_anchor.bbox.x,
                col_right_limit=None,
                y_bottom_limit=row1_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=False,
                is_left_col=False,
            )
            fields["作业类别"] = self._validate_text_field(raw_cat)

        # 3. 性别（有标签直接按栏取；标签缺失时仅在左列起点已确认的 Row2 左栏取唯一男/女）
        gender_anchor = anchors.get("性别")
        if gender_anchor is not None and gender_anchor.label_token is not None:
            raw_gender = self._collect_labeled_field_value(
                token=gender_anchor.label_token,
                col_start_x=gender_anchor.bbox.x,
                col_right_limit=right_col_x,
                y_bottom_limit=row2_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=False,
                is_left_col=True,
            )
            fields["性别"] = self._validate_gender(raw_gender)
        elif left_col_x is not None:
            fields["性别"] = self._recover_unlabeled_gender(
                left_col_x=left_col_x,
                right_col_x=right_col_x,
                row1_anchors=row1_anchors,
                row2_anchors=row2_anchors,
                y_bottom_limit=row2_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
            )

        # 4. 操作项目（必须有本字段标签，允许接续同栏紧邻且左对齐的下一行）
        item_anchor = anchors.get("操作项目")
        if item_anchor is not None and item_anchor.label_token is not None:
            raw_item = self._collect_labeled_field_value(
                token=item_anchor.label_token,
                col_start_x=item_anchor.bbox.x,
                col_right_limit=None,
                y_bottom_limit=row2_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=True,
                is_left_col=False,
            )
            fields["操作项目"] = self._validate_text_field(raw_item)

    def _extract_bottom_region(
        self,
        anchors: Dict[str, _RegionAnchor],
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
        range_evidences: List[_RangeEvidence],
        fields: FieldDict,
    ) -> None:
        """在已确认的卡片底部区域提取：初领日期、有效期截止日期、签发机关。"""
        ref_h = _ref_height(list(anchors.values()))
        left_col_x, right_col_x = self._resolve_column_bounds(anchors, ref_h)

        row2_anchors = [a for a in anchors.values() if a.row == 2 and not a.is_value_anchor]
        row2_label_y = min(a.bbox.y for a in row2_anchors) if row2_anchors else None
        row1_y_limit = row2_label_y - 0.1 * ref_h if row2_label_y is not None else None

        # 1. 有效期截止日期（先解析，因为已确认的有效期区间值行可辅助初领日期标签漏检定位）
        validity_anchor = anchors.get("有效期截止日期")
        confirmed_range_bbox: Optional[TextBlock] = None

        if validity_anchor is not None:
            if validity_anchor.label_token is not None:
                raw_validity, used_blocks = self._collect_labeled_field_value_with_blocks(
                    token=validity_anchor.label_token,
                    col_start_x=validity_anchor.bbox.x,
                    col_right_limit=None,
                    y_bottom_limit=row1_y_limit,
                    ref_h=ref_h,
                    text_blocks=text_blocks,
                    label_block_indices=label_block_indices,
                    allow_multiline=False,
                    is_left_col=False,
                )
                if raw_validity is not None:
                    range_parsed = _parse_date_range(raw_validity)
                    if range_parsed is not None:
                        fields["有效期截止日期"] = range_parsed[1]
                        if used_blocks:
                            confirmed_range_bbox = _union_bbox(used_blocks, raw_validity)
                    elif validity_anchor.label_token.matched_alias in VALID_UNTIL_EXACT_LABELS:
                        # 仅明确的“有效期至”标签可直接接受单个合法日期
                        single_iso = _parse_single_date(raw_validity)
                        if single_iso is not None:
                            fields["有效期截止日期"] = single_iso
                            if used_blocks:
                                confirmed_range_bbox = _union_bbox(used_blocks, raw_validity)
            elif validity_anchor.range_evidence is not None:
                # 标签漏检但完整有效期区间直接作为区域证据被确认
                fields["有效期截止日期"] = validity_anchor.range_evidence.end_date
                confirmed_range_bbox = validity_anchor.range_evidence.bbox
        elif right_col_x is not None:
            # 有效期限标签漏检时，若右列起点已由签发机关确认，检查 Row1 右栏是否存在匹配的完整有效期区间
            matched_ev = self._find_range_in_row1_right(
                right_col_x=right_col_x,
                anchors=anchors,
                row1_y_limit=row1_y_limit,
                ref_h=ref_h,
                range_evidences=range_evidences,
            )
            if matched_ev is not None:
                fields["有效期截止日期"] = matched_ev.end_date
                confirmed_range_bbox = matched_ev.bbox

        # 2. 初领日期
        first_anchor = anchors.get("初领日期")
        if first_anchor is not None and first_anchor.label_token is not None:
            raw_first = self._collect_labeled_field_value(
                token=first_anchor.label_token,
                col_start_x=first_anchor.bbox.x,
                col_right_limit=right_col_x,
                y_bottom_limit=row1_y_limit,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=False,
                is_left_col=True,
            )
            fields["初领日期"] = _parse_single_date(raw_first) if raw_first else None
        elif confirmed_range_bbox is not None:
            # 初领日期标签漏检：要求候选与完整有效期区间同一行、位于其左侧，且为唯一合法单日期
            fields["初领日期"] = self._recover_unlabeled_first_issue_date(
                range_bbox=confirmed_range_bbox,
                left_col_x=left_col_x,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
            )

        # 3. 签发机关（有标签按栏取，允许换行；无标签时仅在右列起点已确认的底部第二行右栏取唯一可辨认机关名）
        org_anchor = anchors.get("签发机关")
        if org_anchor is not None and org_anchor.label_token is not None:
            raw_org = self._collect_labeled_field_value(
                token=org_anchor.label_token,
                col_start_x=org_anchor.bbox.x,
                col_right_limit=None,
                y_bottom_limit=None,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
                allow_multiline=True,
                is_left_col=False,
            )
            fields["签发机关"] = self._validate_org(raw_org, require_suffix=False)
        elif right_col_x is not None:
            fields["签发机关"] = self._recover_unlabeled_org(
                right_col_x=right_col_x,
                anchors=anchors,
                confirmed_range_bbox=confirmed_range_bbox,
                ref_h=ref_h,
                text_blocks=text_blocks,
                label_block_indices=label_block_indices,
            )

    # ------------------------------------------------------------------
    # 第四阶段：按栏收集候选块、同行碎块合并、跨行续接
    # ------------------------------------------------------------------

    def _collect_labeled_field_value(
        self,
        token: _LabelToken,
        col_start_x: float,
        col_right_limit: Optional[float],
        y_bottom_limit: Optional[float],
        ref_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
        allow_multiline: bool,
        is_left_col: bool,
    ) -> Optional[str]:
        """收集带标签字段的组装值。"""
        val, _ = self._collect_labeled_field_value_with_blocks(
            token=token,
            col_start_x=col_start_x,
            col_right_limit=col_right_limit,
            y_bottom_limit=y_bottom_limit,
            ref_h=ref_h,
            text_blocks=text_blocks,
            label_block_indices=label_block_indices,
            allow_multiline=allow_multiline,
            is_left_col=is_left_col,
        )
        return val

    def _collect_labeled_field_value_with_blocks(
        self,
        token: _LabelToken,
        col_start_x: float,
        col_right_limit: Optional[float],
        y_bottom_limit: Optional[float],
        ref_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
        allow_multiline: bool,
        is_left_col: bool,
    ) -> Tuple[Optional[str], List[TextBlock]]:
        """
        在指定标签的所属栏位内收集并组装字段值，同时返回所采纳的文本块列表。
        """
        label_h = max(1.0, token.block.height, ref_h)

        # 情形 A：标签与值粘在同一文本块中，先剥离标签前缀，再结合同栏碎块处理
        if token.inline_value is not None and token.inline_block is not None:
            first_row_blocks = [token.inline_block]
            # 收集与粘连块同一行、位于其右侧且在本栏边界内的同行碎块
            for idx, b in enumerate(text_blocks):
                if idx == token.block_idx or idx in label_block_indices or _is_noise_text(b.text):
                    continue
                if not _is_same_row(token.inline_block, b):
                    continue
                if b.x < token.inline_block.x_max - 0.4 * label_h:
                    continue
                if col_right_limit is not None and b.x >= col_right_limit - 0.3 * label_h:
                    continue
                if col_right_limit is not None and b.x_max > col_right_limit + 0.6 * label_h:
                    return None, []
                first_row_blocks.append(b)

            first_row_blocks.sort(key=lambda b: b.x)
            selected_first = self._take_contiguous_row_blocks(
                row_blocks=first_row_blocks,
                col_start_x=token.inline_block.x,
                label_h=label_h,
                has_right_limit=col_right_limit is not None,
                is_left_col=is_left_col,
            )
            if selected_first is None:
                return None, []

            all_selected = list(selected_first)
            if allow_multiline:
                cont = self._collect_continuation_rows(
                    prev_row=selected_first,
                    col_start_x=col_start_x,
                    col_right_limit=col_right_limit,
                    y_bottom_limit=y_bottom_limit,
                    label_h=label_h,
                    text_blocks=text_blocks,
                    label_block_indices=label_block_indices,
                    used_indices={token.block_idx},
                )
                all_selected.extend(cont)
            return "".join(b.text.strip() for b in all_selected), all_selected

        # 情形 B：常规结构，字段值位于标签下方
        y_top = token.block.y_max - 0.45 * label_h
        max_vertical_span = (5.2 if allow_multiline else 2.6) * label_h
        effective_y_bottom = token.block.y_max + max_vertical_span
        if y_bottom_limit is not None:
            effective_y_bottom = min(effective_y_bottom, y_bottom_limit)

        if effective_y_bottom <= y_top:
            return None, []

        col_x_min = col_start_x - 1.2 * label_h
        candidate_blocks: List[TextBlock] = []
        crossing_blocks: List[TextBlock] = []

        for idx, b in enumerate(text_blocks):
            if idx == token.block_idx or idx in label_block_indices or _is_noise_text(b.text):
                continue

            # 垂直过滤：块必须在标签下方，且中心位于取值窗口内
            if b.y < y_top or b.y_center < token.block.y_center + 0.3 * label_h:
                continue
            if b.y >= effective_y_bottom:
                continue

            # 水平过滤
            if b.x_max <= col_x_min:
                continue

            if col_right_limit is not None:
                # 位于右侧栏中的块跳过
                if b.x >= col_right_limit - 0.35 * label_h:
                    continue
                # 记录可能跨栏的块，待行聚类后再确认是否属于本字段有效行
                if b.x < col_right_limit - 0.35 * label_h and b.x_max > col_right_limit + 0.8 * label_h:
                    crossing_blocks.append(b)
                    continue

            # 起点明显在左栏左侧但向右侵入本栏的块，记录为跨栏候选而非直接一票否决
            if b.x < col_x_min:
                if b.x_max > col_start_x + 0.5 * label_h:
                    crossing_blocks.append(b)
                continue

            candidate_blocks.append(b)

        if not candidate_blocks and not crossing_blocks:
            return None, []

        # 若紧接标签下方的第一行就是跨栏块，直接判定歧义留空
        all_window_rows = self._cluster_rows(candidate_blocks + crossing_blocks)
        if not all_window_rows:
            return None, []

        first_row_all = all_window_rows[0]
        if first_row_all[0].y - token.block.y_max > 2.0 * label_h:
            return None, []

        crossing_set = set(id(b) for b in crossing_blocks)
        if any(id(b) in crossing_set for b in first_row_all):
            return None, []

        selected_first = self._take_contiguous_row_blocks(
            row_blocks=first_row_all,
            col_start_x=col_start_x,
            label_h=label_h,
            has_right_limit=col_right_limit is not None,
            is_left_col=is_left_col,
        )
        if not selected_first:
            return None, []

        all_selected = list(selected_first)
        if allow_multiline and len(all_window_rows) > 1:
            prev_row = selected_first
            for next_row in all_window_rows[1:3]:
                prev_y_max = max(b.y_max for b in prev_row)
                next_y_min = min(b.y for b in next_row)
                # 仅接续紧邻且左对齐的下一行；遇到远处的其他区域直接停止
                if next_y_min - prev_y_max > 1.2 * label_h:
                    break
                if any(id(b) in crossing_set for b in next_row):
                    break
                if abs(next_row[0].x - col_start_x) > 1.8 * label_h:
                    break
                selected_next = self._take_contiguous_row_blocks(
                    row_blocks=next_row,
                    col_start_x=col_start_x,
                    label_h=label_h,
                    has_right_limit=col_right_limit is not None,
                    is_left_col=is_left_col,
                )
                if not selected_next:
                    break
                all_selected.extend(selected_next)
                prev_row = selected_next

        return "".join(b.text.strip() for b in all_selected), all_selected

    @staticmethod
    def _take_contiguous_row_blocks(
        row_blocks: List[TextBlock],
        col_start_x: float,
        label_h: float,
        has_right_limit: bool,
        is_left_col: bool,
    ) -> Optional[List[TextBlock]]:
        """
        在单行候选块中，从与列起点左对齐的首块开始向右收取连续同行碎块。
        遇到明显水平间隔（> 2.0 * label_h）时截断。
        当左栏没有右列边界时，若首块右侧紧邻（<= 2.0 * label_h）又无法判定归属，或存在多处可疑无界碎片，谨慎处理。
        """
        ordered = sorted(row_blocks, key=lambda b: b.x)
        if not ordered:
            return None

        # 首块必须与本栏起点左对齐
        first = ordered[0]
        if abs(first.x - col_start_x) > 1.8 * label_h:
            return None

        selected = [first]
        for nxt in ordered[1:]:
            gap = nxt.x - selected[-1].x_max
            # 明显水平间隔表示已进入右侧其他列或背面卡片区域，立即停止收取
            if gap > 2.0 * label_h:
                # 若是左栏且完全没有已知右列边界，但右侧在较近的右栏常见位置（<= 14 * label_h）出现另一候选，
                # 仅当左侧已收块与右侧候选存在明显大间隔时才安全停止；但若无法确定右界导致列界不明（间隔在 2h~3.2h 的模糊带），留空！
                if is_left_col and not has_right_limit and gap <= 3.2 * label_h:
                    return None
                break
            selected.append(nxt)

        return selected

    def _collect_continuation_rows(
        self,
        prev_row: List[TextBlock],
        col_start_x: float,
        col_right_limit: Optional[float],
        y_bottom_limit: Optional[float],
        label_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
        used_indices: Set[int],
    ) -> List[TextBlock]:
        """收集粘连块下方的多行续接行。"""
        prev_y_max = max(b.y_max for b in prev_row)
        max_y = prev_y_max + 3.5 * label_h
        if y_bottom_limit is not None:
            max_y = min(max_y, y_bottom_limit)

        cands: List[TextBlock] = []
        for idx, b in enumerate(text_blocks):
            if idx in used_indices or idx in label_block_indices or _is_noise_text(b.text):
                continue
            if b.y < prev_y_max - 0.3 * label_h or b.y >= max_y:
                continue
            if b.x < col_start_x - 1.2 * label_h:
                continue
            if col_right_limit is not None and b.x >= col_right_limit - 0.35 * label_h:
                continue
            cands.append(b)

        rows = self._cluster_rows(cands)
        collected: List[TextBlock] = []
        cur_y_max = prev_y_max
        for row in rows[:2]:
            if row[0].y - cur_y_max > 1.2 * label_h:
                break
            if abs(row[0].x - col_start_x) > 1.8 * label_h:
                break
            picked = self._take_contiguous_row_blocks(
                row_blocks=row,
                col_start_x=col_start_x,
                label_h=label_h,
                has_right_limit=col_right_limit is not None,
                is_left_col=col_right_limit is not None,
            )
            if not picked:
                break
            collected.extend(picked)
            cur_y_max = max(b.y_max for b in picked)
        return collected

    @staticmethod
    def _cluster_rows(blocks: Sequence[TextBlock]) -> List[List[TextBlock]]:
        """将文本块按视觉行聚类，行间从上到下、行内从左到右排序。"""
        if not blocks:
            return []
        rows: List[List[TextBlock]] = []
        for b in sorted(blocks, key=lambda item: (item.y_center, item.x)):
            placed = False
            for row in rows:
                if _is_same_row(row[0], b):
                    row.append(b)
                    placed = True
                    break
            if not placed:
                rows.append([b])
        for row in rows:
            row.sort(key=lambda item: item.x)
        rows.sort(key=lambda row: min(item.y for item in row))
        return rows

    # ------------------------------------------------------------------
    # 第五阶段：受限的标签漏检恢复（文档 3.5.3）
    # ------------------------------------------------------------------

    def _recover_unlabeled_gender(
        self,
        left_col_x: float,
        right_col_x: Optional[float],
        row1_anchors: List[_RegionAnchor],
        row2_anchors: List[_RegionAnchor],
        y_bottom_limit: Optional[float],
        ref_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
    ) -> Optional[str]:
        """
        性别标签漏检时的受限恢复：
        仅在人像右侧区域第二行左栏取唯一的“男”或“女”。
        """
        # 必须能明确锁定第二行的垂直范围（由同行右标签操作项目给出，或由第一行标签向下推演）
        if row2_anchors:
            r2_label_y = min(a.bbox.y for a in row2_anchors)
            y_min = r2_label_y + 0.1 * ref_h
            y_max = r2_label_y + 3.2 * ref_h
        elif row1_anchors:
            r1_label_y = min(a.bbox.y for a in row1_anchors)
            y_min = r1_label_y + 2.2 * ref_h
            y_max = r1_label_y + 6.5 * ref_h
        else:
            return None

        if y_bottom_limit is not None:
            y_max = min(y_max, y_bottom_limit)

        candidates: List[str] = []
        for idx, b in enumerate(text_blocks):
            if idx in label_block_indices or _is_noise_text(b.text):
                continue
            if not (y_min <= b.y_center <= y_max):
                continue
            if abs(b.x - left_col_x) > 1.8 * ref_h:
                continue
            if right_col_x is not None and b.x_max > right_col_x - 0.2 * ref_h:
                continue
            g = self._validate_gender(b.text)
            if g is not None:
                candidates.append(g)

        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _find_range_in_row1_right(
        right_col_x: float,
        anchors: Dict[str, _RegionAnchor],
        row1_y_limit: Optional[float],
        ref_h: float,
        range_evidences: List[_RangeEvidence],
    ) -> Optional[_RangeEvidence]:
        """当有效期限标签漏检但右列起点已由签发机关确认时，在底部第一行右栏查找完整有效期区间。"""
        row1_left = anchors.get("初领日期")
        row2_anchors = [a for a in anchors.values() if a.row == 2]

        matches: List[_RangeEvidence] = []
        for ev in range_evidences:
            if abs(ev.bbox.x - right_col_x) > 1.8 * ref_h:
                continue
            if row1_y_limit is not None and ev.bbox.y >= row1_y_limit:
                continue
            if row1_left is not None:
                dy = ev.bbox.y - row1_left.bbox.y
                if not (-0.5 * ref_h <= dy <= 2.5 * ref_h):
                    continue
            elif row2_anchors:
                r2_y = min(a.bbox.y for a in row2_anchors)
                dy = r2_y - ev.bbox.y
                if not (0.5 * ref_h <= dy <= 4.5 * ref_h):
                    continue
            matches.append(ev)

        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _recover_unlabeled_first_issue_date(
        range_bbox: TextBlock,
        left_col_x: Optional[float],
        ref_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
    ) -> Optional[str]:
        """
        初领日期标签漏检时的受限恢复（文档 3.5.3）：
        要求候选与已确认的有效期区间同一行、位于其左侧，且为唯一合法单日期。
        """
        candidates: List[str] = []
        max_left_dist = 18.0 * ref_h

        for idx, b in enumerate(text_blocks):
            if idx in label_block_indices or _is_noise_text(b.text):
                continue
            # 必须与有效期区间同一视觉行
            if not _is_same_row(range_bbox, b):
                continue
            # 必须严格位于有效期区间左侧且不能离得太远
            if b.x_max > range_bbox.x - 0.1 * ref_h:
                continue
            if range_bbox.x - b.x > max_left_dist:
                continue
            # 若底部第二行（应复审日期）已确认左列起点，进一步验证左对齐
            if left_col_x is not None and abs(b.x - left_col_x) > 2.0 * ref_h:
                continue

            iso_date = _parse_single_date(b.text)
            if iso_date is not None:
                candidates.append(iso_date)

        return candidates[0] if len(candidates) == 1 else None

    def _recover_unlabeled_org(
        self,
        right_col_x: float,
        anchors: Dict[str, _RegionAnchor],
        confirmed_range_bbox: Optional[TextBlock],
        ref_h: float,
        text_blocks: List[TextBlock],
        label_block_indices: Set[int],
    ) -> Optional[str]:
        """
        签发机关标签漏检时的受限恢复（文档 3.5.3）：
        仅在底部第二行右栏取唯一、完整且可辨认的机关名称（如以“应急管理局”结尾）。
        """
        review_anchor = anchors.get("应复审日期")
        if review_anchor is not None:
            y_min = review_anchor.bbox.y + 0.1 * ref_h
            y_max = review_anchor.bbox.y + 4.5 * ref_h
        elif confirmed_range_bbox is not None:
            y_min = confirmed_range_bbox.y_max + 0.8 * ref_h
            y_max = confirmed_range_bbox.y_max + 5.5 * ref_h
        else:
            row1_anchors = [a for a in anchors.values() if a.row == 1]
            if not row1_anchors:
                return None
            r1_y = min(a.bbox.y for a in row1_anchors)
            y_min = r1_y + 2.0 * ref_h
            y_max = r1_y + 6.5 * ref_h

        cands: List[TextBlock] = []
        for idx, b in enumerate(text_blocks):
            if idx in label_block_indices or _is_noise_text(b.text):
                continue
            if not (y_min <= b.y_center <= y_max):
                continue
            if abs(b.x - right_col_x) > 1.8 * ref_h:
                continue
            cands.append(b)

        if not cands:
            return None

        rows = self._cluster_rows(cands)
        if not rows:
            return None

        # 合并紧邻左对齐的机关名（允许换行）
        assembled_blocks: List[TextBlock] = []
        prev_y_max: Optional[float] = None
        for row in rows[:2]:
            if prev_y_max is not None and row[0].y - prev_y_max > 1.2 * ref_h:
                break
            picked = self._take_contiguous_row_blocks(
                row_blocks=row,
                col_start_x=right_col_x,
                label_h=ref_h,
                has_right_limit=False,
                is_left_col=False,
            )
            if not picked:
                break
            assembled_blocks.extend(picked)
            prev_y_max = max(b.y_max for b in picked)

        if not assembled_blocks:
            return None

        candidate_text = "".join(b.text.strip() for b in assembled_blocks)
        return self._validate_org(candidate_text, require_suffix=True)

    # ------------------------------------------------------------------
    # 第六阶段：字段级合法性校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(text: Optional[str]) -> Optional[str]:
        """校验中文姓名（允许间隔号，拒绝纯数字、日期或非姓名噪声）。"""
        if not text:
            return None
        cleaned = re.sub(r"\s+", "", text)
        if not (2 <= len(cleaned) <= 20):
            return None
        if _is_noise_text(cleaned):
            return None
        if not _CHINESE_NAME_RE.match(cleaned):
            return None
        # 至少包含两个汉字
        han_count = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
        return cleaned if han_count >= 2 else None

    @staticmethod
    def _validate_gender(text: Optional[str]) -> Optional[str]:
        """仅接受单个“男”或“女”。"""
        if not text:
            return None
        cleaned = re.sub(r"\s+", "", text)
        return cleaned if cleaned in ("男", "女") else None

    @staticmethod
    def _validate_text_field(text: Optional[str]) -> Optional[str]:
        """校验作业类别、操作项目等文本字段，保留原有标点。"""
        if not text:
            return None
        cleaned = text.strip()
        compact = re.sub(r"\s+", "", cleaned)
        if len(compact) < 2:
            return None
        if _is_noise_text(compact):
            return None
        if _CERT_NO_RE.match(compact) or _SINGLE_DATE_RE.fullmatch(compact):
            return None
        return compact

    @staticmethod
    def _validate_org(text: Optional[str], require_suffix: bool) -> Optional[str]:
        """校验签发机关名称。无标签恢复时要求具备明确的机关结尾。"""
        if not text:
            return None
        compact = re.sub(r"\s+", "", text)
        if len(compact) < 4 or _is_noise_text(compact):
            return None
        if _SINGLE_DATE_RE.search(compact) or _CERT_NO_RE.match(compact):
            return None
        if require_suffix:
            if not any(compact.endswith(suffix) for suffix in _ORG_SUFFIXES):
                return None
        return compact


# 供外部模块直接调用的函数入口（契合文档 3.5 节）
_DEFAULT_EXTRACTOR = FieldExtractor()


def extract_fields(
    text_blocks: List[TextBlock],
    image_width: int,
    image_height: int,
) -> FieldDict:
    """从整页 OCR 文本块中提取固定七个字段的标准入口函数。"""
    return _DEFAULT_EXTRACTOR.extract_fields(text_blocks, image_width, image_height)
