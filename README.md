# 特种作业操作证 OCR 识别与结构化提取系统

自动读取指定文件夹中的**中华人民共和国特种作业操作证**文件（PNG、JPG/JPEG、单页 PDF），通过本地离线 OCR 识别整页文字，提取固定七个结构化字段，并将原始文件信息、整页 OCR 原文与提取结果持久化至 SQLite 数据库，支持按 `file_id` 追溯源文件。

---

## 一、环境要求

- **操作系统**：Windows（支持中文路径）
- **Python**：3.11 及以上
- **包与运行管理工具**：[`uv`](https://docs.astral.sh/uv/)

> 首次运行时，RapidOCR 会自动下载并缓存所需的 ONNX 模型文件（`ch_PP-OCRv4_det_mobile.onnx` 与 `ch_PP-OCRv5_rec_server.onnx`），之后即可完全离线运行。

---

## 二、快速开始

### 1. 运行识别命令

在项目根目录执行正式高精度识别：

```bash
uv run main.py --input "./证件模板" --db "./data/ocr.db"
```

如需手动开发联调、启用次高精度快速推理（使用 `PP-OCRv4 Mobile` 识别模型，单图速度更快，默认写入独立测试库 `./data/ocr_fast.db`）：

```bash
uv run main.py --input "./证件模板/set1" --fast
```

> 快速模式禁止写入正式默认数据库 `./data/ocr.db`，避免快速档产生的成功记录导致后续正式识别跳过相同文件。

### 2. 命令行参数说明

| 参数 | 是否必填 | 默认值 | 说明 |
|---|---|---|---|
| `--input` | 是 | 无 | 待处理的文件夹路径（仅扫描该文件夹当前层，不递归子目录） |
| `--db` | 否 | 正式模式 `./data/ocr.db`<br>`--fast` 模式 `./data/ocr_fast.db` | SQLite 数据库文件路径（目录不存在时自动创建） |
| `--fast` | 否 | `False` | 启用次高精度快速推理（`ch_PP-OCRv4_rec_mobile.onnx`），仅用于独立测试库联调 |

### 3. 查看运行输出示例

```text
推理模式: 正式高精度 (PP-OCRv5 Server)
数据库路径: E:\D\zuoyewenjian\MyProject\ocr\data\ocr.db
扫描目录: E:\D\zuoyewenjian\MyProject\ocr\证件模板
发现 2 个候选文件
处理: 模板1.png ... [成功: 7/7 字段 | file_id=3782ae09-1c78-4a89-ba91-eef6e9c1fa1e]
处理: 模板2.jpg ... [成功: 7/7 字段 | file_id=badfbe11-6174-4466-a131-38cf7b2c775a]

==================================================
处理统计:
  扫描总数: 2
  成功: 2
  跳过: 0
  不支持: 0
  失败: 0
==================================================
```

---

## 三、输入规则与提取字段

### 支持的文件类型

- 图片：`.png`、`.jpg`、`.jpeg`（大小写不敏感，自动校正 EXIF 方向）
- 文档：单页 `.pdf`（250 DPI 内存渲染；多页 PDF 记为 `unsupported` 并跳过 OCR）
- 仅扫描 `--input` 目录的**当前层**，自动忽略子目录与其他扩展名文件
- 原始文件保留在原路径，不会被移动或修改

### 固定提取的七个字段

| 字段键（`field_key`） | 输出格式与说明 |
|---|---|
| `姓名` | 持证人中文姓名（支持间隔号） |
| `性别` | `男` 或 `女` |
| `作业类别` | 完整作业类别名称（如 `高处作业`） |
| `操作项目` | 完整操作项目名称，支持折行拼接并保留原有标点 |
| `初领日期` | `YYYY-MM-DD` |
| `签发机关` | 完整发证机关名称，支持换行拼接 |
| `有效期截止日期` | 从有效期区间取截止端，统一输出 `YYYY-MM-DD` |

> 无法确定或缺失的字段在数据库中保存为 `NULL`，不编造数据。

---

## 四、查询数据库结果

处理结果保存在 SQLite 数据库的两张表中：

- **`files`**：记录 `file_id`（UUID4 主键）、`file_name`、`file_path`、`file_hash`（SHA-256）、`status`（`success` / `failed` / `unsupported`）、`raw_text`（整页原文）、`error_message`、`created_at`
- **`extracted_fields`**：记录 `id`、`file_id`（外键，级联删除）、`field_key`、`field_value`

### 方式 1：在 Python 代码中按 `file_id` 查询

```python
from data_repository import DataRepository

repo = DataRepository("./data/ocr.db")
record = repo.query_by_file_id("替换为实际的file_id")

if record:
    print("源文件:", record.file_info.file_path)
    print("状态:", record.status.value)
    print("字段:", record.fields)
    print("整页原文:\n", record.raw_text)
```

### 方式 2：使用 SQL 查询

```sql
-- 查询所有文件及其处理状态
SELECT file_id, file_name, status, error_message, created_at
FROM files;

-- 按 file_id 查询关联的结构化字段
SELECT f.file_name, f.file_path, e.field_key, e.field_value
FROM files f
JOIN extracted_fields e ON f.file_id = e.file_id
WHERE f.file_id = '替换为实际的file_id';
```

---

## 五、幂等跳过与异常处理机制

- **重复运行跳过**：同路径且文件内容 SHA-256 未变化的成功文件会被自动跳过，保留原有 `file_id`。
- **变更与失败重试**：文件内容发生变化、同名但路径不同、或此前处理失败（`failed`）的文件会重新进入识别流程。
- **单文件异常隔离**：单个文件无法读取、损坏、为空、多页 PDF、未识别到有效文字或提取失败时，仅登记当前文件状态，不影响同批次其他文件。
- **原文保留**：已取得 OCR 原文但未能提取出可信字段（或提取过程异常）的文件，记为 `failed`，保留 `raw_text` 且关联字段为零行。
- **事务一致性**：每份文件的写入在单事务中完成；若数据库不可写或事务提交失败，回滚未提交事务、报告批次故障并以非零状态码退出，此前已提交的记录不受影响。

---

## 六、运行自动化测试

```bash
uv run --with pytest pytest -v
```

---

## 七、项目目录结构

```text
ocr/
├── pyproject.toml         # uv 项目配置与核心依赖
├── main.py                # CLI 入口、逐文件读取与哈希、主流程编排
├── config.py              # 扩展名、渲染分辨率、七个字段标签与 OCR 参数常量
├── models.py              # 数据契约定义（dataclass / Enum）
├── file_scanner.py        # 扫描目录当前层候选路径
├── ocr_engine.py          # 图片/单页 PDF 内存解码、校验与整页 OCR
├── field_extractor.py     # 正面双区域定位、按栏组装与字段校验
├── data_repository.py     # SQLite 事务持久化与查询
├── tests/                 # 单元测试与集成测试
├── 证件模板/              # 样本图片目录
└── data/                  # 运行时自动生成的数据库目录（默认 data/ocr.db）
```
