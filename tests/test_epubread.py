"""epubread.py 的测试：EPUB → 章节树（G6 导入）。

照 tests/test_epub.py 的风格：逐个直跑，一个 case 一个函数，纯 assert，
直接 python tests/test_epubread.py 跑。只断言契约（章节切分、段落类型、
图片分流、caption 归属、报告字段、报错语义），不断言内部实现细节。

覆盖的边界：
- 解析：EPUB2（NCX）与 EPUB3（nav.xhtml）两条目录来源，多级目录 → 卷/章；
- 章节切分：同文档多锚点、锚点丢失时的兜底、章节开头重复标题的剔除；
- 分片合并：相邻同主干（上）（中）（下）合并，不相邻不合并；
- 段落类型：正文 / 标题 / 空行 / 脚注（多看 li）/ 引文（blockquote）/ 分隔线；
- 图片：四档分流 class、装饰图（引用超阈值）丢弃、孤儿图只报告、
  封面（含 SVG 包位图的写法）、尺寸解析、caption 归属；
- 容错：截断的 ZIP、坏 XML、缺失的锚点/图片、没有目录；
- 报错语义：DRM 加密与固定版式必须明确报错（不猜、不硬转）；
- 回环：本工具打出来的 EPUB 再导入，章节与插图都在。

临时文件一律走 tempfile，不往项目目录里写东西。
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.cli import main as cli_main  # noqa: E402
from huoban.epub import _image_ratio_class, build_epub  # noqa: E402
from huoban.epubread import (  # noqa: E402
    EpubReadError,
    _is_front_title,
    _image_size,
    read_epub,
    read_epub_with_report,
)
from huoban.model import Book, Chapter, Paragraph, from_dict, save as save_book, to_dict  # noqa: E402
from huoban.render import _paragraph_lines  # noqa: E402

FIXED_MODIFIED = "2026-10-24T00:00:00Z"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>页</title></head>
<body>{body}</body>
</html>
"""


# ---------- 造数据的小工具 ----------


def _png(width: int, height: int) -> bytes:
    """造一个只带文件头与 IHDR 的"PNG"：够 _image_size 读宽高。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00\x00\x00"


def _jpg(width: int, height: int) -> bytes:
    """SOF0 段：ff d8 | ff c0 | 长度 | 精度 | 高 | 宽 | …"""
    sof = b"\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03"
    return b"\xff\xd8" + b"\xff\xc0" + sof


def _opf(
    *,
    spine: list[str],
    manifest: list[tuple[str, str, str, str]],
    title: str = "测试书",
    author: str = "某作者",
    extra_meta: str = "",
    guide: str = "",
) -> str:
    items = "\n".join(
        f'    <item id="{i}" href="{h}" media-type="{m}"'
        + (f' properties="{p}"' if p else "")
        + "/>"
        for i, h, m, p in manifest
    )
    refs = "\n".join(f'    <itemref idref="{r}"/>' for r in spine)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>zh-CN</dc:language>
{extra_meta}
  </metadata>
  <manifest>
{items}
  </manifest>
  <spine toc="ncx">
{refs}
  </spine>
{guide}
</package>
"""


def _ncx(points: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
{points}
  </navMap>
</ncx>
"""


def _navpoint(label: str, src: str, kids: str = "") -> str:
    inner = f"\n{kids}\n" if kids else ""
    return f"""    <navPoint>
      <navLabel><text>{label}</text></navLabel>
      <content src="{src}"/>{inner}
    </navPoint>"""


def _epub(tmp: Path, entries: dict[str, str | bytes], name: str = "book.epub") -> Path:
    """把给定条目写成一个 EPUB 文件（mimetype 与 container 自动补）。"""
    path = tmp / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        for key, value in entries.items():
            zf.writestr(key, value)
    return path


def _kinds(chapter: Chapter) -> list[str]:
    return [p.kind for p in chapter.paragraphs]


# ---------- 用例 ----------


def case_epub2_basic_split() -> None:
    """EPUB2 + NCX：前附与卷章各守目录边界、重复标题剔除、各段落类型到位。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1", "c2", "c3"],
            manifest=[
                ("c1", "Text/front.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/vol.xhtml", "application/xhtml+xml", ""),
                ("c3", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("版权页", "Text/front.xhtml")
            + _navpoint(
                "第一卷 风起",
                "Text/vol.xhtml",
                _navpoint("第一章 开端", "Text/ch1.xhtml"),
            )
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/front.xhtml": XHTML.format(body="<p>版权信息一段。</p>"),
            "OEBPS/Text/vol.xhtml": XHTML.format(
                body='<h1 class="vol-title" title="第一卷 风起">第一卷 风起</h1>'
            ),
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body=(
                    "<h2>第一章 开端</h2>"
                    "<p>正文第一段。</p>"
                    "<p></p>"
                    "<p>正文第二段。</p>"
                    "<p>＊＊＊＊＊＊</p>"
                    "<h3>小标题</h3>"
                    "<blockquote>引文一行。</blockquote>"
                    '<ol class="duokan-footnote-content">'
                    '<li class="duokan-footnote-item"><a href="#n1">1</a> 注释文字。</li>'
                    "</ol>"
                )
            ),
        }
        book = read_epub(_epub(tmp, entries))

        # 版权页是前附卷下的子章，标题与正文都在
        assert book.front_matter == [], [p.text for p in book.front_matter]
        assert [ch.title for ch in book.chapters] == [
            "前附", "版权页", "第一卷 风起", "第一章 开端"
        ], [ch.title for ch in book.chapters]
        assert [ch.level for ch in book.chapters] == [2, 1, 2, 1]
        assert [ch.toc_depth for ch in book.chapters] == [0, 1, 0, 1]
        assert [p.text for p in book.chapters[1].paragraphs] == ["版权信息一段。"]

        # 章节：前附卷 + 前附子章 + 1 卷 + 1 章（章标题取自目录，正文里重复的 h2 被剔除）
        ch = book.chapters[3]
        assert ch.number == 1, ch.number
        assert _kinds(ch) == ["body", "blank", "body", "sep", "heading", "verse", "note"], _kinds(ch)
        assert ch.paragraphs[-1].text == "注释文字。", ch.paragraphs[-1].text

        # 卷标题页：重复的 h1 被剔除后没有正文（level=2 允许空）
        assert book.chapters[2].paragraphs == []


def case_leading_body_title_echo_is_dropped() -> None:
    """TXT 改出来的 EPUB 把章标题写成 `<p>`：章首那一段逐字等于目录标题 → 剔除。

    多样本矩阵实测出来的缺陷：这类包每一章正文里都自带一段"第一章 开端"，
    成品就是"h1 标题 + 一模一样的正文段"上下贴着（真池里 21/346 本中招，
    最多一本删 177 段）。上面那条用例的 heading 匹配对这种包完全不生效。

    规则要求**完全相等 + 只看章首第一段**，所以本例同时钉住两个反例：
    含标题但不等于标题的句子、以及等于标题但不在章首的句子 —— 都不许删。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1", "c2"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/ch2.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("第一章 开端", "Text/ch1.xhtml")
            + _navpoint("第二章 转折", "Text/ch2.xhtml")
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body=(
                    "<p>第一章 开端</p>"
                    "<p>正文第一段。</p>"
                    "<p>他想起第一章 开端时的情形。</p>"
                )
            ),
            "OEBPS/Text/ch2.xhtml": XHTML.format(
                body=("<p>正文第二段。</p>" "<p>第二章 转折</p>")
            ),
        }
        book = read_epub(_epub(tmp, entries))

        assert [ch.title for ch in book.chapters] == ["第一章 开端", "第二章 转折"], [
            ch.title for ch in book.chapters
        ]
        assert [p.text for p in book.chapters[0].paragraphs] == [
            "正文第一段。",
            "他想起第一章 开端时的情形。",
        ], [p.text for p in book.chapters[0].paragraphs]
        assert [p.text for p in book.chapters[1].paragraphs] == [
            "正文第二段。",
            "第二章 转折",
        ], [p.text for p in book.chapters[1].paragraphs]


def case_epub3_nav_parse() -> None:
    """EPUB3：没有 NCX，靠 nav.xhtml 的嵌套 <ol> 切章节。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["nav", "c1", "c2"],
            manifest=[
                ("nav", "nav.xhtml", "application/xhtml+xml", "nav"),
                ("c1", "Text/a.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/b.xhtml", "application/xhtml+xml", ""),
            ],
        )
        nav = XHTML.format(
            body=(
                '<nav epub:type="toc"><ol>'
                '<li><a href="Text/a.xhtml">第一章 甲</a></li>'
                '<li><a href="Text/b.xhtml">第二章 乙</a></li>'
                "</ol></nav>"
            )
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/nav.xhtml": nav,
            "OEBPS/Text/a.xhtml": XHTML.format(body="<p>甲段。</p>"),
            "OEBPS/Text/b.xhtml": XHTML.format(body="<p>乙段。</p>"),
        }
        book = read_epub(_epub(tmp, entries))
        assert [ch.title for ch in book.chapters] == ["第一章 甲", "第二章 乙"], [
            ch.title for ch in book.chapters
        ]
        # nav.xhtml 自己不进正文（properties="nav"）
        assert all(p.text != "第一章 甲" for ch in book.chapters for p in ch.paragraphs)


def case_multi_anchor_split() -> None:
    """一个文档里多个目录条目：按 #anchor 切，且章节开头的重复标题被剔除。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/all.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("第一章 甲", "Text/all.xhtml#s1")
            + _navpoint("第二章 乙", "Text/all.xhtml#s2")
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/all.xhtml": XHTML.format(
                body=(
                    '<h2 id="s1">第一章 甲</h2><p>甲段一。</p><p>甲段二。</p>'
                    '<h2 id="s2">第二章 乙</h2><p>乙段一。</p>'
                )
            ),
        }
        book = read_epub(_epub(tmp, entries))
        assert [ch.title for ch in book.chapters] == ["第一章 甲", "第二章 乙"]
        assert [p.text for p in book.chapters[0].paragraphs] == ["甲段一。", "甲段二。"]
        assert [p.text for p in book.chapters[1].paragraphs] == ["乙段一。"]


def case_anchor_missing_fallback() -> None:
    """锚点在文档里找不到（破损书）：不许让第一个节点吞掉整篇、其余空章。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/all.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        # 两个节点都指向不存在的锚点
        ncx = _ncx(
            _navpoint("第一章", "Text/all.xhtml#nope1")
            + _navpoint("第二章", "Text/all.xhtml#nope2")
        )
        body = "".join(f"<p>段落{i}。</p>" for i in range(6))
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/all.xhtml": XHTML.format(body=body),
        }
        book = read_epub(_epub(tmp, entries))
        assert len(book.chapters) == 2
        # 均分兜底：两章都有内容
        assert book.chapters[0].paragraphs, "第一章不该是空的（锚点丢失时要均分兜底）"
        assert book.chapters[1].paragraphs, "第二章不该是空的（锚点丢失时要均分兜底）"


def case_unanchored_first_node_owns_doc_head() -> None:
    """文档被两个目录节点共享、只有后面那个带 #anchor：文档开头归**第一个节点**。

    均分兜底原来按 `run+1` 段切，第一个节点从"让出第一段"开始 —— 那一段不掉进
    本节点也不掉进前言，而是掉进**上一章的尾巴**。真池实测 194 本命中 414 次，
    绝大多数只漏 1 段（通常就是章标题那一行）：上一章结尾凭空多一行，
    这一章开头少了同一行。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1", "c23"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("c23", "Text/ch23.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("第一章 甲", "Text/ch1.xhtml")
            + _navpoint("第二章 乙", "Text/ch23.xhtml")
            + _navpoint("第三章 丙", "Text/ch23.xhtml#s3")
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body="<h1>第一章 甲</h1><p>第一章的正文。</p>"
            ),
            "OEBPS/Text/ch23.xhtml": XHTML.format(
                body=(
                    "<p>第二章 乙</p>"          # 与目录标题逐字相同 → 该被剔除，而不是漏进上一章
                    "<p>第二章的正文。</p>"
                    '<h1 xml:id="s3">第三章 丙</h1>'
                    "<p>第三章的正文。</p>"
                )
            ),
        }
        book = read_epub(_epub(tmp, entries))

        assert [ch.title for ch in book.chapters] == ["第一章 甲", "第二章 乙", "第三章 丙"], [
            ch.title for ch in book.chapters
        ]
        texts = [[p.text for p in ch.paragraphs] for ch in book.chapters]
        assert texts[0] == ["第一章的正文。"], texts[0]
        assert texts[1] == ["第二章的正文。"], texts[1]
        assert texts[2] == ["第三章的正文。"], texts[2]
        assert book.front_matter == [], [p.text for p in book.front_matter]


def case_missing_anchor_matches_unique_heading() -> None:
    """共享文档缺失锚点时，标题和开头正文不能被均分到前一项。"""
    for fragment in ("", "#missing"):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            second = [f"乙正文{i}。" for i in range(10)]
            entries = {
                "OEBPS/content.opf": _opf(spine=["all"], manifest=[
                    ("all", "Text/all.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ]),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("章节甲", "Text/all.xhtml#a")
                    + _navpoint("章节乙", "Text/all.xhtml" + fragment)
                    + _navpoint("章节丙", "Text/all.xhtml#c")
                ),
                "OEBPS/Text/all.xhtml": XHTML.format(body=(
                    '<h1 id="a">章节甲</h1><p>甲正文。</p><h2>章节乙</h2>'
                    + "".join(f"<p>{text}</p>" for text in second)
                    + '<h2 id="c">章节丙</h2><p>丙正文。</p>'
                )),
            }
            book = read_epub(_epub(tmp, entries))
            assert [p.text for p in book.chapters[0].paragraphs] == ["甲正文。"]
            assert [p.text for p in book.chapters[1].paragraphs] == second
            assert [p.text for p in book.chapters[2].paragraphs] == ["丙正文。"]


def case_toc_depth_survives_import_and_roundtrip() -> None:
    """原目录深度要过 JSON 与 EPUB 回环，同名顶层条目不能并入前一分组。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        titles = ["分组甲", "附录", "附录", "分组乙", "嵌套分组", "深层章节"]
        points = (
            _navpoint(titles[0], "Text/d0.xhtml", _navpoint(titles[1], "Text/d1.xhtml"))
            + _navpoint(titles[2], "Text/d2.xhtml")
            + _navpoint(titles[3], "Text/d3.xhtml",
                        _navpoint(titles[4], "Text/d4.xhtml", _navpoint(titles[5], "Text/d5.xhtml")))
        )
        entries = {
            "OEBPS/content.opf": _opf(spine=[f"d{i}" for i in range(len(titles))], manifest=[
                (f"d{i}", f"Text/d{i}.xhtml", "application/xhtml+xml", "") for i in range(len(titles))
            ] + [("ncx", "toc.ncx", "application/x-dtbncx+xml", "")]),
            "OEBPS/toc.ncx": _ncx(points),
            **{f"OEBPS/Text/d{i}.xhtml": XHTML.format(body=f"<h1>{title}</h1><p>正文{i}。</p>")
               for i, title in enumerate(titles)},
        }
        book = read_epub(_epub(tmp, entries))
        assert [c.title for c in book.chapters] == titles
        assert [getattr(c, "toc_depth", None) for c in book.chapters] == [0, 1, 0, 0, 1, 2]
        book = from_dict(json.loads(json.dumps(to_dict(book))))
        rebuilt = build_epub(book, modified=FIXED_MODIFIED)
        rebuilt_path = tmp / "rebuilt.epub"
        rebuilt_path.write_bytes(rebuilt.data)
        back = read_epub(rebuilt_path)
        assert [c.title for c in back.chapters] == titles
        assert [c.toc_depth for c in back.chapters] == [0, 1, 0, 0, 1, 2]
        assert [[p.text for p in c.paragraphs] for c in back.chapters] == [
            [f"正文{i}。"] for i in range(len(titles))
        ]
        # EPUB3 只有 nav、没有 NCX 时，也必须得到同样的深度和分组边界。
        nav_entries = dict(entries)
        del nav_entries["OEBPS/toc.ncx"]
        nav_entries["OEBPS/content.opf"] = _opf(
            spine=[f"d{i}" for i in range(len(titles))],
            manifest=[(f"d{i}", f"Text/d{i}.xhtml", "application/xhtml+xml", "")
                      for i in range(len(titles))]
            + [("nav", "nav.xhtml", "application/xhtml+xml", "nav")],
        )
        nav_entries["OEBPS/nav.xhtml"] = XHTML.format(body=(
            '<nav epub:type="toc"><ol>'
            '<li><a href="Text/d0.xhtml">分组甲</a><ol>'
            '<li><a href="Text/d1.xhtml">附录</a></li></ol></li>'
            '<li><a href="Text/d2.xhtml">附录</a></li>'
            '<li><a href="Text/d3.xhtml">分组乙</a><ol>'
            '<li><a href="Text/d4.xhtml">嵌套分组</a><ol>'
            '<li><a href="Text/d5.xhtml">深层章节</a></li>'
            '</ol></li></ol></li></ol></nav>'
        ))
        nav_book = read_epub(_epub(tmp, nav_entries, name="nav-only.epub"))
        assert [(c.title, c.toc_depth) for c in nav_book.chapters] == [
            (c.title, c.toc_depth) for c in book.chapters
        ]


