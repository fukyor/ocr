"""
数据持久化模块

使用 Python 标准库 sqlite3，按单文件事务保存处理记录及关联字段。
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from config import DEFAULT_DB_PATH, FIELD_KEYS
from models import (
    CertificateKey,
    CertificateRecord,
    ExportRow,
    FieldDict,
    FileInfo,
    FileStatus,
    ProcessResult,
    count_non_empty_fields,
    extract_certificate_key,
)


_FILES_SCHEMA = """(
    file_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'unsupported')),
    raw_text TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
)"""


class DataRepository:
    """SQLite 数据仓储层。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        """
        初始化数据仓储并确保表结构与索引存在。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """创建启用外键约束的数据库连接。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_database(self) -> None:
        """初始化四表（files、extracted_fields、certificates、certificate_files）及索引。"""
        conn = self._get_connection()
        try:
            # 仅此建库连接需暂停外键，避免重建父表时级联删除历史字段。
            conn.execute("PRAGMA foreign_keys = OFF")
            with conn:
                # 在检查旧结构前取得写锁，确保并发启动时只迁移一次。
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"CREATE TABLE IF NOT EXISTS files {_FILES_SCHEMA}")
                self._migrate_legacy_files(conn)
                # 仅对成功且哈希非空的记录建立唯一组合索引：
                # 既加速幂等查询，也允许历史失败文件重新识别并登记新记录
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_files_success_path_hash
                    ON files(file_path, file_hash)
                    WHERE status = 'success' AND file_hash IS NOT NULL
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS extracted_fields (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id TEXT NOT NULL,
                        field_key TEXT NOT NULL,
                        field_value TEXT,
                        FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
                        UNIQUE(file_id, field_key)
                    )
                    """
                )
                # 新增：certificates 表（证件归并主表，联合主键）
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS certificates (
                        cert_no TEXT NOT NULL,
                        work_category TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (cert_no, work_category),
                        CHECK (length(trim(cert_no)) > 0 AND length(trim(work_category)) > 0)
                    )
                    """
                )
                # 新增：certificate_files 表（证件与文件的多对多关联表）
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS certificate_files (
                        file_id TEXT PRIMARY KEY,
                        cert_no TEXT NOT NULL,
                        work_category TEXT NOT NULL,
                        linked_at TEXT NOT NULL,
                        FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
                        FOREIGN KEY (cert_no, work_category)
                            REFERENCES certificates(cert_no, work_category) ON DELETE RESTRICT
                    )
                    """
                )
                # 为 certificate_files 建立联合主键查询索引
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cert_files_cert_key
                    ON certificate_files(cert_no, work_category)
                    """
                )
        finally:
            conn.close()

    @staticmethod
    def _migrate_legacy_files(conn: sqlite3.Connection) -> None:
        """移除旧库对所有状态生效的路径、哈希唯一约束，保留历史记录。"""
        obsolete_indexes = set()
        for (name,) in conn.execute("""
            SELECT name FROM pragma_index_list('files') WHERE "unique" = 1 AND partial = 0
        """):
            columns = {row[0] for row in conn.execute("SELECT name FROM pragma_index_info(?)", (name,))}
            if columns == {"file_path", "file_hash"}:
                obsolete_indexes.add(name)
        if not obsolete_indexes:
            return

        # 表级 UNIQUE 的自动索引只能通过重建表移除。其余自定义索引和触发器需恢复。
        schema_objects = conn.execute("""
            SELECT type, name, sql FROM sqlite_master
            WHERE tbl_name = 'files' AND type IN ('index', 'trigger') AND sql IS NOT NULL
            ORDER BY rowid
        """).fetchall()
        conn.execute(f"CREATE TABLE files_migrated {_FILES_SCHEMA}")
        extra_column = conn.execute("""
            SELECT name FROM pragma_table_xinfo('files')
            EXCEPT SELECT name FROM pragma_table_xinfo('files_migrated')
        """).fetchone()
        if extra_column is not None:
            raise sqlite3.DatabaseError(f"旧文件表包含需要保留的额外列：{extra_column[0]}")
        conn.execute("""
            INSERT INTO files_migrated
                (rowid, file_id, file_name, file_path, file_hash, status, raw_text, error_message, created_at)
            SELECT rowid, file_id, file_name, file_path, file_hash, status, raw_text, error_message, created_at
            FROM files
        """)
        # 换回原表名时，其他表的外键、视图和触发器仍引用 files。
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE files_migrated RENAME TO files")

        obsolete_indexes.update(("idx_file_path_hash", "idx_files_success_path_hash"))
        for kind, name, sql in schema_objects:
            if kind == "index" and name in obsolete_indexes:
                continue
            conn.execute(sql)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("旧库迁移后发现无效的外键关联")

    def find_existing_success_id(
        self,
        file_path: str,
        file_hash: str,
    ) -> Optional[str]:
        """
        按规范化绝对路径和内容哈希查询是否已有成功记录。

        Args:
            file_path: 规范化绝对路径
            file_hash: 文件 SHA-256 哈希

        Returns:
            命中时返回原 file_id，否则返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT file_id
                FROM files
                WHERE file_path = ? AND file_hash = ? AND status = 'success'
                LIMIT 1
                """,
                (file_path, file_hash),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def save_success(
        self,
        file_info: FileInfo,
        raw_text: str,
        fields: FieldDict,
    ) -> str:
        """
        单事务保存成功文件记录、整页原文和固定九条字段。
        当证号与作业类别均非空时，同事务内原子 upsert certificates 并建立关联。

        前提：必须已取得 file_hash，且 fields 包含固定九个键并至少有一个可信非空值。

        Args:
            file_info: 文件信息
            raw_text: 整页 OCR 原文
            fields: 固定九键字段字典

        Returns:
            本次保存生成的 UUID4 file_id
        """
        if not file_info.file_hash:
            raise ValueError("保存成功记录时 file_hash 不能为空")
        if set(fields.keys()) != set(FIELD_KEYS):
            raise ValueError(f"字段集合必须严格等于固定九个键: {FIELD_KEYS}")
        if count_non_empty_fields(fields) < 1:
            raise ValueError("保存成功记录的前提是至少存在一个可信非空字段")

        file_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        cert_key = extract_certificate_key(fields)

        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO files
                        (file_id, file_name, file_path, file_hash, status, raw_text, error_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        file_info.file_name,
                        file_info.file_path,
                        file_info.file_hash,
                        FileStatus.SUCCESS.value,
                        raw_text,
                        None,
                        created_at,
                    ),
                )
                for key in FIELD_KEYS:
                    conn.execute(
                        """
                        INSERT INTO extracted_fields (file_id, field_key, field_value)
                        VALUES (?, ?, ?)
                        """,
                        (file_id, key, fields[key]),
                    )

                # 若证号与作业类别均非空，建立证件归并与关联
                if cert_key is not None:
                    cert_no, work_category = cert_key
                    conn.execute(
                        """
                        INSERT INTO certificates (cert_no, work_category, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(cert_no, work_category) DO UPDATE SET updated_at = excluded.updated_at
                        """,
                        (cert_no, work_category, created_at, created_at),
                    )
                    conn.execute(
                        """
                        INSERT INTO certificate_files (file_id, cert_no, work_category, linked_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (file_id, cert_no, work_category, created_at),
                    )
            return file_id
        finally:
            conn.close()

    def save_non_success(
        self,
        file_info: FileInfo,
        status: str,
        error_message: str,
        raw_text: Optional[str] = None,
    ) -> str:
        """
        单事务保存失败或不支持的文件记录（关联字段为零行）。

        Args:
            file_info: 文件信息（读取失败时 file_hash 可为 None）
            status: 'failed' 或 'unsupported'
            error_message: 具体错误或不支持原因
            raw_text: 可选的已取得整页原文

        Returns:
            本次保存生成的 UUID4 file_id
        """
        if status not in (FileStatus.FAILED.value, FileStatus.UNSUPPORTED.value):
            raise ValueError(f"非成功状态仅允许 failed 或 unsupported，收到: {status}")

        file_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO files
                        (file_id, file_name, file_path, file_hash, status, raw_text, error_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        file_info.file_name,
                        file_info.file_path,
                        file_info.file_hash,
                        status,
                        raw_text,
                        error_message,
                        created_at,
                    ),
                )
            return file_id
        finally:
            conn.close()

    def query_by_file_id(self, file_id: str) -> Optional[ProcessResult]:
        """
        按 file_id 查回文件信息、整页原文和关联字段。

        失败及不支持的记录也可查回，其 fields 为空字典 {}。

        Args:
            file_id: 文件唯一标识

        Returns:
            ProcessResult 对象；若不存在则返回 None
        """
        conn = self._get_connection()
        try:
            file_row = conn.execute(
                """
                SELECT file_name, file_path, file_hash, status, raw_text, error_message, created_at
                FROM files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()

            if file_row is None:
                return None

            file_name, file_path, file_hash, status_str, raw_text, error_message, created_at = file_row
            file_info = FileInfo(
                file_name=file_name,
                file_path=file_path,
                file_ext=Path(file_path).suffix.lower(),
                file_hash=file_hash,
            )

            field_rows = conn.execute(
                """
                SELECT field_key, field_value
                FROM extracted_fields
                WHERE file_id = ?
                ORDER BY id ASC
                """,
                (file_id,),
            ).fetchall()

            fields: FieldDict = {key: value for key, value in field_rows}

            # 查询证件关联（若存在）
            cert_row = conn.execute(
                """
                SELECT cert_no, work_category
                FROM certificate_files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()

            certificate_key: Optional[CertificateKey] = None
            if cert_row is not None:
                certificate_key = (cert_row[0], cert_row[1])

            return ProcessResult(
                file_id=file_id,
                file_info=file_info,
                status=FileStatus(status_str),
                raw_text=raw_text,
                fields=fields,
                error_message=error_message,
                created_at=created_at,
                certificate_key=certificate_key,
            )
        finally:
            conn.close()

    def query_all_files(self) -> List[ProcessResult]:
        """按创建时间顺序查询库中所有记录。"""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT file_id FROM files ORDER BY rowid ASC"
            ).fetchall()
        finally:
            conn.close()

        results: List[ProcessResult] = []
        for (fid,) in rows:
            record = self.query_by_file_id(fid)
            if record is not None:
                results.append(record)
        return results

    def query_by_certificate_key(
        self,
        cert_no: str,
        work_category: str,
    ) -> Optional[CertificateRecord]:
        """
        按联合主键查询证件归并记录。

        Args:
            cert_no: 证号
            work_category: 作业类别

        Returns:
            CertificateRecord 对象；若不存在则返回 None
        """
        conn = self._get_connection()
        try:
            cert_row = conn.execute(
                """
                SELECT created_at, updated_at
                FROM certificates
                WHERE cert_no = ? AND work_category = ?
                """,
                (cert_no, work_category),
            ).fetchone()

            if cert_row is None:
                return None

            created_at, updated_at = cert_row

            # 查询关联的所有文件 ID（按关联时间升序）
            file_rows = conn.execute(
                """
                SELECT file_id
                FROM certificate_files
                WHERE cert_no = ? AND work_category = ?
                ORDER BY linked_at ASC, rowid ASC
                """,
                (cert_no, work_category),
            ).fetchall()

            file_ids = [row[0] for row in file_rows]

            return CertificateRecord(
                cert_no=cert_no,
                work_category=work_category,
                file_ids=file_ids,
                created_at=created_at,
                updated_at=updated_at,
            )
        finally:
            conn.close()

    def query_files_by_certificate_key(
        self,
        cert_no: str,
        work_category: str,
    ) -> List[ProcessResult]:
        """
        按联合主键查询该证件关联的所有文件记录。

        Args:
            cert_no: 证号
            work_category: 作业类别

        Returns:
            ProcessResult 列表（按关联时间升序）
        """
        cert_record = self.query_by_certificate_key(cert_no, work_category)
        if cert_record is None:
            return []

        results: List[ProcessResult] = []
        for file_id in cert_record.file_ids:
            record = self.query_by_file_id(file_id)
            if record is not None:
                results.append(record)
        return results

    def query_all_certificates(self) -> List[CertificateRecord]:
        """按创建时间顺序查询库中所有证件归并记录。"""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT cert_no, work_category
                FROM certificates
                ORDER BY rowid ASC
                """
            ).fetchall()
        finally:
            conn.close()

        results: List[CertificateRecord] = []
        for cert_no, work_category in rows:
            record = self.query_by_certificate_key(cert_no, work_category)
            if record is not None:
                results.append(record)
        return results

    @staticmethod
    def query_export_rows_readonly(db_path: str) -> List[ExportRow]:
        """
        FR-06 纯只读导出查询入口（静态方法，不实例化 DataRepository）。

        以 SQLite URI ?mode=ro + PRAGMA query_only = ON 打开连接，
        在单读事务内校验表结构并按 files.rowid ASC 批量装配 ExportRow 列表。
        旧库无 certificate_files 表时自动兼容降级，绝不报错。

        前置校验：
        - 路径存在且为常规文件（不存在/是目录 → 抛出 FileNotFoundError/IsADirectoryError，绝不新建空库）
        - 数据库可读且包含 files 和 extracted_fields 表及必需列（损坏/缺表 → 抛出 sqlite3.DatabaseError）

        Args:
            db_path: 数据库文件路径

        Returns:
            ExportRow 列表（按入库顺序，可为空列表）

        Raises:
            FileNotFoundError: 数据库文件不存在
            IsADirectoryError: 数据库路径是目录而非文件
            sqlite3.Error: 数据库无法读取、损坏或表结构不完整
            OSError: 其他文件系统错误
        """
        target = Path(db_path)

        # 前置校验：路径存在且为常规文件
        if not target.exists():
            raise FileNotFoundError(f"数据库文件不存在: {target}")
        if not target.is_file():
            raise IsADirectoryError(f"数据库路径不是有效文件: {target}")

        resolved = target.resolve()
        uri = f"file:///{resolved.as_posix()}?mode=ro"

        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")  # 保证多表查询位于同一只读快照中

            # 1. 探测现有表集合并校验必需表
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing_tables = {"files", "extracted_fields"} - tables
            if missing_tables:
                raise sqlite3.DatabaseError(
                    f"数据库表结构不完整，缺少必需表: {sorted(missing_tables)}"
                )

            # 2. 校验必需列存在（即使 0 条记录也能提前暴露异常表结构）
            files_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info('files')")
            }
            required_files_columns = {"file_id", "file_name", "file_path", "error_message"}
            missing_files_columns = required_files_columns - files_columns
            if missing_files_columns:
                raise sqlite3.DatabaseError(
                    f"files 表缺少必需列: {sorted(missing_files_columns)}"
                )

            fields_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info('extracted_fields')")
            }
            required_fields_columns = {"file_id", "field_key", "field_value"}
            missing_fields_columns = required_fields_columns - fields_columns
            if missing_fields_columns:
                raise sqlite3.DatabaseError(
                    f"extracted_fields 表缺少必需列: {sorted(missing_fields_columns)}"
                )

            # 3. 按入库顺序读取全部 files 记录
            file_rows = conn.execute(
                """
                SELECT file_id, file_name, file_path, error_message
                FROM files
                ORDER BY rowid ASC
                """
            ).fetchall()

            # 4. 批量读取 extracted_fields 并按 file_id 分组（避免 N+1 查询）
            fields_by_file: Dict[str, FieldDict] = {}
            for fid, fkey, fval in conn.execute(
                """
                SELECT file_id, field_key, field_value
                FROM extracted_fields
                ORDER BY id ASC
                """
            ):
                fields_by_file.setdefault(fid, {})[fkey] = fval

            # 5. 若存在 certificate_files 表则批量读取证件关联（兼容早期 7 字段旧库）
            cert_by_file: Dict[str, CertificateKey] = {}
            if "certificate_files" in tables:
                cert_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info('certificate_files')")
                }
                required_cert_columns = {"file_id", "cert_no", "work_category"}
                if required_cert_columns.issubset(cert_columns):
                    for fid, cno, wcat in conn.execute(
                        """
                        SELECT file_id, cert_no, work_category
                        FROM certificate_files
                        """
                    ):
                        cert_by_file[fid] = (cno, wcat)

            # 6. 逐条组装 ExportRow（每条 files 记录严格占一行）
            result: List[ExportRow] = []
            for fid, fname, fpath, err_msg in file_rows:
                result.append(
                    ExportRow(
                        file_id=fid,
                        file_name=fname,
                        file_path=fpath,
                        error_message=err_msg,
                        fields=fields_by_file.get(fid, {}),
                        certificate_key=cert_by_file.get(fid),
                    )
                )
            return result
        finally:
            conn.close()

