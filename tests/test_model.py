"""model.py 的测试：章节树 JSON 中间态。

用假数据覆盖：往返序列化、卷章两级、序号重复/缺失的校验、中英文混排字数统计、
空书、读回 JSON 再写出一致。只断言"结构对不对"，不断言错误措辞。

跑法：python tests/test_model.py（不依赖 pytest）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.model import (  # noqa: E402
    SCHEMA_VERSION,
    Book,
    Chapter,
    Paragraph,
    chapter_from_title,
    from_dict,
    load,
    normalize_rows,
    parse_heading,
    save,
    stats,
    table_plain_text,
    to_dict,
    validate,
)


def _tmp_dir() -> Path:
    """测试产物目录固定在项目 backups/ 下：规则要求临时文件不落项目外。

    用固定目录而不是 mkdtemp()：本项目的规则禁止删除文件，每次跑都新建
    一个 mkdtemp 目录会一直堆积。固定目录每次覆盖，跑一百次也只占一份。
    """
    d = ROOT / "backups" / "test-model-tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sample_book() -> Book:
    """一本结构齐全的假书：前言 + 卷章两级 + 后记，段落类型都有。"""
    return Book(
        title="测试之书",
        author="佚名",
        encoding="gb18030",
        source="samples/messy_gbk.txt",
        front_matter=[
            Paragraph(text="本书由某某整理，仅供试读。", kind="note"),
            Paragraph(text="", kind="blank"),
        ],
        chapters=[
            Chapter(
                title="第一卷 风起",
                number=1,
                level=2,
                start_line=10,
                paragraphs=[Paragraph(text="")],
            ),
            Chapter(
                title="第一章 开始",
                number=1,
                level=1,
                start_line=12,
                paragraphs=[
                    Paragraph(text="他走了三天。", kind="body"),
                    Paragraph(text="", kind="blank"),
                    Paragraph(text="第一章 开始", kind="heading"),
                ],
            ),
            Chapter(
                title="第二章 结束",
                number=2,
                level=1,
                start_line=30,
                paragraphs=[Paragraph(text="风停了。", kind="body")],
            ),
        ],
        tail_matter=[Paragraph(text="全书完。", kind="note")],
    )


def case_roundtrip() -> None:
    """to_dict → JSON → from_dict 必须一字不差地还原。"""
    book = _sample_book()
    d = to_dict(book)
    assert d["version"] == SCHEMA_VERSION, d["version"]
    assert set(d) == {
        "version", "title", "author", "encoding", "source",
        "front_matter", "chapters", "tail_matter",
    }, sorted(d)

    # 经过真正的 JSON 文本往返，而不是只过一遍 dict
    back = from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
    assert back == book, "往返后有字段丢失或被改动"

    # 未解析出序号的章节要保住 None，不能变成 0
    b2 = Book(chapters=[Chapter(title="楔子", number=None)])
    assert from_dict(to_dict(b2)).chapters[0].number is None

    # 段落允许写成纯字符串（手写 JSON 的常见写法）
    loose = from_dict({"version": "1", "chapters": [{"title": "第一章", "paragraphs": ["正文一"]}]})
    assert loose.chapters[0].paragraphs[0] == Paragraph(text="正文一")
    assert loose.chapters[0].paragraphs[0].kind == "body"


def case_two_levels() -> None:
    """卷/部/集是 level 2，章/回/节是 level 1；序号复用 diagnose.cn_to_int()。"""
    cases = [
        ("第一卷 风起", 1, 2),
        ("第 2 部 云涌", 2, 2),
        ("第三集", 3, 2),
        ("第一章 开始", 1, 1),
        ("第十二章 夜行", 12, 1),
        ("第一百二十三章", 123, 1),
        ("第3回 惊变", 3, 1),
        ("第 ０９ 节", 9, 1),
        ("Chapter 7", 7, 1),
        ("  42  ", 42, 1),
        ("## 第五章 井", 5, 1),
        ("楔子", None, 1),
        ("番外 旧事", None, 1),
    ]
    for line, number, level in cases:
        got = parse_heading(line)
        assert got is not None, f"{line!r} 应该被认成标题"
        assert (got.number, got.level) == (number, level), (line, got)

    # 正文行必须返回"没判定"，而不是瞎猜一个层级
    for line in ("他走了三天。", "", "   ", "第", "这里说的是第一卷的内容"):
        assert parse_heading(line) is None, line

    ch = chapter_from_title("第十二章 夜行", start_line=88)
    assert (ch.number, ch.level, ch.start_line) == (12, 1, 88), ch


def case_validate_problems() -> None:
    """序号重复、序号倒退、标题为空、段落全空、没记录编码都要报出来。"""
    book = Book(
        encoding="",  # 故意漏掉
        chapters=[
            Chapter(title="第一章", number=1, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章", number=2, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章（重复）", number=2, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第五章", number=5, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第三章", number=3, paragraphs=[Paragraph(text="正文")]),  # 倒退
            Chapter(title="", number=6, paragraphs=[Paragraph(text="")]),  # 标题空 + 段落全空
            Chapter(title="第七章", number=7, paragraphs=[Paragraph(text="正文", kind="ad")]),  # 类型越界
        ],
    )
    issues = validate(book)
    joined = "\n".join(issues)
    assert any("encoding" in s for s in issues), joined
    assert any("标题为空" in s for s in issues), joined
    assert any("段落全空" in s for s in issues), joined
    assert any("序号重复" in s for s in issues), joined
    assert any("契约外的段落类型" in s for s in issues), joined
    assert any("非递增" in s for s in issues), joined
    # 与章节有关的问题都要能核对：必须点名"第几个章节"
    # （encoding 那条不是章节问题，本来就不该带章节号）
    assert all("第 " in s for s in issues if "encoding" not in s), joined


def case_validate_missing_number() -> None:
    """序号缺失（None）不是问题，不该被当成 0 或报重复；健康书返回空清单。"""
    book = Book(
        encoding="utf-8",
        chapters=[
            Chapter(title="楔子", number=None, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第一章", number=1, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="番外", number=None, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章", number=2, paragraphs=[Paragraph(text="正文")]),
        ],
    )
    assert validate(book) == [], validate(book)

    # 空书：必须同时提醒"没章节"和"没编码"
    empty_issues = validate(Book())
    assert any("没有任何章节" in s for s in empty_issues), empty_issues
    assert any("encoding" in s for s in empty_issues), empty_issues


def case_validate_volume_restart() -> None:
    """每卷章号从 1 重新开始，不能误报成"序号倒退"。"""
    chapters = []
    for vol in (1, 2):
        chapters.append(Chapter(title=f"第{vol}卷", number=vol, level=2, paragraphs=[Paragraph(text="卷首")]))
        for n in (1, 2, 3):
            chapters.append(
                Chapter(title=f"第{vol}卷第{n}章", number=n, level=1, paragraphs=[Paragraph(text="正文")])
            )
    assert validate(Book(encoding="utf-8", chapters=chapters)) == [], validate(
        Book(encoding="utf-8", chapters=chapters)
    )


def case_validate_volume_may_be_empty() -> None:
    """卷/部标题本身没有段落是正常的（正文在下面各章里），不能报"正文丢了"。

    level=2 是容器：渲染层会把它排成一个小标题，它后面跟着的章才是内容。
    每本分卷的书都报这条错误，等于让警告失去意义。
    """
    chapters = [
        Chapter(title="第一卷 风起", number=1, level=2, paragraphs=[]),
        Chapter(title="第一章 起始", number=1, paragraphs=[Paragraph(text="正文")]),
        Chapter(title="第二章 相遇", number=2, paragraphs=[Paragraph(text="正文")]),
        Chapter(title="第二卷 云涌", number=2, level=2, paragraphs=[]),
        Chapter(title="第三章 别离", number=3, paragraphs=[Paragraph(text="正文")]),
    ]
    issues = validate(Book(encoding="utf-8", chapters=chapters))
    assert issues == [], issues

    # 但 level=1 的章还是不许空 —— 豁免只给容器标题，别把闸整个拆了
    broken = Book(
        encoding="utf-8",
        chapters=[
            Chapter(title="第一章 起始", number=1, paragraphs=[]),
        ],
    )
    broken_issues = validate(broken)
    assert any("一个段落都没有" in s for s in broken_issues), broken_issues


def case_validate_duplicate_volume_titles() -> None:
    """两卷都写成"第1卷"（盗版合并本常见）时，各卷的章不能混成一组误报重复。"""
    chapters = []
    for _ in range(2):
        chapters.append(Chapter(title="第1卷", number=1, level=2, paragraphs=[Paragraph(text="卷首")]))
        for n in (1, 2):
            chapters.append(
                Chapter(title=f"第{n}章", number=n, level=1, paragraphs=[Paragraph(text="正文")])
            )
    issues = validate(Book(encoding="utf-8", chapters=chapters))
    # 卷序号确实重复了，这一条该报 —— 说明分组没有把两卷的章混在一起
    assert len(issues) == 1, issues
    assert "章节序号重复" in issues[0] and "卷/部/集标题" in issues[0], issues
    # 各卷内部的章 1、2 递增，不许出现任何误报
    assert not any("非递增" in s for s in issues), issues


def case_anchor_refs_roundtrip() -> None:
    """anchor 与 refs 只在非空时才写进 JSON（v1 旧文件重新序列化必须逐字节不变）。"""
    para = Paragraph(text="正文继续。", refs=[{"at": 2, "note": "n1", "label": "[1]"}],
                     anchor="nr1")
    note = Paragraph(text="注一。", kind="note", anchor="n1")
    plain = Paragraph(text="普通段。")
    book = Book(title="书", chapters=[Chapter(title="第一章", number=1, level=1,
                                              start_line=1, paragraphs=[para, note, plain])])
    data = to_dict(book)
    p0, p1, p2 = data["chapters"][0]["paragraphs"]
    assert p0["anchor"] == "nr1" and p0["refs"][0]["note"] == "n1", p0
    assert p1["anchor"] == "n1" and "refs" not in p1, p1
    assert "anchor" not in p2 and "refs" not in p2, p2

    back = from_dict(data)
    got = back.chapters[0].paragraphs
    assert got[0].anchor == "nr1" and got[0].refs == [{"at": 2, "note": "n1", "label": "[1]"}]
    assert got[1].anchor == "n1" and got[1].refs == []
    assert got[2].anchor == "" and got[2].refs == []
    # 再序列化一次必须与第一次逐字节相同
    assert json.dumps(to_dict(back), ensure_ascii=False) == json.dumps(data, ensure_ascii=False)


def case_ref_with_own_text() -> None:
    """引用可以自带注文（掌阅格式的书没有注释段落可指）。

    这类引用没有落点，只有文字：JSON 里写 `text`、不写 `note`；再读回来必须
    一字不差 —— 它是注文唯一的存身处，读丢一次，全书注释就少一段。
    """
    para = Paragraph(text="正文继续。", refs=[{"at": 2, "text": "注文一句。", "label": ""}])
    book = Book(encoding="utf-8", title="书", chapters=[Chapter(title="第一章", number=1, level=1,
                                              start_line=1, paragraphs=[para])])
    data = to_dict(book)
    written = data["chapters"][0]["paragraphs"][0]["refs"][0]
    assert written == {"at": 2, "label": "", "text": "注文一句。"}, written
    assert "note" not in written, written

    back = from_dict(data)
    assert back.chapters[0].paragraphs[0].refs == [
        {"at": 2, "note": "", "label": "", "text": "注文一句。"}
    ], back.chapters[0].paragraphs[0].refs
    # 没有落点不是错误：渲染层把 text 渲成注标，不需要锚点
    assert validate(book) == [], validate(book)


def case_validate_refs() -> None:
    """锚点/引用里的 id 会原样拼进 xhtml 属性，必须过白名单校验。"""
    bad_anchor = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                        paragraphs=[Paragraph(text="正文。", anchor='a"b')])])
    assert any("anchor" in msg for msg in validate(bad_anchor)), validate(bad_anchor)

    bad_note = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                      paragraphs=[Paragraph(text="正文。",
                                                            refs=[{"at": 0, "note": "x y", "label": "[1]"}])])])
    assert any("行内引用" in msg for msg in validate(bad_note)), validate(bad_note)

    bad_at = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                    paragraphs=[Paragraph(text="正文。",
                                                          refs=[{"at": 99, "note": "n1", "label": "[1]"}])])])
    assert any("行内引用" in msg for msg in validate(bad_at)), validate(bad_at)

    ok = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                paragraphs=[Paragraph(text="正文。",
                                                      refs=[{"at": 2, "note": "n1", "label": "[1]"}],
                                                      anchor="nr1"),
                                            Paragraph(text="注。", kind="note", anchor="n1")])])
    assert validate(ok) == [], validate(ok)


def case_stats() -> None:
    """字数只数汉字与字母数字：中英混排、标点空白都不算。"""
    assert stats(Book()) == {
        "chapters": 0,
        "paragraphs": 0,
        "chars": 0,
        "avg_chars_per_chapter": 0.0,
        "longest_title": "",
    }, stats(Book())

    book = Book(
        encoding="utf-8",
        front_matter=[Paragraph(text="序言abc")],  # 2 + 3 = 5
        chapters=[
            Chapter(
                title="第一章 开始",
                number=1,
                paragraphs=[Paragraph(text="他走了三天abc123，路很远。")],  # 5 + 3 + 3 + 3 = 14
            ),
            Chapter(
                title="第二章 结束以及一个很长很长很长的尾巴",
                number=2,
                paragraphs=[Paragraph(text="第二段的字。")],  # 5
            ),
        ],
        tail_matter=[Paragraph(text="全书完")],  # 3
    )
    s = stats(book)
    assert s["chapters"] == 2, s
    assert s["paragraphs"] == 4, s          # 前言 1 + 章内 2 + 后记 1
    assert s["chars"] == 5 + 14 + 5 + 3, s
    # 平均每章字数只算章节正文，不把前言后记摊进去
    assert s["avg_chars_per_chapter"] == round((14 + 5) / 2, 1), s
    assert s["longest_title"] == "第二章 结束以及一个很长很长很长的尾巴", s


def case_save_load_identical() -> None:
    """存盘 → 读回 → 再存盘，两次文件内容必须一致（稳定结构）。"""
    book = _sample_book()
    d = _tmp_dir()
    p1 = d / "book.json"
    p2 = d / "sub" / "book2.json"  # 父目录不存在也要能写

    save(book, p1)
    text1 = p1.read_text(encoding="utf-8")
    assert "第一章 开始" in text1, "中文不该被转义成 \\uXXXX"
    assert f'"version": "{SCHEMA_VERSION}"' in text1, text1[:200]

    back = load(p1)
    assert back == book, "存盘读回后结构变了"

    save(back, p2)
    assert p2.read_text(encoding="utf-8") == text1, "读回再写出，文件内容不一致"


def case_align_roundtrip() -> None:
    """align 只在该段真靠右时写盘；老树不产生这个键；认不出的值归空。

    为什么非法值不报错而是归空：手改 JSON 写错一个词（"Center"）不该让整本
    书打不出来，但也不能放它进 xhtml 变成一个凭空出现的 class。
    """
    book = _sample_book()
    book.chapters[1].paragraphs[0].align = "right"
    raw = to_dict(book)
    para = raw["chapters"][1]["paragraphs"][0]
    assert para["align"] == "right", para
    assert to_dict(from_dict(raw)) == raw

    # 老树（没有这个键）重新序列化后不该凭空多出一个空串键
    plain = to_dict(_sample_book())
    assert all("align" not in p for p in plain["chapters"][1]["paragraphs"])

    para["align"] = "Center"
    assert from_dict(raw).chapters[1].paragraphs[0].align == ""
    para["align"] = ""
    assert "align" not in to_dict(from_dict(raw))["chapters"][1]["paragraphs"][0]


def case_version_guard() -> None:
    """主版本号不认识时必须报错，不能按旧语义读出一本错位的书。

    v2 相对 v1 只是 Paragraph 多了三个默认字段（image 段专用），v3 相对 v2
    只多一个 rows（table 段专用），v4 增加 refs[].text，v5 增加 Chapter.refs，
    v6 增加可选目录深度，v7 增加 ruby，v8 增加 align。旧文件按新版本读入
    都无损；其余主版本仍然拒绝。
    """
    for good in ("1", "2", "3", "4", "5", "6", "7", "8", "8.0", "1.5.9-rc"):
        ok = from_dict({"version": good, "chapters": []})
        assert isinstance(ok, Book), good
    for bad in ("0", "9", "9.0"):
        try:
            from_dict({"version": bad, "chapters": []})
        except ValueError:
            continue
        raise AssertionError(f"版本 {bad} 应该被拒绝")


def case_toc_depth_roundtrip() -> None:
    """显式目录深度独立于卷章样式，存盘读回不能消失；旧树不补字段。"""
    raw = to_dict(_sample_book())
    raw["chapters"][0]["toc_depth"] = 0
    raw["chapters"][1]["toc_depth"] = 2
    back = to_dict(from_dict(raw))
    assert back["chapters"][0].get("toc_depth") == 0, back["chapters"][0]
    assert back["chapters"][1].get("toc_depth") == 2, back["chapters"][1]
    assert back == raw
    assert all("toc_depth" not in c for c in to_dict(_sample_book())["chapters"])
    for legacy_version in ("1", "2", "3", "4", "5"):
        legacy = to_dict(_sample_book())
        legacy["version"] = legacy_version
        migrated = to_dict(from_dict(legacy))
        legacy["version"] = SCHEMA_VERSION
        assert migrated == legacy


def case_framed_heading_keeps_its_number() -> None:
    """剥边框后不能只认家族不抽号：Heading.number 必须跟着回来。

    家族由 match_chapter 认、序号由 parse_heading 里的 _HEADING_NUM 抽，
    两处若有一处没走 strip_frame，目录编号检查就会整族失真。
    """
    h = parse_heading("========== 第001章 ==========")
    assert h is not None
    assert h.family == "第N章类", h
    assert h.number == 1, h
    assert h.level == 1, h
    assert parse_heading("==========").family == "分隔线"


def case_chapter_refs_roundtrip() -> None:
    """标题注（Chapter.refs）要原样进出 JSON；没有时不产生这个键。

    这是 schema v5 的增量：老树没有这个键，读进来是空列表，写回去也不能凭空
    多出来 —— 否则"旧文件重新序列化后除 version 外逐字节不变"这条验收就破了。
    """
    book = Book(
        encoding="utf-8",
        chapters=[
            Chapter(
                title="作者生平述要",
                level=1,
                refs=[
                    {"at": 6, "note": "n1", "label": ""},
                    {"at": 2, "note": "", "label": "", "text": "自带注文的标题注。"},
                ],
            ),
            Chapter(title="第二章", level=1),
        ],
    )
    d = to_dict(book)
    assert "refs" in d["chapters"][0], d["chapters"][0]
    assert "refs" not in d["chapters"][1], d["chapters"][1]
    assert d["version"] == SCHEMA_VERSION, d["version"]

    back = from_dict(json.loads(json.dumps(d)))
    assert back.chapters[0].refs == book.chapters[0].refs, back.chapters[0].refs
    assert back.chapters[1].refs == [], back.chapters[1].refs

    # 越界的标题注要报出来：渲染层会跳过它，注文却已经被搬走 —— 等于丢注
    bad = Book(
        encoding="utf-8",
        chapters=[Chapter(title="短标题", level=1, refs=[{"at": 99, "note": "n1"}])],
    )
    assert any("行内引用的位置 99" in t for t in validate(bad)), validate(bad)


def case_v1_tree_reserializes_unchanged() -> None:
    """docs/gui-design.md 的「EPUB 导入」一节验收要求：旧 v1 章节树读入后重新序列化，**除 version 外**逐字节不变。

    为什么不是"逐字节"：schema 升到 2 了，to_dict 写出的 version 字段必须是
    新的（否则下次读入会以为这是 v1 树）。除这一处以外一个字节都不许动 ——
    这是"schema 升级对老文件零影响"的可执行证明。
    """
    v1 = {
        "version": "1",
        "title": "旧书",
        "author": "某人",
        "encoding": "utf-8",
        "source": "X:/样本/旧书.txt",
        "front_matter": [
            {"text": "前言一段。", "kind": "note"},
            {"text": "", "kind": "blank"},
        ],
        "chapters": [
            {
                "title": "第一卷",
                "number": 1,
                "level": 2,
                "start_line": 0,
                "paragraphs": [],
            },
            {
                "title": "第一章 开端",
                "number": 1,
                "level": 1,
                "start_line": 10,
                "paragraphs": [
                    {"text": "正文一。", "kind": "body"},
                    {"text": "＊＊＊", "kind": "sep"},
                    {"text": "词曰。", "kind": "verse"},
                ],
            },
        ],
        "tail_matter": [],
    }
    text_in = json.dumps(v1, ensure_ascii=False, indent=2) + "\n"
    book = from_dict(json.loads(text_in))
    d = _tmp_dir()
    p = d / "v1-book.json"
    save(book, p)
    text_out = p.read_text(encoding="utf-8")

    # 只允许 version 那一行不同：把两边都换成占位再比
    patched_in = text_in.replace('"version": "1"', '"version": "<V>"')
    patched_out = text_out.replace(f'"version": "{SCHEMA_VERSION}"', '"version": "<V>"')
    assert patched_in == patched_out, "旧 v1 树重新序列化后除 version 外应该逐字节不变"

    # 读回来的结构也一模一样（含空卷、number、start_line）
    assert to_dict(book)["chapters"] == v1["chapters"]
    assert to_dict(book)["front_matter"] == v1["front_matter"]


def case_table_rows_roundtrip() -> None:
    """规范化后的 table 段 rows 必须稳定往返 JSON；非法跨度与裸字符串先按统一规则归一。

    真实场景来源：《嘯風山莊》中的年表、译本对照表等 4 张表。表格能不能挺过"导入 → 打包
    → 再导入"，全看这一层 —— 结构在序列化上丢一次，回环就永远回不去，
    用户每导一次烂一层。
    """
    book = Book(
        encoding="utf-8",
        chapters=[
            Chapter(
                title="第一章",
                number=1,
                level=1,
                start_line=1,
                paragraphs=[
                    Paragraph(
                        text="年代｜重要事件\n1771｜去利物浦。",
                        kind="table",
                        rows=[
                            [{"text": "年代", "header": True}, {"text": "重要事件", "header": True}],
                            [{"text": "1771", "rowspan": 2}, {"text": "去利物浦。"}],
                        ],
                    )
                ],
            )
        ],
    )
    d = to_dict(book)
    para = d["chapters"][0]["paragraphs"][0]
    assert para["kind"] == "table", para
    # 默认值不落盘：colspan/rowspan=1、空 anchor、空 refs 都不写，
    # 否则一张 24 行 3 列的年表会变成一堆 "colspan": 1 的噪声。
    assert para["rows"][0][0] == {"text": "年代", "header": True}, para["rows"][0][0]
    assert para["rows"][1][0] == {"text": "1771", "rowspan": 2}, para["rows"][1][0]

    back = from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
    got = back.chapters[0].paragraphs[0]
    assert got.kind == "table", got.kind
    assert [[c["text"] for c in r] for r in got.rows] == [["年代", "重要事件"], ["1771", "去利物浦。"]]
    assert [c["header"] for c in got.rows[0]] == [True, True], got.rows[0]
    assert got.rows[1][0]["rowspan"] == 2, got.rows[1][0]

    # 归一：裸字符串当普通格；span 夹到 1..MAX_SPAN；缺的键补默认值
    rows = normalize_rows([["甲", {"text": "乙", "colspan": "9999", "rowspan": "0"}]])
    assert rows[0][0] == {"text": "甲", "header": False, "colspan": 1, "rowspan": 1,
                          "anchor": "", "refs": []}, rows[0][0]
    assert rows[0][1]["colspan"] == 20, rows[0][1]
    assert rows[0][1]["rowspan"] == 1, "rowspan=0 是非法的，按不跨行处理"

    # 摊平：行尾空列去掉（否则会出现一串没有意义的 ｜），中间空列保留
    flat = table_plain_text([
        [{"text": "年代"}, {"text": "事件"}],
        [{"text": "1771"}, {"text": ""}],
        [{"text": "1774"}, {"text": "上学"}, {"text": ""}],
    ])
    assert flat == "年代｜事件\n1771\n1774｜上学", repr(flat)


def case_validate_table() -> None:
    """空网格的表格段要报出来：渲染层会静默跳过它，读者不会知道这里原本有张表。"""
    empty = Book(
        encoding="utf-8",
        chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                          paragraphs=[Paragraph(text="正文。"),
                                      Paragraph(text="", kind="table", rows=[])])],
    )
    assert any("表格" in msg for msg in validate(empty)), validate(empty)

    # 单元格里的 anchor/refs 同样过白名单：它们会被原样拼进 xhtml 属性
    bad = Book(
        encoding="utf-8",
        chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                          paragraphs=[Paragraph(text="甲", kind="table", rows=[
                              [{"text": "甲", "anchor": 'a"b',
                                "refs": [{"at": 9, "note": "n1", "label": "[1]"}]}],
                          ])])],
    )
    msgs = validate(bad)
    assert any("anchor" in m for m in msgs), msgs
    assert any("行内引用" in m for m in msgs), msgs

    good = Book(
        encoding="utf-8",
        chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                          paragraphs=[Paragraph(text="甲", kind="table", rows=[
                              [{"text": "甲", "anchor": "cell1",
                                "refs": [{"at": 1, "note": "n1", "label": "[1]"}]}],
                          ]),
                              Paragraph(text="注。", kind="note", anchor="n1")])],
    )
    assert validate(good) == [], validate(good)

    # 手写的格子缺键、或干脆是裸字符串：校验器不能崩。归一发生在读入
    # （from_dict）与渲染两层，但校验不能假定调用方一定走过那两步 ——
    # 校验器自己抛 KeyError，比"没校验出来"更难查。
    loose = Book(
        encoding="utf-8",
        chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                          paragraphs=[Paragraph(text="甲", kind="table", rows=[["甲", {"text": "乙"}]])])],
    )
    assert isinstance(validate(loose), list), validate(loose)


def main() -> int:
    cases = [
        case_roundtrip,
        case_two_levels,
        case_validate_problems,
        case_validate_missing_number,
        case_validate_volume_restart,
        case_validate_volume_may_be_empty,
        case_validate_duplicate_volume_titles,
        case_anchor_refs_roundtrip,
        case_ref_with_own_text,
        case_validate_refs,
        case_stats,
        case_save_load_identical,
        case_version_guard,
        case_align_roundtrip,
        case_toc_depth_roundtrip,
        case_table_rows_roundtrip,
        case_validate_table,
        case_v1_tree_reserializes_unchanged,
        case_chapter_refs_roundtrip,
        case_framed_heading_keeps_its_number,
    ]
    failed = 0
    for c in cases:
        try:
            c()
            print(f"ok   {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {c.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
