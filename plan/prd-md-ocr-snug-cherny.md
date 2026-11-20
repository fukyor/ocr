# 特种作业操作证OCR系统 - 第二阶段模块设计方案

## Context

本项目旨在实现特种作业操作证的自动识别与结构化数据提取。系统需要处理PDF、PNG、JPG三种格式的证件文件，提取10个固定字段并保存为结构化数据。

**核心挑战：**
- 证件存在两种不同排版（横版纸质证件 + 竖版电子证件），字段位置不固定
- 需要通过语义匹配而非固定坐标来提取字段
- 日期格式多样（"2025-05-13"、"2025-05-13至2031-05-12"、"应于2028-05-12前进行复审"）

**设计原则：**
- 最小化设计：只实现当前必需功能
- 统一处理：一套算法适配两种排版
- 配置驱动：字段定义可扩展
- 异常隔离：单文件失败不影响整体

---

## 一、模块划分

系统采用4个核心模块 + 1个主流程编排，职责清晰分离：

### 1.1 文件扫描模块（file_scanner.py）

**职责：** 扫描指定文件夹，过滤支持的文件格式

**核心功能：**
- 扫描指定目录（不递归子文件夹）
- 过滤PDF、PNG、JPG/JPEG文件
- PDF多页检测（只处理单页PDF）
- 生成文件清单供后续处理

**关键接口：**
```python
def scan_folder(folder_path: str) -> List[FileInfo]:
    """扫描文件夹，返回可处理的文件列表"""
    pass

def is_single_page_pdf(pdf_path: str) -> bool:
    """检测PDF是否为单页"""
    pass
```

---

### 1.2 OCR识别模块（ocr_engine.py）

**职责：** 调用PaddleOCR进行文字识别，返回文本内容和坐标

**核心功能：**
- PDF转图片（使用pdf2image）
- 调用PaddleOCR识别文字
- 返回文本内容 + 文本块坐标（支持邻近性算法）
- 异常处理（识别失败、文件损坏）

**关键接口：**
```python
class OCRResult:
    text_blocks: List[TextBlock]  # [(坐标, 文本, 置信度)]
    raw_text: str                 # 完整文本
    success: bool
    error_message: Optional[str]

def recognize(file_path: str) -> OCRResult:
    """识别文件中的文字，返回文本和坐标"""
    pass
```

**技术选型：**
- **PaddleOCR (PP-OCRv5)**: 开源免费，中文识别准确率高，CPU可运行
- **pdf2image**: PDF转图片处理
- **Pillow**: 图片预处理

---

### 1.3 字段提取模块（field_extractor.py）

**职责：** 从OCR结果中提取10个固定字段，适配两种排版

**核心策略：三层提取算法**

#### 策略1: 坐标邻近性搜索（主策略）
适用于有坐标信息的OCR结果

**算法流程：**
1. 在所有文本块中搜索字段关键词（如"姓名"）
2. 找到关键词后，在其右侧或下方搜索候选值
3. 按距离排序，取最近的符合正则的文本块
4. 支持多关键词匹配（如"有效期至"、"有效期截止"）

**核心代码逻辑：**
```python
def extract_by_proximity(text_blocks, field_config):
    # 1. 找到字段名关键词的文本块
    keyword_block = find_keyword(text_blocks, field_config['keywords'])
    
    # 2. 在右侧或下方搜索候选值
    candidates = find_nearby_blocks(
        text_blocks, 
        keyword_block, 
        direction=field_config['search_direction']
    )
    
    # 3. 按距离排序 + 正则过滤
    for candidate in sorted(candidates, key=lambda b: distance(keyword_block, b)):
        if re.match(field_config['value_pattern'], candidate.text):
            return candidate.text
    
    return None
```

#### 策略2: 正则后备匹配（降级策略）
当OCR不返回坐标或邻近性搜索失败时使用

**模式：** `字段名[：:\s]*值正则`

示例：
- `姓名[:：\s]*([一-龥]{2,4})`
- `有效期至[:：\s]*(\d{4}-\d{2}-\d{2})`

#### 策略3: 特殊字段处理

**日期字段（4个）：**
- **初领日期**: 匹配"初领日期"关键词
- **有效期起始日期**: 提取"有效期X至Y"中的X，或"初领日期"（当无起始日期时）
- **有效期截止日期**: 提取"有效期X至Y"中的Y，或"有效期至X"
- **复审截止日期**: 匹配"应于X前进行复审"或"复审日期X"

**日期归一化**: 统一转换为YYYY-MM-DD格式
- "2025-05-13" → "2025-05-13"
- "2025年5月13日" → "2025-05-13"
- "2025.05.13" → "2025-05-13"

