"""
单元测试 - 字段提取模块（覆盖文档 7.1 全部位置归属、缩放平移、干扰错字、折行拆块、标签漏检与降级用例）
"""
from typing import List

from field_extractor import extract_fields
from models import TextBlock


def _template1_blocks() -> List[TextBlock]:
    """人工核对过的模板1（横版打印页，1679x1127）真实 OCR 文本块。"""
    raw = [
        ("中华人民共和国特种作业操作证", 0.998, 407.0, 163.0, 905.0, 59.0),
        ("档案编码：A44130044325002074", 0.994, 617.0, 274.0, 457.0, 29.0),
        ("证号", 0.999, 505.0, 449.0, 23.0, 15.0),
        ("T445221199507034550", 1.000, 503.0, 463.0, 205.0, 20.0),
        ("姓名", 0.997, 505.0, 486.0, 24.0, 16.0),
        ("作业类别", 0.998, 648.0, 485.0, 46.0, 18.0),
        ("廖浩庄", 0.998, 502.0, 502.0, 53.0, 21.0),
        ("高处作业", 0.998, 648.0, 502.0, 64.0, 21.0),
        ("性别", 0.999, 503.0, 537.0, 27.0, 19.0),
        ("操作项目", 0.997, 649.0, 538.0, 42.0, 17.0),
        ("男", 1.000, 505.0, 552.0, 17.0, 20.0),
        ("高处安装、维护、拆除", 0.998, 647.0, 550.0, 156.0, 24.0),
        ("作业", 0.997, 648.0, 569.0, 36.0, 22.0),
        ("中华人民共和国", 1.000, 999.0, 584.0, 216.0, 25.0),
        ("初领日期", 0.998, 377.0, 614.0, 45.0, 17.0),
        ("有效期限", 0.994, 494.0, 616.0, 43.0, 14.0),
        ("特种作业操作证", 0.999, 1002.0, 617.0, 212.0, 27.0),
        ("2025-05-13", 0.999, 376.0, 627.0, 84.0, 21.0),
        ("2025-05-13至2031-05-12", 0.999, 494.0, 628.0, 175.0, 20.0),
        ("应复审日期", 0.998, 377.0, 651.0, 57.0, 21.0),
        ("签发机关", 0.995, 494.0, 653.0, 44.0, 18.0),
        ("2028-05-12前", 0.999, 376.0, 667.0, 100.0, 20.0),
        ("惠州市应急管理局", 0.999, 492.0, 668.0, 127.0, 20.0),
        ("中华人民共和国应急管理部监制|www.mem.gov.cn", 0.966, 950.0, 672.0, 315.0, 21.0),
        ("备注：本证书应于2028-05-12前进行复审", 0.995, 587.0, 770.0, 524.0, 30.0),
        ("本电子证书和实体证书具有同等法律效力。", 0.999, 585.0, 1019.0, 514.0, 30.0),
    ]
    return [TextBlock(*item) for item in raw]


