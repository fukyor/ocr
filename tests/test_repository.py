"""
集成测试 - SQLite 数据持久化、事务回滚、外键约束与幂等查询
"""
import sqlite3
from contextlib import closing
from pathlib import Path
import pytest

from config import FIELD_KEYS
from data_repository import DataRepository
from models import FileInfo, FileStatus, create_empty_fields


def _make_full_fields(name: str = "廖浩庄") -> dict:
    return {
        "姓名": name,
        "性别": "男",
        "作业类别": "高处作业",
        "操作项目": "高处安装、维护、拆除作业",
        "初领日期": "2025-05-13",
        "签发机关": "惠州市应急管理局",
        "有效期截止日期": "2031-05-12",
    }


def test_save_query_and_reopen_persistence(tmp_path: Path):
    """验证两个文件分别按 file_id 查回路径、原文和七个字段，且重开数据库后仍可查回。"""
    db_path = tmp_path / "ocr.db"
    repo = DataRepository(str(db_path))

    f1 = FileInfo("模板1.png", str(tmp_path / "模板1.png"), ".png", "hash_1")
    f2 = FileInfo("模板2.jpg", str(tmp_path / "模板2.jpg"), ".jpg", "hash_2")

    fields2 = create_empty_fields()
    fields2["姓名"] = "罗德财"

    id1 = repo.save_success(f1, "原文1", _make_full_fields("廖浩庄"))
    id2 = repo.save_success(f2, "原文2", fields2)
    assert id1 != id2

    # 重新实例化仓储模拟程序重启
    reopened = DataRepository(str(db_path))
    rec1 = reopened.query_by_file_id(id1)
    rec2 = reopened.query_by_file_id(id2)

    assert rec1 is not None and rec2 is not None
    assert rec1.file_info.file_path == f1.file_path
    assert rec1.raw_text == "原文1"
    assert rec1.status == FileStatus.SUCCESS
    assert len(rec1.fields) == 7
    assert rec1.fields["姓名"] == "廖浩庄"

    # 即使只有一个非空字段，也写齐七行，缺项为 None
    assert rec2.file_info.file_path == f2.file_path
    assert set(rec2.fields.keys()) == set(FIELD_KEYS)
    assert rec2.fields["姓名"] == "罗德财"
    assert rec2.fields["性别"] is None