**关键接口：**
```python
def extract_fields(ocr_result: OCRResult) -> Dict[str, Optional[str]]:
    """从OCR结果提取10个字段"""
    pass

def normalize_date(date_str: str) -> Optional[str]:
    """日期归一化为YYYY-MM-DD"""
    pass
```

---

### 1.4 数据持久化模块（data_repository.py）

**职责：** SQLite数据库操作，保存文件信息和提取字段

**数据库设计：**

```sql
-- 文件表
CREATE TABLE files (
    file_id TEXT PRIMARY KEY,           -- UUID
    file_name TEXT NOT NULL,            -- 原始文件名
    file_path TEXT NOT NULL,            -- 原始文件路径
    raw_text TEXT,                      -- OCR识别的完整文本
    status TEXT NOT NULL,               -- success | failed
    error_message TEXT,                 -- 失败原因
    created_at TEXT NOT NULL            -- 处理时间 ISO8601
);

-- 字段表
CREATE TABLE extracted_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,              -- 关联文件ID
    field_key TEXT NOT NULL,            -- 字段名称
    field_value TEXT,                   -- 字段值（NULL表示未识别）
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);

-- 索引
CREATE INDEX idx_file_id ON extracted_fields(file_id);
CREATE INDEX idx_field_key ON extracted_fields(field_key);
```

**关键接口：**
```python
def save_file_record(file_info, ocr_result, status) -> str:
    """保存文件记录，返回file_id"""
    pass

def save_extracted_fields(file_id: str, fields: Dict[str, Optional[str]]):
    """保存提取的字段"""
    pass

def query_by_file_id(file_id: str) -> Dict:
    """查询文件及其所有字段"""
    pass
```

---

### 1.5 主流程编排（main.py）

**职责：** 串联各模块，实现完整的处理流程

**流程：**
```
1. 扫描文件夹 → 获取文件列表
2. 遍历每个文件：
   a. OCR识别
   b. 字段提取
   c. 保存数据库
   d. 异常隔离（单文件失败不影响其他）
3. 输出处理报告
```

**关键逻辑：**
```python
def process_folder(folder_path: str):
    files = scan_folder(folder_path)
    
    for file_info in files:
        try:
            # OCR识别
            ocr_result = ocr_engine.recognize(file_info.path)
            
            if not ocr_result.success:
                save_failed_record(file_info, ocr_result.error_message)
                continue
            
            # 字段提取
            fields = field_extractor.extract_fields(ocr_result)
            
            # 保存数据库
            file_id = repository.save_file_record(file_info, ocr_result, 'success')
            repository.save_extracted_fields(file_id, fields)
            
        except Exception as e:
            save_failed_record(file_info, str(e))
            # 继续处理下一个文件
```

---

## 二、字段配置（config.py）

**10个字段的配置定义：**

```python
FIELD_PATTERNS = {
    '姓名': {
        'keywords': ['姓名'],
        'value_pattern': r'[一-龥]{2,4}',
        'search_direction': 'right_or_below'
    },
    '性别': {
        'keywords': ['性别'],
        'value_pattern': r'[男女]',
        'search_direction': 'right_or_below'
    },
    '作业类别': {
        'keywords': ['作业类别', '作业类型'],
        'value_pattern': r'[一-龥]+作业',
        'search_direction': 'right_or_below'
    },
    '操作项目': {
        'keywords': ['操作项目', '操作类型'],
        'value_pattern': r'.{2,20}',
        'search_direction': 'right_or_below'
    },
    '初领日期': {
        'keywords': ['初领日期', '初次领取日期'],
        'value_pattern': r'\d{4}[-年.]\d{1,2}[-月.]\d{1,2}日?',
        'search_direction': 'right_or_below'
    },
    '有效期起始日期': {
        'keywords': ['有效期', '有效期限'],
        'value_pattern': r'\d{4}[-年.]\d{1,2}[-月.]\d{1,2}日?(?=至|[-—])',
        'search_direction': 'right_or_below',
        'extract_mode': 'range_start'  # 提取"X至Y"中的X
    },
    '有效期截止日期': {
        'keywords': ['有效期至', '有效期截止', '有效期'],
        'value_pattern': r'\d{4}[-年.]\d{1,2}[-月.]\d{1,2}日?',
        'search_direction': 'right_or_below',
        'extract_mode': 'range_end'  # 提取"X至Y"中的Y
    },
    '复审截止日期': {
        'keywords': ['应于.*前进行复审', '复审日期', '复审有效期'],
        'value_pattern': r'\d{4}[-年.]\d{1,2}[-月.]\d{1,2}日?',
        'search_direction': 'right_or_below'
    },
    '签发机关': {
        'keywords': ['签发机关', '发证机关', '签发单位'],
        'value_pattern': r'.{2,20}(应急)?管理局',
        'search_direction': 'right_or_below'
    },
    '档案编码': {  # 额外字段，便于追溯
        'keywords': ['档案编码', '编号'],
        'value_pattern': r'[A-Z0-9]{10,20}',
        'search_direction': 'right_or_below'
    }
}
```

