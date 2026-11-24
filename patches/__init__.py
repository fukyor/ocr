"""
补丁模块包

用于承接特定版式变体的后置补齐逻辑，确保与原识别逻辑物理隔离。
"""
from .no_review_layout_patch import apply_no_review_layout_patch

__all__ = ["apply_no_review_layout_patch"]