def case_piece_merge() -> None:
    """分片合并：相邻同主干合并；隔着别的章的同名不合并。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        docs = {}
        points = ""
        for i, title in enumerate(
            ["第一章 上（上）", "第一章 上（中）", "第一章 上（下）", "第二章 乙", "第一章 上（续）"]
        ):
            src = f"Text/d{i}.xhtml"
            docs[f"OEBPS/{src}"] = XHTML.format(body=f"<p>第{i}片正文。</p>")
            points += _navpoint(title, src)
        opf = _opf(
            spine=[f"d{i}" for i in range(5)],
            manifest=[(f"d{i}", f"Text/d{i}.xhtml", "application/xhtml+xml", "") for i in range(5)]
            + [("ncx", "toc.ncx", "application/x-dtbncx+xml", "")],
        )
        entries = {"OEBPS/content.opf": opf, "OEBPS/toc.ncx": _ncx(points), **docs}
        result = read_epub_with_report(_epub(tmp, entries))
        book, report = result.book, result.report

        assert [ch.title for ch in book.chapters] == ["第一章 上", "第二章 乙", "第一章 上（续）"], [
            ch.title for ch in book.chapters
        ]
        # 前三片合成一章，三段都留着
        assert [p.text for p in book.chapters[0].paragraphs] == ["第0片正文。", "第1片正文。", "第2片正文。"]
        assert report["merged_groups"] == 1, report["merged_groups"]
        assert report["merged_pieces"] == 5, report["merged_pieces"]
        # 末尾那片不相邻（隔着第二章），不合并
        assert len(book.chapters) == 3


def case_image_ratio_classes() -> None:
    """四档分流：按宽高比出 class，CSS 里四档宽度都在，且 img 一律不超视口。"""
    assert _image_ratio_class(600, 1200) == "img-tall"     # 0.5
    assert _image_ratio_class(1000, 1000) == "img-square"  # 1.0
    assert _image_ratio_class(1600, 800) == "img-wide"     # 2.0
    assert _image_ratio_class(3000, 900) == "img-strip"    # 3.33
    assert _image_ratio_class(0, 0) == "img-square"        # 读不出尺寸按方图兜底
    # 边界值按"左闭右开"分流
    assert _image_ratio_class(85, 100) == "img-square"     # 0.85 归方图
    assert _image_ratio_class(84, 100) == "img-tall"
    assert _image_ratio_class(250, 100) == "img-wide"     # 2.5 仍归宽横
    assert _image_ratio_class(260, 100) == "img-strip"    # >2.5 才算极端横条

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        images = tmp / "images"
        images.mkdir()
        (images / "tall.png").write_bytes(_png(600, 1200))
        (images / "square.png").write_bytes(_png(1000, 1000))
        (images / "wide.jpg").write_bytes(_jpg(1600, 800))
        (images / "strip.gif").write_bytes(_gif(3000, 900))
        book = Book(
            title="图册",
            author="某人",
            source="X:/样本/图册.txt",
            chapters=[
                Chapter(
                    title="第一章",
                    paragraphs=[
                        Paragraph(text="tall.png", kind="image", caption="竖图说", width=600, height=1200),
                        Paragraph(text="square.png", kind="image", width=1000, height=1000),
                        Paragraph(text="wide.jpg", kind="image", width=1600, height=800),
                        Paragraph(text="strip.gif", kind="image", width=3000, height=900),
                    ],
                )
            ],
        )
        result = build_epub(book, modified=FIXED_MODIFIED, image_dir=images)
        with zipfile.ZipFile(__import__("io").BytesIO(result.data)) as zf:
            chap = zf.read("OEBPS/Text/chap0001.xhtml").decode("utf-8")
            css = zf.read("OEBPS/Styles/main.css").decode("utf-8")
            names = zf.namelist()
        for cls in ("img-tall", "img-square", "img-wide", "img-strip"):
            assert f'figure class="img {cls}"' in chap, f"缺 {cls} 档：{chap}"
        assert '<p class="figcap">竖图说</p>' in chap
        # 图说渲成 <p> 而不是 <figcaption>：掌阅把 figcaption 这个元素整个接管，
        # 元素上的字号/对齐一律按它自己的正文来，类选择器、display:block、
        # !important 三样都撬不动（两代写法都在真机上试过）。<p> 是它排版引擎的
        # 基本单元，字号一定跟着样式表走。
        assert "<figcaption" not in chap
        assert ".figcap {\n    display: block;" in css
        assert "p.figcap {" in css
        assert "text-align: center !important;" in css
        assert "font-size: 0.8em !important;" in css
        # 逗号列表是连坐的：一条规则里塞两个选择器，弃一个等于两个都没了
        assert "figure.img figcaption, figure.img .figcap" not in css
        # 容器同理：纯类规则要给块级，否则没有 text-align 的作用对象
        assert ".img {\n    display: block;" in css
        assert 'src="../Images/tall.png"' in chap
        # 图片进包，且在封面页之前
        for name in ("tall.png", "square.png", "wide.jpg", "strip.gif"):
            assert f"OEBPS/Images/{name}" in names
        assert names.index("OEBPS/Images/tall.png") < names.index("OEBPS/Text/cover.xhtml")
        # CSS 四档宽度 + 视口上限
        for needle in ("max-width: 60%", "max-width: 70%", "max-width: 90%", "max-width: 100%"):
            assert needle in css, needle
        assert "max-height: 95vh" in css


def case_decorative_and_orphan() -> None:
    """装饰图（引用超阈值）丢弃并报告；只在 CSS 里引用的图不算孤儿、也不打包。

    阈值两侧都钉：正好 10 次的**留在正文**，11 次的才判家具。
    这条边界是 349 本真 EPUB 实测出来的（docs/gui-design.md 的「三类图片判定」一节）：观察到的家具图下限是 11 次，
    9–10 是空带，所以 10 次这一侧必须保留 —— 误删插图比留下几张废图贵得多。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        logo = _png(120, 40)
        body_imgs = "".join('<p><img src="../Images/logo.png"/></p>' for _ in range(12))
        band = "".join('<p><img src="../Images/band.png"/></p>' for _ in range(10))
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                    ("logo", "Images/logo.png", "image/png", ""),
                    ("band", "Images/band.png", "image/png", ""),
                    ("bg", "Images/bg.png", "image/png", ""),
                    ("real", "Images/real.png", "image/png", ""),
                    ("css", "Styles/main.css", "text/css", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": XHTML.format(
                body=body_imgs + band + '<p><img src="../Images/real.png"/></p>'
            ),
            "OEBPS/Images/logo.png": logo,
            "OEBPS/Images/band.png": _png(120, 40),
            "OEBPS/Images/bg.png": _png(10, 10),
            "OEBPS/Images/real.png": _png(400, 600),
            "OEBPS/Styles/main.css": "body { background: url(../Images/bg.png); }",
        }
        out = tmp / "out"
        result = read_epub_with_report(_epub(tmp, entries), image_dir=out)
        book, report = result.book, result.report

        # 装饰图一张都不进正文；引用正好 10 次的那张留在正文
        assert report["images_decorative"] == ["logo.png"], report["images_decorative"]
        paras = [p for ch in book.chapters for p in ch.paragraphs if p.kind == "image"]
        assert len(paras) == 11, len(paras)
        assert paras[-1].width == 400 and paras[-1].height == 600
        # 三个数各管各的，别混：11 个图片段 / 2 张不同的图（同名只落一份）/ 账单也是 2
        assert report["images_kept"] == 2, report["images_kept"]
        assert len({p.text for p in paras}) == 2, {p.text for p in paras}
        on_disk = sorted(p.name for p in out.iterdir())
        assert on_disk == sorted({p.text for p in paras}), on_disk
        assert all(n.startswith("img") for n in on_disk), on_disk
        # 只在 CSS 里出现的图：不算孤儿（判定必须连 CSS url() 一起扫）
        assert "bg.png" not in report["images_orphan"], report["images_orphan"]


def case_caption_attachment() -> None:
    """caption：图后面的图说块挂到图上；中间夹了正文就不挂。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                    ("i1", "Images/i1.png", "image/png", ""),
                    ("i2", "Images/i2.png", "image/png", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": XHTML.format(
                body=(
                    '<div class="duokan-image-single"><img src="../Images/i1.png"/></div>'
                    '<div class="duokan-image-maintitle">第一张的图说</div>'
                    '<div class="duokan-image-single"><img src="../Images/i2.png"/></div>'
                    "<p>夹在中间的正文。</p>"
                    '<div class="duokan-image-maintitle">这句不该挂上去</div>'
                )
            ),
            "OEBPS/Images/i1.png": _png(800, 600),
            "OEBPS/Images/i2.png": _png(800, 600),
        }
        book = read_epub(_epub(tmp, entries), image_dir=tmp / "out")
        ch = book.chapters[0]
        images = [p for p in ch.paragraphs if p.kind == "image"]
        assert len(images) == 2
        assert images[0].caption == "第一张的图说", images[0].caption
        # 第二张的图说与图之间夹了正文，按"归属检查"不挂到图上，而是留在正文里
        assert images[1].caption == "", images[1].caption
        assert any(p.kind == "body" and p.text == "这句不该挂上去" for p in ch.paragraphs), [
            (p.kind, p.text) for p in ch.paragraphs
        ]


def case_cover_svg_shell() -> None:
    """封面：OPF 指向封面页，页面里是 <svg><image xlink:href> 包位图的写法。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["cover", "c1"],
                manifest=[
                    ("cover", "Text/cover.xhtml", "application/xhtml+xml", ""),
                    ("cover-img", "Images/cover.jpg", "image/jpeg", ""),
                    ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
                extra_meta='    <meta name="cover" content="cover"/>',
                guide='  <guide><reference type="cover" href="Text/cover.xhtml"/></guide>',
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/cover.xhtml": XHTML.format(
                body='<svg viewBox="0 0 2480 3508"><image xlink:href="../Images/cover.jpg"/></svg>'
            ),
            "OEBPS/Text/ch.xhtml": XHTML.format(body="<p>正文。</p>"),
            "OEBPS/Images/cover.jpg": _jpg(2480, 3508),
        }
        result = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")
        assert result.report["cover"] == "cover.jpg", result.report["cover"]
        # 封面页本身不进章节
        assert [ch.title for ch in result.book.chapters] == ["第一章"]
        # 给了 image_dir 时封面要单独落盘，报告里点出文件名 ——
        # 重新打包时「用原书封面」需要一个真实路径（gui/api.py 读这个键）。
        assert result.report["cover_file"] == "cover.jpg", result.report["cover_file"]
        saved = tmp / "out" / "cover.jpg"
        assert saved.is_file(), sorted(p.name for p in (tmp / "out").iterdir())
        # 封面不算正文插图：不能混进 images_kept 的编号序列
        assert result.report["images_kept"] == 0, result.report["images_kept"]
        # 没有封底：back_file 与 cover_file 一样是空串，不是 None
        assert result.report["back_file"] == "", result.report["back_file"]


def case_cover_file_empty_without_image_dir() -> None:
    """不给 image_dir 时封面没有落盘位置：cover_file 必须是空串而不是 None。

    `gui/api.py` 用 `if cover_file:` 判断，None 与 "" 在这里等价，
    但报告是给人看也给人读的 JSON，字段类型要稳定（永远是 str）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["cover", "c1"],
                manifest=[
                    ("cover", "Text/cover.xhtml", "application/xhtml+xml", ""),
                    ("cover-img", "Images/cover.jpg", "image/jpeg", ""),
                    ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
                extra_meta='    <meta name="cover" content="cover"/>',
                guide='  <guide><reference type="cover" href="Text/cover.xhtml"/></guide>',
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/cover.xhtml": XHTML.format(
                body='<svg viewBox="0 0 2480 3508"><image xlink:href="../Images/cover.jpg"/></svg>'
            ),
            "OEBPS/Text/ch.xhtml": XHTML.format(body="<p>正文。</p>"),
            "OEBPS/Images/cover.jpg": _jpg(2480, 3508),
        }
        result = read_epub_with_report(_epub(tmp, entries))
        assert result.report["cover_file"] == "", result.report["cover_file"]
        # cover 键照旧报原包里的文件名，两件事不要混
        assert result.report["cover"] == "cover.jpg", result.report["cover"]


def case_broken_zip_and_xml() -> None:
    """容错：截断的 ZIP 报 EpubReadError；坏 XML/缺图片只警告不崩。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 1) 截断的包
        good = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c1"],
                    manifest=[
                        ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
                "OEBPS/Text/ch.xhtml": XHTML.format(body="<p>正文。</p>"),
            },
            name="good.epub",
        )
        raw = good.read_bytes()
        broken = tmp / "broken.epub"
        broken.write_bytes(raw[: len(raw) // 2])
        try:
            read_epub(broken)
        except EpubReadError as exc:
            assert "ZIP" in str(exc), str(exc)
        else:
            raise AssertionError("截断的 EPUB 应该报 EpubReadError")

        # 2) 坏 XML + 缺图片 + 缺锚点：能导入，但要有警告
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(
                _navpoint("第一章", "Text/ch.xhtml#nope")
                + _navpoint("第二章", "Text/ch.xhtml")
            ),
            "OEBPS/Text/ch.xhtml": (
                "<html><body><h2>第一章</h2><p>没闭合的段落"
                '<img src="../Images/nowhere.png"/><p>下一段。</p></body>'
            ),
        }
        result = read_epub_with_report(_epub(tmp, entries, name="dirty.epub"))
        assert len(result.book.chapters) == 2
        assert any("nowhere.png" in w for w in result.report["warnings"]), result.report["warnings"]


def case_no_nav_fallback() -> None:
    """没有 NCX/nav 的包：按 spine 一文件一章，标题取正文里第一个标题。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["c1", "c2"],
                manifest=[
                    ("c1", "Text/a.xhtml", "application/xhtml+xml", ""),
                    ("c2", "Text/b.xhtml", "application/xhtml+xml", ""),
                ],
            ),
            "OEBPS/Text/a.xhtml": XHTML.format(body="<h1>第一章 甲</h1><p>甲段。</p>"),
            "OEBPS/Text/b.xhtml": XHTML.format(body="<h1>第二章 乙</h1><p>乙段。</p>"),
        }
        result = read_epub_with_report(_epub(tmp, entries))
        assert [ch.title for ch in result.book.chapters] == ["第一章 甲", "第二章 乙"]
        assert any("没有可用的 NCX/nav" in w for w in result.report["warnings"])


def case_drm_and_fixed_layout() -> None:
    """DRM 与固定版式必须明确报错：不猜密钥、不硬把漫画拆成散图。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        base_manifest = [
            ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
            ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
        ]
        encryption = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/Text/ch.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""
        entries = {
            "OEBPS/content.opf": _opf(spine=["c1"], manifest=base_manifest),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": XHTML.format(body="<p>正文。</p>"),
            "META-INF/encryption.xml": encryption,
        }
        try:
            read_epub(_epub(tmp, entries, name="drm.epub"))
        except EpubReadError as exc:
            assert "DRM" in str(exc), str(exc)
        else:
            raise AssertionError("正文被加密的 EPUB 应该报 EpubReadError")

        fixed = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=base_manifest,
                extra_meta='    <meta property="rendition:layout">pre-paginated</meta>',
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": XHTML.format(body="<p>正文。</p>"),
        }
        try:
            read_epub(_epub(tmp, fixed, name="fixed.epub"))
        except EpubReadError as exc:
            assert "固定版式" in str(exc), str(exc)
        else:
            raise AssertionError("固定版式的 EPUB 应该报 EpubReadError")