def _template2_blocks() -> List[TextBlock]:
    """人工核对过的模板2（竖版手机查询页，1240x2772）真实 OCR 文本块。"""
    raw = [
        ("11:16", 0.997, 85.0, 55.0, 113.0, 41.0),
        ("50%", 0.992, 1064.0, 53.0, 99.0, 43.0),
        ("国家安全生产考试", 0.997, 392.0, 141.0, 460.0, 54.0),
        ("X", 0.961, 46.0, 170.0, 49.0, 46.0),
        ("cx.mem.gov.cn", 0.993, 482.0, 222.0, 276.0, 34.0),
        ("有效证书：1个", 0.997, 180.0, 487.0, 335.0, 55.0),
        ("无效证书：0个", 0.996, 817.0, 487.0, 345.0, 54.0),
        ("证号", 1.000, 437.0, 687.0, 48.0, 28.0),
        ("T441322198911030010", 1.000, 443.0, 714.0, 335.0, 31.0),
        ("姓名", 1.000, 435.0, 765.0, 52.0, 30.0),
        ("作业类别", 0.999, 760.0, 766.0, 94.0, 27.0),
        ("罗德财", 1.000, 438.0, 795.0, 107.0, 40.0),
        ("高处作业", 0.999, 762.0, 796.0, 141.0, 40.0),
        ("性别", 0.998, 439.0, 872.0, 49.0, 30.0),
        ("操作项目", 1.000, 760.0, 873.0, 91.0, 28.0),
        ("男", 1.000, 439.0, 897.0, 37.0, 44.0),
        ("登高架设作业", 0.973, 761.0, 901.0, 209.0, 38.0),
        ("初领日期", 0.997, 154.0, 1033.0, 93.0, 24.0),
        ("有效期限", 1.000, 412.0, 1028.0, 93.0, 30.0),
        ("2019-03-06", 0.999, 152.0, 1056.0, 176.0, 31.0),
        ("2022-08-29至2028-08-28", 0.999, 415.0, 1057.0, 387.0, 30.0),
        ("应复审日期", 0.998, 155.0, 1112.0, 115.0, 27.0),
        ("签发机关", 0.999, 412.0, 1109.0, 94.0, 30.0),
        ("2025-08-28", 1.000, 151.0, 1135.0, 184.0, 37.0),
        ("东莞市应急管理局", 0.998, 416.0, 1138.0, 272.0, 34.0),
        ("中华人民共和国", 0.998, 385.0, 1648.0, 471.0, 38.0),
        ("特种作业操作证", 0.997, 386.0, 1713.0, 470.0, 54.0),
        ("中华人民共和国应急管理部监制|www.mem.gov.cn", 0.963, 273.0, 1831.0, 697.0, 38.0),
        ("备注：本证书应于2025-08-28前进行", 0.983, 122.0, 1983.0, 979.0, 65.0),
        ("复审", 0.999, 117.0, 2062.0, 126.0, 67.0),
        ("主办单位：中华人民共和国应急管理部", 0.994, 332.0, 2370.0, 577.0, 35.0),
        ("承办单位：应急管理部干部培训学院（应急管理部党校）", 0.985, 196.0, 2443.0, 825.0, 33.0),
        ("备案号：京ICP备18056520号-2", 0.992, 386.0, 2513.0, 470.0, 38.0),
    ]
    return [TextBlock(*item) for item in raw]


EXPECTED_TEMPLATE1 = {
    "姓名": "廖浩庄",
    "性别": "男",
    "作业类别": "高处作业",
    "操作项目": "高处安装、维护、拆除作业",
    "初领日期": "2025-05-13",
    "签发机关": "惠州市应急管理局",
    "有效期截止日期": "2031-05-12",
}

EXPECTED_TEMPLATE2 = {
    "姓名": "罗德财",
    "性别": "男",
    "作业类别": "高处作业",
    "操作项目": "登高架设作业",
    "初领日期": "2019-03-06",
    "签发机关": "东莞市应急管理局",
    "有效期截止日期": "2028-08-28",
}


def _transform(blocks: List[TextBlock], scale: float, dx: float, dy: float) -> List[TextBlock]:
    return [
        TextBlock(
            text=b.text,
            confidence=b.confidence,
            x=b.x * scale + dx,
            y=b.y * scale + dy,
            width=b.width * scale,
            height=b.height * scale,
        )
        for b in blocks
    ]


def test_baseline_and_translation_scaling_invariance():
    """验证两张样本基准值 14/14 准确，且整体平移与等比缩放后输出完全一致。"""
    t1 = _template1_blocks()
    t2 = _template2_blocks()

    assert extract_fields(t1, 1679, 1127) == EXPECTED_TEMPLATE1
    assert extract_fields(t2, 1240, 2772) == EXPECTED_TEMPLATE2

    # 整体平移与多种等比缩放
    for scale, dx, dy in ((0.5, 120.0, 80.0), (1.0, 450.0, 300.0), (2.2, 50.0, 150.0)):
        assert extract_fields(_transform(t1, scale, dx, dy), int(1679 * scale + dx), int(1127 * scale + dy)) == EXPECTED_TEMPLATE1
        assert extract_fields(_transform(t2, scale, dx, dy), int(1240 * scale + dx), int(2772 * scale + dy)) == EXPECTED_TEMPLATE2


