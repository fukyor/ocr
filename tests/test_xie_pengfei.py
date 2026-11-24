#!/usr/bin/env python
"""测试谢鹏飞证书识别"""
from pathlib import Path
from ocr_engine import OCREngine
from field_extractor import FieldExtractor

# 文件路径
cert_file = Path("证件模板/set2/A44130032326012072谢鹏飞登高证.pdf")

print(f"测试文件: {cert_file.name}")
print("=" * 60)

# 创建 OCR 引擎（使用默认高精度配置）
engine = OCREngine()

# 识别
result = engine.recognize(str(cert_file.suffix), cert_file.read_bytes())

if result.status.value != 'ok':
    print(f"OCR 失败: {result.status.value}")
    exit(1)

print(f"OCR 成功，检测到 {len(result.text_blocks)} 个文本块")

# 提取字段
extractor = FieldExtractor(enable_patches=True)
fields = extractor.extract_fields(
    result.text_blocks,
    result.image_width,
    result.image_height
)

# 显示结果
print("\n识别结果:")
print("-" * 60)
field_count = 0
for key in ["姓名", "性别", "作业类别", "操作项目", "初领日期", "有效期截止日期", "签发机关"]:
    value = fields.get(key)
    if value:
        field_count += 1
        print(f"  [OK] {key:12s}: {value}")
    else:
        print(f"  [--] {key:12s}: [缺失]")

print("-" * 60)
print(f"\n总计: {field_count}/7 字段")

if field_count == 7:
    print("\n修复成功！谢鹏飞证书已达到 7/7")
elif field_count >= 6:
    print(f"\n接近成功，当前 {field_count}/7")
else:
    print(f"\n仍有缺失，当前 {field_count}/7")