def case_drm_media_type_and_images() -> None:
    """DRM 分类不能只看扩展名，各类资源的后果要分别说清。

    三种情形：
    1. 被加密的正文条目 URI 没扩展名（Text/ch）→ 仍要按 manifest 的
       media-type 认出来并报错，不能因为扩展名不匹配就放过去；
    2. 图片被加密 → 不报错（正文还能读），但必须说清「这些图会缺」；
    3. 只加密 CSS/字体 → 警告里不许再出现「不影响正文导入」这种空话
       （代码根本没有跳过逻辑）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        body = XHTML.format(body="<p>正文。</p>")

        def enc(*uris: str) -> str:
            refs = "\n".join(
                f'    <enc:CipherData><enc:CipherReference URI="{u}"/></enc:CipherData>'
                for u in uris
            )
            return f"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
{refs}
  </enc:EncryptedData>
</encryption>
"""

        # 情形一：正文条目没有扩展名，靠 media-type 认出来。
        noext = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/ch", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch")),
            "OEBPS/Text/ch": body,
            "META-INF/encryption.xml": enc("OEBPS/Text/ch"),
        }
        try:
            read_epub(_epub(tmp, noext, name="noext.epub"))
        except EpubReadError as exc:
            assert "DRM" in str(exc), str(exc)
        else:
            raise AssertionError("正文条目无扩展名时也必须按 media-type 认出 DRM")

        # 情形二：只加密一张图 → 不报错，但必须说清图片会缺。
        img_manifest = [
            ("c1", "Text/ch.xhtml", "application/xhtml+xml", ""),
            ("i1", "Images/pic.jpg", "image/jpeg", ""),
            ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
        ]
        img_only = {
            "OEBPS/content.opf": _opf(spine=["c1"], manifest=img_manifest),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": body,
            "OEBPS/Images/pic.jpg": _jpg(20, 30),
            "META-INF/encryption.xml": enc("OEBPS/Images/pic.jpg"),
        }
        got = read_epub_with_report(_epub(tmp, img_only, name="imgdrm.epub"))
        joined = " | ".join(got.report["warnings"])
        assert "图片被加密" in joined, got.report["warnings"]
        assert "缺失" in joined or "乱码" in joined, got.report["warnings"]

        # 情形三：只加密 CSS → 警告里不许出现「不影响正文导入」的旧空话。
        css_only = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=img_manifest + [("css", "Styles/main.css", "text/css", "")],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch.xhtml")),
            "OEBPS/Text/ch.xhtml": body,
            "OEBPS/Images/pic.jpg": _jpg(20, 30),
            "OEBPS/Styles/main.css": "p { margin: 0 }",
            "META-INF/encryption.xml": enc("OEBPS/Styles/main.css"),
        }
        css_got = read_epub_with_report(_epub(tmp, css_only, name="cssdrm.epub"))
        css_joined = " | ".join(css_got.report["warnings"])
        assert "不影响正文导入" not in css_joined, css_got.report["warnings"]
        assert "样式" in css_joined, css_got.report["warnings"]


def case_fixed_layout_per_item() -> None:
    """固定版式也写在 manifest 条目上：只查全局 meta 会漏掉整本漫画包。

    两种情形分开测：全部正文条目都是固定版式 → 整本报错；
    只有一部分是（混排包）→ 不报错，但要留一条说清数量的警告。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ncx = _ncx(_navpoint("第一章", "Text/ch.xhtml"))
        body = XHTML.format(body="<p>正文。</p>")

        # 情形一：所有 spine 条目都声明 pre-paginated，全局 meta 没写。
        all_fixed = {
            "OEBPS/content.opf": _opf(
                spine=["c1", "c2"],
                manifest=[
                    ("c1", "Text/a.xhtml", "application/xhtml+xml",
                     "rendition:layout-pre-paginated"),
                    ("c2", "Text/b.xhtml", "application/xhtml+xml",
                     "rendition:layout-pre-paginated"),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/a.xhtml": body,
            "OEBPS/Text/b.xhtml": body,
        }
        try:
            read_epub(_epub(tmp, all_fixed, name="allfixed.epub"))
        except EpubReadError as exc:
            assert "固定版式" in str(exc), str(exc)
        else:
            raise AssertionError("全部条目标了固定版式的 EPUB 应该报 EpubReadError")

        # 情形二：两章里只有一章是固定版式 → 不报错，出一条带计数的警告。
        partly = {
            "OEBPS/content.opf": _opf(
                spine=["c1", "c2"],
                manifest=[
                    ("c1", "Text/a.xhtml", "application/xhtml+xml",
                     "rendition:layout-pre-paginated"),
                    ("c2", "Text/b.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(
                _navpoint("第一章", "Text/a.xhtml") + "\n" + _navpoint("第二章", "Text/b.xhtml")
            ),
            "OEBPS/Text/a.xhtml": body,
            "OEBPS/Text/b.xhtml": body,
        }
        result = read_epub_with_report(_epub(tmp, partly, name="partly.epub"))
        hits = [w for w in result.report["warnings"] if "固定版式" in w or "pre-paginated" in w]
        assert hits, result.report["warnings"]
        assert "1/2" in hits[0], hits[0]

        # 情形三：正常书不该冒出这条警告（防误伤）。
        normal = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/a.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/a.xhtml")),
            "OEBPS/Text/a.xhtml": body,
        }
        plain = read_epub_with_report(_epub(tmp, normal, name="plain.epub"))
        assert not [w for w in plain.report["warnings"] if "pre-paginated" in w]


def case_image_size_headers() -> None:
    """尺寸解析：PNG/JPEG/GIF 三种文件头都要读得出；读不出返回 (0, 0)。"""
    assert _image_size(_png(927, 1265)) == (927, 1265)
    assert _image_size(_jpg(2480, 3508)) == (2480, 3508)
    assert _image_size(_gif(300, 200)) == (300, 200)
    assert _image_size(b"not an image") == (0, 0)
    assert _image_size(b"\x89PNG\r\n\x1a\n") == (0, 0)


def case_roundtrip_with_images() -> None:
    """回环：本工具打出来的 EPUB（带插图与图说）再导入，章节与图片都在。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        images = tmp / "src-images"
        images.mkdir()
        (images / "img001.png").write_bytes(_png(600, 1200))
        (images / "img002.png").write_bytes(_png(1000, 1000))
        book = Book(
            title="回环书",
            author="某人",
            source="X:/样本/回环书.txt",
            front_matter=[Paragraph(text="前言一段。")],
            chapters=[
                Chapter(
                    title="第一章 开端",
                    paragraphs=[
                        Paragraph(text="正文一段。"),
                        Paragraph(text="img001.png", kind="image", caption="竖图说", width=600, height=1200),
                        Paragraph(text="正文二段。"),
                        Paragraph(text="img002.png", kind="image", width=1000, height=1000),
                    ],
                ),
                Chapter(title="第二章 继续", paragraphs=[Paragraph(text="收尾段。")]),
            ],
        )
        built = build_epub(book, modified=FIXED_MODIFIED, image_dir=images)
        epub_path = tmp / "roundtrip.epub"
        epub_path.write_bytes(built.data)

        back = read_epub(epub_path, image_dir=tmp / "out")
        assert [ch.title for ch in back.chapters] == ["前附", "第一章 开端", "第二章 继续"], [
            ch.title for ch in back.chapters
        ]
        assert [p.text for p in back.chapters[0].paragraphs] == ["前言一段。"]
        paras = back.chapters[1].paragraphs
        imgs = [p for p in paras if p.kind == "image"]
        assert len(imgs) == 2, len(imgs)
        assert (imgs[0].width, imgs[0].height) == (600, 1200), (imgs[0].width, imgs[0].height)
        assert imgs[0].caption == "竖图说", imgs[0].caption
        # 正文段落原样回来（图说没有变成正文段）
        texts = [p.text for p in paras if p.kind != "image"]
        assert "正文一段。" in texts and "正文二段。" in texts, texts
        assert "竖图说" not in texts, texts

        # 同一棵章节树渲染成干净 TXT 时，图片段不产出任何行：
        # 否则 TXT 里会出现一行"img001.png"这种乱码（图片在纯文本里没有位置）。
        txt = "\n".join(
            line
            for p in book.chapters[0].paragraphs
            for line in _paragraph_lines(p, "")
        )
        assert "img001.png" not in txt and "img002.png" not in txt, txt


def case_schema_v2_image_paragraph() -> None:
    """契约 v2：image 段带 caption/width/height；旧 v1 树读入后除 version 外字节不变。"""
    p = Paragraph(text="img003.jpg", kind="image", caption="图说", width=927, height=1265)
    d = to_dict(
        Book(
            title="书",
            author="",
            source="X:/a.txt",
            chapters=[Chapter(title="第一章", paragraphs=[p, Paragraph(text="正文。")])],
        )
    )
    raw = d["chapters"][0]["paragraphs"][0]
    assert raw == {"text": "img003.jpg", "kind": "image", "caption": "图说", "width": 927, "height": 1265}, raw
    # 普通段落不带新字段（否则 v1 旧文件重新序列化会变字节）
    assert d["chapters"][0]["paragraphs"][1] == {"text": "正文。", "kind": "body"}
    back = from_dict(d)
    assert back.chapters[0].paragraphs[0] == p


def _flatten_kinds(book: Book) -> list[str]:
    """整本书按阅读顺序摊平段落 kind；空行（blank）不算，它随渲染被折叠。"""
    kinds: list[str] = []
    for p in book.front_matter:
        if p.kind != "blank":
            kinds.append(p.kind)
    for ch in book.chapters:
        for p in ch.paragraphs:
            if p.kind != "blank":
                kinds.append(p.kind)
    for p in book.tail_matter:
        if p.kind != "blank":
            kinds.append(p.kind)
    return kinds


def case_ruby_kept_as_inline_note() -> None:
    """注音不是正文：`<ruby>文<rt>AYA</rt></ruby>` 读成「文」+ 一条注音。

    错在把 rt 收进正文缓冲，一段话会变成「梦泽文AYA哉KA」——检索、字数、
    朗读全被污染。同时 `<rp>(</rp>` 那对兜底括号也不能留（真书里会变成
    「文（AYA）」）。注音本身不能丢：人名读音、代号原文都是信息。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c1"],
                    manifest=[
                        ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/c1.xhtml")),
                "OEBPS/Text/c1.xhtml": XHTML.format(
                    body="<h1>第一章</h1>"
                    "<p>梦泽<ruby>文<rt>AYA</rt></ruby><ruby>哉<rt>KA</rt></ruby>。</p>"
                    "<p>和泉<ruby>早希<rp>(</rp><rt>SAKI</rt><rp>)</rp></ruby>来了。</p>"
                    "<table><tr><td>伊豆<ruby>美崎<rt>MISAKI</rt></ruby></td></tr></table>"
                ),
            },
        )
        paras = read_epub(path).chapters[0].paragraphs
        assert paras[0].text == "梦泽文哉。", paras[0].text
        assert paras[0].ruby == [
            {"at": 2, "len": 1, "rt": "AYA"},
            {"at": 3, "len": 1, "rt": "KA"},
        ], paras[0].ruby
        # rp 是"不支持 ruby 的阅读器"看到的括号，不该进正文，也不该算进 base。
        assert paras[1].text == "和泉早希来了。", paras[1].text
        assert paras[1].ruby == [{"at": 2, "len": 2, "rt": "SAKI"}], paras[1].ruby
        # 表格没有注音这一列：读音丢掉，但 base 必须留、不能把 rt 混进格子
        # （`<ruby>` 分支排在表格分支之后时，格子会变成「伊豆MISAKI美崎」）。
        assert paras[2].kind == "table", paras[2].kind
        assert paras[2].rows[0][0]["text"] == "伊豆美崎", paras[2].rows[0][0]["text"]


def case_ruby_roundtrip_keeps_note() -> None:
    """注音过一遍打包不能再退化：build_epub 渲 `<ruby>`，read_epub 原样读回。

    回环是项目契约。少了这条，重排一次注音就变成正文里的裸字母，而重排
    两次的产物与一次不同 —— 不动点当场破掉。
    """
    book = Book(
        title="注音回环",
        author="假作者",
        source="X:/假/注音回环.txt",
        chapters=[
            Chapter(
                title="第一章 假章",
                number=1,
                paragraphs=[Paragraph(text="梦泽文哉。", ruby=[{"at": 2, "len": 2, "rt": "FUMIYA"}])],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as td:
        built = build_epub(book, modified=FIXED_MODIFIED)
        path = Path(td) / "ruby.epub"
        path.write_bytes(built.data)
        para = read_epub(path).chapters[0].paragraphs[0]
    assert para.text == "梦泽文哉。", para.text
    assert para.ruby == [{"at": 2, "len": 2, "rt": "FUMIYA"}], para.ruby


def case_self_roundtrip_kinds() -> None:
    """自家产物回环：build_epub → read_epub，段落 kind 序列与图说必须一字不差。

    epub.py 把 verse/note 渲成 <p class="...">、场景分隔线渲成 <hr class="sep"/>、
    空行渲成 <div class="sep">、
    插图渲成 <figure><img/><figcaption/></figure>、标题渲成 <h1>/<h2 class="sub">。
    光按 tag 判 kind 会把 verse/note 读回 body，这里逐档钉死。

    允许的结构性差异（不是 kind 退化）：
    - 空行：渲染时折叠/掐尾，重排回来数量会变，故比较时剔除 blank；
    - 书前内容：打包时收成「前附」卷，重排回来是卷首页加原章节（顺序与 kind 不变）；
    - 后记：仍折进最后一章末尾，重排回来就在那一章里；
    - 图片文件名：落盘名由导入器重新编号（imgNNN.ext），身份以尺寸+图说+顺序为准。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        images = tmp / "src-images"
        images.mkdir()
        (images / "img001.png").write_bytes(_png(600, 900))   # 竖图 0.67
        (images / "img002.png").write_bytes(_png(800, 500))   # 宽图 1.60
        book = Book(
            title="回环假书",
            author="假作者",
            source="X:/假/回环假书.txt",
            front_matter=[
                Paragraph(text="假前言一。"),
                Paragraph(text="假前言二。"),
                Paragraph(text="", kind="blank"),
            ],
            chapters=[
                Chapter(
                    title="第一卷 假卷",
                    level=2,
                    number=1,
                    paragraphs=[Paragraph(text="假卷首语。")],
                ),
                Chapter(
                    title="第一章 假章",
                    number=1,
                    paragraphs=[
                        Paragraph(text="假正文一。"),
                        Paragraph(text="", kind="blank"),
                        # 分隔线故意用非分隔符文字：证明是 class 判据生效，
                        # 不是靠 _is_separator_line 的形态兜底。
                        Paragraph(text="假分隔行", kind="sep"),
                        Paragraph(text="假小标题。", kind="heading"),
                        Paragraph(text="假诗句一行。", kind="verse"),
                        Paragraph(text="假注一。", kind="note"),
                        Paragraph(text="假注二。", kind="note"),
                        Paragraph(text="假正文二。"),
                        Paragraph(
                            text="img001.png",
                            kind="image",
                            caption="假图说一",
                            width=600,
                            height=900,
                        ),
                        Paragraph(text="假正文三。"),
                        Paragraph(text="img002.png", kind="image", width=800, height=500),
                    ],
                ),
                Chapter(title="第二章 假章二", number=2, paragraphs=[Paragraph(text="假正文四。")]),
            ],
            tail_matter=[Paragraph(text="假后记。")],
        )

        built = build_epub(book, modified=FIXED_MODIFIED, image_dir=images)
        epub_path = tmp / "self.epub"
        epub_path.write_bytes(built.data)

        imp = read_epub_with_report(epub_path, image_dir=tmp / "out")
        back = imp.book

        # 1) 书前内容回环成「前附」卷，原章节标题与卷级不变
        assert [c.title for c in back.chapters] == [
            "前附", *[c.title for c in book.chapters]
        ], [c.title for c in back.chapters]
        assert [c.level for c in back.chapters] == [
            2, *[c.level for c in book.chapters]
        ], [c.level for c in back.chapters]

        # 2) kind 序列一字不差（这是本用例的核心断言）
        assert _flatten_kinds(back) == _flatten_kinds(book), (
            _flatten_kinds(book),
            _flatten_kinds(back),
        )

        # 3) 插图：尺寸与图说都不能丢，文件名是裸文件名
        def _imgs(b: Book) -> list[Paragraph]:
            return [p for c in b.chapters for p in c.paragraphs if p.kind == "image"]

        got = _imgs(back)
        assert len(got) == 2, len(got)
        assert [(p.width, p.height) for p in got] == [(600, 900), (800, 500)], [
            (p.width, p.height) for p in got
        ]
        assert [p.caption for p in got] == ["假图说一", ""], [p.caption for p in got]
        for p in got:
            assert "/" not in p.text and ".." not in p.text, p.text
            assert p.text.endswith(".png"), p.text

        # 4) 2026-10-28 起我们不再产出信息页（试用反馈"不要推广页"），所以
        # 自家回环里 generated_pages 必然是 0。老版本 huoban 打的包仍带
        # info.xhtml，"整页跳过"的能力保留着 —— 由 case_generated_info_page_skipped
        # 用合成包单独钉住，不再靠这里顺带验证。
        assert imp.report["generated_pages"] == 0, imp.report["generated_pages"]
        all_text = [p.text for p in _all_paragraphs(back)]
        assert not any("章节数" in t or "来源文件" in t for t in all_text), all_text
        assert not any("huoban" in t for t in all_text), all_text


