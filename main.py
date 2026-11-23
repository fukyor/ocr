"""
主流程编排模块与命令行入口

默认使用正式高精度 OCR 组合，也支持通过 --fast 切换为次高精度快速推理（仅写入独立测试库）。
逐文件执行读取、哈希校验、幂等跳过、OCR 识别、字段提取与事务持久化。
单文件读取/解码/识别/提取异常仅登记当前文件并继续；目录不可枚举或数据库读写失败属于批次故障，以非零状态退出。
"""
import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from config import (
    DEFAULT_DB_PATH,
    DEFAULT_FAST_DB_PATH,
    OCR_FAST_CONFIG,
    OCR_PRODUCTION_CONFIG,
)
from data_repository import DataRepository
from field_extractor import extract_fields
from file_scanner import scan_folder
from models import (
    FieldDict,
    FileInfo,
    FileStatus,
    OCRStatus,
    TextBlock,
    count_non_empty_fields,
)
from ocr_engine import OCREngine
from two_stage_processor import process_with_two_stage_review

ExtractorFn = Callable[[list[TextBlock], int, int], FieldDict]


class OCRProcessor:
    """特种作业操作证批量 OCR 处理器。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        ocr_engine: Optional[OCREngine] = None,
        extractor: ExtractorFn = extract_fields,
        repository: Optional[DataRepository] = None,
        enable_two_stage: bool = True,
        fast_mode: bool = False,
    ):
        """
        初始化处理器。

        Args:
            db_path: SQLite 数据库路径（默认正式模式使用 DEFAULT_DB_PATH，快速模式使用 DEFAULT_FAST_DB_PATH）
            ocr_engine: 可注入的 OCR 引擎（默认按需初始化对应精度配置引擎）
            extractor: 可注入的字段提取函数
            repository: 可注入的数据仓储实例
            enable_two_stage: 是否启用两阶段复核（默认启用）
            fast_mode: 是否启用次高精度快速推理模式（默认关闭，使用正式高精度组合）
        """
        if db_path is None:
            db_path = DEFAULT_FAST_DB_PATH if fast_mode else DEFAULT_DB_PATH
        self.db_path = db_path
        self.fast_mode = fast_mode
        self.db = repository if repository is not None else DataRepository(db_path)
        self._ocr_engine = ocr_engine
        self.extractor = extractor
        self.enable_two_stage = enable_two_stage
        self._file_content_cache: Dict[str, bytes] = {}  # 缓存文件内容供两阶段使用

        # 批次统计（严格按已提交事务或已确认跳过计数）
        self.stats: Dict[str, int] = {
            "scanned": 0,
            "success": 0,
            "skipped": 0,
            "unsupported": 0,
            "failed": 0,
        }

    def _get_ocr_engine(self) -> OCREngine:
        """批次内复用同一个 OCR 引擎实例，无候选文件时不额外加载模型。"""
        if self._ocr_engine is None:
            ocr_config = OCR_FAST_CONFIG if self.fast_mode else OCR_PRODUCTION_CONFIG
            self._ocr_engine = OCREngine(ocr_config=ocr_config)
        return self._ocr_engine

    def process_folder(self, input_dir: str) -> bool:
        """
        处理指定目录当前层的所有候选文件。

        Args:
            input_dir: 待处理输入目录

        Returns:
            True 表示批次正常完成；False 表示发生批次故障（如目录无效或数据库故障）
        """
        self.stats = {
            "scanned": 0,
            "success": 0,
            "skipped": 0,
            "unsupported": 0,
            "failed": 0,
        }

        try:
            candidate_files = scan_folder(input_dir)
        except Exception as exc:
            print(f"批次错误: 扫描目录失败: {exc}", file=sys.stderr)
            return False

        self.stats["scanned"] = len(candidate_files)
        mode_label = "快速推理 (PP-OCRv4 Mobile)" if self.fast_mode else "正式高精度 (PP-OCRv5 Server)"
        print(f"推理模式: {mode_label}")
        print(f"数据库路径: {Path(self.db_path).resolve()}")
        print(f"扫描目录: {Path(input_dir).resolve()}")
        print(f"发现 {len(candidate_files)} 个候选文件")

        if not candidate_files:
            self._print_statistics()
            return True

        try:
            for file_path in candidate_files:
                self._process_single_file(file_path)
        except sqlite3.Error as exc:
            print(f"\n批次错误: 数据库操作失败（已提交记录保持不变）: {exc}", file=sys.stderr)
            self._print_statistics()
            return False

        self._print_statistics()
        return True

    def _process_single_file(self, file_path: str) -> None:
        """
        处理单个候选文件。

        单文件级异常登记后返回；sqlite3.Error 直接上抛以中止批次。
        """
        path_obj = Path(file_path).resolve()
        norm_path = str(path_obj)
        file_name = path_obj.name
        file_ext = path_obj.suffix.lower()

        print(f"处理: {file_name} ... ", end="", flush=True)

        # 1. 读取文件内容并计算 SHA-256
        file_content, file_hash, read_error = self._read_file(norm_path)
        if file_content is None or file_hash is None:
            file_info = FileInfo(
                file_name=file_name,
                file_path=norm_path,
                file_ext=file_ext,
                file_hash=None,
            )
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.FAILED.value,
                error_message=read_error or "无法读取文件",
                raw_text=None,
            )
            self.stats["failed"] += 1
            print(f"[失败: 读取失败 | file_id={file_id}]")
            return

        # 缓存文件内容供两阶段使用
        self._file_content_cache[norm_path] = file_content

        file_info = FileInfo(
            file_name=file_name,
            file_path=norm_path,
            file_ext=file_ext,
            file_hash=file_hash,
        )

        # 2. 幂等校验：同路径同哈希已有成功记录则跳过，保留原 file_id
        existing_id = self.db.find_existing_success_id(norm_path, file_hash)
        if existing_id is not None:
            self.stats["skipped"] += 1
            print(f"[跳过: 已有成功记录 | file_id={existing_id}]")
            return

        # 3. 媒体校验与整页 OCR
        try:
            ocr_result = self._get_ocr_engine().recognize(file_ext, file_content)
        except Exception as exc:
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.FAILED.value,
                error_message=f"OCR 执行异常: {exc}",
                raw_text=None,
            )
            self.stats["failed"] += 1
            print(f"[失败: OCR 异常 | file_id={file_id}]")
            return

        if ocr_result.status == OCRStatus.UNSUPPORTED:
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.UNSUPPORTED.value,
                error_message=ocr_result.error_message or "不支持的文件类型或页数",
                raw_text=None,
            )
            self.stats["unsupported"] += 1
            print(f"[不支持: {ocr_result.error_message} | file_id={file_id}]")
            return

        if ocr_result.status == OCRStatus.FAILED:
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.FAILED.value,
                error_message=ocr_result.error_message or "OCR 识别失败",
                raw_text=None,
            )
            self.stats["failed"] += 1
            print(f"[失败: {ocr_result.error_message} | file_id={file_id}]")
            return

        # 4. 在已确认的正面局部区域内提取七个字段（启用两阶段复核）
        try:
            if self.enable_two_stage:
                # 两阶段识别与复核
                fields, _, review_note = process_with_two_stage_review(
                    ocr_result.text_blocks,
                    ocr_result.image_width,
                    ocr_result.image_height,
                    self._get_ocr_engine(),
                    file_ext,
                    file_content,
                )
            else:
                # 单阶段识别（测试模式或禁用两阶段时）
                fields = self.extractor(
                    ocr_result.text_blocks,
                    ocr_result.image_width,
                    ocr_result.image_height,
                )
                review_note = "单阶段识别"
        except Exception as exc:
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.FAILED.value,
                error_message=f"字段提取异常: {exc}",
                raw_text=ocr_result.raw_text,
            )
            self.stats["failed"] += 1
            print(f"[失败: 提取异常 | file_id={file_id}]")
            # 清理缓存
            self._file_content_cache.pop(norm_path, None)
            return

        non_empty_count = count_non_empty_fields(fields)
        if non_empty_count == 0:
            file_id = self.db.save_non_success(
                file_info=file_info,
                status=FileStatus.FAILED.value,
                error_message="未能从正面证件区域提取出有效字段",
                raw_text=ocr_result.raw_text,
            )
            self.stats["failed"] += 1
            print(f"[失败: 零个可信字段 | file_id={file_id}]")
            return

        # 5. 至少一个字段可信 -> 单事务保存成功记录、完整原文和七条字段
        file_id = self.db.save_success(
            file_info=file_info,
            raw_text=ocr_result.raw_text or "",
            fields=fields,
        )
        self.stats["success"] += 1

        # 清理缓存
        self._file_content_cache.pop(norm_path, None)

        print(f"[成功: {non_empty_count}/7 字段 | file_id={file_id}]")

    @staticmethod
    def _read_file(file_path: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
        """
        读取单个文件的字节内容并计算 SHA-256 哈希。

        Returns:
            (字节内容, SHA-256 十六进制字符串, 错误原因)
        """
        try:
            with open(file_path, "rb") as fp:
                content = fp.read()
            file_hash = hashlib.sha256(content).hexdigest()
            return content, file_hash, None
        except OSError as exc:
            return None, None, f"文件读取失败: {exc}"

    def _print_statistics(self) -> None:
        """打印批次处理统计信息。"""
        print("\n" + "=" * 50)
        print("处理统计:")
        print(f"  扫描总数: {self.stats['scanned']}")
        print(f"  成功: {self.stats['success']}")
        print(f"  跳过: {self.stats['skipped']}")
        print(f"  不支持: {self.stats['unsupported']}")
        print(f"  失败: {self.stats['failed']}")
        print("=" * 50)


def main(argv: Optional[list[str]] = None) -> int:
    """命令行入口函数。"""
    parser = argparse.ArgumentParser(
        description="中华人民共和国特种作业操作证 OCR 识别与结构化提取系统",
    )
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="输入目录路径（必填，仅扫描当前层）",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=f"SQLite 数据库路径（默认正式模式: {DEFAULT_DB_PATH}；--fast 模式: {DEFAULT_FAST_DB_PATH}）",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="启用次高精度快速推理（使用 PP-OCRv4 Mobile 识别模型，仅写入独立测试库）",
    )

    args = parser.parse_args(argv)

    if args.db is None:
        db_path = DEFAULT_FAST_DB_PATH if args.fast else DEFAULT_DB_PATH
    else:
        db_path = args.db
        if args.fast and Path(db_path).resolve() == Path(DEFAULT_DB_PATH).resolve():
            print(
                f"批次错误: 快速模式（--fast）仅用于独立测试库，禁止写入正式默认数据库 {DEFAULT_DB_PATH}；"
                f"请省略 --db（默认使用 {DEFAULT_FAST_DB_PATH}）或通过 --db 指定独立测试库路径。",
                file=sys.stderr,
            )
            return 1

    try:
        processor = OCRProcessor(db_path=db_path, fast_mode=args.fast)
    except sqlite3.Error as exc:
        print(f"批次错误: 初始化数据库失败: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"批次错误: 无法准备数据库路径: {exc}", file=sys.stderr)
        return 1

    ok = processor.process_folder(args.input)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
