# Fix2 实施总结

## 修复目标

针对 2026 新版"无复审"特种作业操作证（谢鹏飞证书）识别结果从 5/7 提升到 7/7，补齐缺失的 `初领日期` 和 `签发机关` 字段。

## 实施方案

严格按照 [fix2.md](fix2.md) 的"微调+解耦补丁"双轨架构：

### 一、原位微调（2处，零阈值变更）

#### 1. field_extractor.py - 排除已确认的有效期块索引

**修改位置**: `_extract_bottom_region` 方法（第 843-958 行）

**改动内容**:
- 在方法开头定义 `validity_block_indices: Set[int] = set()`
- 在有效期截止日期的三个确认分支中记录已确认的块索引：
  - 有标签 + 区间解析成功分支
  - 有标签 + 单日期解析成功分支  
  - 标签漏检但区间证据直接确认分支
- 提取初领日期时，构造 `first_excluded_indices = label_block_indices | validity_block_indices`
- 将 `first_excluded_indices` 传递给 `_collect_labeled_field_value` 和 `_recover_unlabeled_first_issue_date`

**效果**: 
- 新版证书中，右栏有效期值块左偏 `16px ≈ 0.71h`，原本会被误判为跨栏块导致初领日期返回 `None`
- 修改后，已确认的有效期块被提前排除，初领日期可正确提取
- 对旧版证书零影响（已确认的块原本就会被 `0.35h` 阈值跳过）

#### 2. two_stage_processor.py - 过滤卡外噪声块

**修改位置**: `_estimate_front_card_roi` 方法（第 117-127 行）

**改动内容**:
- 从 `field_extractor` 导入 `_is_noise_text`
- 在匹配 `known_values` 前跳过卡外干扰块：
  ```python
  for block in text_blocks:
      if _is_noise_text(block.text):
          continue
      for val in known_values:
          ...
  ```

**效果**:
- 避免卡外红色备注（如 `'备注：本证书应于2032-09-19前进行换证。'`）污染 ROI 估算
- 对正常件零影响（正常件根本不走二阶段）

### 二、解耦补丁（1处独立模块）

#### 创建 patches/ 补丁目录

**新增文件**:
- `patches/__init__.py` - 补丁包初始化
- `patches/no_review_layout_patch.py` - 新版"无复审"底栏补丁（约 400 行）

#### 补丁架构设计

**双重门控机制**:

1. **强正向门控**:
   - 在 `_NO_REVIEW_BOTTOM_SLOTS` 上确认最优锚点组
   - 必须包含至少 2 个不同槽位
   - **必须明确包含 `签发机关 (row=2, col=0)` 标签锚点**

2. **强负向守卫**:
   - 在同一底部区域不得存在与 Row 1 几何相容的 `应复审日期` 标签
   - 若存在则立即退出（确认为旧版证件）

**零误触保证**:
- 旧版证件的 `签发机关` 位于右栏 `(2,1)`，几何上不可能通过 `(2,0)` 校验
- 无 `签发机关` 标签的压缩图/干扰图被正向门控拦截

**补齐逻辑**:

1. **核心目标 - 补齐 `签发机关`**:
   - 以确认的 `签发机关 (2,0)` 标签为起点向下收集
   - 允许同栏左对齐换行续接（最多 2 行）
   - 新版底栏第二行右侧无其他字段，**不设置右界限制**
   - 允许超长机关名（如"广东省惠州市安全生产监督管理局"）向右跨过原有效期限列
   - 保留同行水平间隔 `> 2.0 * label_h` 即截断的守卫，防止串入二维码或背面卡

2. **协同兜底 - 补齐 `初领日期` / `有效期截止日期`**:
   - 仅当极端场景（新版证因初领日期标签漏检导致原版未确认底部区域）
   - 先解析右栏有效期，记录 `validity_block_indices` 与 `confirmed_range_bbox`
   - 若初领日期仍为 `None`：
     - 有标签时在签发机关上方且排除有效期块后收集单日期
     - 标签漏检但区间已确认时，借签发机关提供的左列起点在有效期区间同排左侧提取唯一合法单日期

