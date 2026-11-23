"""
文件扫描模块

仅枚举指定目录当前层的候选文件，零第三方依赖。
"""
import os
from pathlib import Path
from typing import List

from config import SUPPORTED_EXTENSIONS


def scan_folder(input_dir: str) -> List[str]:
    """
    扫描指定目录的当前层，返回按文件名排序的候选文件规范化绝对路径。

    仅枚举当前层，按小写扩展名筛选 .pdf、.png、.jpg、.jpeg，跳过子目录。
    空目录返回空列表；目录不存在、非目录或无权限枚举时抛出异常。

    Args:
        input_dir: 待扫描目录路径

    Returns:
        按文件名升序排列的规范化绝对路径列表
    """
    input_path = Path(input_dir).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"输入目录不存在: {input_path}")
    if not input_path.is_dir():
        raise NotADirectoryError(f"输入路径不是目录: {input_path}")

    candidate_files: List[str] = []
    try:
        for entry in input_path.iterdir():
            # 仅处理常规文件，跳过子目录
            if not entry.is_file():
                continue

            if entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            candidate_files.append(str(entry.resolve()))
    except PermissionError as exc:
        raise PermissionError(f"无权限访问目录 {input_path}: {exc}") from exc
    except OSError as exc:
        raise OSError(f"枚举目录失败 {input_path}: {exc}") from exc

    # 按文件名排序，保证批次处理顺序稳定
    candidate_files.sort(key=lambda path_str: os.path.basename(path_str))
    return candidate_files
