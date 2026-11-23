"""
字段提取调试脚本 - 诊断区域识别问题
"""
import sys
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from ocr_engine import OCREngine
from field_extractor import FieldExtractor


def debug_extraction():
    """调试字段提取"""
    # 读取模板1
    template_path = Path("./证件模板/模板1.png")
    with open(template_path, "rb") as f:
        file_content = f.read()

    # 执行 OCR
    ocr_engine = OCREngine()
    ocr_result = ocr_engine.recognize(".png", file_content)

    print("=" * 60)
    print("OCR 结果诊断")
    print("=" * 60)
    print(f"识别状态: {ocr_result.status.value}")
    print(f"文本块数: {len(ocr_result.text_blocks)}")
    print(f"图像尺寸: {ocr_result.image_width} × {ocr_result.image_height}")

    # 显示所有识别到的文本（前30个）
    print(f"\n前 30 个识别文本块:")
    for i, block in enumerate(ocr_result.text_blocks[:30]):
        print(f"  [{i:2d}] Y={block.y:7.1f} X={block.x:7.1f} '{block.text}' (conf={block.confidence:.3f})")

    # 执行字段提取并诊断
    extractor = FieldExtractor()

    # 获取标签块
    normalized_blocks = extractor._normalize_blocks(ocr_result.text_blocks)
    label_blocks = extractor._extract_label_blocks(normalized_blocks)

    print(f"\n标签块诊断:")
    print(f"总标签块数: {len(label_blocks)}")
    for i, block in enumerate(label_blocks):
        print(f"  [{i:2d}] Y={block.y:7.1f} X={block.x:7.1f} '{block.text}'")

    # 尝试确认区域
    regions = extractor._confirm_card_regions(label_blocks)
    print(f"\n区域确认结果:")
    print(f"确认的区域数: {len(regions)}")
    for region_name, region_blocks in regions.items():
        print(f"  区域: {region_name} ({len(region_blocks)} 个标签块)")
        for block in region_blocks:
            print(f"    Y={block.y:7.1f} X={block.x:7.1f} '{block.text}'")


if __name__ == "__main__":
    debug_extraction()