**挂载方式**:

在 `FieldExtractor.__init__` 中添加 `enable_patches: bool = True` 参数，在 `extract_fields` 返回前：

```python
# 6. 应用补丁（仅当存在缺失字段时触发）
if (
    self._enable_patches
    and (fields["签发机关"] is None or fields["初领日期"] is None)
):
    from patches import apply_no_review_layout_patch
    apply_no_review_layout_patch(text_blocks, fields)
```

**Fill-Missing-Only 原则**:
```python
# 只补空位（绝不覆盖原版非空字段）
for key, val in patch_fields.items():
    if fields.get(key) is None and val is not None:
        fields[key] = val
```

**异常静默回退**:
```python
try:
    # 补丁逻辑
    ...
except Exception:
    # 补丁内部任何异常均静默回退，完整保留原版结果
    pass
```

### 三、明确不改动边界

以下模块与核心逻辑本次**完全不修改**：
- `ocr_engine.py`, `config.py`, `data_repository.py`, `file_scanner.py`, `models.py`, `main.py`
- `field_extractor.py` 中的 `_BOTTOM_SLOTS`, `_confirm_region`, `_anchors_compatible`, `_collect_labeled_field_value_with_blocks`（含 `0.35 * label_h` 跨栏判定与 `all_window_rows[0]` 选行守卫）, `_recover_unlabeled_org`
- `two_stage_processor.py` 中的 `expand_x / expand_y` 外扩倍率与 `_merge_fields` 合并策略

## 验证结果

### 1. 单元测试

```bash
pytest -v
```

**结果**: ✓ 所有 22 个测试全部通过

### 2. 谢鹏飞证书识别验证

**修复前**: 5/7 字段（缺失 `初领日期`、`签发机关`）

**修复后**: **7/7 字段**
- ✓ 姓名: 谢鹏飞
- ✓ 性别: 男
- ✓ 作业类别: 高处作业
- ✓ 操作项目: 高处安装、维护、拆除作业
- ✓ **初领日期: 2026-09-20** (新提取)
- ✓ 有效期截止日期: 2032-09-19
- ✓ **签发机关: 惠州市应急管理局** (新提取)

### 3. 零回归保证

- set1 全部 8 份证件（7 份清晰件 + 黄伟平低清图）识别结果不变
- set2 其余 9 份旧版证件识别结果不变（全部 7/7）
- 新版证件（谢鹏飞）从 5/7 提升到 7/7

## 实施记录

**代码改动统计**:
- 原位微调: 2 处（`field_extractor.py` 约 20 行，`two_stage_processor.py` 3 行）
- 解耦补丁: 1 个新模块（`patches/no_review_layout_patch.py` 约 400 行）
- 补丁挂载: 3 行（`field_extractor.py` 中的钩子）

**总计**: 约 426 行新增代码，完全与原识别逻辑解耦

## 技术亮点

1. **严格遵循工程准则**: 按照 AGENTS.md 的要求，能微调的原位微调，可能影响稳定性的严格解耦
2. **双重门控设计**: 强正向+强负向，确保旧版证件零误触
3. **Fill-Missing-Only**: 补丁只补空位，绝不覆盖原版非空字段
4. **异常静默回退**: 任何补丁内部异常都不影响原版结果
5. **一键开关**: `enable_patches=False` 可立即回退到原版逻辑
6. **零阈值变更**: 原位微调不改变任何通用几何阈值，确保旧版稳定性

## 后续建议

1. 若未来新版证件成为主流，可考虑将补丁逻辑合并到主识别流程
2. 若出现更多版式变体，可扩展 patches/ 目录添加新的补丁模块
3. 建议在生产环境收集更多新版证件样本，进一步验证补丁稳定性

---

**实施日期**: 2024-11-24  
**实施人**: Claude (Sonnet 5.5)  
**参考文档**: [fix2.md](fix2.md), [AGENTS.md](../AGENTS.md)
