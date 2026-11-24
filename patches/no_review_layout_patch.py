"""
2026 新版"无复审"特种作业操作证底栏补丁

针对新版底图取消"应复审日期"、将"签发机关"左移至 (2, 0) 的版式变更，
提供独立的后置补齐逻辑。本补丁严格与原识别逻辑解耦，只补空位，不覆盖原版非空字段。

触发条件（双重门控）：
1. 强正向门控：确认存在新版三槽位底栏（包含至少 2 个槽位，必须含签发机关 (2,0) 标签）
2. 强负向守卫：同一区域不得存在与 Row 1 几何相容的"应复审日期"标签

零误触保证：
- 旧版证件的"签发机关"位于右栏 (2,1)，几何上不可能通过 (2,0) 校验
- 无"签发机关"标签的压缩图/干扰图被正向门控拦截
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from models import FieldDict, TextBlock


# 新版"无复审"底栏三槽位模板（取消应复审日期，签发机关左移）
_NO_REVIEW_BOTTOM_SLOTS = {
    "初领日期": (1, 0),
    "有效期限": (1, 1),  # 逻辑槽位，解析后产出起日与止日
    "签发机关": (2, 0),
}

# 单日期匹配
_SINGLE_DATE_PATTERN = r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?"
_SINGLE_DATE_RE = re.compile(_SINGLE_DATE_PATTERN)

# 有效期区间匹配
_DATE_RANGE_RE = re.compile(
    _SINGLE_DATE_PATTERN
    + r"\s*(?:至|到|[-—–~～]+)\s*"
    + _SINGLE_DATE_PATTERN
)

# 卡外噪声关键词
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

# 签发机关明确结尾关键词
_ORG_SUFFIXES: Tuple[str, ...] = (
    "应急管理局",
    "安全生产监督管理局",
    "应急管理厅",
    "应急管理部",
)

# 旧版底栏应复审日期别名（用于负向守卫）
_REVIEW_DATE_ALIASES: Tuple[str, ...] = (
    "应复审日期",
    "复审日期",
)


@dataclass(frozen=True)
class _Anchor:
    """补丁内部锚点。"""
    field_key: str
    row: int
    col: int
    bbox: TextBlock
    block_idx: int


def _normalize_label(text: str) -> str:
    """标签标准化。"""
    cleaned = re.sub(r"\s+", "", text)
    return cleaned.rstrip(":：")


def _is_noise_text(text: str) -> bool:
    """判断是否为卡外噪声。"""
    compact = re.sub(r"\s+", "", text)
    return any(kw in compact for kw in _NOISE_KEYWORDS)


def _is_same_row(a: TextBlock, b: TextBlock) -> bool:
    """判断两块是否同一视觉行。"""
    min_h = max(1.0, min(a.height, b.height))
    max_h = max(1.0, max(a.height, b.height))
    y_overlap = min(a.y_max, b.y_max) - max(a.y, b.y)

    if y_overlap >= min_h * 0.45 and abs(a.y_center - b.y_center) <= max_h * 0.65:
        return True
    return abs(a.y_center - b.y_center) <= min_h * 0.45


def _is_left_aligned(a: TextBlock, b: TextBlock, ref_h: float) -> bool:
    """判断两块是否左对齐。"""
    return abs(a.x - b.x) <= max(1.8 * ref_h, 0.35 * max(a.width, b.width, 1.0))


def _validate_calendar_date(year: int, month: int, day: int) -> Optional[str]:
    """日历规则校验并格式化为 YYYY-MM-DD。"""
    if not (1900 <= year <= 2100):
        return None
    try:
        from datetime import date
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_single_date(text: str) -> Optional[str]:
    """解析唯一合法单日期。"""
    compact = re.sub(r"\s+", "", text)
    if not compact or "至" in compact or "到" in compact:
        return None

    matches = list(_SINGLE_DATE_RE.finditer(compact))
    if len(matches) != 1:
        return None

    m = matches[0]
    rem = (compact[: m.start()] + compact[m.end() :]).strip()
    if rem:
        return None

    return _validate_calendar_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_date_range(text: str) -> Optional[Tuple[str, str]]:
    """解析完整有效期区间。"""
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


def apply_no_review_layout_patch(
    text_blocks: List[TextBlock],
    fields: FieldDict,
) -> None:
    """
    2026 新版"无复审"底栏补丁入口。

    仅当原版识别结果存在缺失字段时触发，经双重门控确认新版底栏后，
    独立提取缺失项并只补空位（绝不覆盖原版非空字段）。

    Args:
        text_blocks: 整页 OCR 文本块
        fields: 原版识别结果（原地修改）
    """
    try:
        # 快速退出：若初领日期和签发机关都已有值，补丁无需触发
        if fields.get("初领日期") is not None and fields.get("签发机关") is not None:
            return

        # 双重门控
        gate_passed, anchors, ref_h = _check_no_review_layout_gates(text_blocks)
        if not gate_passed or not anchors:
            return

        # 在局部字典中计算待补字段
        patch_fields: Dict[str, Optional[str]] = {}

        # 提取有效期双端点（若原版任一为 None）
        validity_anchor = anchors.get("有效期限")
        validity_block_indices: Set[int] = set()
        confirmed_range_bbox: Optional[TextBlock] = None

        if (fields.get("有效期开始日期") is None or fields.get("有效期截止日期") is None) and validity_anchor is not None:
            start_date, end_date, range_bbox, block_indices = _extract_validity_dates(
                validity_anchor, text_blocks, ref_h
            )
            if start_date is not None:
                patch_fields["有效期开始日期"] = start_date
            if end_date is not None:
                patch_fields["有效期截止日期"] = end_date
            # 只要任一端点有效，就记录块索引和区间框
            if start_date is not None or end_date is not None:
                validity_block_indices = block_indices
            if start_date is not None and end_date is not None:
                confirmed_range_bbox = range_bbox

        # 提取初领日期（若原版为 None）
        if fields.get("初领日期") is None:
            first_anchor = anchors.get("初领日期")
            org_anchor = anchors.get("签发机关")

            first_date = _extract_first_issue_date(
                first_anchor,
                org_anchor,
                confirmed_range_bbox,
                validity_block_indices,
                text_blocks,
                ref_h,
            )
            if first_date is not None:
                patch_fields["初领日期"] = first_date

        # 提取签发机关（若原版为 None）
        if fields.get("签发机关") is None:
            org_anchor = anchors.get("签发机关")
            if org_anchor is not None:
                org_name = _extract_issuing_org(org_anchor, text_blocks, ref_h)
                if org_name is not None:
                    patch_fields["签发机关"] = org_name

        # 只补空位（Fill-Missing-Only）
        for key, val in patch_fields.items():
            if fields.get(key) is None and val is not None:
                fields[key] = val

    except Exception:
        # 补丁内部任何异常均静默回退，完整保留原版结果
        pass


def _check_no_review_layout_gates(
    text_blocks: List[TextBlock],
) -> Tuple[bool, Dict[str, _Anchor], float]:
    """
    双重门控检查。

    Returns:
        (是否通过门控, 锚点字典, 参考行高)
    """
    # 1. 收集新版三槽位底栏的标签锚点
    label_anchors: List[_Anchor] = []

    for idx, block in enumerate(text_blocks):
        if _is_noise_text(block.text):
            continue

        normalized = _normalize_label(block.text)
        if normalized in ("初领日期", "有效期限", "有效期至", "有效期", "签发机关"):
            # 映射到标准键
            if normalized in ("有效期限", "有效期至", "有效期"):
                field_key = "有效期限"
            else:
                field_key = normalized

            if field_key in _NO_REVIEW_BOTTOM_SLOTS:
                row, col = _NO_REVIEW_BOTTOM_SLOTS[field_key]
                label_anchors.append(_Anchor(
                    field_key=field_key,
                    row=row,
                    col=col,
                    bbox=block,
                    block_idx=idx,
                ))

    if len({a.field_key for a in label_anchors}) < 2:
        return False, {}, 0.0

    # 2. 计算参考行高
    heights = [a.bbox.height for a in label_anchors if a.bbox.height > 0]
    if not heights:
        return False, {}, 0.0
    from statistics import median
    ref_h = max(1.0, float(median(heights)))

    # 3. 确认最优锚点组（必须包含签发机关 (2,0)）
    best_group: Dict[str, _Anchor] = {}
    best_score: Tuple[int, float] = (0, -1e9)

    for seed in label_anchors:
        group: Dict[str, _Anchor] = {seed.field_key: seed}

        for cand in sorted(label_anchors, key=lambda a: abs(a.bbox.y - seed.bbox.y) + abs(a.bbox.x - seed.bbox.x)):
            if cand.field_key == seed.field_key or cand.field_key in group:
                continue

            if all(_anchors_compatible(existing, cand, ref_h) for existing in group.values()):
                group[cand.field_key] = cand

        if len(group) >= 2 and "签发机关" in group and group["签发机关"].row == 2 and group["签发机关"].col == 0:
            dispersion = -_group_dispersion(list(group.values()))
            score = (len(group), dispersion)
            if score > best_score:
                best_score = score
                best_group = group

    # 强正向门控：必须明确包含签发机关 (2,0)
    if "签发机关" not in best_group:
        return False, {}, 0.0
    if best_group["签发机关"].row != 2 or best_group["签发机关"].col != 0:
        return False, {}, 0.0

    # 4. 强负向守卫：不得存在与 Row 1 几何相容的"应复审日期"标签
    row1_anchors = [a for a in best_group.values() if a.row == 1]
    if not row1_anchors:
        return False, {}, 0.0

    for idx, block in enumerate(text_blocks):
        if _is_noise_text(block.text):
            continue
        normalized = _normalize_label(block.text)
        if normalized in _REVIEW_DATE_ALIASES:
            # 检查是否与 Row 1 任一锚点几何相容
            for r1_anchor in row1_anchors:
                if _is_same_row(block, r1_anchor.bbox):
                    # 存在旧版"应复审日期"标签，立即退出
                    return False, {}, 0.0

    return True, best_group, ref_h


def _anchors_compatible(a: _Anchor, b: _Anchor, ref_h: float) -> bool:
    """判断两个锚点几何兼容性。"""
    if a.field_key == b.field_key:
        return False

    # 行高差异过大拒绝
    ratio_h = max(a.bbox.height, b.bbox.height) / max(1.0, min(a.bbox.height, b.bbox.height))
    if ratio_h > 2.8:
        return False

    # 同行不同列
    if a.row == b.row and a.col != b.col:
        left_a, right_a = (a, b) if a.col < b.col else (b, a)

        if not _is_same_row(left_a.bbox, right_a.bbox):
            return False

        dx = right_a.bbox.x - left_a.bbox.x
        return (
            2.2 * ref_h <= dx <= 22.0 * ref_h
            and right_a.bbox.x >= left_a.bbox.x_max - 0.3 * ref_h
        )

    # 同列不同行
    if a.col == b.col and a.row != b.row:
        top_a, bot_a = (a, b) if a.row < b.row else (b, a)

        if not _is_left_aligned(top_a.bbox, bot_a.bbox, ref_h):
            return False

        dy = bot_a.bbox.y - top_a.bbox.y
        return 1.2 * ref_h <= dy <= 7.5 * ref_h

    # 对角关系
    top_a, bot_a = (a, b) if a.row < b.row else (b, a)
    left_a, right_a = (a, b) if a.col < b.col else (b, a)

    dx = right_a.bbox.x - left_a.bbox.x
    dy = bot_a.bbox.y - top_a.bbox.y

    return 2.2 * ref_h <= dx <= 22.0 * ref_h and 1.2 * ref_h <= dy <= 7.5 * ref_h


def _group_dispersion(anchors: List[_Anchor]) -> float:
    """计算锚点组紧凑度。"""
    ref_h = max(1.0, sum(a.bbox.height for a in anchors) / len(anchors))
    xs = [a.bbox.x for a in anchors]
    ys = [a.bbox.y for a in anchors]
    return ((max(xs) - min(xs)) + (max(ys) - min(ys))) / ref_h

def _extract_validity_dates(
    anchor: _Anchor,
    text_blocks: List[TextBlock],
    ref_h: float,
) -> Tuple[Optional[str], Optional[str], Optional[TextBlock], Set[int]]:
    """
    提取有效期起始日期与截止日期（双端点独立解析）。

    Returns:
        (开始日期, 截止日期, 区间包围框, 块索引集合)
    """
    label_h = max(1.0, anchor.bbox.height, ref_h)
    y_top = anchor.bbox.y_max - 0.45 * label_h
    y_bottom = anchor.bbox.y_max + 2.6 * label_h

    # 收集候选块（右栏无右界限制）
    candidates: List[Tuple[int, TextBlock]] = []
    for idx, b in enumerate(text_blocks):
        if idx == anchor.block_idx or _is_noise_text(b.text):
            continue
        if b.y < y_top or b.y_center < anchor.bbox.y_center + 0.3 * label_h:
            continue
        if b.y >= y_bottom:
            continue
        # 右栏左对齐
        if abs(b.x - anchor.bbox.x) <= 1.2 * label_h:
            candidates.append((idx, b))

    if not candidates:
        return None, None, None, set()

    # 按同行分组
    rows: List[List[Tuple[int, TextBlock]]] = []
    for idx, block in sorted(candidates, key=lambda item: (item[1].y_center, item[1].x)):
        placed = False
        for row in rows:
            if _is_same_row(row[0][1], block):
                row.append((idx, block))
                placed = True
                break
        if not placed:
            rows.append([(idx, block)])

    if not rows:
        return None, None, None, set()

    # 取第一行
    first_row = sorted(rows[0], key=lambda item: item[1].x)

    # 收取连续块
    selected: List[Tuple[int, TextBlock]] = [first_row[0]]
    for nxt in first_row[1:]:
        gap = nxt[1].x - selected[-1][1].x_max
        if gap > 2.0 * label_h:
            break
        selected.append(nxt)

    # 组合文本并解析双端点
    combined_text = "".join(item[1].text.strip() for item in selected)

    # 使用简化的双端点解析（补丁内部复制核心逻辑）
    start_date, end_date = _parse_validity_endpoints_simple(combined_text)

    if start_date is not None or end_date is not None:
        blocks = [item[1] for item in selected]
        bbox = _union_bbox(blocks, combined_text) if start_date and end_date else None
        indices = {item[0] for item in selected}
        return start_date, end_date, bbox, indices

    return None, None, None, set()


def _parse_validity_endpoints_simple(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    简化的有效期双端点解析（补丁内部使用）。

    仅支持：
    1. 完整双日期区间
    2. 单个日期（返回为止日）
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None, None

    # 尝试完整双日期区间
    range_parsed = _parse_date_range(compact)
    if range_parsed is not None:
        return range_parsed[0], range_parsed[1]

    # 尝试单日期（作为止日）
    single_date = _parse_single_date(compact)
    if single_date is not None:
        return None, single_date

    return None, None


def _union_bbox(blocks: List[TextBlock], text: str = "") -> TextBlock:
    """计算并集包围框。"""
    x_min = min(b.x for b in blocks)
    y_min = min(b.y for b in blocks)
    x_max = max(b.x + b.width for b in blocks)
    y_max = max(b.y + b.height for b in blocks)
    avg_conf = sum(b.confidence for b in blocks) / len(blocks)
    return TextBlock(
        text=text or "".join(b.text for b in blocks),
        confidence=avg_conf,
        x=x_min,
        y=y_min,
        width=max(0.0, x_max - x_min),
        height=max(0.0, y_max - y_min),
    )


def _extract_first_issue_date(
    first_anchor: Optional[_Anchor],
    org_anchor: Optional[_Anchor],
    confirmed_range_bbox: Optional[TextBlock],
    validity_block_indices: Set[int],
    text_blocks: List[TextBlock],
    ref_h: float,
) -> Optional[str]:
    """
    提取初领日期。

    策略：
    1. 若有初领日期标签，在其下方左栏收集单日期（排除有效期块）
    2. 标签漏检但有确认的有效期区间时，借签发机关 (2,0) 提供的左列起点，
       在有效期区间同排左侧提取唯一合法单日期
    """
    if first_anchor is not None:
        # 有标签，直接提取
        label_h = max(1.0, first_anchor.bbox.height, ref_h)
        y_top = first_anchor.bbox.y_max - 0.45 * label_h
        y_bottom = first_anchor.bbox.y_max + 2.6 * label_h

        # 确定右界（若有签发机关在左下方）
        right_limit: Optional[float] = None
        if org_anchor is not None and org_anchor.row == 2 and org_anchor.col == 0:
            # 新版签发机关在左下方，初领日期右界较宽松
            # 但仍需判断同行是否有有效期限标签
            for idx, b in enumerate(text_blocks):
                if idx == first_anchor.block_idx or _is_noise_text(b.text):
                    continue
                normalized = _normalize_label(b.text)
                if normalized in ("有效期限", "有效期至", "有效期截止日期"):
                    if _is_same_row(first_anchor.bbox, b):
                        right_limit = b.x
                        break

        candidates: List[str] = []
        for idx, b in enumerate(text_blocks):
            if idx == first_anchor.block_idx or idx in validity_block_indices or _is_noise_text(b.text):
                continue
            if b.y < y_top or b.y_center < first_anchor.bbox.y_center + 0.3 * label_h:
                continue
            if b.y >= y_bottom:
                continue
            # 左对齐
            if abs(b.x - first_anchor.bbox.x) > 1.8 * label_h:
                continue
            # 右界检查
            if right_limit is not None and b.x >= right_limit - 0.35 * label_h:
                continue

            iso_date = _parse_single_date(b.text)
            if iso_date is not None:
                candidates.append(iso_date)

        return candidates[0] if len(candidates) == 1 else None

    # 标签漏检，借有效期区间 + 签发机关左列起点恢复
    if confirmed_range_bbox is None or org_anchor is None:
        return None

    left_col_x = org_anchor.bbox.x
    candidates: List[str] = []

    for idx, b in enumerate(text_blocks):
        if idx in validity_block_indices or _is_noise_text(b.text):
            continue
        # 必须与有效期区间同行
        if not _is_same_row(confirmed_range_bbox, b):
            continue
        # 必须在有效期区间左侧
        if b.x_max > confirmed_range_bbox.x - 0.1 * ref_h:
            continue
        # 不能离得太远
        if confirmed_range_bbox.x - b.x > 18.0 * ref_h:
            continue
        # 左对齐检查
        if abs(b.x - left_col_x) > 2.0 * ref_h:
            continue

        iso_date = _parse_single_date(b.text)
        if iso_date is not None:
            candidates.append(iso_date)

    return candidates[0] if len(candidates) == 1 else None


def _extract_issuing_org(
    anchor: _Anchor,
    text_blocks: List[TextBlock],
    ref_h: float,
) -> Optional[str]:
    """
    提取签发机关（新版位于 (2, 0) 左栏）。

    允许超长机关名向右跨过原有效期限列，因新版底栏第二行右侧无其他字段。
    """
    label_h = max(1.0, anchor.bbox.height, ref_h)
    y_top = anchor.bbox.y_max - 0.45 * label_h
    y_bottom = anchor.bbox.y_max + 5.2 * label_h

    # 收集候选块（左栏无右界）
    candidates: List[Tuple[int, TextBlock]] = []
    for idx, b in enumerate(text_blocks):
        if idx == anchor.block_idx or _is_noise_text(b.text):
            continue
        if b.y < y_top or b.y_center < anchor.bbox.y_center + 0.3 * label_h:
            continue
        if b.y >= y_bottom:
            continue
        # 左对齐
        if abs(b.x - anchor.bbox.x) <= 1.8 * label_h:
            candidates.append((idx, b))

    if not candidates:
        return None

    # 按行分组
    rows: List[List[Tuple[int, TextBlock]]] = []
    for idx, block in sorted(candidates, key=lambda item: (item[1].y_center, item[1].x)):
        placed = False
        for row in rows:
            if _is_same_row(row[0][1], block):
                row.append((idx, block))
                placed = True
                break
        if not placed:
            rows.append([(idx, block)])

    # 合并最多 2 行紧邻左对齐的机关名
    assembled: List[TextBlock] = []
    prev_y_max: Optional[float] = None

    for row in rows[:2]:
        if prev_y_max is not None:
            if row[0][1].y - prev_y_max > 1.2 * label_h:
                break

        # 排序并收取连续块（无右界限制，但遇大间隔停止）
        row_sorted = sorted(row, key=lambda item: item[1].x)

        # 首块必须左对齐
        if abs(row_sorted[0][1].x - anchor.bbox.x) > 1.8 * label_h:
            break

        selected: List[TextBlock] = [row_sorted[0][1]]
        for nxt_item in row_sorted[1:]:
            nxt = nxt_item[1]
            gap = nxt.x - selected[-1].x_max
            # 遇到明显大间隔（进入背面卡或其他区域），停止
            if gap > 2.0 * label_h:
                break
            selected.append(nxt)

        assembled.extend(selected)
        prev_y_max = max(b.y + b.height for b in selected)

    if not assembled:
        return None

    candidate_text = "".join(b.text.strip() for b in assembled)

    # 验证机关名合法性
    compact = re.sub(r"\s+", "", candidate_text)
    if len(compact) < 4 or _is_noise_text(compact):
        return None
    if _SINGLE_DATE_RE.search(compact):
        return None
    # 新版补丁要求机关名必须以明确结尾
    if not any(compact.endswith(suffix) for suffix in _ORG_SUFFIXES):
        return None

    return compact