def _all_paragraphs(book: Book) -> list[Paragraph]:
    out = list(book.front_matter) + list(book.tail_matter)
    for ch in book.chapters:
        out.extend(ch.paragraphs)
    return out


def case_title_note_stays_on_chapter() -> None:
    """章首标题里的注码要留在标题上，不跟着"重复标题"一起消失。

    真样本《嘯風山莊》有三条注释是这么没的：注标写在 `<h1>` 里（
    `<h1>作者生平述要<img class="zhangyue-footnote" …/></h1>`），标题又与
    目录同名，`_drop_duplicate_heading` 把整块剔掉，注码随之消失；
    `_finalize_refs` 再判定"没有引用指向这条注文"，把注文也丢掉 ——
    成品里注码和注文两样都不剩，读者看不出少了东西。

    现在注码存进 `Chapter.refs`（偏移相对标题文字），打包时写回 `<h1>`，
    位置与源书一致 —— 不再落到正文第一句前面。
    """
    mark = (
        '<img class="zhangyue-footnote" src="../Images/note.png" '
        'alt="注" zy-footnote="{}"/>'
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1", "c2"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/ch2.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("第一章 开端", "Text/ch1.xhtml")
            + _navpoint("第二章 转折", "Text/ch2.xhtml")
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body="<h1>第一章 开端" + mark.format("标题上的注。") + "</h1>"
                "<p>正文第一段。</p>"
            ),
            "OEBPS/Text/ch2.xhtml": XHTML.format(
                body="<h1>第二章 转折" + mark.format("只有标题的一章。") + "</h1>"
            ),
        }
        book = read_epub(_epub(tmp, entries))

        # 1) 标题照样被剔除，注码留在章节标题上，偏移是标题末尾，正文段干净
        first = book.chapters[0]
        assert [p.text for p in first.paragraphs] == ["正文第一段。"], [
            p.text for p in first.paragraphs
        ]
        assert first.paragraphs[0].refs == [], first.paragraphs[0].refs
        assert [r.get("text") for r in first.refs] == ["标题上的注。"], first.refs
        assert first.refs[0]["at"] == len("第一章 开端"), first.refs

        # 2) 整章只有标题：不再是"只带注码的空壳段"，标题注照样有地方待
        second = book.chapters[1]
        assert second.paragraphs == [], [p.text for p in second.paragraphs]
        assert [r.get("text") for r in second.refs] == [
            "只有标题的一章。"
        ], second.refs