---

## 三、技术栈

| 组件 | 技术选型 | 说明 |
|------|---------|------|
| 编程语言 | Python 3.8+ | OCR生态成熟 |
| OCR引擎 | PaddleOCR (PP-OCRv5) | 开源免费，中文友好，CPU可运行，返回坐标支持邻近性算法 |
| PDF处理 | pdf2image + PyMuPDF | PDF转图片 + 页数检测 |
| 图片处理 | Pillow | 图片预处理 |
| 数据存储 | SQLite | 轻量级，零运维，单文件数据库 |
| 日期解析 | dateutil.parser | 自动识别多种日期格式 |

**依赖清单（requirements.txt）：**
```
paddleocr==2.7.0
paddlepaddle==2.6.0
pdf2image==1.16.3
PyMuPDF==1.23.8
Pillow==10.1.0
python-dateutil==2.8.2
```

---

## 四、目录结构

```
E:/D/zuoyewenjian/MyProject/ocr/
├── main.py                  # 主入口
├── config.py                # 字段配置
├── file_scanner.py          # 文件扫描模块
├── ocr_engine.py            # OCR识别模块
├── field_extractor.py       # 字段提取模块
├── data_repository.py       # 数据持久化模块
├── requirements.txt         # 依赖清单
├── README.md                # 使用说明
├── prd.md                   # 需求文档（已存在）
├── 证件模板/                # 测试样本（已存在）
│   ├── 模板1.png
│   └── 模板2.jpg
├── data/                    # 数据存储目录（运行时创建）
│   └── ocr_database.db      # SQLite数据库
└── input/                   # 待处理文件夹（用户指定）
```

---

## 五、关键实现文件

### 核心文件优先级：

1. **config.py** - 字段配置，驱动整个提取逻辑
2. **field_extractor.py** - 字段提取模块，核心算法实现
3. **ocr_engine.py** - OCR集成，返回文本和坐标
4. **data_repository.py** - 数据库操作
5. **file_scanner.py** - 文件扫描
6. **main.py** - 流程编排

---

## 六、异常处理机制

### 6.1 文件级异常隔离

**原则：** 单个文件处理失败不影响其他文件

**处理方式：**
```python
try:
    # 处理单个文件
    process_file(file_info)
except Exception as e:
    # 记录失败状态
    save_failed_record(file_info, error=str(e))
    logger.error(f"处理文件失败: {file_info.name}, 错误: {e}")
    # 继续处理下一个文件
```

### 6.2 具体异常类型

| 异常类型 | 处理策略 | 状态记录 |
|---------|---------|---------|
| 文件损坏/无法读取 | 记录失败，raw_text=NULL | status='failed' |
| 多页PDF | 跳过处理，记录日志 | 不入库 |
| OCR识别失败 | 记录失败原因 | status='failed' |
| 字段提取失败 | 部分字段为NULL，仍保存 | status='success' |

### 6.3 字段级容错

**原则：** 未识别字段返回NULL，不编造数据

```python
fields = {
    '姓名': '苏志冲',
    '性别': '男',
    '作业类别': '电工作业',
    '操作项目': '低压电工作业',
    '初领日期': '2022-12-21',
    '有效期起始日期': None,  # 未识别
    '有效期截止日期': '2028-12-20',
    '复审截止日期': None,  # 未识别
    '签发机关': '云浮市应急管理局'
}
```

---

## 七、扩展性设计

### 7.1 新增字段

**步骤：**
1. 在`config.py`的`FIELD_PATTERNS`添加字段定义
2. 无需修改核心代码

### 7.2 新增证件类型

**预留接口：**
```python
def detect_certificate_type(ocr_result) -> str:
    """通过关键词识别证件类型"""
    if '特种作业操作证' in ocr_result.raw_text:
        return 'special_work_certificate'
    # 未来可扩展其他类型
    return 'unknown'

def get_field_config(cert_type: str) -> Dict:
    """根据证件类型获取字段配置"""
    return FIELD_PATTERNS_MAP[cert_type]
```

### 7.3 OCR引擎可替换

