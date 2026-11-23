"""
OCR 识别模块

统一处理 PNG、JPG/JPEG 和单页 PDF 的内存解码、校验、渲染及整页 OCR 识别。
"""
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError
from rapidocr import (
    EngineType,
    LangDet,
    LangRec,
    ModelType,
    OCRVersion,
    RapidOCR,
)

from config import OCR_PRODUCTION_CONFIG, PDF_RENDER_DPI
from models import OCRResult, OCRStatus, TextBlock


class OCREngine:
    """基于 RapidOCR + ONNX Runtime CPU 后端的整页 OCR 引擎。"""

    def __init__(self, ocr_config: Optional[Dict[str, Any]] = None):
        """
        初始化 OCR 引擎。

        Args:
            ocr_config: OCR 参数配置字典，默认使用正式高精度配置 OCR_PRODUCTION_CONFIG
        """
        cfg = dict(OCR_PRODUCTION_CONFIG if ocr_config is None else ocr_config)
        self.config = cfg

        params = {
            "Global.max_side_len": int(cfg["global_max_side_len"]),
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.use_cuda": bool(cfg.get("use_cuda", False)),
            "EngineConfig.onnxruntime.use_dml": bool(cfg.get("use_dml", False)),
            "Det.engine_type": EngineType(cfg["engine_type"]),
            "Det.lang_type": LangDet(cfg["det_lang"]),
            "Det.ocr_version": OCRVersion(cfg["det_ocr_version"]),
            "Det.model_type": ModelType(cfg["det_model_type"]),
            "Det.limit_type": str(cfg["det_limit_type"]),
            "Det.limit_side_len": int(cfg["det_limit_side_len"]),
            "Rec.engine_type": EngineType(cfg["engine_type"]),
            "Rec.lang_type": LangRec(cfg["rec_lang"]),
            "Rec.ocr_version": OCRVersion(cfg["rec_ocr_version"]),
            "Rec.model_type": ModelType(cfg["rec_model_type"]),
        }
        self.ocr = RapidOCR(params=params)

    def recognize_roi(
        self,
        file_ext: str,
        file_content: bytes,
        roi_box: tuple[float, float, float, float],
    ) -> OCRResult:
        """
        识别指定局部区域（纠偏后整页像素坐标）。

        Args:
            file_ext: 文件扩展名
            file_content: 文件字节内容
            roi_box: (x_min, y_min, x_max, y_max) 像素坐标

        Returns:
            OCRResult，坐标已映射回整页像素
        """
        ext = file_ext.lower()
        if ext in (".png", ".jpg", ".jpeg"):
            return self._recognize_image_roi(file_content, roi_box)
        if ext == ".pdf":
            return self._recognize_pdf_roi(file_content, roi_box)
        return OCRResult(
            status=OCRStatus.UNSUPPORTED,
            error_message=f"不支持的文件格式: {file_ext}",
        )

    def _recognize_image_roi(
        self, file_content: bytes, roi_box: tuple[float, float, float, float]
    ) -> OCRResult:
        """从图片中裁取局部区域并识别。"""
        try:
            with Image.open(io.BytesIO(file_content)) as opened_img:
                opened_img.load()
                corrected = ImageOps.exif_transpose(opened_img)
                rgb_image = corrected.convert("RGB")

            # 裁取局部区域
            x_min, y_min, x_max, y_max = roi_box
            w, h = rgb_image.size
            x_min = max(0, min(int(x_min), w))
            y_min = max(0, min(int(y_min), h))
            x_max = max(0, min(int(x_max), w))
            y_max = max(0, min(int(y_max), h))

            if x_max <= x_min or y_max <= y_min:
                return OCRResult(status=OCRStatus.FAILED, error_message="局部区域无效")

            roi_img = rgb_image.crop((x_min, y_min, x_max, y_max))

            # 计算放大倍率
            roi_w, roi_h = roi_img.size
            max_side = max(roi_w, roi_h)
            scale = min(2.5, 1800.0 / max_side) if max_side < 1600 else 1.0

            if scale > 1.0:
                new_w = int(roi_w * scale)
                new_h = int(roi_h * scale)
                roi_img = roi_img.resize((new_w, new_h), Image.Resampling.BICUBIC)

            # 识别
            result = self._recognize_rgb_image(roi_img)

            # 坐标映射回整页
            if result.status == OCRStatus.OK and result.text_blocks:
                for block in result.text_blocks:
                    block.x = block.x / scale + x_min
                    block.y = block.y / scale + y_min
                    block.width = block.width / scale
                    block.height = block.height / scale

            return result

        except Exception as exc:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message=f"局部识别失败: {exc}",
            )

    def _recognize_pdf_roi(
        self, file_content: bytes, roi_box: tuple[float, float, float, float]
    ) -> OCRResult:
        """从 PDF 中裁取局部区域并识别。"""
        pdf_doc = None
        try:
            pdf_doc = pymupdf.open(stream=file_content, filetype="pdf")
            if pdf_doc.needs_pass or len(pdf_doc) != 1:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    error_message="PDF 加密或非单页",
                )

            page = pdf_doc[0]
            page_w, page_h = page.rect.width, page.rect.height

            # 像素转页面点
            base_scale = PDF_RENDER_DPI / 72.0
            x_min_pt = roi_box[0] / base_scale
            y_min_pt = roi_box[1] / base_scale
            x_max_pt = roi_box[2] / base_scale
            y_max_pt = roi_box[3] / base_scale

            # 裁剪到页面范围
            x_min_pt = max(0, min(x_min_pt, page_w))
            y_min_pt = max(0, min(y_min_pt, page_h))
            x_max_pt = max(0, min(x_max_pt, page_w))
            y_max_pt = max(0, min(y_max_pt, page_h))

            if x_max_pt <= x_min_pt or y_max_pt <= y_min_pt:
                return OCRResult(status=OCRStatus.FAILED, error_message="局部区域无效")

            clip_rect = pymupdf.Rect(x_min_pt, y_min_pt, x_max_pt, y_max_pt)

            # 计算放大倍率
            roi_w = x_max_pt - x_min_pt
            roi_h = y_max_pt - y_min_pt
            max_side = max(roi_w, roi_h) * base_scale
            scale = min(2.5, 1800.0 / max_side) if max_side < 1600 else 1.0

            # 渲染
            render_scale = base_scale * scale
            matrix = pymupdf.Matrix(render_scale, render_scale)
            pix = page.get_pixmap(matrix=matrix, clip=clip_rect, colorspace=pymupdf.csRGB, alpha=False)

            if pix.width <= 0 or pix.height <= 0:
                return OCRResult(status=OCRStatus.FAILED, error_message="渲染失败")

            roi_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            result = self._recognize_rgb_image(roi_img)

            # 坐标映射回整页像素
            if result.status == OCRStatus.OK and result.text_blocks:
                for block in result.text_blocks:
                    block.x = block.x / scale + roi_box[0]
                    block.y = block.y / scale + roi_box[1]
                    block.width = block.width / scale
                    block.height = block.height / scale

            return result

        except Exception as exc:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message=f"PDF 局部识别失败: {exc}",
            )
        finally:
            if pdf_doc is not None:
                pdf_doc.close()

    def get_runtime_info(self) -> Dict[str, Any]:
        """返回当前引擎实际生效的模型与限长参数，便于核实配置。"""
        det_model = None
        rec_model = None
        if self.ocr.text_det is not None and hasattr(self.ocr.text_det, "session"):
            ort_sess = getattr(self.ocr.text_det.session, "session", None)
            det_path = getattr(ort_sess, "_model_path", None) or self.ocr.cfg.Det.model_path
            det_model = Path(det_path).name if det_path else None
        if self.ocr.text_rec is not None and hasattr(self.ocr.text_rec, "session"):
            ort_sess = getattr(self.ocr.text_rec.session, "session", None)
            rec_path = getattr(ort_sess, "_model_path", None) or self.ocr.cfg.Rec.model_path
            rec_model = Path(rec_path).name if rec_path else None

        return {
            "global_max_side_len": self.ocr.cfg.Global.max_side_len,
            "det_ocr_version": self.ocr.cfg.Det.ocr_version.value,
            "det_model_type": self.ocr.cfg.Det.model_type.value,
            "det_limit_type": self.ocr.cfg.Det.limit_type,
            "det_limit_side_len": self.ocr.cfg.Det.limit_side_len,
            "rec_ocr_version": self.ocr.cfg.Rec.ocr_version.value,
            "rec_model_type": self.ocr.cfg.Rec.model_type.value,
            "det_model_file": det_model,
            "rec_model_file": rec_model,
        }

    def recognize(self, file_ext: str, file_content: bytes) -> OCRResult:
        """
        对已读取的单个文件内容执行格式校验、解码/渲染与整页 OCR。

        Args:
            file_ext: 小写扩展名（.png / .jpg / .jpeg / .pdf）
            file_content: 文件原始字节内容

        Returns:
            OCRResult
        """
        ext = file_ext.lower()
        if ext in (".png", ".jpg", ".jpeg"):
            return self._recognize_image_bytes(file_content)
        if ext == ".pdf":
            return self._recognize_pdf_bytes(file_content)
        return OCRResult(
            status=OCRStatus.UNSUPPORTED,
            error_message=f"不支持的文件格式: {file_ext}",
        )

    def _recognize_image_bytes(self, file_content: bytes) -> OCRResult:
        """从内存解码图片、校正 EXIF 方向并转为 RGB 图像后执行 OCR。"""
        if not file_content:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message="图片文件为空",
            )

        try:
            with Image.open(io.BytesIO(file_content)) as opened_img:
                # 强制完整解码像素数据，尽早捕获截断或损坏图片
                opened_img.load()
                corrected = ImageOps.exif_transpose(opened_img)
                rgb_image = corrected.convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message=f"图片解码失败: {exc}",
            )

        # 裁边与放大
        rgb_image, crop_info = self._crop_and_scale_image(rgb_image)
        result = self._recognize_rgb_image(rgb_image)

        # 坐标还原
        if result.status == OCRStatus.OK and result.text_blocks and crop_info:
            left, top, scale_x, scale_y = crop_info
            for block in result.text_blocks:
                block.x = block.x / scale_x + left
                block.y = block.y / scale_y + top
                block.width = block.width / scale_x
                block.height = block.height / scale_y

        return result

    @staticmethod
    def _crop_and_scale_image(
        rgb_image: Image.Image,
    ) -> tuple[Image.Image, Optional[tuple[float, float, float, float]]]:
        """
        裁边与小图放大。

        返回：(处理后图像, (left, top, scale_x, scale_y))
        """
        img_np = np.array(rgb_image)
        h, w = img_np.shape[:2]

        # 转灰度检测边缘
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY) if len(img_np.shape) == 3 else img_np

        # 从四边扫描连续低方差行列
        std_threshold = 2.5

        # 上边
        top = 0
        for i in range(h):
            if np.std(gray[i, :]) > std_threshold:
                top = i
                break

        # 下边
        bottom = h
        for i in range(h - 1, -1, -1):
            if np.std(gray[i, :]) > std_threshold:
                bottom = i + 1
                break

        # 左边
        left = 0
        for i in range(w):
            if np.std(gray[:, i]) > std_threshold:
                left = i
                break

        # 右边
        right = w
        for i in range(w - 1, -1, -1):
            if np.std(gray[:, i]) > std_threshold:
                right = i + 1
                break

        # 检查裁框是否有效
        crop_w = right - left
        crop_h = bottom - top
        crop_area = crop_w * crop_h
        orig_area = w * h

        should_crop = (
            crop_area >= orig_area * 0.08
            and crop_w >= 200
            and crop_h >= 200
        )

        if should_crop:
            cropped = rgb_image.crop((left, top, right, bottom))
            crop_w, crop_h = cropped.size
        else:
            cropped = rgb_image
            left = top = 0
            crop_w, crop_h = w, h

        # 小图放大
        max_side = max(crop_w, crop_h)
        min_side = min(crop_w, crop_h)

        if max_side < 1500 or min_side < 950:
            scale = max(1.0, min(2.0, 2600.0 / max_side))
        else:
            scale = 1.0

        if scale > 1.0:
            new_w = int(crop_w * scale)
            new_h = int(crop_h * scale)
            scaled = cropped.resize((new_w, new_h), Image.Resampling.BICUBIC)
            return scaled, (left, top, scale, scale)

        return cropped, (left, top, 1.0, 1.0) if should_crop else None

    def _recognize_pdf_bytes(self, file_content: bytes) -> OCRResult:
        """在内存中校验 PDF 页数与加密状态，仅对单页 PDF 以 250 DPI 渲染为 RGB 图像后执行 OCR。"""
        if not file_content:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message="PDF 文件为空",
            )

        pdf_doc = None
        try:
            pdf_doc = pymupdf.open(stream=file_content, filetype="pdf")

            # 加密且需要密码时无法解密
            if pdf_doc.needs_pass:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    error_message="PDF 文件已加密且无法解密",
                )

            page_count = len(pdf_doc)
            if page_count == 0:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    error_message="PDF 页数为零",
                )
            if page_count > 1:
                return OCRResult(
                    status=OCRStatus.UNSUPPORTED,
                    error_message=f"不支持多页 PDF（共 {page_count} 页）",
                )

            page = pdf_doc[0]
            scale = PDF_RENDER_DPI / 72.0
            matrix = pymupdf.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)

            if pix.width <= 0 or pix.height <= 0:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    error_message="PDF 单页渲染结果尺寸无效",
                )

            rgb_image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except Exception as exc:
            return OCRResult(
                status=OCRStatus.FAILED,
                error_message=f"PDF 打开或渲染失败: {exc}",
            )
        finally:
            if pdf_doc is not None:
                pdf_doc.close()

        return self._recognize_rgb_image(rgb_image)

    def _recognize_rgb_image(self, rgb_image: Image.Image) -> OCRResult:
        """对统一的 Pillow RGB 图像执行一次整页 OCR，对文字块补边以提升识别准确率。"""
        image_width, image_height = rgb_image.size
        if image_width <= 0 or image_height <= 0:
            return OCRResult(
                status=OCRStatus.FAILED,
                image_width=image_width,
                image_height=image_height,
                error_message="图像尺寸无效",
            )

        # 转换为 numpy 数组供手动流程使用
        img_np = np.array(rgb_image)

        try:
            # 预处理图像
            img_processed, op_record = self.ocr.preprocess_img(img_np)

            # 检测文字区域（需要先加载模型）
            det_model = self.ocr._load_det_model()
            det_result = det_model(img_processed)

            if det_result.boxes is None or len(det_result.boxes) == 0:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    image_width=image_width,
                    image_height=image_height,
                    error_message="未检测到有效文字区域",
                )

            # 裁剪文字块
            cropped_imgs = self.ocr.crop_text_regions(img_processed, det_result.boxes)

            # 方向分类
            if self.ocr.use_cls:
                cls_model = self.ocr._load_cls_model()
                cls_result = cls_model(cropped_imgs)
                if cls_result.img_list is not None:
                    cropped_imgs = cls_result.img_list

            # 对每个文字块补边后识别
            padded_imgs = [self._pad_text_block(img) for img in cropped_imgs]

            from rapidocr.ch_ppocr_rec import TextRecInput
            rec_model = self.ocr._load_rec_model()
            rec_input = TextRecInput(img=padded_imgs, return_word_box=False)
            rec_result = rec_model(rec_input)

            if rec_result.txts is None or len(rec_result.txts) == 0:
                return OCRResult(
                    status=OCRStatus.FAILED,
                    image_width=image_width,
                    image_height=image_height,
                    error_message="未识别到有效文字",
                )

            # 将检测框映射回原图坐标
            from rapidocr.utils.process_img import map_boxes_to_original
            boxes = map_boxes_to_original(det_result.boxes, op_record, image_height, image_width)
            txts = rec_result.txts
            scores = rec_result.scores

        except Exception as exc:
            import traceback
            return OCRResult(
                status=OCRStatus.FAILED,
                image_width=image_width,
                image_height=image_height,
                error_message=f"OCR 推理失败: {exc}\n{traceback.format_exc()}",
            )

        text_blocks: List[TextBlock] = []
        for box, txt, score in zip(boxes, txts, scores):
            if txt is None:
                continue
            cleaned_txt = str(txt).strip()
            if not cleaned_txt:
                continue

            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)

            text_blocks.append(
                TextBlock(
                    text=cleaned_txt,
                    confidence=float(score),
                    x=x_min,
                    y=y_min,
                    width=max(0.0, x_max - x_min),
                    height=max(0.0, y_max - y_min),
                )
            )

        if not text_blocks:
            return OCRResult(
                status=OCRStatus.FAILED,
                image_width=image_width,
                image_height=image_height,
                error_message="未识别到有效文字",
            )

        raw_text = self._assemble_raw_text(text_blocks)
        return OCRResult(
            status=OCRStatus.OK,
            text_blocks=text_blocks,
            image_width=image_width,
            image_height=image_height,
            raw_text=raw_text,
            error_message=None,
        )

    @staticmethod
    def _pad_text_block(img: np.ndarray) -> np.ndarray:
        """
        对单个文字块补边，提升识别准确率。

        补边宽度 = max(4, min(10, round(0.12 * h)))
        灰度中位数 >= 128 时补白边，否则取外圈像素各通道第 20 百分位补深色边。
        """
        h, w = img.shape[:2]
        pad = max(4, min(10, round(0.12 * h)))

        # 判断背景色
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        median_val = np.median(gray)

        if median_val >= 128:
            # 浅底补白边
            border_color = (255, 255, 255) if len(img.shape) == 3 else 255
        else:
            # 深底取外圈各通道第 20 百分位
            if len(img.shape) == 3:
                # 提取外圈像素：上下边、左右边
                top_bottom = np.concatenate([img[0, :], img[-1, :]], axis=0)
                left_right = np.concatenate([img[:, 0], img[:, -1]], axis=0)
                border_pixels = np.concatenate([top_bottom, left_right], axis=0)
                border_color = tuple(int(np.percentile(border_pixels[:, i], 20)) for i in range(3))
            else:
                border_pixels = np.concatenate([img[0, :], img[-1, :], img[:, 0], img[:, -1]])
                border_color = int(np.percentile(border_pixels, 20))

        padded = cv2.copyMakeBorder(
            img, pad, pad, pad, pad,
            cv2.BORDER_CONSTANT,
            value=border_color
        )
        return padded

    @staticmethod
    def _assemble_raw_text(text_blocks: List[TextBlock]) -> str:
        """
        将整页文本块按视觉阅读顺序拼接为整页原文：
        行间从上到下，行内从左到右。
        """
        if not text_blocks:
            return ""

        sorted_by_y = sorted(text_blocks, key=lambda b: (b.y_center, b.x))
        rows: List[List[TextBlock]] = []

        for block in sorted_by_y:
            placed = False
            for row in rows:
                # 采用行中心和平均行高判断是否同行，避免跨行大字框将上下两行小字粘成一行
                row_avg_h = sum(item.height for item in row) / len(row)
                row_y_center = sum(item.y_center for item in row) / len(row)
                row_top = row_y_center - row_avg_h / 2.0
                row_bottom = row_y_center + row_avg_h / 2.0

                overlap = min(block.y_max, row_bottom) - max(block.y, row_top)
                min_h = max(1.0, min(block.height, row_avg_h))
                max_h = max(1.0, max(block.height, row_avg_h))

                if (
                    overlap >= min_h * 0.5
                    and abs(block.y_center - row_y_center) <= min_h * 0.55
                    and max_h / min_h <= 1.8
                ):
                    row.append(block)
                    placed = True
                    break

            if not placed:
                rows.append([block])

        # 行间按行顶部中位/平均值从上到下排序
        rows.sort(key=lambda r: sum(item.y_center for item in r) / len(r))

        lines: List[str] = []
        for row in rows:
            row.sort(key=lambda item: item.x)
            lines.append(" ".join(item.text for item in row))

        return "\n".join(lines)