def test_borrow_column_within_same_region_only():
    """去掉同行右侧标签时，只能借本区域另一行确定列界；再移除可借用证据时无法归属字段为空，已确定字段仍取原值。"""
    # 1. 底部区域：去掉“签发机关”标签，借用本区域 Row1 的“有效期限”列起点仍可恢复签发机关
    blocks = [b for b in _template1_blocks() if b.text != "签发机关"]
    res = extract_fields(blocks, 1679, 1127)
    assert res["签发机关"] == "惠州市应急管理局"
    assert res["初领日期"] == "2025-05-13"
    assert res["有效期截止日期"] == "2031-05-12"

    # 再移除本区域另一行可借用的右列证据（“有效期限”标签与有效期区间值），
    # 即使上方人像右侧仍有右列标签，也不得跨区域借用 -> 签发机关必须为空，初领日期仍取原值
    blocks_no_right = [
        b for b in blocks if b.text not in ("有效期限", "2025-05-13至2031-05-12")
    ]
    res_no_right = extract_fields(blocks_no_right, 1679, 1127)
    assert res_no_right["签发机关"] is None
    assert res_no_right["有效期截止日期"] is None
    assert res_no_right["初领日期"] == "2025-05-13"
    assert res_no_right["姓名"] == "廖浩庄"

    # 2. 人像右侧：去掉“性别”标签，借用“姓名”/“证号”确定左列起点可恢复性别；
    # 再移除“姓名”和“证号”后，性别失去左列边界应为空，已确定的作业类别、操作项目仍取原值
    blocks_no_gender = [b for b in _template2_blocks() if b.text != "性别"]
    res_g = extract_fields(blocks_no_gender, 1240, 2772)
    assert res_g["性别"] == "男"
    assert res_g["作业类别"] == "高处作业"
    assert res_g["操作项目"] == "登高架设作业"

    blocks_no_left = [
        b
        for b in blocks_no_gender
        if b.text not in ("姓名", "证号", "T441322198911030010")
    ]
    res_no_left = extract_fields(blocks_no_left, 1240, 2772)
    assert res_no_left["性别"] is None
    assert res_no_left["姓名"] is None
    assert res_no_left["作业类别"] == "高处作业"
    assert res_no_left["操作项目"] == "登高架设作业"


def test_noise_only_and_typo_rules():
    """验证纯卡外干扰全空、已知错字“初领目期”可匹配、改动一字的短标签不自动成为锚点。"""
    # 仅保留页眉、背面、备注、页脚干扰文字
    noise_texts = {
        "中华人民共和国特种作业操作证",
        "档案编码：A44130044325002074",
        "中华人民共和国",
        "特种作业操作证",
        "中华人民共和国应急管理部监制|www.mem.gov.cn",
        "备注：本证书应于2028-05-12前进行复审",
        "本电子证书和实体证书具有同等法律效力。",
    }
    noise_only = [b for b in _template1_blocks() if b.text in noise_texts]
    assert all(v is None for v in extract_fields(noise_only, 1679, 1127).values())

    # “初领目期”可按已知别名匹配
    typo_blocks = [
        TextBlock("初领目期", b.confidence, b.x, b.y, b.width, b.height)
        if b.text == "初领日期"
        else b
        for b in _template2_blocks()
    ]
    res_typo = extract_fields(typo_blocks, 1240, 2772)
    assert res_typo["初领日期"] == "2019-03-06"

    # 短标签改一字（“姓明”、“姓别”、“证好”）不自动成为锚点
    bad_short = []
    for b in _template1_blocks():
        if b.text == "姓名":
            bad_short.append(TextBlock("姓明", b.confidence, b.x, b.y, b.width, b.height))
        elif b.text == "性别":
            bad_short.append(TextBlock("姓别", b.confidence, b.x, b.y, b.width, b.height))
        elif b.text in ("证号", "T445221199507034550"):
            continue
        else:
            bad_short.append(b)
    res_bad_short = extract_fields(bad_short, 1679, 1127)
    assert res_bad_short["姓名"] is None
    assert res_bad_short["性别"] is None
    assert res_bad_short["作业类别"] == "高处作业"
    assert res_bad_short["操作项目"] == "高处安装、维护、拆除作业"