**接口抽象：**
```python
class OCREngine(ABC):
    @abstractmethod
    def recognize(self, file_path: str) -> OCRResult:
        pass

# 当前实现
class PaddleOCREngine(OCREngine):
    def recognize(self, file_path: str) -> OCRResult:
        # PaddleOCR实现
        pass

# 未来可替换为云服务
class CloudOCREngine(OCREngine):
    def recognize(self, file_path: str) -> OCRResult:
        # 调用阿里云/腾讯云API
        pass
```

---

## 八、验证方案

### 8.1 单元测试

**测试文件：** `tests/test_*.py`

**测试用例：**
1. **文件扫描测试**
   - 正确过滤PDF/PNG/JPG
   - 多页PDF检测准确
   - 空文件夹处理

2. **OCR识别测试**
   - 模板1（横版）识别准确率
   - 模板2（竖版）识别准确率
   - PDF转图片正确

3. **字段提取测试**
   - 10个字段全部提取正确
   - 日期归一化测试
   - 缺失字段返回NULL

4. **数据库测试**
   - file_id唯一性
   - 关联关系正确
   - 查询功能正常

### 8.2 集成测试

**测试场景：**

**场景1: 标准流程**
- 输入：证件模板文件夹（2个文件）
- 预期：2条文件记录，各10个字段
- 验证：字段值与模板对应

**场景2: 异常文件**
- 输入：包含损坏图片、多页PDF的文件夹
- 预期：异常文件记录失败状态，其他文件正常处理
- 验证：status='failed'，不产生虚假数据

**场景3: 持久化验证**
- 操作：处理文件 → 退出程序 → 重新启动
- 预期：数据仍然存在
- 验证：通过file_id查询到完整记录

### 8.3 端到端验证

**命令示例：**
```bash
python main.py --input ./证件模板 --output ./data
```

**验收标准：**
1. ✅ 自动读取2个模板文件
2. ✅ OCR识别成功，raw_text非空
3. ✅ 10个字段提取准确（至少80%完整率）
4. ✅ 日期格式统一为YYYY-MM-DD
5. ✅ 数据保存到SQLite
6. ✅ 通过file_id可追溯原文件
7. ✅ 单文件失败不影响其他文件

---

## 九、实现计划（预估7.5天）

### Phase 1: 基础框架（2天）
- 搭建目录结构
- 实现file_scanner.py
- 实现data_repository.py
- SQLite建表脚本
- main.py流程编排（不含OCR和提取）

### Phase 2: OCR集成（1天）
- 安装PaddleOCR及依赖
- 实现ocr_engine.py
- PDF转图片处理
- 在模板文件上测试识别效果

### Phase 3: 字段提取（3天）
- 编写config.py字段配置
- 实现邻近性算法
- 实现正则后备匹配
- 日期归一化处理
- 在两个模板上测试提取准确率
- 调优算法参数

### Phase 4: 异常处理与测试（1天）
- 异常文件处理
- 单元测试编写
- 边界情况测试
- 异常隔离验证

### Phase 5: 文档与交付（0.5天）
- 编写README.md
- 使用说明文档
- 代码注释完善

---

## 十、关键设计决策总结

| 决策点 | 选择 | 理由 |
|-------|------|------|
| OCR引擎 | PaddleOCR | 开源、中文友好、CPU可运行、返回坐标 |
| 字段提取 | 坐标邻近性 + 正则后备 | 适配两种排版，不依赖固定坐标 |
| 数据存储 | SQLite | 轻量级、零运维、关系查询 |
| 模块数量 | 4个核心模块 | 最小化设计，职责清晰 |
| 字段配置 | config.py集中配置 | 便于扩展新字段 |
| 异常处理 | 文件级隔离 | 单文件失败不影响整体 |
| 日期格式 | 统一YYYY-MM-DD | 便于后续查询和比较 |

---

## 十一、潜在风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| OCR识别率低 | 字段提取失败 | 图片预处理（去噪、二值化）<br>降级到云API |
| 两种排版差异大 | 邻近性算法失败 | 正则后备匹配<br>多关键词匹配 |
| 日期格式多样 | 解析失败 | dateutil.parser<br>多正则模式 |
| 手机截图包含干扰 | 误识别 | 坐标过滤（只搜索证件区域）<br>置信度阈值 |

---

## 结语

本模块设计方案遵循MVP最小化原则，采用统一的语义匹配策略适配两种排版，通过配置驱动实现扩展性。核心算法（坐标邻近性 + 正则后备）既保证了准确率，又避免了过度设计。

下一步将按Phase 1-5的计划逐步实现各模块，并在两个模板文件上持续验证效果。