def test_save_non_success_and_retry_idempotency(tmp_path: Path):
    """验证失败记录（含空哈希和保留原文）可查询且零字段，同文件失败后可重试并成功。"""
    repo = DataRepository(str(tmp_path / "ocr.db"))

    # 1. 读取失败：哈希为空、原文为空
    unreadable = FileInfo("locked.png", str(tmp_path / "locked.png"), ".png", None)
    unreadable_id = repo.save_non_success(unreadable, "failed", "文件被占用", raw_text=None)
    rec_unreadable = repo.query_by_file_id(unreadable_id)
    assert rec_unreadable is not None
    assert rec_unreadable.file_info.file_hash is None
    assert rec_unreadable.raw_text is None
    assert rec_unreadable.fields == {}

    # 2. 首次处理失败（带哈希与原文），随后同一路径同一哈希重试成功
    retry_file = FileInfo("retry.png", str(tmp_path / "retry.png"), ".png", "same_hash")
    fail_id = repo.save_non_success(retry_file, "failed", "首次提取失败", raw_text="已取得原文")
    assert repo.find_existing_success_id(retry_file.file_path, "same_hash") is None

    rec_fail = repo.query_by_file_id(fail_id)
    assert rec_fail is not None
    assert rec_fail.status == FileStatus.FAILED
    assert rec_fail.raw_text == "已取得原文"
    assert rec_fail.fields == {}

    # 重试成功不应与历史 failed 记录冲突
    success_id = repo.save_success(retry_file, "重试原文", _make_full_fields())
    assert success_id != fail_id
    assert repo.find_existing_success_id(retry_file.file_path, "same_hash") == success_id


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """创建旧版数据库，其中已有失败记录、成功记录及其七条字段。"""
    db_path = tmp_path / "ocr.db"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, file_path TEXT NOT NULL,
                file_hash TEXT, status TEXT NOT NULL, raw_text TEXT, error_message TEXT,
                created_at TEXT NOT NULL, UNIQUE(file_path, file_hash)
            );
            CREATE UNIQUE INDEX idx_file_path_hash ON files(file_path, file_hash)
                WHERE file_hash IS NOT NULL AND status = 'success';
            CREATE TABLE extracted_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT NOT NULL,
                field_key TEXT NOT NULL, field_value TEXT,
                FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
                UNIQUE(file_id, field_key)
            );
        """)
        conn.executemany(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("旧失败", "重试.png", str(tmp_path / "重试.png"), "哈希一", "failed",
                 None, "识别失败", "2026-01-01"),
                ("旧成功", "成功.png", str(tmp_path / "成功.png"), "哈希二", "success",
                 "成功原文", None, "2026-01-02"),
            ],
        )
        conn.executemany(
            "INSERT INTO extracted_fields (file_id, field_key, field_value) VALUES ('旧成功', ?, ?)",
            _make_full_fields().items(),
        )
    return db_path


def test_legacy_database_preserves_history_and_allows_retry(legacy_db: Path):
    """旧失败可以反复重试，历史字段、原标识和成功记录的唯一性均保留。"""
    repo = DataRepository(str(legacy_db))
    failed = repo.query_by_file_id("旧失败")
    success = repo.query_by_file_id("旧成功")
    assert failed is not None and success is not None
    assert failed.error_message == "识别失败"
    assert failed.created_at == "2026-01-01"
    assert success.raw_text == "成功原文"
    assert success.fields == _make_full_fields()
    assert repo.find_existing_success_id(success.file_info.file_path, "哈希二") == "旧成功"

    info = failed.file_info
    assert repo.find_existing_success_id(info.file_path, info.file_hash) is None
    retry_id = repo.save_non_success(info, "failed", "再次失败")
    success_id = repo.save_success(info, "重试原文", _make_full_fields())
    reopened = DataRepository(str(legacy_db))
    assert [row.file_id for row in reopened.query_all_files()] == ["旧失败", "旧成功", retry_id, success_id]
    assert reopened.find_existing_success_id(info.file_path, info.file_hash) == success_id
    with pytest.raises(sqlite3.IntegrityError):
        reopened.save_success(info, "重复成功", _make_full_fields())
    with closing(sqlite3.connect(legacy_db)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(("change", "reason"), [
    ("INSERT INTO extracted_fields (file_id, field_key) VALUES ('缺少来源', '姓名')", "外键关联"),
    ("ALTER TABLE files ADD COLUMN 备注 TEXT DEFAULT '历史备注'", "额外列"),
])
def test_legacy_database_migration_rolls_back_on_error(legacy_db: Path, change: str, reason: str):
    """遇到无法迁移的旧数据时，原表、原约束及关联字段仍完整可用。"""
    with closing(sqlite3.connect(legacy_db)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(change)
        original = list(conn.iterdump())

    with pytest.raises(sqlite3.DatabaseError, match=reason):
        DataRepository(str(legacy_db))

    with closing(sqlite3.connect(legacy_db)) as conn:
        assert list(conn.iterdump()) == original


def test_save_success_requires_at_least_one_field(tmp_path: Path):
    """验证零个非空字段不能调用 save_success。"""
    repo = DataRepository(str(tmp_path / "ocr.db"))
    info = FileInfo("empty.png", str(tmp_path / "empty.png"), ".png", "h1")
    with pytest.raises(ValueError):
        repo.save_success(info, "原文", create_empty_fields())


def test_foreign_key_unique_and_transaction_rollback(tmp_path: Path):
    """验证外键级联、同文件字段键唯一，以及写字段中途失败时文件行与字段行一并回滚。"""
    db_path = tmp_path / "ocr.db"
    repo = DataRepository(str(db_path))

    info = FileInfo("fk.png", str(tmp_path / "fk.png"), ".png", "hash_fk")
    fid = repo.save_success(info, "原文", _make_full_fields())

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    # 验证同文件 field_key 唯一约束
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO extracted_fields (file_id, field_key, field_value) VALUES (?, ?, ?)",
            (fid, "姓名", "重复值"),
        )

    # 验证外键约束：不存在的 file_id 不能插入 extracted_fields
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO extracted_fields (file_id, field_key, field_value) VALUES (?, ?, ?)",
            ("non-existent-id", "姓名", "张三"),
        )

    # 验证级联删除
    with conn:
        conn.execute("DELETE FROM files WHERE file_id = ?", (fid,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM extracted_fields WHERE file_id = ?", (fid,)
    ).fetchone()[0]
    assert remaining == 0

    # 通过触发器模拟写第 5 条字段中途失败，验证整笔事务回滚（files 与 extracted_fields 均为零行）
    with conn:
        conn.execute(
            """
            CREATE TRIGGER abort_on_first_date
            BEFORE INSERT ON extracted_fields
            WHEN NEW.field_key = '初领日期'
            BEGIN
                SELECT RAISE(ABORT, 'simulated mid-transaction failure');
            END;
            """
        )
    conn.close()

    info_abort = FileInfo("abort.png", str(tmp_path / "abort.png"), ".png", "hash_abort")
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_success(info_abort, "原文", _make_full_fields())

    conn = sqlite3.connect(str(db_path))
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    field_count = conn.execute("SELECT COUNT(*) FROM extracted_fields").fetchone()[0]
    conn.close()

    assert file_count == 0
    assert field_count == 0