def test_split_validity_range_and_date_validation():
    """验证有效期同行拆块、残缺/非法日期置空、以及“有效期至”配单日期。"""
    # 1. 拆成三块：["2022-08-29", "至", "2028-08-28"]
    split_blocks = []
    for b in _template2_blocks():
        if b.text == "2022-08-29至2028-08-28":
            split_blocks.extend(
                [
                    TextBlock("2022-08-29", 0.99, 415.0, 1057.0, 170.0, 30.0),
                    TextBlock("至", 0.99, 592.0, 1057.0, 30.0, 30.0),
                    TextBlock("2028-08-28", 0.99, 630.0, 1057.0, 172.0, 30.0),
                ]
            )
        else:
            split_blocks.append(b)
    assert extract_fields(split_blocks, 1240, 2772)["有效期截止日期"] == "2028-08-28"

    # 2. 删去截止端 -> 应为空
    only_start = [b for b in split_blocks if b.text != "2028-08-28"]
    assert extract_fields(only_start, 1240, 2772)["有效期截止日期"] is None

    # 3. 截止端改成非法日期 2031-55-12 -> 应为空
    invalid_end = [
        TextBlock("2022-08-29至2031-55-12", b.confidence, b.x, b.y, b.width, b.height)
        if b.text == "2022-08-29至2028-08-28"
        else b
        for b in _template2_blocks()
    ]
    assert extract_fields(invalid_end, 1240, 2772)["有效期截止日期"] is None

    # 4. 明确标签“有效期至”配单个合法日期 -> 返回该日期
    valid_until = []
    for b in _template2_blocks():
        if b.text == "有效期限":
            valid_until.append(TextBlock("有效期至", b.confidence, b.x, b.y, b.width, b.height))
        elif b.text == "2022-08-29至2028-08-28":
            valid_until.append(TextBlock("2028-08-28", b.confidence, b.x, b.y, 176.0, b.height))
        else:
            valid_until.append(b)
    assert extract_fields(valid_until, 1240, 2772)["有效期截止日期"] == "2028-08-28"

    # 5. 普通标签“有效期限”下仅有单个日期 -> 仍为空
    single_under_range_label = [
        TextBlock("2028-08-28", b.confidence, b.x, b.y, 176.0, b.height)
        if b.text == "2022-08-29至2028-08-28"
        else b
        for b in _template2_blocks()
    ]
    assert extract_fields(single_under_range_label, 1240, 2772)["有效期截止日期"] is None


def test_missing_first_issue_date_label():
    """验证移除竖版“初领日期”标签后从区间同排左侧恢复 2019-03-06；再移除该值后应为空。"""
    no_label = [b for b in _template2_blocks() if b.text != "初领日期"]
    res1 = extract_fields(no_label, 1240, 2772)
    assert res1["初领日期"] == "2019-03-06"
    assert res1["有效期截止日期"] == "2028-08-28"

    # 再移除 2019-03-06 值块，绝不能误取下一行的 2025-08-28 或备注日期
    no_label_and_val = [b for b in no_label if b.text != "2019-03-06"]
    res2 = extract_fields(no_label_and_val, 1240, 2772)
    assert res2["初领日期"] is None
    assert res2["有效期截止日期"] == "2028-08-28"