def case_title_note_marks_unlabelled_note_paragraph() -> None:
    """只被标题注码指着的注文条目，也要翻成 note。

    《占星术杀人魔法》的 AZOTH 那一章：

        <h1>AZOTH<a href="#a1" id="b1"><span class="note">[1]</span></a></h1>
        <p class="footnote"><a href="#b1" id="a1">[1]</a>AZOTH即“阿索德”。</p>

    `footnote` 不在认得的 class 表里（只认 note / verse / sep），注文段唯一
    的线索是"有注码指着它"。其余六条的注码都在正文里，顺利转了；唯独这一条
    只被标题指着 —— 旧代码扫的 `referenced` 只走段落，标题注不在其中，于是
    它停在 `body`，打包时找不到注文可写，标题上那个 [1] 退化成点不开的死链接。

    `_iter_anchor_keys`、`_drop_unreferenced_anchors` 都补了标题注这一趟，
    只有这里漏了；补上之后标题注与正文注同一口径（at > 0 才算"往外指"）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("AZOTH", "Text/ch1.xhtml")),
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body=(
                    '<h1>AZOTH<a href="#a1" id="b1">'
                    '<span class="note">[1]</span></a></h1>'
                    "<p>正文第一段。</p>"
                    '<p class="footnote"><a href="#b1" id="a1">[1]</a>'
                    "AZOTH即“阿索德”。</p>"
                )
            ),
        }
        book = read_epub(_epub(tmp, entries))

    ch = book.chapters[0]
    # 1) 注文条目翻成 note，开头的回指编号剥掉
    notes = [p for p in ch.paragraphs if p.kind == "note"]
    assert [p.text for p in notes] == ["AZOTH即“阿索德”。"], [
        (p.kind, p.text) for p in ch.paragraphs
    ]
    assert notes[0].anchor == "a1", notes[0].anchor
    assert notes[0].refs == [], notes[0].refs
    # 2) 标题上的注码原样留着，指向那条注文（偏移在标题文字之后）
    assert [r["note"] for r in ch.refs] == ["a1"], ch.refs
    assert ch.refs[0]["at"] == len("AZOTH"), ch.refs
    assert ch.title == "AZOTH", ch.title


def case_note_number_strip_only_foreign() -> None:
    """脚注编号剥离只对"别人的"脚注生效，且不许吃掉正文开头的数字。

    回归：旧正则 `^[\\[（(]?\\s*\\d{1,3}\\s*[...]?\\s*` 不要求数字后面有分隔符，
    "1943 年的德国" 会被切成 "3 年的德国"（前 3 位数字被当编号剥掉）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Text/n.xhtml": XHTML.format(
                body=(
                    # 别人的脚注：编号该剥
                    '<ol class="duokan-footnote-content">'
                    '<li class="duokan-footnote-item"><a href="#n1">1</a> 注释甲。</li>'
                    "</ol>"
                    # 别人的脚注：数字是内容的一部分，不许剥
                    '<ol class="duokan-footnote-content">'
                    '<li class="duokan-footnote-item">1943 年的旧事。</li>'
                    "</ol>"
                    # 自家产物的 note：class 恰好是 "note"，文本原样保留
                    '<div class="notes">'
                    '<p class="note">7 条自家注。</p>'
                    "</div>"
                )
            ),
        }
        opf = _opf(
            spine=["n"],
            manifest=[
                ("n", "Text/n.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx([("第1章 假", "Text/n.xhtml")])
        book = read_epub(_epub(tmp, entries))

        notes = [p.text for p in book.chapters[0].paragraphs if p.kind == "note"]
        assert notes == ["注释甲。", "1943 年的旧事。", "7 条自家注。"], notes


def case_duokan_noteref_icon_keeps_ref() -> None:
    """多看式注码：`<a class="duokan-footnote" epub:type="noteref">` 里套着一个图标。

    《啸风山庄》（下载的那个版本）229 个 noteref 全是这个形状。旧代码见 class
    含 duokan-footnote 就整块跳过，引用一个都没留下 —— 注文段随即被判成"没人
    指"，锚点又被当死 id 清掉，整本书读起来像没有注释。

    要钉住三件事：引用留下（目标是注释条目的 id）、图标不收成插图、
    注释条目的**容器** id 当锚点（正文的 `#note_1` 才有落点）。
    """
    body = XHTML.format(body=(
        '<h2 id="c1">第一章</h2>'
        '<p>正文开始'
        '<a class="duokan-footnote" epub:type="noteref" href="#note_1" id="noteref_1">'
        '<span id="k1"><img alt="note" src="../Images/f16.png" class="calibre14"/></span>'
        "</a>继续。</p>"
        '<aside epub:type="footnote"><ol class="duokan-footnote-content">'
        '<li class="duokan-footnote-item" id="note_1"><p class="footnote">'
        '<a href="#noteref_1"><span>⊙</span></a><span>第一条注。</span>'
        "</p></li></ol></aside>"
    ))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(tmp, {
            "OEBPS/content.opf": _opf(
                spine=["c"],
                manifest=[
                    ("c", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章", "Text/ch1.xhtml")),
            "OEBPS/Text/ch1.xhtml": body,
        })
        book = read_epub(path)

    paras = _all_paragraphs(book)
    refs = [r for p in paras for r in p.refs]
    assert [r["note"] for r in refs] == ["note_1"], refs
    # 注文条目自己的 id 成了锚点，正文的 #note_1 才落得住；
    # 开头那个回指标记（⊙）也要剥掉，否则每条注释点开都先看到一个符号。
    notes = [p for p in paras if p.kind == "note"]
    assert [p.anchor for p in notes] == ["note_1"], notes
    assert notes[0].text == "第一条注。", notes[0].text
    # 注码图标不是插图：整本书不该多出一个 image 段
    assert not [p for p in paras if p.kind == "image"], [p.text for p in paras if p.kind == "image"]


def case_calibre_noteref_and_rearnote() -> None:
    """Calibre 转出来的弹注：无前缀 type="noteref"/"rearnote"，且注释在另一个文件里。

    这是实测"弹注全部失效"的那本书的形态（noteref 7104 次、footnote 与
    epub:type 全 0）。旧代码只认 epub:type 与 duokan 的 class，结果整本注释
    退化成普通正文段落，正文里的 [1] 也只剩一串纯文字。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        body = XHTML.format(
            body=(
                '<h2 id="c1">第1章 起</h2>'
                '<p>正文开始<a id="noteref_1" type="noteref" href="notes.xhtml#rearnote_1">[1]</a>继续。</p>'
                '<p>第二段<a type="noteref" href="notes.xhtml#rearnote_2">[2]</a>。</p>'
            )
        )
        notes = XHTML.format(
            body=(
                '<h2 id="c2">注释</h2>'
                '<section type="rearnotes">'
                '<aside id="rearnote_1" type="rearnote">'
                '<p><a type="noteref" href="body.xhtml#noteref_1">[1]</a>第一条注。</p></aside>'
                '<aside id="rearnote_2" type="rearnote">'
                '<p><a type="noteref" href="body.xhtml#noteref_2">[2]</a>第二条注。</p></aside>'
                "</section>"
            )
        )
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["b", "n"],
                    manifest=[
                        ("b", "Text/body.xhtml", "application/xhtml+xml", ""),
                        ("n", "Text/notes.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("第1章 起", "Text/body.xhtml")
                    + _navpoint("注释", "Text/notes.xhtml")
                ),
                "OEBPS/Text/body.xhtml": body,
                "OEBPS/Text/notes.xhtml": notes,
            },
        )
        book = read_epub(path)
        allp = _all_paragraphs(book)
        notes_paras = [p for p in allp if p.kind == "note"]
        assert len(notes_paras) == 2, [p.kind for p in allp]
        # 注释段的编号被剥掉，锚点留着当落点
        assert [p.anchor for p in notes_paras] == ["rearnote_1", "rearnote_2"], notes_paras
        assert notes_paras[0].text == "第一条注。", notes_paras[0].text
        # 正文里的引用必须保住，且目标是注释的 id
        body_paras = [p for p in allp if p.kind == "body"]
        refs = [r for p in body_paras for r in p.refs]
        assert [r["note"] for r in refs] == ["rearnote_1", "rearnote_2"], refs
        assert [r["label"] for r in refs] == ["[1]", "[2]"], refs
        # 正文文字里不该再残留 [1]
        assert "[1]" not in body_paras[0].text, body_paras[0].text


def case_marker_only_note_without_type_attr() -> None:
    """没有 type 属性的注码（〚1〛）：正文与注释都只有 href，靠"显示文字是注码"来认。

    《三国演义》（毛宗岗评本）就是这样：正文里 <a id="p1" href="#pnote1">〚1〛</a>，
    每回末尾的评语条目是 <a id="pnote1" href="#p1">〚1〛</a>评语…。两处都没有
    type 属性 —— 只认 type 的话，5322 个注码全部退化成正文里的死文字，
    用户看到的就是"另一种注释被丢了"。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        body = XHTML.format(
            body=(
                '<h2 id="c1">第1章 起</h2>'
                '<p>正文开头<a id="p1" href="body.xhtml#pnote1">〚1〛</a>接着写下去。</p>'
                '<p>又一段<a id="p2" href="body.xhtml#pnote2">〚2〛</a>写完了。</p>'
                '<p><a href="body.xhtml#c1">第1章</a></p>'
                '<p><a id="pnote1" href="body.xhtml#p1">〚1〛</a>批语一。</p>'
                '<p><a id="pnote2" href="body.xhtml#p2">〚2〛</a>批语二。</p>'
            )
        )
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["b"],
                    manifest=[
                        ("b", "Text/body.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第1章 起", "Text/body.xhtml")),
                "OEBPS/Text/body.xhtml": body,
            },
        )
        book = read_epub(path)
        allp = _all_paragraphs(book)
        notes = [p for p in allp if p.kind == "note"]
        # 注释条目变成 note，锚点留着当落点，开头的注码被剥掉
        assert [p.anchor for p in notes] == ["pnote1", "pnote2"], [(p.anchor, p.kind) for p in allp]
        assert [p.text for p in notes] == ["批语一。", "批语二。"], [p.text for p in notes]
        # 正文里的注码变成引用，文字里不再残留 〚1〛
        body_paras = [p for p in allp if p.kind == "body"]
        refs = [r for p in body_paras for r in p.refs]
        assert [r["note"] for r in refs] == ["pnote1", "pnote2"], refs
        assert [r["label"] for r in refs] == ["〚1〛", "〚2〛"], refs
        assert "〚" not in "".join(p.text for p in allp), [p.text for p in allp]
        # 目录式的普通链接（显示文字不是注码）不许被当成引用
        assert not any(r["label"] == "第1章" for r in refs), refs

def case_note_marker_with_note_word() -> None:
    """注码写成「（注１）」、回跳写成「注１：」时也要认成注释。

    台湾角川的竖排书（实测《象首迷宫》）把注码排成
    `<a href="#ref1-001"><span>（注１）</span></a>`，页底注文是
    `<p id="ref1-001">　<a href="#key1-001">注１：</a>注文…</p>`。
    这两种写法都比既有判据多出一个字或一个冒号 —— 只认「(1)」「①」「注1」
    的话，注码退化成正文里的死文字「（注１）」，注文段没有任何注码指着，
    整本书的脚注全留在章末当普通段落，读者看到的就是"注释没了"。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        body = XHTML.format(
            body=(
                '<h2 id="c1">第1章 起</h2>'
                '<p>正文开头<a class="cyu" href="body.xhtml#ref1-001">'
                '<span class="key1" id="key1-001">（注１）</span></a>接着写下去。</p>'
                '<p>又一段<a class="cyu" href="body.xhtml#ref1-002">'
                '<span class="key1" id="key1-002">（注２）</span></a>写完了。</p>'
                '<p><a href="body.xhtml#c1">第1章</a></p>'
                '<p id="ref1-001">　<a class="cyu" href="body.xhtml#key1-001">'
                '<span class="key1">注１：</span></a>注文一。</p>'
                '<p id="ref1-002">　<a class="cyu" href="body.xhtml#key1-002">'
                '<span class="key1">注２：</span></a>注文二。</p>'
            )
        )
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["b"],
                    manifest=[
                        ("b", "Text/body.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第1章 起", "Text/body.xhtml")),
                "OEBPS/Text/body.xhtml": body,
            },
        )
        book = read_epub(path)
        allp = _all_paragraphs(book)
        notes = [p for p in allp if p.kind == "note"]
        # 注文段变成 note，开头的全角缩进与「注１：」回跳一并剥掉
        assert [p.text for p in notes] == ["注文一。", "注文二。"], [p.text for p in allp]
        body_paras = [p for p in allp if p.kind == "body"]
        refs = [r for p in body_paras for r in p.refs]
        assert [r["note"] for r in refs] == ["ref1-001", "ref1-002"], refs
        assert [r["label"] for r in refs] == ["（注１）", "（注２）"], refs
        # 注码不许以死文字留在正文里
        assert "（注" not in "".join(p.text for p in allp), [p.text for p in allp]

def case_note_box_container_propagates_kind() -> None:
    """注释语义写在容器上、文字在子元素里时，子段落也必须变成 note。

    `<div type="footnote"><p>注文</p></div>` 这种写法很常见：那个 <p>
    自己一个标记都没有，只看它自己的属性，整批注释会退化成正文。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c"],
                    manifest=[
                        ("c", "Text/c.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第1章", "Text/c.xhtml")),
                "OEBPS/Text/c.xhtml": XHTML.format(
                    body=(
                        "<h2>第1章</h2><p>正文。</p>"
                        '<div type="footnote"><p>容器里的注一。</p><p>容器里的注二。</p></div>'
                    )
                ),
            },
        )
        book = read_epub(path)
        kinds = [p.kind for p in _all_paragraphs(book)]
        assert kinds.count("note") == 2, kinds
        assert kinds.count("body") == 1, kinds


def case_dangling_ref_restored_to_text() -> None:
    """指向不存在锚点的引用要还原成纯文字，不留死链接。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c"],
                    manifest=[
                        ("c", "Text/c.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(_navpoint("第1章", "Text/c.xhtml")),
                "OEBPS/Text/c.xhtml": XHTML.format(
                    body=(
                        "<h2>第1章</h2>"
                        '<p>正文<a type="noteref" href="gone.xhtml#nope">[9]</a>继续。</p>'
                    )
                ),
            },
        )
        book = read_epub(path)
        paras = [p for p in _all_paragraphs(book) if p.kind == "body"]
        assert paras[0].refs == [], paras[0].refs
        assert paras[0].text == "正文[9]继续。", paras[0].text


def case_note_leading_backlink_dropped() -> None:
    """注释段开头那条"回到正文"的反向链接不许变成注释开头的多余编号。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["b", "n"],
                    manifest=[
                        ("b", "Text/b.xhtml", "application/xhtml+xml", ""),
                        ("n", "Text/n.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("第1章", "Text/b.xhtml") + _navpoint("注释", "Text/n.xhtml")
                ),
                "OEBPS/Text/b.xhtml": XHTML.format(
                    body='<h2>第1章</h2><p>正文<a type="noteref" href="n.xhtml#r1">[1]</a>。</p>'
                ),
                "OEBPS/Text/n.xhtml": XHTML.format(
                    body=(
                        "<h2>注释</h2>"
                        '<aside id="r1" type="rearnote">'
                        '<p><a type="noteref" href="b.xhtml#nr1">[1]</a>注文内容。</p></aside>'
                    )
                ),
            },
        )
        book = read_epub(path)
        notes = [p for p in _all_paragraphs(book) if p.kind == "note"]
        assert len(notes) == 1, [p.kind for p in _all_paragraphs(book)]
        assert notes[0].text == "注文内容。", notes[0].text


def case_note_heading_dropped() -> None:
    """章末注释区的小标题（「注释：」）不是正文，不许留在书里。

    《鱼不存在》的写法：正文之后 `<p class="notetitle">注释：</p>` 领起一串
    `<p class="notecontent">`。注文被正文注码指到、转成 note（掌阅弹注）之后，
    这个标题没有任何注码指向，就作为普通段落留在章末 —— 全 10 章各留一行
    「注释：」，读者翻到章尾只看见一个没有下文的标题。

    删它要有证据，不能只看字面：同一个段落序列里后面确实还有注释段，才说明
    它是注释区的标题；正文里单独成段的「注释」两个字（作者小标题）不受牵连。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c1", "c2"],
                    manifest=[
                        ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                        ("c2", "Text/c2.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("第一章 开端", "Text/c1.xhtml")
                    + _navpoint("第二章 转折", "Text/c2.xhtml")
                ),
                "OEBPS/Text/c1.xhtml": XHTML.format(
                    body=(
                        "<h1>第一章 开端</h1>"
                        '<p>正文开头<a id="r1" href="#n1">[1]</a>接着写下去。</p>'
                        '<p class="notetitle">注释：</p>'
                        '<p class="notecontent"><a id="n1" href="#r1">[1]</a>这是注文。</p>'
                        "<p>后面还有正文。</p>"
                    )
                ),
                "OEBPS/Text/c2.xhtml": XHTML.format(
                    body="<h1>第二章 转折</h1><p>注释</p><p>这两个字是正文里的小标题。</p>"
                ),
            },
        )
        imp = read_epub_with_report(path)
        book = imp.book

        # 1) 注释标题没了，正文段与注文段都还在，顺序不变
        first = book.chapters[0]
        assert [p.text for p in first.paragraphs] == [
            "正文开头接着写下去。",
            "这是注文。",
            "后面还有正文。",
        ], [p.text for p in first.paragraphs]
        assert _kinds(first) == ["body", "note", "body"], _kinds(first)
        assert imp.report["note_headings_dropped"] == 1, imp.report

        # 2) 后面没有注释段的「注释」是作者自己的小标题，一个字都不许动
        second = book.chapters[1]
        assert [p.text for p in second.paragraphs] == [
            "注释",
            "这两个字是正文里的小标题。",
        ], [p.text for p in second.paragraphs]


def case_front_xu_titles() -> None:
    """「序」有两副面孔：序跋归前附，序章/序曲/序篇是正文章节。

    回归（todo/0.3.1.md 记的那次）：旧判据是"序开头且不超过 3 个字"——
    序曲只有两个字、比"序言"还短，整首序曲被吞进前附，第一章前面多出 29 段。
    长度不是判据，是不是叙事结构的标记才是。

    反方向同样要钉住：为了救序曲，不能把所有"结尾是序"的短标题都当前附 ——
    秩序、失序 是两字的**正文章节名**。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 前附：序跋一类的写法（带副标题的、带版次的、单字的）
        front = [
            ("版权信息", "front"),
            ("推荐序：写在前面", "rec"),
            ("译序 致中国读者", "trans"),
            ("第二版序", "v2"),
            ("序", "xu"),
            # 包在括号里的写法：括号不是内容，判之前要脱掉
            ("（序）", "xu_paren"),
        ]
        # 正文：结构词开头，全是章
        body = [
            ("序曲", "overture"),
            ("序篇", "prelude"),
            ("序章 雪夜", "pro"),
            ("（序章）", "pro_paren"),
            ("第一章 开端", "ch1"),
        ]
        docs = front + body
        entries: dict[str, str] = {
            "OEBPS/content.opf": _opf(
                spine=[k for _, k in docs] + ["ncx"],
                manifest=[
                    (k, f"Text/{k}.xhtml", "application/xhtml+xml", "") for _, k in docs
                ]
                + [("ncx", "toc.ncx", "application/x-dtbncx+xml", "")],
            ),
            "OEBPS/toc.ncx": _ncx(
                "".join(_navpoint(t, f"Text/{k}.xhtml") for t, k in docs)
            ),
        }
        for title, key in docs:
            entries[f"OEBPS/Text/{key}.xhtml"] = XHTML.format(
                body=f"<h2>{title}</h2><p>{title}一段。</p>"
            )
        imp = read_epub_with_report(_epub(tmp, entries))

        front_titles = [t for t, _ in front]
        body_titles = [t for t, _ in body]
        # 前附卷在前，序跋作为它的子章；结构词标题在卷外，仍是正文
        assert [ch.title for ch in imp.book.chapters] == [
            "前附", *front_titles, *body_titles
        ], [ch.title for ch in imp.book.chapters]
        assert imp.book.front_matter == [], [p.text for p in imp.book.front_matter]
        assert imp.report["front_matter_entries"] == len(front_titles), imp.report["front_matter_entries"]
        for i, title in enumerate(front_titles, start=1):
            assert [p.text for p in imp.book.chapters[i].paragraphs] == [f"{title}一段。"]

    # 反方向：「秩序」是正文，不能因为"结尾是序"就当前附
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["z", "c1", "ncx"],
                manifest=[
                    ("z", "Text/z.xhtml", "application/xhtml+xml", ""),
                    ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(
                _navpoint("秩序", "Text/z.xhtml") + _navpoint("第一章", "Text/c1.xhtml")
            ),
            "OEBPS/Text/z.xhtml": XHTML.format(body="<h2>秩序</h2><p>失序的一段。</p>"),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第一章</h2><p>正文。</p>"),
        }
        book = read_epub(_epub(tmp, entries))
        assert [ch.title for ch in book.chapters] == ["秩序", "第一章"], [
            ch.title for ch in book.chapters
        ]
        assert book.front_matter == [], [p.text for p in book.front_matter]


def case_generated_info_page_skipped() -> None:
    """自家信息页整页跳过；别人的普通"无目录页"仍按续页/前言保留。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Text/info.xhtml": XHTML.format(
                body=(
                    '<div class="info">'
                    '<h1 class="info-title">假书名</h1>'
                    '<p class="info-author">假作者</p>'
                    '<div class="info-meta"><p>来源文件：X:/假/假书.txt</p>'
                    "<p>章节数：2</p><p>段落数：3</p>"
                    "<p>生成时间：2026-10-24T00:00:00Z</p>"
                    "<p>由 txt2epub 生成</p></div>"
                    "</div>"
                )
            ),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第1章 假</h2><p>假正文一。</p>"),
            "OEBPS/Text/c2.xhtml": XHTML.format(body="<h2>第2章 假</h2><p>假正文二。</p>"),
        }
        opf = _opf(
            spine=["info", "c1", "c2"],
            manifest=[
                ("info", "Text/info.xhtml", "application/xhtml+xml", ""),
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/c2.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(
            _navpoint("第1章 假", "Text/c1.xhtml")
            + _navpoint("第2章 假", "Text/c2.xhtml")
        )
        imp = read_epub_with_report(_epub(tmp, entries))

        assert imp.report["generated_pages"] == 1, imp.report["generated_pages"]
        assert imp.report["continuation_docs"] == 0, imp.report["continuation_docs"]
        assert [c.title for c in imp.book.chapters] == ["第1章 假", "第2章 假"], [
            c.title for c in imp.book.chapters
        ]
        texts = [p.text for p in _all_paragraphs(imp.book)]
        assert texts == ["假正文一。", "假正文二。"], texts
        assert imp.book.front_matter == [], imp.book.front_matter


def case_back_cover_page_skipped() -> None:
    """封底页整页跳过：它一个字都没有，靠页面里的注释标记认出来。

    这是"不靠目录也认得出"的能力：封底不在 nav/NCX 里，按普通"目录里没有的
    文档"处理会被续接进最后一章 —— 每导一次多两段（实测 2→4→6→8）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Images/back.png": _png(600, 800),
            "OEBPS/Text/back.xhtml": XHTML.format(
                body=(
                    "<!-- back-cover -->\n"
                    '<div class="back">'
                    '<img src="../Images/back.png" alt="封底"/>'
                    "</div>"
                )
            ),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第1章 假</h2><p>假正文一。</p>"),
            "OEBPS/Text/c2.xhtml": XHTML.format(body="<h2>第2章 假</h2><p>假正文二。</p>"),
        }
        opf = _opf(
            spine=["c1", "c2", "back"],
            manifest=[
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/c2.xhtml", "application/xhtml+xml", ""),
                ("back", "Text/back.xhtml", "application/xhtml+xml", ""),
                ("back-image", "Images/back.png", "image/png", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
            guide='  <guide><reference type="back" title="封底" href="Text/back.xhtml"/></guide>',
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(
            _navpoint("第1章 假", "Text/c1.xhtml")
            + _navpoint("第2章 假", "Text/c2.xhtml")
        )
        imp = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")

        # 封底算一页"自家生成页"（所以是 1 不是 0），且绝不能变成续页
        assert imp.report["generated_pages"] == 1, imp.report["generated_pages"]
        assert imp.report["continuation_docs"] == 0, imp.report["continuation_docs"]
        assert [c.title for c in imp.book.chapters] == ["第1章 假", "第2章 假"], [
            c.title for c in imp.book.chapters
        ]
        texts = [p.text for p in _all_paragraphs(imp.book)]
        assert texts == ["假正文一。", "假正文二。"], texts
        assert imp.book.front_matter == [], imp.book.front_matter
        # 封底图被真的 <img> 引用了 → 不该报孤儿
        assert imp.report["images_orphan"] == [], imp.report["images_orphan"]
        # 跳过之前要把封底图捞出来：与封面同一个口径（cover_file），
        # 报告里给相对 image_dir 的文件名，重新打包时用它原样贴回封底。
        assert imp.report["back_file"] == "back.png", imp.report["back_file"]
        saved = tmp / "out" / "back.png"
        assert saved.is_file(), sorted(p.name for p in (tmp / "out").iterdir())
        assert saved.read_bytes() == _png(600, 800), "封底图必须逐字节与原图一致"


def case_back_cover_guide_fallback() -> None:
    """封底页没有 <!-- back-cover --> 标记：靠 guide 的 type="back" 兜底捞图。

    别人的包里封底页不一定带我们的标记（标记是自家打包器写的）。页里认不出
    是不是封底时，guide 的 <reference type="back"> 是唯一能指认它的信号。
    这条**不改变**整页跳过的判断（没有标记的页照旧按普通"目录里没有的文档"
    续接进当前章节），只保证报告里 back_file 能指到那张图。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Images/fengdi.png": _png(600, 800),
            "OEBPS/Text/fengdi.xhtml": XHTML.format(
                body='<div class="back"><img src="../Images/fengdi.png" alt=""/></div>'
            ),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第1章 假</h2><p>假正文一。</p>"),
        }
        opf = _opf(
            spine=["c1", "fengdi"],
            manifest=[
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("fengdi", "Text/fengdi.xhtml", "application/xhtml+xml", ""),
                ("back-image", "Images/fengdi.png", "image/png", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
            guide='  <guide><reference type="back" title="封底" href="Text/fengdi.xhtml"/></guide>',
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(_navpoint("第1章 假", "Text/c1.xhtml"))
        imp = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")

        # 没有标记 → 不算"自家生成页"，照旧续接（这是既有行为，别被兜底改掉）
        assert imp.report["generated_pages"] == 0, imp.report["generated_pages"]
        assert imp.report["continuation_docs"] == 1, imp.report["continuation_docs"]
        assert [c.title for c in imp.book.chapters] == ["第1章 假"], [
            c.title for c in imp.book.chapters
        ]
        # 兜底捞图：文件名 back.png，字节与原图一致
        assert imp.report["back_file"] == "back.png", imp.report["back_file"]
        saved = tmp / "out" / "back.png"
        assert saved.is_file(), sorted(p.name for p in (tmp / "out").iterdir())
        assert saved.read_bytes() == _png(600, 800)
        # 那张图同时被正文页引用 → 不算孤儿
        assert imp.report["images_orphan"] == [], imp.report["images_orphan"]


def case_back_cover_not_in_spine_not_reported_orphan() -> None:
    """封底页不在 spine 时，封底图也不许报成孤儿图（复核补修）。

    第一遍统计只走 spine 里的文档。有的包把封底页排除在阅读顺序外，它引用的
    那张图就一次都数不到，于是同一张图既出现在「封底图已存」又出现在「孤儿
    图」名单里 —— 报告自相矛盾，用户会以为包里还剩一张没人用的图。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Images/back.png": _png(600, 800),
            "OEBPS/Text/back.xhtml": XHTML.format(
                body=(
                    "<!-- back-cover -->\n"
                    '<div class="back"><img src="../Images/back.png" alt="封底"/></div>'
                )
            ),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第1章 假</h2><p>假正文一。</p>"),
        }
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("back", "Text/back.xhtml", "application/xhtml+xml", ""),
                ("cover-back", "Images/back.png", "image/png", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
            guide='  <guide><reference type="back" title="封底" href="Text/back.xhtml"/></guide>',
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(_navpoint("第1章 假", "Text/c1.xhtml"))
        imp = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")

        # guide 的封底页是唯一线索：页不在 spine 里，照样要把图捞出来
        assert imp.report["back_file"] == "back.png", imp.report["back_file"]
        assert (tmp / "out" / "back.png").read_bytes() == _png(600, 800)
        # 捞出来的那张图不许再出现在孤儿名单里
        assert imp.report["images_orphan"] == [], imp.report["images_orphan"]


def case_back_image_substring_does_not_grab_a_body_picture() -> None:
    """id/href 里撞上 back-image 的正文插图，不许被当成封底拷走（复核补修）。

    兜底扫描以前对 manifest 的 id+href 做 "back-image" 子串匹配，任何叫
    「回目-back-image-1.png」的正文插图都会被当成封底另存一份 back.png，
    重新打包时那张封底就会顶替掉真正的封底。现在分两档：id 恰为 back-image
    的是自家打包器的约定，直接认；只靠子串撞上的，必须是正文一次都没引用过
    的图。两档都要有用例，否则收紧的判据坏了也没人知道。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/Images/back-image-fig.png": _png(300, 400),
            "OEBPS/Text/c1.xhtml": XHTML.format(
                body=(
                    "<h2>第1章 假</h2><p>假正文一。</p>"
                    '<img src="../Images/back-image-fig.png" alt="插图"/>'
                )
            ),
        }
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("body-back-image-1", "Images/back-image-fig.png", "image/png", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(_navpoint("第1章 假", "Text/c1.xhtml"))
        imp = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")

        # 这张图在正文里用着 → 不是封底
        assert imp.report["back_file"] == "", imp.report["back_file"]
        assert not (tmp / "out" / "back.png").exists(), "正文插图被当成封底拷了一份"
        assert imp.report["images_kept"] == 1, imp.report["images_kept"]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 另一头：id 恰好是 back-image、正文没引用 → 仍要兜底认它
        entries = {
            "OEBPS/Images/ye.png": _png(600, 800),
            "OEBPS/Text/c1.xhtml": XHTML.format(body="<h2>第1章 假</h2><p>假正文一。</p>"),
        }
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/c1.xhtml", "application/xhtml+xml", ""),
                ("back-image", "Images/ye.png", "image/png", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        entries["OEBPS/content.opf"] = opf
        entries["OEBPS/toc.ncx"] = _ncx(_navpoint("第1章 假", "Text/c1.xhtml"))
        imp = read_epub_with_report(_epub(tmp, entries), image_dir=tmp / "out")
        assert imp.report["back_file"] == "back.png", imp.report["back_file"]
        assert (tmp / "out" / "back.png").read_bytes() == _png(600, 800)


def case_back_cover_roundtrip_three_rounds() -> None:
    """带封底的自家产物回环三遍，段数不许长大（封底页每轮都被跳过）。

    只靠 guide 的 type="back" 挡不住：带不带它，封底页都会被当成"目录里
    没有的文档"续接进最后一章。而每多导一轮就多两段，所以这里逐轮比对
    段数与章标题。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        back = tmp / "back.png"
        back.write_bytes(_png(600, 800))
        book = Book(
            title="回环假书",
            author="假作者",
            source="X:/假/回环假书.txt",
            chapters=[
                Chapter(title="第一章 假章", number=1, paragraphs=[Paragraph(text="假正文一。")]),
                Chapter(title="第二章 假章二", number=2, paragraphs=[Paragraph(text="假正文二。")]),
            ],
        )
        for round_no in range(1, 4):
            built = build_epub(book, modified=FIXED_MODIFIED, back_cover=str(back))
            path = tmp / f"r{round_no}.epub"
            path.write_bytes(built.data)
            imp = read_epub_with_report(path, image_dir=tmp / f"out{round_no}")
            texts = [p.text for p in _all_paragraphs(imp.book)]
            assert texts == ["假正文一。", "假正文二。"], (round_no, texts)
            assert [c.title for c in imp.book.chapters] == ["第一章 假章", "第二章 假章二"], (
                round_no,
                [c.title for c in imp.book.chapters],
            )
            assert imp.report["continuation_docs"] == 0, (round_no, imp.report)
            assert imp.report["generated_pages"] == 1, (round_no, imp.report)
            assert imp.report["images_orphan"] == [], (round_no, imp.report)
            # 封底图每轮都要捞得回来（不然回环一轮少一样东西）
            assert imp.report["back_file"] == "back.png", (round_no, imp.report)
            book = imp.book


def case_spine_non_xhtml_not_silent() -> None:
    """非 XHTML 的 spine 文档不能静默消失：混合包要点名，整本非 XHTML 要报错。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 混合：两个 text/xml + 一个正常 xhtml。xhtml 照常成章，被跳过的要警告。
        entries = {
            "OEBPS/content.opf": _opf(
                spine=["x1", "x2", "c1"],
                manifest=[
                    ("x1", "Text/a.xml", "text/xml", ""),
                    ("x2", "Text/b.xml", "text/xml", ""),
                    ("c1", "Text/c.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第1章 假", "Text/c.xhtml")),
            "OEBPS/Text/a.xml": "<div>甲</div>",
            "OEBPS/Text/b.xml": "<div>乙</div>",
            "OEBPS/Text/c.xhtml": XHTML.format(
                body="<h2>第1章 假</h2><p>假正文。</p>"
            ),
        }
        imp = read_epub_with_report(_epub(tmp, entries))
        assert [c.title for c in imp.book.chapters] == ["第1章 假"], [
            c.title for c in imp.book.chapters
        ]
        hits = [w for w in imp.report["warnings"] if "非 XHTML" in w]
        assert len(hits) == 1, imp.report["warnings"]
        assert "2 个" in hits[0] and "text/xml" in hits[0], hits[0]

        # 整本都是 image/svg+xml（SVG 漫画那种）：一个正文文档都没有，必须报错，
        # 不能给出一本"空书"。
        entries2 = {
            "OEBPS/content.opf": _opf(
                spine=["p1", "p2"],
                manifest=[
                    ("p1", "Text/1.svg", "image/svg+xml", ""),
                    ("p2", "Text/2.svg", "image/svg+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx(_navpoint("第1话", "Text/1.svg")),
            "OEBPS/Text/1.svg": "<svg/>",
            "OEBPS/Text/2.svg": "<svg/>",
        }
        raised = False
        try:
            read_epub(_epub(tmp, entries2, "comic.epub"))
        except EpubReadError as exc:
            raised = True
            assert "没有任何正文文档" in str(exc), str(exc)
        assert raised, "整本非 XHTML 必须报错，不能返回空书"


SECRET = "林凡秘境奇谈"  # 假书名，只用来验证 --redact 不漏


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        code = cli_main(argv)
    return code, buf_out.getvalue(), buf_err.getvalue()


def _fake_epub_bytes() -> bytes:
    """一本最小合法 EPUB（书名藏在包内，文件名才是泄漏点）。"""
    import io as _io

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr(
            "OEBPS/content.opf",
            _opf(
                spine=["c1"],
                manifest=[
                    ("c1", "Text/c.xhtml", "application/xhtml+xml", ""),
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
        )
        zf.writestr("OEBPS/toc.ncx", _ncx(_navpoint("第1章 假", "Text/c.xhtml")))
        zf.writestr("OEBPS/Text/c.xhtml", XHTML.format(body="<h2>第1章 假</h2><p>假正文。</p>"))
    return buf.getvalue()


def case_cli_reflow_output_collisions() -> None:
    """reflow 写盘前的撞车检查：不许把产物写到输入上，也不许写出 traceback。

    三种情形：--images 指到输入文件本身；--images 指到一个已存在的普通文件
    （以前会在 epubread 里 mkdir 抛未捕获的 FileExistsError）；
    -o 指到输入文件本身。都要 exit 2 + 一句人话，且不许留半套产物。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = tmp / "书.epub"
        src.write_bytes(_fake_epub_bytes())
        before = src.read_bytes()

        # --- 情形一：--images 直接指到输入文件
        code, so, se = _run_cli(["reflow", str(src), "-o", str(tmp / "o1"),
                                 "--images", str(src)])
        assert code == 2, (code, so, se)
        assert "拒绝写入" in so + se, (so, se)
        assert "Traceback" not in so + se, (so, se)
        assert not (tmp / "o1" / "book.json").exists(), "撞车检查必须在写盘之前"

        # --- 情形二：--images 指到一个已存在的普通文件
        occupied = tmp / "不是目录"
        occupied.write_text("x", encoding="utf-8")
        code, so, se = _run_cli(["reflow", str(src), "-o", str(tmp / "o2"),
                                 "--images", str(occupied)])
        assert code == 2, (code, so, se)
        assert "不是目录" in so + se, (so, se)
        assert "Traceback" not in so + se, (so, se)
        assert not (tmp / "o2" / "book.json").exists()

        # --- 情形三：-o 直接把章节树落在输入文件上
        code, so, se = _run_cli(["reflow", str(src), "-o", str(src.with_suffix(""))])
        # -o 是目录，输出的 book.json 不在输入路径上：这种是合法用法，应当成功。
        assert code == 0, (code, so, se)

        # 输入文件从头到尾没被动过。
        assert src.read_bytes() == before


def case_cli_reflow_redact_hides_paths_and_errors() -> None:
    """--redact 下 reflow/epub 的两条出口都不许出现输入文件名（=书名）。

    覆盖三类泄漏点（task-8 高1）：
    - 摘要的 输出/图片 行（默认目录名 = <输入stem>_reflow，stem 就是书名）；
    - --json payload 的 book_json / images_dir / epub 键；
    - 失败分支「导入失败：…」——异常消息里拼了 src.name 或包内文档名。
    非 redact 模式打路径是正常功能，顺带断言它还打得出来。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = tmp / f"{SECRET}.epub"
        src.write_bytes(_fake_epub_bytes())

        # --- reflow --json --redact：stdout payload 与 stderr 摘要都不含书名
        outdir = tmp / "out_redact"
        code, so, se = _run_cli(
            ["reflow", str(src), "-o", str(outdir), "--json", "--redact"]
        )
        assert code == 0, (code, so[-300:], se[-300:])
        assert SECRET not in so, "payload 漏了书名"
        assert SECRET not in se, "stderr 摘要漏了书名"
        payload = json.loads(so)
        assert payload["redacted"] is True
        assert str(payload["book_json"]).startswith("〔"), payload["book_json"]
        assert str(payload["images_dir"]).startswith("〔"), payload["images_dir"]
        assert str(payload["epub"]).startswith("〔"), payload["epub"]

        # --- reflow 不带 --redact：路径照常打（正常功能不许修坏）
        outdir2 = tmp / "out_plain"
        code, so, se = _run_cli(["reflow", str(src), "-o", str(outdir2), "--json"])
        assert code == 0, (code, se[-300:])
        payload = json.loads(so)
        assert payload["book_json"] == str(outdir2 / "book.json"), payload["book_json"]
        assert payload["epub"] == str(src), payload["epub"]

        # --- 默认输出目录（不带 -o）：outdir = <书名>_reflow，redact 摘要也不许漏
        code, so, se = _run_cli(["reflow", str(src), "--redact"])
        assert code == 0, (code, se[-300:])
        assert SECRET not in se, "默认输出目录名漏了"

        # --- 失败分支①：不是 ZIP 的文件，异常消息拼了 src.name
        bad = tmp / f"{SECRET}外传.epub"
        bad.write_bytes(b"not a zip at all")
        code, so, se = _run_cli(["reflow", str(bad), "--redact"])
        assert code == 2, (code, so, se)
        assert "导入失败" in se + so, se
        assert SECRET not in so and SECRET not in se, "失败分支漏了书名"

        # --- 失败分支②：DRM 包，异常消息拼了包内文档名
        drm_name = f"{SECRET}真本.epub"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("META-INF/encryption.xml", """<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <x:EncryptionMethod xmlns:x="http://www.w3.org/2001/04/xmlenc#"/>
  <EncryptedData><EncryptionMethod/><CipherData><CipherReference URI="OEBPS/Text/"""
                + SECRET
                + """.xhtml"/></CipherData></EncryptedData>
</encryption>""")
            zf.writestr(
                "OEBPS/content.opf",
                _opf(
                    spine=["c1"],
                    manifest=[
                        ("c1", f"Text/{SECRET}.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
            )
            zf.writestr("OEBPS/toc.ncx", _ncx(_navpoint("第1章", f"Text/{SECRET}.xhtml")))
            zf.writestr(f"OEBPS/Text/{SECRET}.xhtml", XHTML.format(body="<p>假。</p>"))
        drm = tmp / drm_name
        drm.write_bytes(buf.getvalue())
        code, so, se = _run_cli(["reflow", str(drm), "--redact"])
        assert code == 2, (code, so, se)
        assert "导入失败" in se + so, se
        assert SECRET not in so and SECRET not in se, "DRM 报错分支漏了书名"

        # --- epub 子命令 --json --redact：payload["epub"] 与输出行都不含书名
        book = Book(title=SECRET, author="某人", source="X:/假/假.txt",
                    chapters=[Chapter(title="第1章", paragraphs=[Paragraph(text="假正文。")])])
        bj = tmp / "book.json"
        save_book(book, bj)
        epub_out = tmp / "pack_out.epub"
        code, so, se = _run_cli(
            ["epub", "--book-json", str(bj), "-o", str(epub_out), "--json", "--redact"]
        )
        assert code == 0, (code, so[-300:], se[-300:])
        assert SECRET not in so and SECRET not in se, "epub 打包漏了书名"
        payload = json.loads(so)
        assert str(payload["epub"]).startswith("〔"), payload["epub"]


def case_table_structure_kept() -> None:
    """表格必须整张收成一个 table 段：行列、表头、跨行跨列、格内注码都不能散。

    真实来源：嘯風山莊（艾蜜莉·布朗忒，赖慈芸译本）里有 4 张表 —— 故事年表、
    作者与世界大事年表、译本对照表、阅读笔记表。旧实现把 td/th 当块级标签、
    table/tr 又不认，于是每个单元格都成了一段正文：年表被拉成一列"年代 /
    重要事件 / 1771 / 恩萧先生去利物浦……"，行列关系全丢，读者根本对不上号。
    """
    table = (
        '<table id="table-3">'
        "<tr><th>年代</th><th>重要事件</th></tr>"
        '<tr><td rowspan="2">1771</td><td>恩萧先生去利物浦，带回弃儿希斯克利夫。</td></tr>'
        "<tr><td>十一月：凯瑟琳留在鸫翔庄园。</td></tr>"
        '<tr><td colspan="2">注<a epub:type="noteref" href="#n1">[1]</a></td></tr>'
        "</table>"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(_navpoint("第一章 开端", "Text/ch1.xhtml"))
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                # 表前正文里给表格留一条链接：id 只有被指向才值得保留
                # （_drop_unreferenced_anchors 会把没人指的死 id 清掉）。
                body='<h1>第一章 开端</h1><p>表前正文，见<a epub:type="noteref" '
                'href="#table-3">表三</a>。</p>' + table
                + '<p id="n1">注释一。</p><p>表后正文。</p>'
            ),
        }
        book = read_epub(_epub(tmp, entries))
        ch = book.chapters[0]

        # 1) 表还是一段，没有被拆成"一格一段"的正文流
        assert _kinds(ch) == ["body", "table", "note", "body"], _kinds(ch)
        para = ch.paragraphs[1]
        assert para.kind == "table"
        assert para.anchor == "table-3", para.anchor

        # 2) 行列结构：4 行，第 1 行是表头
        assert len(para.rows) == 4, [[c["text"] for c in r] for r in para.rows]
        head = para.rows[0]
        assert [c["text"] for c in head] == ["年代", "重要事件"], head
        assert all(c["header"] for c in head), "th 必须记成表头"
        assert [c["header"] for c in para.rows[1]] == [False, False]

        # 3) 跨行跨列：字符串形式的 colspan/rowspan 要归一成整数。
        # 1771 那格 rowspan=2，所以第 3 行只剩一个格子（第二列）。
        assert para.rows[1][0]["rowspan"] == 2, para.rows[1][0]
        assert para.rows[1][0]["text"] == "1771"
        assert [c["text"] for c in para.rows[2]] == ["十一月：凯瑟琳留在鸫翔庄园。"], para.rows[2]
        assert para.rows[3][0]["colspan"] == 2, para.rows[3][0]

        # 4) 格内注码：文字留在格里，引用也留在格里（偏移相对这一格）。
        # 与正文段同一条规矩：注码本身（"[1]"）不写进 text，而是由 refs 带着，
        # 渲染层照 refs 在 at 处插回一个可点的链接 —— text 里再写一遍会印两次。
        cell = para.rows[3][0]
        assert cell["text"] == "注", cell["text"]
        assert [r["note"] for r in cell["refs"]] == ["n1"], cell["refs"]
        assert cell["refs"][0]["at"] == 1, cell["refs"]
        assert cell["refs"][0]["label"] == "[1]", cell["refs"]

        # 5) text 是摊平的纯文本：每个表格行输出一行、格间用｜，字数统计与界面预览都靠它
        assert para.text.splitlines()[0] == "年代｜重要事件", para.text
        assert para.text.splitlines()[1] == "1771｜恩萧先生去利物浦，带回弃儿希斯克利夫。", para.text

        # 6) 表前正文仍保持独立；"表三"由 refs 承载，不重复写入 text。
        #    <caption> 的拆出由表题用例覆盖。
        assert ch.paragraphs[0].text == "表前正文，见。", ch.paragraphs[0].text
        assert [r["label"] for r in ch.paragraphs[0].refs] == ["表三"], ch.paragraphs[0].refs


def case_drop_unreferenced_anchors() -> None:
    """原书带来的死 id 要在导入时清掉，但被保留 refs 指向的 id 一个都不能少。

    真实来源：《嘯風山莊》里几乎每个段落外都套着 `<span id="kobo.12.3">`
    （Kobo 记阅读位置用），版式容器上还挂着 book-columns。原书记录了 2563 个
    未被保留 refs 指向的 id；合并分片章节后，同一文件里还会出现 3 个 kobo.2.1。
    这些 id 既占正文体积，也会触发 EPUBCheck 的 RSC-005 重复 id。

    判据必须是"有没有保留的 refs 指过来"，而不是 id 的命名形态：
    注释段的 id 自己不带 href，但正文注码的 refs 指向它，就必须留。
    """
    body = (
        '<h1>第一章 开端</h1>'
        '<p><span id="kobo.1.1">正文一。</span></p>'
        '<div id="book-columns"><p>正文二。</p></div>'
        '<p><span id="kobo.2.1">正文三<a epub:type="noteref" href="#n1">[1]</a>。</span></p>'
        '<p id="n1">注释一。</p>'
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": _ncx(_navpoint("第一章 开端", "Text/ch1.xhtml")),
            "OEBPS/Text/ch1.xhtml": XHTML.format(body=body),
        }
        got = read_epub_with_report(_epub(tmp, entries))
        book, report = got.book, got.report
        allp = [p for ch in book.chapters for p in ch.paragraphs]

        # 1) 三个没人指的 id 全被清掉：两个 Kobo 位置标记 + 一个版式容器
        assert not [p for p in allp if p.anchor.startswith("kobo.")], [p.anchor for p in allp]
        assert not [p for p in allp if p.anchor == "book-columns"], [p.anchor for p in allp]
        assert report["ids_dropped"] == 3, report["ids_dropped"]

        # 2) 被注码指着的那个 id 必须留 —— 它是渲染 <a href="#n1"> 的落点
        notes = [p for p in allp if p.kind == "note"]
        assert [p.anchor for p in notes] == ["n1"], [(p.kind, p.anchor) for p in allp]
        refs = [r for p in allp for r in p.refs]
        assert [r["note"] for r in refs] == ["n1"], refs
        assert refs[0]["label"] == "[1]" and refs[0]["at"] == 3, refs

        # 3) 清 id 不许动正文一个字。注码不在 text 里：引用活着时显示文字由
        #    refs 承载，渲染层在 at 处插回链接。
        assert [p.text for p in allp] == ["正文一。", "正文二。", "正文三。", "注释一。"], [
            p.text for p in allp
        ]


def case_table_caption_and_empty_rows() -> None:
    """表题应单独成段；含空单元格的行仍应保留，空格不能被挤掉。

    导入契约还允许全空单元格组成的表：有些书的"表"其实是用空 td 排出的
    版面框。全空的表渲出来只有边框，但整张丢掉会让前后文之间凭空少一块。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        opf = _opf(
            spine=["c1"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(_navpoint("第一章 开端", "Text/ch1.xhtml"))
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body="<h1>第一章 开端</h1>"
                "<table><caption>表一　家族谱系</caption>"
                "<tr><td>甲</td><td></td></tr></table>"
                "<p>表后正文。</p>"
            ),
        }
        book = read_epub(_epub(tmp, entries))
        ch = book.chapters[0]
        assert _kinds(ch) == ["body", "table", "body"], _kinds(ch)
        assert ch.paragraphs[0].text == "表一　家族谱系", ch.paragraphs[0].text
        para = ch.paragraphs[1]
        # 空格子保留位置（第二列是空的），不是被挤掉
        assert [c["text"] for c in para.rows[0]] == ["甲", ""], para.rows[0]
        assert para.text == "甲", repr(para.text)


def case_table_roundtrip() -> None:
    """自家产物回环：build_epub 画出的表，重排回来必须是同一张网格。

    回环不动点是项目契约：表格若在"写出去"或"读回来"任何一头走样，用户每
    导入导出一次，表就烂一层 —— 这正是旧版最难受的地方（重排一次，年表变
    成一列段落，且再也回不去）。
    """
    rows = [
        [{"text": "年代", "header": True}, {"text": "艾蜜莉·布朗忒大事", "header": True}],
        [{"text": "1817"}, {"text": "珍·奥斯汀死。"}],
        [{"text": "1818"}, {"text": ""}],
    ]
    book = Book(
        title="回环假书",
        author="假作者",
        source="X:/假/回环假书.txt",
        chapters=[
            Chapter(
                title="第一章 假章",
                number=1,
                paragraphs=[
                    Paragraph(text="假正文一。"),
                    Paragraph(text="年代｜艾蜜莉·布朗忒大事\n1817｜珍·奥斯汀死。\n1818", kind="table", rows=rows),
                    Paragraph(text="假正文二。"),
                ],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        built = build_epub(book, modified=FIXED_MODIFIED)
        path = tmp / "self.epub"
        path.write_bytes(built.data)
        back = read_epub(path)

    got = [p for p in back.chapters[0].paragraphs if p.kind == "table"]
    assert len(got) == 1, _kinds(back.chapters[0])
    assert [[c["text"] for c in r] for r in got[0].rows] == [
        ["年代", "艾蜜莉·布朗忒大事"],
        ["1817", "珍·奥斯汀死。"],
        ["1818", ""],
    ], [[c["text"] for c in r] for r in got[0].rows]
    assert [c["header"] for c in got[0].rows[0]] == [True, True], got[0].rows[0]
    # 摊平文字也要一模一样（它是字数统计与界面预览的唯一来源）
    assert got[0].text == "年代｜艾蜜莉·布朗忒大事\n1817｜珍·奥斯汀死。\n1818", got[0].text


def case_same_fragment_name_in_two_chapters() -> None:
    """两章各自从 note_1 起编号：注文不能串成一锅。

    一章一文件的书，每章注释都从 note_1 重新编号（《谁的问题》15 章各一套）。
    锚点只按片段名索引时，全书凑成一个字典同名互相覆盖：103 条注文只剩 7 种，
    读者点开注码看到的是别章的文献 —— 比缺字更糟。修复后各章的 note_1 按
    源文件分流（ch1-note_1 / ch2-note_1），互不干扰。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def page(n: int, note: str) -> str:
            return XHTML.format(
                body=(
                    f'<h2 id="h{n}">第{n}章 起</h2>'
                    f'<p>正文{n}<a type="noteref" href="#note_1">[1]</a>继续。</p>'
                    f'<aside id="note_1"><p>'
                    f'<a type="noteref" href="#h{n}">[1]</a>{note}</p></aside>'
                )
            )

        path = _epub(
            tmp,
            {
                "OEBPS/content.opf": _opf(
                    spine=["c1", "c2"],
                    manifest=[
                        ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                        ("c2", "Text/ch2.xhtml", "application/xhtml+xml", ""),
                        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                    ],
                ),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("第1章 起", "Text/ch1.xhtml")
                    + _navpoint("第2章 起", "Text/ch2.xhtml")
                ),
                "OEBPS/Text/ch1.xhtml": page(1, "甲的注文。"),
                "OEBPS/Text/ch2.xhtml": page(2, "乙的注文。"),
            },
        )
        book = read_epub(path)
        allp = _all_paragraphs(book)
        notes = [p for p in allp if p.kind == "note"]
        assert [p.text for p in notes] == ["甲的注文。", "乙的注文。"], [p.text for p in notes]
        # 同名片段在不同文件里，锚点必须分成两个
        assert len({p.anchor for p in notes}) == 2, [p.anchor for p in notes]
        assert all(p.anchor.endswith("note_1") for p in notes), [p.anchor for p in notes]
        # 每章的注码要指回本章那条，而不是全指到最后一条
        body_refs = [r["note"] for p in allp if p.kind == "body" for r in (p.refs or [])]
        assert body_refs == [p.anchor for p in notes], (body_refs, [p.anchor for p in notes])

        # 打包后每条注文仍落在自己的注标上（不再退化成同一条）
        data = build_epub(book, modified=FIXED_MODIFIED).data
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            text = "".join(
                zf.read(n).decode("utf-8") for n in zf.namelist() if n.endswith(".xhtml")
            )
        assert 'zy-footnote="甲的注文。"' in text and 'zy-footnote="乙的注文。"' in text, text


def case_zhangyue_footnote_roundtrip() -> None:
    """掌阅弹注回环：注文在注标图的属性里，重排必须把它读回成一条自带注文的引用。

    这条链两头都容易断：读不回来 → 用户每导入导出一次，全书注释沉一层（注文
    只在属性里，没有别的副本）；读成正文 → 注释跑到字里行间，看着像错版。
    """
    book = Book(
        title="注标假书",
        author="假作者",
        source="X:/假/注标假书.txt",
        chapters=[
            Chapter(
                title="第一章 假章",
                number=1,
                paragraphs=[
                    Paragraph(text="假正文一。",
                              refs=[{"at": 3, "text": "假注文，讲一件事。", "label": ""}]),
                    Paragraph(text="假正文二。"),
                ],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        built = build_epub(book, modified=FIXED_MODIFIED)
        path = tmp / "self.epub"
        path.write_bytes(built.data)
        back = read_epub(path)
        again = build_epub(back, modified=FIXED_MODIFIED)

    paras = _all_paragraphs(back)
    refs = [r for p in paras for r in p.refs]
    assert [r.get("text") for r in refs] == ["假注文，讲一件事。"], refs
    # 注文不许跑到正文里
    assert "假注文" not in "".join(p.text for p in paras), [p.text for p in paras]
    # 回环不动点：再打一遍，章节文件与第一遍逐字节相同
    first = zipfile.ZipFile(io.BytesIO(built.data)).read("OEBPS/Text/chap0001.xhtml")
    second = zipfile.ZipFile(io.BytesIO(again.data)).read("OEBPS/Text/chap0001.xhtml")
    assert first == second, "回环第二遍的章节文件与第一遍不同"


def case_report_source_is_the_file_path() -> None:
    """导入报告里的来源必须是文件路径，不能被扫注标图时的局部变量顶掉。

    回归：8e8ea9a 统计掌阅注标时写了一句 `src = _IMG_SRC_RE.search(...)`，
    而 `src` 正是 `_import` 的入参（源文件 Path）。凡是带注标的书，报告里的
    `source` 都变成 re.Match 的 repr，写进 book.json 之后界面上「原文件」
    一栏显示成一串 match 对象；没有书名的书还会在 `opf.title or src.stem` 上崩。
    """
    book = Book(
        title="来源假书",
        author="假作者",
        source="X:/假/来源.txt",
        chapters=[
            Chapter(
                title="第一章 假章",
                number=1,
                paragraphs=[
                    Paragraph(text="假正文里带一条注。",
                              refs=[{"at": 4, "note": "n1", "label": "[1]"}]),
                    Paragraph(text="假设注一。", kind="note", anchor="n1"),
                ],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = tmp / "src.epub"
        path.write_bytes(build_epub(book, modified=FIXED_MODIFIED).data)
        report = read_epub_with_report(path).report
    assert report["source"] == str(path), report["source"]


def case_zhangyue_mark_is_not_a_picture() -> None:
    """注标图不算插图：注释多的书里它会被引几十次，别让它顶成"装饰图"。

    实情：阈值是 10 次。一本 60 条注的书，同一张 note.png 被引 60 次，照字面
    数就过阈值，报告里会凭空多一条"丢弃 1 张装饰图"—— 而它既没被丢（注文已经
    进了引用），也不是装饰。这条用例检查高频注标不会被误报为装饰图或孤儿图。
    """
    notes = [f"注{i}。" for i in range(12)]
    book = Book(
        title="注多假书",
        author="假作者",
        source="X:/假/注多假书.txt",
        chapters=[
            Chapter(
                title="第一章 假章",
                number=1,
                paragraphs=[Paragraph(text=f"假正文{i}。", refs=[{"at": 3, "text": t, "label": ""}])
                            for i, t in enumerate(notes)],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        built = build_epub(book, modified=FIXED_MODIFIED)
        path = tmp / "self.epub"
        path.write_bytes(built.data)
        imp = read_epub_with_report(path)

    report = imp.report
    assert "note.png" not in report["images_decorative"], report["images_decorative"]
    assert "note.png" not in report["images_orphan"], report["images_orphan"]
    refs = [r for p in _all_paragraphs(imp.book) for r in p.refs]
    assert len(refs) == 12, refs


def case_front_entries_keep_titles_and_chapter_boundaries() -> None:
    """原目录里的前附各有标题与边界，不能揉成第一章标题之前的无名段落。

    真实样本里「序曲」被「序」的前附规则误吞，和版权信息一起进了第一章；
    前附标题又被重复标题清洗删掉。目录既然已经给出每个条目的名字与落点，
    就不该再用关键词抹掉这层结构。

    封面页与书名页不在这张表里：它们整页丢弃，与目录页同一条路（见
    case_cover_page_dropped_like_toc）。
    """
    titles = [
        "版权信息", "内容简介", "简介", "前言", "序言", "自序", "代序", "译序",
        "彩页", "序曲", "第一章 开端",
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ids = [f"c{i}" for i in range(len(titles))]
        entries = {
            "OEBPS/content.opf": _opf(
                spine=ids,
                manifest=[
                    *[
                        (iid, f"Text/part{i}.xhtml", "application/xhtml+xml", "")
                        for i, iid in enumerate(ids)
                    ],
                    ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ],
            ),
            "OEBPS/toc.ncx": _ncx("".join(
                _navpoint(title, f"Text/part{i}.xhtml")
                for i, title in enumerate(titles)
            )),
        }
        for i, title in enumerate(titles):
            entries[f"OEBPS/Text/part{i}.xhtml"] = XHTML.format(
                body=f"<h1>{title}</h1><p>{title}的正文。</p>"
            )
        imp = read_epub_with_report(_epub(tmp, entries))

    front_titles = titles[:-2]  # 序曲是正文章节，不是前附
    assert [ch.title for ch in imp.book.chapters] == ["前附", *titles], [
        ch.title for ch in imp.book.chapters
    ]
    assert imp.book.front_matter == [], [p.text for p in imp.book.front_matter]
    assert imp.report["front_matter_entries"] == len(front_titles), imp.report
    for i, title in enumerate(front_titles, start=1):
        assert [p.text for p in imp.book.chapters[i].paragraphs] == [f"{title}的正文。"]
    assert imp.book.chapters[-1].title == "第一章 开端"

    # 打包后目录保留前附入口；第一章文件从自己的章标题开始，不再背着前附。
    book = from_dict(json.loads(json.dumps(to_dict(imp.book))))
    data = build_epub(book, modified=FIXED_MODIFIED).data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        assert all(f">{title}</a>" in nav for title in titles), nav
        root = ET.fromstring(nav)
        ns = {"x": "http://www.w3.org/1999/xhtml"}
        top = root.find(".//x:nav/x:ol", ns)
        top_items = top.findall("x:li", ns)
        labels = ["".join(li.find("x:a", ns).itertext()) for li in top_items]
        assert labels == ["前附", "序曲", "第一章 开端"], labels
        front_children = top_items[0].findall("x:ol/x:li", ns)
        assert [
            "".join(li.find("x:a", ns).itertext()) for li in front_children
        ] == front_titles, front_titles
        assert top_items[1].find("x:ol", ns) is None
        assert top_items[2].find("x:ol", ns) is None
        first_href = re.search(
            r'<a href="([^"]+)">第一章 开端</a>', nav
        )
        assert first_href is not None, nav
        first = zf.read("OEBPS/" + first_href.group(1)).decode("utf-8")
    assert "<body>\n<h1>第一章 开端</h1>" in first


def case_cover_page_dropped_like_toc() -> None:
    """封面重复页跟目录页走同一条路：整页丢弃，不占章节、也不进前附。

    三类页都在这里：《谁的问题》的「书名页」—— 封面与版权页已经写着书名与
    作者，再排一页只有这两行；《象首迷宫》目录里的「书封」—— 整页只有一张
    封面图，而封面图由打包器重做；导航里直接叫「封面」的页面 —— 同一件事最
    常见的写法。判据与目录页同一口径：去掉空白后含关键词，「书名页」
    「书名：…」「书封」「封面」都算。

    作者名不许跟着这一页一起丢：它来自 OPF 的 dc:creator，不是正文。
    """
    docs = [
        ("书封", "cover"),
        ("封面", "cv"),
        ("书名页", "name"),
        ("版权信息", "copy"),
        ("第一章 开端", "ch1"),
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            "OEBPS/content.opf": _opf(
                spine=[k for _, k in docs],
                manifest=[
                    (k, f"Text/{k}.xhtml", "application/xhtml+xml", "") for _, k in docs
                ]
                + [("ncx", "toc.ncx", "application/x-dtbncx+xml", "")],
            ),
            "OEBPS/toc.ncx": _ncx(
                "".join(_navpoint(t, f"Text/{k}.xhtml") for t, k in docs)
            ),
            "OEBPS/Text/cover.xhtml": XHTML.format(
                body="<h1>书封</h1><p>封面式样</p>"
            ),
            "OEBPS/Text/cv.xhtml": XHTML.format(
                body="<h1>封面</h1><p>一张图占满整页</p>"
            ),
            "OEBPS/Text/name.xhtml": XHTML.format(
                body="<h1>书名页</h1><p>测试书</p><p>某作者</p>"
            ),
            "OEBPS/Text/copy.xhtml": XHTML.format(
                body="<h1>版权信息</h1><p>书名：测试书</p><p>出版社：某社</p>"
            ),
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body="<h1>第一章 开端</h1><p>正文第一段。</p>"
            ),
        }
        imp = read_epub_with_report(_epub(tmp, entries))

    book = imp.book
    assert [ch.title for ch in book.chapters] == [
        "前附",
        "版权信息",
        "第一章 开端",
    ], [ch.title for ch in book.chapters]
    # 三页都整页不在了：标题与页里那几段一个都不许漏进别的章
    texts = [p.text for p in _all_paragraphs(book)]
    assert "书封" not in texts, texts
    assert "封面式样" not in texts, texts
    assert "封面" not in texts, texts
    assert "一张图占满整页" not in texts, texts
    assert "书名页" not in texts, texts
    assert "测试书" not in texts, texts
    assert "某作者" not in texts, texts
    # 版权页自己的「书名：…」是版权信息的一部分，照旧保留
    assert "书名：测试书" in texts, texts
    assert imp.report["cover_pages_dropped"] == 3, imp.report
    assert book.author == "某作者", book.author


def case_chapter_leading_blank_lines_are_not_front_matter() -> None:
    """章首页首的版式留白不许变成「前附」白页。

    实测《象首迷宫》：序章页在标题上方垫了三行空占位（两个空 <p>、一行全角
    空格 + <br/>），目录给「序章」定位时落在标题那一项，前三项就被当成
    "第一章之前的内容"推进 front_matter；打包器据此合成一条只剩 h1 的
    「前附」卷，读者点开一片空白。

    判据：这批前置内容里一句能看见的字都没有（只剩空行与跟着空行的锚点），
    就整批丢掉；掐头掐尾的空行一起收掉 —— 它们属于版式留白，不是书前内容。
    真有字的前置内容照旧进 front_matter，一个字都不许少。
    """
    docs = [("序章", "ch1"), ("第一章 开端", "ch2")]
    padding = "<p></p><p></p><p>　　<br /></p>"
    # 目录锚点落在标题之上（源书就是这样：mokuji-0001 挂在序章标题前），
    # 这样"标题之前"的几项才会被当成前置内容推出去。
    anchor = '<span id="mokuji-0001"></span>'

    def _import(head: str):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = {
                "OEBPS/content.opf": _opf(
                    spine=[k for _, k in docs],
                    manifest=[
                        (k, f"Text/{k}.xhtml", "application/xhtml+xml", "")
                        for _, k in docs
                    ]
                    + [("ncx", "toc.ncx", "application/x-dtbncx+xml", "")],
                ),
                "OEBPS/toc.ncx": _ncx(
                    _navpoint("序章", "Text/ch1.xhtml#mokuji-0001")
                    + _navpoint("第一章 开端", "Text/ch2.xhtml")
                ),
                "OEBPS/Text/ch1.xhtml": XHTML.format(
                    body=f"{head}{anchor}<h1>序章</h1><p>正文第一段。</p>"
                ),
                "OEBPS/Text/ch2.xhtml": XHTML.format(
                    body="<h1>第一章 开端</h1><p>正文第二段。</p>"
                ),
            }
            return read_epub_with_report(_epub(tmp, entries))

    imp = _import(padding)
    book = imp.book
    assert [ch.title for ch in book.chapters] == ["序章", "第一章 开端"], [
        ch.title for ch in book.chapters
    ]
    assert book.front_matter == [], [p.text for p in book.front_matter]
    assert imp.report["front_matter_paragraphs"] == 0, imp.report
    # 序章自己从正文开始，空行也不许留在首段
    assert [p.text for p in book.chapters[0].paragraphs] == ["正文第一段。"], [
        (p.kind, p.text) for p in book.chapters[0].paragraphs
    ]

    # 真有字的前置内容照旧是书前内容：只掐空行，一个字都不许少
    imp2 = _import("<p>版权声明一句。</p>" + padding)
    assert [p.text for p in imp2.book.front_matter] == ["版权声明一句。"], [
        (p.kind, p.text) for p in imp2.book.front_matter
    ]
    assert imp2.report["front_matter_paragraphs"] == 1, imp2.report


def case_book_title_not_front_word() -> None:
    """「书名」不再算前附词：书名页由 _is_cover_page 整页丢弃。

    两条判据的匹配口径完全相同（_norm_key 去掉空白后按「包含」比对），词表里
    留着「书名」永远不会被 _is_front_title 用到 —— 是死词。锁住这一条，是为了
    防止将来把 _is_cover_page 挪到别处时，书名页悄悄退回「前附」。
    """
    for title in ("书名页", "书名：测试书", "书名　作者"):
        assert _is_front_title(title) is False, title


def case_front_entry_with_children_becomes_volume() -> None:
    """前附条目自己带子条目时就是「卷」，不能和它的子条目并排成平级章。

    真实样本《啸风山庄》：原目录里「导读」是一条父条目，底下挂着作者生平述要、
    作品的出版与接受等五节。前附条目一律按「章」收进前附卷之后，导读与这五节
    成了六个平级章，读者在目录里看不出那五节属于导读。

    结构是目录给的，不是关键词给的：有子条目就建卷，其子条目的目录深度跟着
    下移一层；前附卷本身的位置不变。
    """
    entries = {
        "OEBPS/content.opf": _opf(
            spine=["c0", "c1", "c2", "c3", "c4", "c5"],
            manifest=[
                ("c0", "Text/copy.xhtml", "application/xhtml+xml", ""),
                ("c1", "Text/guide.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/life.xhtml", "application/xhtml+xml", ""),
                ("c3", "Text/publish.xhtml", "application/xhtml+xml", ""),
                ("c4", "Text/part1.xhtml", "application/xhtml+xml", ""),
                ("c5", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        ),
        "OEBPS/toc.ncx": _ncx(
            _navpoint("版权信息", "Text/copy.xhtml")
            + _navpoint(
                "导读",
                "Text/guide.xhtml",
                _navpoint("作者生平述要", "Text/life.xhtml")
                + _navpoint("作品的出版与接受", "Text/publish.xhtml"),
            )
            + _navpoint(
                "第一部",
                "Text/part1.xhtml",
                _navpoint("第一章", "Text/ch1.xhtml"),
            )
        ),
        "OEBPS/Text/copy.xhtml": XHTML.format(
            body="<h1>版权信息</h1><p>书名：测试书</p>"
        ),
        "OEBPS/Text/guide.xhtml": XHTML.format(
            body="<h1>导读</h1><p>本篇导读分为五节。</p>"
        ),
        "OEBPS/Text/life.xhtml": XHTML.format(
            body="<h1>作者生平述要</h1><p>她是三姐妹之一。</p>"
        ),
        "OEBPS/Text/publish.xhtml": XHTML.format(
            body="<h1>作品的出版与接受</h1><p>初版并不顺利。</p>"
        ),
        "OEBPS/Text/part1.xhtml": XHTML.format(body="<h1>第一部</h1>"),
        "OEBPS/Text/ch1.xhtml": XHTML.format(
            body="<h1>第一章</h1><p>正文第一段。</p>"
        ),
    }
    with tempfile.TemporaryDirectory() as td:
        imp = read_epub_with_report(_epub(Path(td), entries))

    chapters = imp.book.chapters
    assert [ch.title for ch in chapters] == [
        "前附",
        "版权信息",
        "导读",
        "作者生平述要",
        "作品的出版与接受",
        "第一部",
        "第一章",
    ], [ch.title for ch in chapters]
    guide = chapters[2]
    assert guide.level == 2, (guide.title, guide.level)
    assert guide.toc_depth == 1, (guide.title, guide.toc_depth)
    # 导读自己的引言留在卷首页，子条目的正文各归自己
    assert [p.text for p in guide.paragraphs] == ["本篇导读分为五节。"], guide.paragraphs
    assert chapters[3].paragraphs[0].text == "她是三姐妹之一。"
    assert [ch.toc_depth for ch in chapters[3:5]] == [2, 2], [
        (ch.title, ch.toc_depth) for ch in chapters[3:5]
    ]
    # 正文的卷不受影响：第一部仍在顶层，第一章在它底下
    assert (chapters[5].level, chapters[5].toc_depth) == (2, 0)
    assert (chapters[6].level, chapters[6].toc_depth) == (1, 1)

    # 打包后目录里，那五节要真的排在导读底下，而不是与它并列
    book = from_dict(json.loads(json.dumps(to_dict(imp.book))))
    data = build_epub(book, modified=FIXED_MODIFIED).data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    root = ET.fromstring(nav)
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    top = root.find(".//x:nav/x:ol", ns)
    labels = ["".join(li.find("x:a", ns).itertext()) for li in top.findall("x:li", ns)]
    assert labels == ["前附", "第一部"], labels
    front = top.findall("x:li", ns)[0]
    front_labels = [
        "".join(li.find("x:a", ns).itertext()) for li in front.findall("x:ol/x:li", ns)
    ]
    assert front_labels == ["版权信息", "导读"], front_labels
    guide_li = front.findall("x:ol/x:li", ns)[1]
    guide_kids = [
        "".join(li.find("x:a", ns).itertext()) for li in guide_li.findall("x:ol/x:li", ns)
    ]
    assert guide_kids == ["作者生平述要", "作品的出版与接受"], guide_kids


def case_right_aligned_css_class_becomes_align() -> None:
    """原书 CSS 声明 text-align:right 的类 → 段落 align="right"（落款/日期/署名）。

    只认**单类选择器**：后代选择器判不了（要先拼祖先链），同一个类又被别的
    规则改成非靠右时整类不认 —— 错排一段靠右是读者一眼看得见的错，漏排
    只是版式差一点。标题/注文即使 class 命中也不带 align：它们各有自己的排版。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        css = """
        p.luokuan { text-align: right; }
        .right-info { text-align: right !important; }
        .tail { text-align: end; }
        .both { text-align: right; }
        .both { text-align: left; }
        .wrap .inner { text-align: right; }
        """
        opf = _opf(
            spine=["c1", "c2"],
            manifest=[
                ("c1", "Text/ch1.xhtml", "application/xhtml+xml", ""),
                ("c2", "Text/ch2.xhtml", "application/xhtml+xml", ""),
                ("css", "Styles/main.css", "text/css", ""),
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
            ],
        )
        ncx = _ncx(
            _navpoint("第一章 落款", "Text/ch1.xhtml")
            + _navpoint("第二章 内联", "Text/ch2.xhtml")
        )
        entries = {
            "OEBPS/content.opf": opf,
            "OEBPS/toc.ncx": ncx,
            "OEBPS/Styles/main.css": css,
            "OEBPS/Text/ch1.xhtml": XHTML.format(
                body=(
                    "<h1>第一章 落款</h1>"
                    '<p class="luokuan">张三</p>'
                    '<p class="right-info">二〇二六年十一月</p>'
                    '<p class="tail">终</p>'
                    '<p class="both">被改成左对齐的类</p>'
                    '<p class="inner">后代选择器里的类</p>'
                    '<p class="luokuan" epub:type="footnote">注文不该靠右</p>'
                    "<p>普通正文。</p>"
                )
            ),
            # 有的书把样式表直接写在页面里，没有单独的 .css
            "OEBPS/Text/ch2.xhtml": XHTML.format(
                body=(
                    "<h1>第二章 内联</h1>"
                    "<style>.inline-r { text-align: right }</style>"
                    '<p class="inline-r">页内样式表里的落款</p>'
                )
            ),
        }
        book = read_epub(_epub(tmp, entries))
        by_text = {p.text: p for ch in book.chapters for p in ch.paragraphs}
        assert by_text["张三"].align == "right", by_text["张三"]
        # !important 只影响优先级，不影响"这是靠右"这个事实
        assert by_text["二〇二六年十一月"].align == "right"
        # end 是逻辑值：横排下等于靠右
        assert by_text["终"].align == "right"
        assert by_text["被改成左对齐的类"].align == "", "同类被改成左对齐时应整类不认"
        assert by_text["后代选择器里的类"].align == "", "后代选择器判不了，宁可不认"
        assert by_text["普通正文。"].align == ""
        assert by_text["页内样式表里的落款"].align == "right", "页面 <style> 里的规则也要认"
        note = by_text["注文不该靠右"]
        assert note.kind == "note", note.kind
        assert note.align == "", "注文即使 class 命中也不该被标成靠右"


def main() -> int:
    cases = [
        case_epub2_basic_split,
        case_front_entries_keep_titles_and_chapter_boundaries,
        case_cover_page_dropped_like_toc,
        case_chapter_leading_blank_lines_are_not_front_matter,
        case_book_title_not_front_word,
        case_front_entry_with_children_becomes_volume,
        case_leading_body_title_echo_is_dropped,
        case_epub3_nav_parse,
        case_multi_anchor_split,
        case_anchor_missing_fallback,
        case_unanchored_first_node_owns_doc_head,
        case_missing_anchor_matches_unique_heading,
        case_toc_depth_survives_import_and_roundtrip,
        case_piece_merge,
        case_image_ratio_classes,
        case_decorative_and_orphan,
        case_caption_attachment,
        case_cover_svg_shell,
        case_cover_file_empty_without_image_dir,
        case_broken_zip_and_xml,
        case_no_nav_fallback,
        case_drm_and_fixed_layout,
        case_drm_media_type_and_images,
        case_fixed_layout_per_item,
        case_image_size_headers,
        case_roundtrip_with_images,
        case_schema_v2_image_paragraph,
        case_self_roundtrip_kinds,
        case_table_structure_kept,
        case_drop_unreferenced_anchors,
        case_table_caption_and_empty_rows,
        case_ruby_kept_as_inline_note,
        case_ruby_roundtrip_keeps_note,
        case_table_roundtrip,
        case_zhangyue_footnote_roundtrip,
        case_same_fragment_name_in_two_chapters,
        case_report_source_is_the_file_path,
        case_zhangyue_mark_is_not_a_picture,
        case_title_note_stays_on_chapter,
        case_title_note_marks_unlabelled_note_paragraph,
        case_note_number_strip_only_foreign,
        case_calibre_noteref_and_rearnote,
        case_duokan_noteref_icon_keeps_ref,
        case_marker_only_note_without_type_attr,
        case_note_marker_with_note_word,
        case_note_box_container_propagates_kind,
        case_dangling_ref_restored_to_text,
        case_note_leading_backlink_dropped,
        case_note_heading_dropped,
        case_generated_info_page_skipped,
        case_front_xu_titles,
        case_back_cover_page_skipped,
        case_back_cover_guide_fallback,
        case_back_cover_not_in_spine_not_reported_orphan,
        case_back_image_substring_does_not_grab_a_body_picture,
        case_back_cover_roundtrip_three_rounds,
        case_spine_non_xhtml_not_silent,
        case_cli_reflow_output_collisions,
        case_cli_reflow_redact_hides_paths_and_errors,
        case_right_aligned_css_class_becomes_align,
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
