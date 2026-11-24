#!/usr/bin/env python3
"""
临时脚本：批量更新 field_extractor.py 中的关键方法
"""
import re

def update_field_extractor():
    with open("field_extractor.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 在 _extract_upper_region 方法开头添加证号提取逻辑
    # 找到 "def _extract_upper_region(" 后的第一个 ref_h = 行之后插入证号提取
    pattern = r'(def _extract_upper_region\([\s\S]*?ref_h = _ref_height\(list\(anchors\.values\(\)\)\)\n        left_col_x, right_col_x = self\._resolve_column_bounds\(anchors, ref_h\)\n)'

    cert_no_extraction = '''
        # 计算 Row1 标签的最小 y 坐标（作为证号区域的纵向底界）
        row1_anchors = [a for a in anchors.values() if a.row == 1 and not a.is_value_anchor]
        row1_label_y = min(a.bbox.y for a in row1_anchors) if row1_anchors else None

        # 0. 证号（通栏提取，分层校验）
        fields["证号"] = self._extract_cert_no(
            anchors=anchors,
            row1_label_y=row1_label_y,
            left_col_x=left_col_x,
            ref_h=ref_h,
            text_blocks=text_blocks,
            label_block_indices=label_block_indices,
        )
'''

    replacement = r'\1' + cert_no_extraction + '\n'
    content = re.sub(pattern, replacement, content, count=1)

    # 2. 修改底栏提取中的有效期字段名（将 "有效期截止日期" 改为 VALIDITY_SLOT_KEY）
    # 注意要保持逻辑一致，将其改为提取双端点

    # 保存修改后的内容
    with open("field_extractor.py", "w", encoding="utf-8") as f:
        f.write(content)

    print("field_extractor.py 已更新")

if __name__ == "__main__":
    update_field_extractor()