def test_single_region_degradation_and_inline_label():
    """验证双区域独立确认（仅一组可确认时仅提取该组）以及标签+值粘连拆分。"""
    # 仅保留人像右侧相关块
    upper_only = [b for b in _template1_blocks() if b.y < 600.0]
    res_upper = extract_fields(upper_only, 1679, 1127)
    assert res_upper["姓名"] == "廖浩庄"
    assert res_upper["性别"] == "男"
    assert res_upper["作业类别"] == "高处作业"
    assert res_upper["操作项目"] == "高处安装、维护、拆除作业"
    assert res_upper["初领日期"] is None
    assert res_upper["签发机关"] is None
    assert res_upper["有效期截止日期"] is None

    # 仅保留卡片底部相关块
    bottom_only = [b for b in _template2_blocks() if b.y >= 1000.0]
    res_bottom = extract_fields(bottom_only, 1240, 2772)
    assert res_bottom["姓名"] is None
    assert res_bottom["性别"] is None
    assert res_bottom["作业类别"] is None
    assert res_bottom["操作项目"] is None
    assert res_bottom["初领日期"] == "2019-03-06"
    assert res_bottom["签发机关"] == "东莞市应急管理局"
    assert res_bottom["有效期截止日期"] == "2028-08-28"

    # 标签与值粘连在同块中（含中文间隔号姓名）
    inline_blocks = [
        TextBlock("姓名：阿依古丽·买买提", 0.99, 100.0, 100.0, 180.0, 20.0),
        TextBlock("作业类别：电工作业", 0.99, 340.0, 100.0, 150.0, 20.0),
        TextBlock("性别：女", 0.99, 100.0, 160.0, 80.0, 20.0),
        TextBlock("操作项目：低压电工作业", 0.99, 340.0, 160.0, 180.0, 20.0),
    ]
    res_inline = extract_fields(inline_blocks, 800, 600)
    assert res_inline["姓名"] == "阿依古丽·买买提"
    assert res_inline["性别"] == "女"
    assert res_inline["作业类别"] == "电工作业"
    assert res_inline["操作项目"] == "低压电工作业"


def test_compressed_template1_recovers_unique_evidence_without_false_positives():
    """验证模拟压缩图（0.65x, Q55）下有唯一归属证据的字段可准确恢复且零错填。"""
    compressed_t1 = [
        TextBlock("中华人民共和国特种作业操作证", 0.99, 265.0, 107.0, 587.0, 38.0),
        TextBlock("档案编码：A44130044325002074", 0.99, 400.0, 176.0, 301.0, 23.0),
        TextBlock("T445221199507034550", 0.99, 326.0, 300.0, 136.0, 17.0),
        TextBlock("挂名", 0.80, 326.0, 315.0, 21.0, 15.0),
        TextBlock("作业类丽", 0.80, 421.0, 315.0, 32.0, 15.0),
        TextBlock("廖浩庄", 0.99, 325.0, 327.0, 36.0, 15.0),
        TextBlock("高处作业", 0.99, 421.0, 327.0, 43.0, 14.0),
        TextBlock("男", 0.99, 327.0, 360.0, 13.0, 12.0),
        TextBlock("操作项目", 0.99, 421.0, 349.0, 32.0, 14.0),
        TextBlock("高处安装、维护、拆除", 0.99, 421.0, 358.0, 102.0, 16.0),
        TextBlock("作业", 0.99, 421.0, 370.0, 24.0, 15.0),
        TextBlock("中华人民共和国", 0.99, 648.0, 378.0, 144.0, 20.0),
        TextBlock("2025-05-13", 0.99, 244.0, 407.0, 58.0, 17.0),
        TextBlock("2025-05-13至2031-05-12", 0.99, 319.0, 407.0, 118.0, 17.0),
        TextBlock("特种作业操作证", 0.99, 648.0, 399.0, 143.0, 23.0),
        TextBlock("发X", 0.70, 321.0, 422.0, 32.0, 15.0),
        TextBlock("2028-05-12前", 0.99, 245.0, 433.0, 66.0, 17.0),
        TextBlock("惠州市应急管理局", 0.99, 320.0, 433.0, 83.0, 17.0),
    ]
    res = extract_fields(compressed_t1, 1091, 733)
    # 证号值 + 操作项目确认人像右侧区域，且下方底部有效期区间不得干扰操作项目
    assert res["性别"] == "男"
    assert res["操作项目"] == "高处安装、维护、拆除作业"
    # 标签缺失或证据不足的字段保持 None，错填为零
    assert res["姓名"] is None
    assert res["作业类别"] is None
    assert res["初领日期"] is None
    assert res["签发机关"] is None
    assert res["有效期截止日期"] is None

