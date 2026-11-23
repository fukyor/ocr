"""
OCR 调试脚本 - 诊断识别问题
"""
import sys
from pathlib import Path
from ocr_engine import OCREngine


def debug_ocr():
    """调试 OCR 识别"""
    ocr_engine = OCREngine()

    # 读取两张模板证件
    template_dir = Path("./证件模板")

    files_to_test = [
        template_dir / "模板1.png",
        template_dir / "模板2.jpg"
    ]

    for file_path in files_to_test:
        if not file_path.exists():
            print(f"❌ 文件不存在: {file_path}")
            continue

        print(f"\n{'='*60}")
        print(f"处理: {file_path.name}")
        print(f"{'='*60}")

        # 读取文件
        with open(file_path, "rb") as f:
            file_content = f.read()

        print(f"文件大小: {len(file_content)} 字节")
        print(f"文件扩展名: {file_path.suffix}")

        # 执行 OCR
        result = ocr_engine.recognize(file_path.suffix.lower(), file_content)

        print(f"识别状态: {result.status.value}")
        print(f"图像尺寸: {result.image_width} × {result.image_height}")
        print(f"文本块数: {len(result.text_blocks)}")

        if result.error_message:
            print(f"错误原因: {result.error_message}")

        if result.text_blocks:
            print(f"\n前 10 个文本块:")
            for i, block in enumerate(result.text_blocks[:10]):
                print(f"  [{i+1}] '{block.text}' (置信度: {block.confidence:.3f})")

        if result.raw_text:
            print(f"\n整页原文 (前 500 字):")
            print(result.raw_text[:500])

        # 统计分析
        if result.text_blocks:
            confidences = [b.confidence for b in result.text_blocks]
            print(f"\n置信度统计:")
            print(f"  平均: {sum(confidences) / len(confidences):.3f}")
            print(f"  最小: {min(confidences):.3f}")
            print(f"  最大: {max(confidences):.3f}")


if __name__ == "__main__":
    debug_ocr()
