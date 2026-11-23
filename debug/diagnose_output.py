"""
RapidOCROutput 结构诊断脚本
"""
from pathlib import Path
from PIL import Image
from rapidocr import RapidOCR
import io


# 读取模板1
template_path = Path("./证件模板/模板1.png")
image = Image.open(template_path).convert("RGB")

# 执行 OCR
ocr = RapidOCR()
result = ocr(image)

print("=" * 60)
print("RapidOCROutput 对象结构诊断")
print("=" * 60)
print(f"类型: {type(result)}")
print(f"结果: {result}")
print(f"dir(result): {dir(result)}")

# 尝试访问各种属��
if hasattr(result, '__dict__'):
    print(f"\n__dict__: {result.__dict__}")

if hasattr(result, 'boxes'):
    print(f"\nboxes: {result.boxes}")

if hasattr(result, 'txts'):
    print(f"\ntxts: {result.txts}")

if hasattr(result, 'scores'):
    print(f"\nscores: {result.scores}")

# 尝试迭代
print("\n尝试迭代:")
try:
    for i, item in enumerate(result):
        print(f"  [{i}]: {item}")
        if i >= 5:
            break
except Exception as e:
    print(f"  错误: {e}")

# 检查是否是列表/元组
print(f"\n是否为列表: {isinstance(result, list)}")
print(f"是否为元组: {isinstance(result, tuple)}")
print(f"长度: {len(result) if hasattr(result, '__len__') else 'N/A'}")
