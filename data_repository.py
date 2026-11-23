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
    FieldDict,
    FileInfo,
    FileStatus,
    ProcessResult,
    count_non_empty_fields,
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
        """初始化 files 表、extracted_fields 表及索引。"""
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
        单事务保存成功文件记录、整页原文和固定七条字段。

        前提：必须已取得 file_hash，且 fields 包含固定七个键并至少有一个可信非空值。

        Args:
            file_info: 文件信息
            raw_text: 整页 OCR 原文
            fields: 固定七键字段字典

        Returns:
            本次保存生成的 UUID4 file_id
        """
        if not file_info.file_hash:
            raise ValueError("保存成功记录时 file_hash 不能为空")
        if set(fields.keys()) != set(FIELD_KEYS):
            raise ValueError(f"字段集合必须严格等于固定七个键: {FIELD_KEYS}")
        if count_non_empty_fields(fields) < 1:
            raise ValueError("保存成功记录的前提是至少存在一个可信非空字段")

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

            return ProcessResult(
                file_id=file_id,
                file_info=file_info,
                status=FileStatus(status_str),
                raw_text=raw_text,
                fields=fields,
                error_message=error_message,
                created_at=created_at,
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
