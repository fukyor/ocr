"""
验证脚本 - 查询并对比提取的字段值
"""
import sqlite3
from pathlib import Path


def query_and_verify():
    """查询数据库并验证提取结果"""
    db_path = Path("./data/test_ocr.db")

    if not db_path.exists():
        print("数据库不存在")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # 查询所有文件记录
    cursor.execute("SELECT file_id, file_name, status FROM files")
    files = cursor.fetchall()

    print("=" * 80)
    print("OCR 处理结果验证")
    print("=" * 80)

    # 验收基准
    baseline = {
        "模板1.png": {
            "姓名": "廖浩庄",
            "性别": "男",
            "作业类别": "高处作业",
            "操作项目": "高处安装、维护、拆除作业",
            "初领日期": "2025-05-13",
            "签发机关": "惠州市应急管理局",
            "有效期截止日期": "2031-05-12",
        },
        "模板2.jpg": {
            "姓名": "罗德财",
            "性别": "男",
            "作业类别": "高处作业",
            "操作项目": "登高架设作业",
            "初领日期": "2019-03-06",
            "签发机关": "东莞市应急管理局",
            "有效期截止日期": "2028-08-28",
        },
    }

    for file_id, file_name, status in files:
        print(f"\n文件: {file_name}")
        print(f"  状态: {status}")

        if status != "success":
            continue

        # 查询该文件的字段
        cursor.execute(
            "SELECT field_key, field_value FROM extracted_fields WHERE file_id = ?",
            (file_id,)
        )
        fields = cursor.fetchall()

        print(f"\n  提取字段:")
        actual_fields = {}
        for field_key, field_value in fields:
            actual_fields[field_key] = field_value
            print(f"    {field_key}: {field_value}")

        # 对比验收基准
        if file_name in baseline:
            print(f"\n  验收对比:")
            expected = baseline[file_name]
            correct_count = 0
            missing_count = 0
            wrong_count = 0

            for key, expected_value in expected.items():
                actual_value = actual_fields.get(key)
                if actual_value == expected_value:
                    print(f"    [OK] {key}: {actual_value}")
                    correct_count += 1
                elif actual_value is None:
                    print(f"    [MISS] {key}: 缺失 (期望: {expected_value})")
                    missing_count += 1
                else:
                    print(f"    [ERR] {key}: {actual_value} (期望: {expected_value})")
                    wrong_count += 1

            print(f"\n  统计: [OK]{correct_count}/7 | [MISS]{missing_count} | [ERR]{wrong_count}")

    conn.close()

    print("\n" + "=" * 80)


if __name__ == "__main__":
    query_and_verify()
