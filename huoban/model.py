"""章节树 JSON（中间态）。

为什么需要这一层：清洗只负责"把脏 TXT 变成带段落的章节树"，
打包只负责"把章节树变成 EPUB / AZW3 / TXT"。两边只通过这个 schema 对话，
互不知道对方的规则细节。好处是：加一种输出格式不用回头碰清洗，
改一条清洗规则也不用碰渲染。没有这一层，两种输出就会各自长出一套清洗逻辑，
迟早不一致。

为什么用 dataclass 而不是裸 dict：
- 字段有类型，clean.py 里拼错字段名会当场报错，而不是被 JSON 静默吞掉；
- 序列化只集中在 to_dict / from_dict 两个函数，schema 迁移（version 字段）
  只需要改这两处，不用全项目搜索 json.dumps；
- 段落类型 kind 是渲染层唯一的排版依据（要不要缩进、要不要落空行、
  要不要包 <p>），裸 dict 里它很容易被写成随便什么字符串。

为什么不复用 diagnose.py 的 CHAPTER_PATTERNS 自己再写一套编号识别：
那会导致"体检说这是第 12 章、清洗出来是 11 章"这种两份实现打架的情况。
本模块的序号解析只做一件事 —— 从已经判定为标题的行里把数字抽出来，
交给 diagnose.cn_to_int() 转换，判"像不像标题"一律用 diagnose.match_chapter()。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .diagnose import cn_to_int, match_chapter, strip_frame

# schema 版本。字段增删、语义变化（例如 level 的含义改了）时必须 +1，
# 让 from_dict 能拒绝按旧语义读新文件，而不是读出一本错的书。
#
# v1 → v2（EPUB 导入 G6）：Paragraph 新增 caption / width / height 三个字段，
# 只在 kind="image" 的段落上出现。这是**纯增量**：v1 的章节树按 v2 读入后
# 一个字段都不会变（from_dict 接受 1 和 2 两个主版本），to_dict 也只对 image
# 段写新字段 —— 旧 v1 文件重新序列化后除 version 外逐字节不变（见 docs/gui-design.md 的「EPUB 导入」一节里的验收要求）。
#
# v2 → v3（EPUB 导入识别表格）：Paragraph 新增 rows 字段，只在 kind="table" 的
# 段落上出现，同时 PARAGRAPH_KINDS 多一个 "table"。同样是**纯增量**：v1/v2 的
# 章节树按 v3 读入后一个字段都不会变（from_dict 接受 1/2/3 三个主版本），
# to_dict 也只在 table 段写 rows —— 旧文件重新序列化后除 version 外逐字节不变。
#
# v3 → v4（注释改成掌阅弹注）：refs 的条目多一个可选的 text 字段，用来装"自带
# 注文的引用"—— 掌阅格式的书里注文只在注标的属性里，全文档没有注释段落可以
# 指向，文字只能跟引用待在一起。同样是**纯增量**：老树的引用没有 text，
# 写出来的键序与 v3 一模一样；from_dict 四个主版本都接受。
#
# v4 → v5（标题注）：Chapter 新增 refs 字段，装写在标题里的注码。同样是**纯增量**：
# 老树的章节没有这个键，读入后是空列表，重新序列化也不写它。
#
# v5 → v6（原目录层级）：Chapter 新增可选 toc_depth，记原 EPUB 的目录深度。
# 同样**纯增量**：旧树没有这个键，读入后是 None，仍按 level 推断卷章关系，
# 重新序列化也不写它。
#
# v6 → v7（保留注音）：Paragraph 新增 ruby 字段，装段内的注音区间。
# 同样**纯增量**：旧树没有这个键，读入后是空列表，重新序列化也不写它。
#
# v7 → v8（右对齐段落）：Paragraph 新增 align 字段，记原书 CSS 里
# `text-align: right` 的那一类段落（落款、日期、署名）。为什么单开一列而不新增
# 一个 kind：kind 是"这段是什么"（标题/正文/注文），align 是"这段怎么排"，
# 两者正交 —— 一段正文右对齐之后仍然是正文，字数统计、注标裁决、章节切分
# 都还得按正文对待。同样**纯增量**：旧树没有这个键，读入后是空串，
# 重新序列化也不写它。
SCHEMA_VERSION = "8"

# 段落类型。渲染层按这个决定排版，清洗层按这个标注，两边共用同一份取值。
# verse = 诗词/韵文（词曰、诗云、右调·XXX 之类）：本质是"引文"，
# 排版上要跟正文拉开距离 —— 居中、不缩进、换楷体、字号略小。
# image = 插图（EPUB 导入专用）：text 是**图片文件名**（不是正文），
# caption 是图说，width/height 是像素尺寸。渲染层把它排成 figure。
# table = 表格（EPUB 导入专用）：text 是**逐行摊平的纯文本**（每个表格行输出
# 一行，单元格用「｜」连接，行间用换行），rows 才是真正的二维网格。为什么两份都留：
# 渲染层要网格才画得出 <table>，而字数统计、字体子集、清洗报告、界面预览
# 都只认 text —— 让它们各自再去遍历一遍网格，等于把"表格怎么摊平"这件事
# 复制到四个地方，迟早长歪。
PARAGRAPH_KINDS = ("body", "heading", "blank", "note", "verse", "sep", "image", "table")

# 段落对齐方式（schema v8）：空串 = 不特殊处理（跟着 CSS 的 p 走，两端对齐 + 首行缩进），
# "right" = 靠右、不缩进。只做右对齐一种 —— 真实样本里需要单独标出来的只有落款、
# 日期、署名（原书写成 `p.right { text-align: right }`）；居中已有 heading/verse/sep
# 各自的样式，左对齐就是正文，都不必再开一列。
PARAGRAPH_ALIGNS = ("", "right")

# 图片文件名允许的扩展名（与 epub.py 的 _IMAGE_TYPES 对齐）。
# image 段的 text 必须满足：非空、纯文件名（不含 / \ 与 ..）、扩展名在此表内 ——
# 渲染层会把它直接拼成 ../Images/<name> 写进 xhtml，脏名字会变成路径穿越。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif")

# anchor / refs 里的 id 会被原样拼进 xhtml 的 id="..." 与 href="#..."：
# 只要带空白、引号、尖括号或 & 就能跳出属性、注入标签。用"白名单"而不是
# "黑名单"：合法 id 本来就只是字母数字加 - _ . :。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass
class Paragraph:
    """一个段落。

    为什么不直接把段落存成字符串：标题、空行、注释在渲染时待遇完全不同
    （标题要进目录、空行是场景分隔、注释可能要斜体或跳过），
    这些信息在"合并碎行"那一步就丢掉了，事后靠内容猜不回来。
    """

    text: str
    kind: str = "body"  # "body" | "heading" | "blank" | "note" | "verse" | "sep" | "image"
    # 以下三个字段只在 kind == "image" 时有意义（schema v2，EPUB 导入专用）：
    # - caption：图说（原书没有就是空串；不要自己编）；
    # - width / height：图片像素尺寸，读不出时都是 0（渲染层按方图兜底）。
    # 旧代码（清洗 TXT 那条线）永远不碰这三个字段，默认值让它们保持原样。
    caption: str = ""
    width: int = 0
    height: int = 0
    # 以下两个字段同样只在 EPUB 导入时出现（schema v2 的增量，纯可选）：
    # - anchor：本段在原书里的 HTML id。注释段靠它被正文里的引用指到；
    # - refs：本段正文里的**行内引用**，每条 {"at": 字符偏移, "note": 目标 id,
    #   "label": 显示文字（如 "[1]"）}。偏移是"插在第 at 个字符之前"。
    #   没有它，正文里的 [1] 只是一串死文字，弹注在阅读器里点不开。
    # 清洗 TXT 那条线永远不写这两个字段，to_dict 也只在非空时才写。
    anchor: str = ""
    refs: list[dict] = field(default_factory=list)
    # 段内的**注音**（schema v7，EPUB 导入专用）：每条
    # `{"at": 起点偏移, "len": 被注音的字数, "rt": 注音文字}`。
    # 覆盖 text[at : at+len]，`rt` 是原书 `<rt>` 里的读音（罗马字/假名/拼音）。
    # 为什么单独存一列而不是拼进 text：注音不是正文，拼进去会让「梦泽文哉」
    # 变成「梦泽文AYA哉KA」，检索、字数、朗读全被污染；可它又确实是信息
    # （人名怎么念、代号原文是哪个英文词），不能一声不响扔掉。
    # 清洗 TXT 那条线永远不写这个字段（TXT 表达不了行内注音），
    # to_dict 也只在非空时才写。
    ruby: list[dict] = field(default_factory=list)
    # 这一段在**重排后**要不要靠右排（schema v8，EPUB 导入专用）：
    # "" = 按正文样式（两端对齐、首行缩进），"right" = 靠右且不缩进。
    # 来自原书 CSS：`p.right-info { text-align: right }` 这类单类规则命中本段
    # class 时置 "right"（见 epubread._right_aligned_classes）。为什么要留下来：
    # 落款、日期、署名靠右是作者的排版意图，按正文两端对齐会顶到左边，
    # 读者会把它当成正文里的最后一句。清洗 TXT 那条线永远不写它。
    align: str = ""
    # 以下字段只在 kind == "table" 时有意义（schema v3，EPUB 导入专用）：
    # 整张表的二维网格，rows[行][列] = 一个单元格 dict：
    #   {"text": 文字, "header": 是不是表头格（<th>）,
    #    "colspan": 跨列数, "rowspan": 跨行数,
    #    "anchor": 单元格自己的 HTML id（空串=没有）,
    #    "refs": 格内行内引用，格式与段落的 refs 相同，偏移相对**本格**文字}
    # 一律走 normalize_rows() 归一：列数不齐、span 越界、格子写成裸字符串
    # 都在那一处收敛，渲染层只认规范形态。
    rows: list[list[dict]] = field(default_factory=list)
    # 这一段在**清洗前原文**里的起始行号（1 基），只有清洗 TXT 那条线会填。
    # **不进 JSON、不参与序列化**（`_paragraph_to_dict` 不写它，schema 不变）。
    # 为什么需要它：段落树里"注文续行"和"紧跟的正文"长得一模一样，光看文字
    # 分不开。原文行号是唯一可靠证据 —— 续行必须紧挨上一行（见
    # clean.py 的 `_strip_note_marks`）。EPUB 导入那条线留 0（没有原文行号）。
    src_line: int = 0
    # 这一段**覆盖到的原文末行号**（并碎行会把后面几行吸收进来）。
    # 0 表示"与 src_line 相同"。同样不进 JSON。续行判定要拿它算相邻。
    src_line_end: int = 0


@dataclass
class Chapter:
    """一个章节节点。

    start_line 记录的是**清洗前**原文里的行号（1 基）。它的价值不是给人看，
    而是清洗报告和人工复核时能"从章节树一步跳回原文第几行" ——
    没有它，出了错只能整本重新肉眼比对。
    """

    title: str
    number: int | None = None  # 解析出的章节序号（"第十二章" → 12），解析不出就是 None
    level: int = 1  # 1=章级，2=卷/部/集级
    start_line: int = 0  # 清洗前的行号（1 基）
    paragraphs: list[Paragraph] = field(default_factory=list)
    # 标题里的行内引用（schema v5，EPUB 导入专用）。格式与 Paragraph.refs 相同，
    # 偏移相对 `title`。为什么要单开一列：注码可以写在 `<h1>` 里
    # （`<h1>作者生平述要<img class="zhangyue-footnote" …></h1>`），而标题是
    # 打包时重新生成的，段落列表里没有它的位置。以前删重复标题时只能把注码
    # 挪进正文，位置差几个字；存进这里之后注码能原样回到标题末尾。
    # 清洗 TXT 那条线永远不写它，to_dict 也只在非空时才写。
    refs: list[dict] = field(default_factory=list)
    # 原 EPUB 的目录深度（schema v6），0 是顶层；None 表示旧章节树或 TXT，
    # 打包时按 level 推断目录。
    # 为什么不复用 level：两者回答的不是同一个问题。level 只说"正文怎么排版"
    # （卷/章），目录深度说"原书怎么分组"。有子项的不一定是卷，分组结束后也可以
    # 回到顶层叶子 —— 后记被排到末章下面，靠 level 永远看不出来。
    toc_depth: int | None = None


@dataclass
class Book:
    """整本书的章节树。"""

    title: str = ""
    author: str = ""
    encoding: str = ""
    source: str = ""
    # 第一个章节之前、又不在原目录里的零散内容（广告残留、无标题扉页）。
    # 有目录的 EPUB 会把前附条目收成「前附」卷及其子章，不落在这里；
    # 这些零散段落由渲染层收进前附卷的卷首页，既不丢，也不挤进第一章。
    front_matter: list[Paragraph] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    # 最后一个章节之后的内容：后记、全书完、作者的话、广告尾巴。
    tail_matter: list[Paragraph] = field(default_factory=list)


# ---------- 章节标题解析（复用 diagnose 的判定） ----------

# "第<数字><单位>"。数字部分直接交给 diagnose.cn_to_int()，这里只负责定位。
# 与 diagnose._NUM_TITLE 的区别：这里带上"单位"捕获组，因为单位决定层级
# （卷/部/集 是 level 2，章/回/节 是 level 1）。
_HEADING_NUM = re.compile(
    r"^\s*第\s*([0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,10})\s*([卷部集篇章回节幕话話])"
)
_CHAPTER_EN = re.compile(r"^\s*[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*([0-9]{1,4})")
# 「星号 Chapter」（`　　☆、CH.1`）的序号：在 `CH.` 后面。
# 与 diagnose 的「星号 Chapter」家族是同一条判据的两半 —— 那边判"是不是章行"，
# 这里取"第几章"。两处正则必须同时认这个形态，否则家族认得出、序号恒为 None。
_STAR_CHAPTER = re.compile(
    r"^[\s\u3000]*[☆★]\s*[、.．]\s*[Cc][Hh][0-9]{0,2}\s*[.．]\s*([0-9]{1,4})\s*$"
)
# 章标题文本：剥掉行首的「星 + 顿号 + 空白」，留下 `CH.N`。
# 用户看到的章名就该是 `CH.1`，不带 `☆、` 与前导全角空格。
# 源文件里的异体 `CH1.19` 也在这里统一成 `CH.19` —— 同一本书两种写法，
# 目录里出现两种章名只会让用户以为切错了。
_STAR_CHAPTER_TITLE = re.compile(
    r"^[\s\u3000]*[☆★]\s*[、.．]\s*[Cc][Hh][0-9]{0,2}\s*[.．]\s*([0-9]{1,4})\s*$"
)
# 「（一）」「（十二）」式分节的序号。括号里的数字同样是"第几个"，
# 所以照样交给 cn_to_int() 转，不另写一套中文数字转换。
_BRACKET_SECTION = re.compile(r"^\s*[（(]\s*([0-9０-９〇零一二三四五六七八九十百]{1,4})\s*[）)]\s*$")
# 「1、掳君（1）」式分节的序号，在顿号前面。
_NUM_SECTION = re.compile(r"^\s*((?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4}))\s*[、.．]")

# 卷/部/集/篇 比章高一级。按契约：level 1 = 章级，level 2 = 卷/部/集级。
_VOLUME_UNITS = "卷部集篇"

# diagnose.match_chapter() 返回的家族名 → 默认层级。
# 没有数字的家族（序章、楔子、番外、目录）一律算章级：
# 它们确实是"章这一层"的节点，只是没有序号。
_LEVEL_BY_FAMILY = {
    "第N卷部类": 2,
    "第N章类": 1,
    "Chapter N": 1,
    "卷首特殊章": 1,
    "括号分节": 1,
    "数字顿号分节": 1,
    "Markdown 标题": 1,
    "纯数字行": 1,
    "分隔线": 1,
}


class Heading(NamedTuple):
    """标题解析结果：序号、层级、命中的模式家族。"""

    number: int | None
    level: int
    family: str


def parse_heading(line: str) -> Heading | None:
    """认出"这一行是不是章节标题"，并给出序号与层级；不是标题返回 None。

    为什么复用 diagnose.match_chapter() 判"是不是标题"：体检报告和清洗结果
    必须是同一套判据。两边各写一套正则，迟早出现"报告说有 30 章、清洗出 29 章"。

    为什么返回 None 而不是"猜一个 level 1"：调用方（clean.py）靠 None 决定
    "这行是正文，别当标题切章"。瞎猜会把正文行切成章节，破坏比漏判严重得多，
    所以宁可返回"没判定"。

    为什么先剥 Markdown 井号再找序号："## 第十二章" 命中 diagnose 的
    "Markdown 标题"家族，但序号在井号后面。不剥就会丢掉序号，
    后面的重复/连续性检查跟着全瞎。
    """
    # 下载站常给标题套一圈对称装饰边框（「========== 第001章 ==========」）。
    # 体检的 match_chapter 已经剥过，这里必须同样剥一次再抽序号，否则
    # 家族认得出、序号却全丢（Heading.number 恒为 None），目录编号检查跟着瞎。
    # 纯装饰分隔线 strip_frame 原样保留，仍归「分隔线」家族由 clean.py 挡掉。
    line = strip_frame(line)
    family = match_chapter(line)
    if family is None:
        return None

    level = _LEVEL_BY_FAMILY.get(family, 1)
    number: int | None = None

    m = _HEADING_NUM.match(line)
    if m is None:
        m = _HEADING_NUM.match(line.lstrip("# \t"))
    if m is not None:
        unit = m.group(2)
        level = 2 if unit in _VOLUME_UNITS else 1
        # cn_to_int 是 diagnose 的实现，这里绝不自带一套转换：
        # "一百二十三" 的进位规则只允许存在一份。
        number = cn_to_int(m.group(1))
    else:
        m_en = _CHAPTER_EN.match(line)
        if m_en is not None:
            try:
                number = int(m_en.group(1))
            except ValueError:
                number = None
        elif _STAR_CHAPTER.match(line) is not None:
            # 「星号 Chapter」（`　　☆、CH.1`）：序号在 CH. 后面。
            # 与 diagnose 的「星号 Chapter」家族是同一条判据的两半，
            # 那边判"是不是章行"，这里取"第几章"。
            try:
                number = int(_STAR_CHAPTER.match(line).group(1))
            except ValueError:
                number = None
        elif family == "纯数字行":
            # "123" 单独一行也是章节标记的一种形态，序号就在行里。
            # 仍然交给 cn_to_int()（它同时认全角数字），不另写转换。
            number = cn_to_int(line.strip())
        elif family == "数字顿号分节":
            # 顿号前面的阿拉伯数字就是这个"节"的序号（《书M》1..66）。
            m_num = _NUM_SECTION.match(line)
            if m_num is not None:
                number = cn_to_int(m_num.group(1))
        elif family == "括号分节":
            # 括号里的数字就是这个"节"的序号（《书F》1..220）。
            # 取不到就留 None —— 序号只影响报告里的连续性检查，不影响切章。
            m_br = _BRACKET_SECTION.match(line)
            if m_br is not None:
                number = cn_to_int(m_br.group(1))
    return Heading(number, level, family)


def chapter_from_title(title: str, start_line: int = 0) -> Chapter:
    """用一个标题行造出 Chapter，序号与层级自动解析。

    给 clean.py 省掉"先 parse_heading 再手填两个字段"的重复代码，
    也保证所有章节的 number/level 都出自同一个解析器。
    """
    heading = parse_heading(title)
    if heading is None:
        # 调用方明确说"这是一章"就照办，只是序号不知道、层级按章级处理。
        return Chapter(title=title, number=None, level=1, start_line=start_line)
    return Chapter(title=title, number=heading.number, level=heading.level, start_line=start_line)


# ---------- 表格 ----------


# 单元格跨行/跨列的上限。真书中常见跨度较小，超过 20 按异常数据处理
# （某些书商把 <td colspan="9999"> 当"整行合并"用）。夹到上限而不是报错，
# 是为了避免异常跨度导致整章无法渲染。
MAX_SPAN = 20


def _as_span(value) -> int:
    """把 colspan/rowspan 归一成 1..MAX_SPAN 的整数。"""
    n = _as_int(value, 1)
    if n < 1:
        return 1
    return min(n, MAX_SPAN)


def _normalize_refs(raw) -> list[dict]:
    """把 JSON 里的 refs 归一成 [{"at", "note", "label"[, "text"]}]。

    两种引用共用这份结构：`note` 是注释段的 id（渲染层据此找注文）；`text` 是
    引用自带的注文，没有落点 —— 掌阅格式的书就是这样，注文只存在 `zy-footnote`
    属性里，文档里没有注释块可指。两者都没有的条目丢掉（拼不出任何东西）。

    段落与表格单元格共用这一份：两处各写一遍，迟早出现偏移基准对不上的事。
    """
    out: list[dict] = []
    for r in _as_list(raw):
        if not isinstance(r, dict):
            continue
        note = r.get("note")
        text = r.get("text")
        if not note and not text:
            continue
        item = {
            "at": _as_int(r.get("at"), 0),
            "note": "" if not note else str(note),
            "label": "" if r.get("label") is None else str(r.get("label")),
        }
        if text:
            item["text"] = str(text)
        out.append(item)
    return out


def _normalize_ruby(raw) -> list[dict]:
    """把 JSON 里的 ruby 归一成 `[{"at", "len", "rt"}]`。

    三条约束都在这一处收敛，渲染层不必再做防御：
    - `len` 必须 > 0（零长度的注音没有可覆盖的字，渲出来只能是一对空标签）；
    - `rt` 不能是空串（没有读音就没什么可注的）；
    - 区间按 `at` 升序且**互不重叠**（`<ruby>` 在 HTML 里本身不允许嵌套，
      手改 JSON 改出重叠时保留靠前的一条，后面的丢掉）。

    越界（`at` 或 `at+len` 超出 text）这里判不了 —— 归一发生在读文件时，
    还没跟 `text` 对上号。渲染层按 text 长度再判一次（见 epub.py 的
    `_ruby_spans`），跳过的条目不会把注音渲到错的地方。
    """
    out: list[dict] = []
    last_end = -1
    # 先排序再去重叠：输入顺序是文件里写的，手改 JSON 很容易把两条写反。
    # 边读边判会把"排在前面的那条 at 更大"当成重叠，白丢一条。
    candidates = sorted(
        (
            {"at": _as_int(r.get("at"), -1), "len": _as_int(r.get("len"), 0), "rt": str(r["rt"])}
            for r in _as_list(raw)
            if isinstance(r, dict) and r.get("rt") is not None and str(r["rt"]).strip()
        ),
        key=lambda x: x["at"],
    )
    for item in candidates:
        if item["at"] < 0 or item["len"] <= 0 or item["at"] < last_end:
            continue
        out.append(item)
        last_end = item["at"] + item["len"]
    return out


def _ruby_to_dict(item: dict) -> dict:
    """ruby 的一条落盘形态。键序固定，老树不产生这个键。"""
    return {"at": item["at"], "len": item["len"], "rt": item["rt"]}


def _normalize_align(raw) -> str:
    """把 JSON 里的 align 收敛到 PARAGRAPH_ALIGNS 之内。

    不认识的值一律当"没标"：手改 JSON 写错一个词（"Right"、"center"）不该让
    整本书打包失败，但也不能让它漏进 xhtml 变成一个凭空出现的 class。
    大小写与首尾空白在这一处抹平，调用方按小写比较。
    """
    value = "" if raw is None else str(raw).strip().lower()
    return value if value in PARAGRAPH_ALIGNS else ""


def fit_refs(refs, text: str) -> list[dict]:
    """把行内引用的偏移收进 `text` 的长度之内，返回新列表。

    为什么需要：引用的偏移原本相对**源文件里的那块文字**，而挂上去的标题取
    自目录条目，两者可能不等长（正文写「第一章　劫后梦醒」，目录只写「第一章」）。
    越界的引用渲染层会跳过，可它指向的注文已经按"引用在"搬进了注标 ——
    不钳就等于丢注。所以一律往回收，落到末尾；多条一起越界时向前错开一格，
    否则同一位置只插得进第一条（渲染时遇到 at < pos 会跳过）。

    导入端写标题注、打包端渲染标题都走这一份，口径必须一致：两边各写一套，
    迟早出现"存进去的偏移合法、渲出来却被跳过"的事。
    """
    if not refs:
        return []
    limit = len(text)
    last = len(refs) - 1
    out: list[dict] = []
    for i, ref in enumerate(refs):
        try:
            at = int(ref.get("at", 0))
        except (TypeError, ValueError):
            at = 0
        out.append({**ref, "at": max(0, min(at, limit - (last - i)))})
    return out


def _ref_to_dict(ref: dict) -> dict:
    """refs 的一条落盘形态：`note` / `text` 只在真有值时写。

    沿用"空字段不写"的老规矩（v1 旧文件重新序列化必须逐字节不变）：
    老树的引用没有 text，写出来的键序与以前完全一样。
    """
    out = {"at": int(ref.get("at", 0))}
    note = str(ref.get("note", "") or "")
    if note:
        out["note"] = note
    out["label"] = str(ref.get("label", "") or "")
    text = str(ref.get("text", "") or "")
    if text:
        out["text"] = text
    return out


def _normalize_cell(cell) -> dict:
    """把一个单元格归一成 {text, header, colspan, rowspan, anchor, refs}。

    允许格子是裸字符串（手写 JSON 时写 "1771" 比写一整个 dict 省事），
    也允许缺任意字段 —— 缺的一律按"一个普通格"补，而不是拒绝整张表。
    """
    if isinstance(cell, str):
        return {"text": cell, "header": False, "colspan": 1, "rowspan": 1, "anchor": "", "refs": []}
    if not isinstance(cell, dict):
        return {
            "text": "" if cell is None else str(cell),
            "header": False,
            "colspan": 1,
            "rowspan": 1,
            "anchor": "",
            "refs": [],
        }
    text = cell.get("text")
    anchor = cell.get("anchor")
    return {
        "text": "" if text is None else str(text),
        "header": bool(cell.get("header")),
        "colspan": _as_span(cell.get("colspan")),
        "rowspan": _as_span(cell.get("rowspan")),
        "anchor": "" if anchor is None else str(anchor),
        "refs": _normalize_refs(cell.get("refs")),
    }


def normalize_rows(raw) -> list[list[dict]]:
    """把任意形状的 rows 归一成"每格都是规范 dict"的二维表。

    不做"列数对齐"：colspan/rowspan 本来就允许行与行长短不一，硬补齐会凭空
    造出格子。渲染层按 span 排布，列不齐是它的正常输入。
    """
    return [[_normalize_cell(cell) for cell in _as_list(row)] for row in _as_list(raw)]


def table_plain_text(rows: list[list[dict]], cell_sep: str = "｜") -> str:
    """把网格摊成纯文本：每个表格行输出一行，格间用 cell_sep，行间用换行。

    字数统计、字体子集、清洗报告、界面预览与 TXT 输出共用这一摊平规则，
    避免各消费者重复实现。每格文字先去除首尾空白，并省略行尾空单元格，
    避免无意义的尾随分隔符；中间空单元格仍保留。
    """
    lines: list[str] = []
    for row in rows:
        cells = [c.get("text", "").strip() for c in row]
        while cells and not cells[-1]:
            cells.pop()
        lines.append(cell_sep.join(cells))
    return "\n".join(lines)


# ---------- 序列化 ----------


def _paragraph_to_dict(p: Paragraph) -> dict:
    d = {"text": p.text, "kind": p.kind}
    # 三个新字段只在 image 段上写：普通段落多出 "caption": "" 会让
    # v1 的旧文件重新序列化后字节发生变化（docs/gui-design.md 的「EPUB 导入」一节验收要求：除 version 外不变）。
    if p.kind == "image":
        d["caption"] = p.caption
        d["width"] = p.width
        d["height"] = p.height
    if p.kind == "table":
        # 只写"不是默认值"的键：一张 24 行 × 3 列的年表有 70 多个格子，
        # 每格都写全六个键，book.json 会变成一堆 "colspan": 1 的噪声，
        # 人眼再也看不出表格结构。缺的键由 _normalize_cell 补默认值。
        d["rows"] = [
            [
                {
                    "text": c["text"],
                    **({"header": True} if c["header"] else {}),
                    **({"colspan": c["colspan"]} if c["colspan"] > 1 else {}),
                    **({"rowspan": c["rowspan"]} if c["rowspan"] > 1 else {}),
                    **({"anchor": c["anchor"]} if c["anchor"] else {}),
                    **({"refs": c["refs"]} if c["refs"] else {}),
                }
                for c in row
            ]
            for row in normalize_rows(p.rows)
        ]
    # anchor / refs 同理：只在真有值时写。清洗 TXT 那条线产出的段落
    # 两个字段都空，序列化结果与之前逐字节相同。
    if p.anchor:
        d["anchor"] = p.anchor
    if p.refs:
        d["refs"] = [_ref_to_dict(r) for r in p.refs]
    # 注音同理：只在这段真有注音时写，老树重新序列化后逐字节不变。
    if p.ruby:
        d["ruby"] = [_ruby_to_dict(r) for r in p.ruby]
    # 对齐同理：只在这段真被标成右对齐时写。
    if p.align:
        d["align"] = p.align
    return d


def _paragraph_from_dict(raw) -> Paragraph:
    # 允许直接写字符串（{"paragraphs": ["正文一", "正文二"]}）：
    # 手写 JSON 和测试里造样本会方便很多，代价只是多一个 isinstance 判断。
    if isinstance(raw, str):
        return Paragraph(text=raw)
    if not isinstance(raw, dict):
        return Paragraph(text=str(raw))
    text = raw.get("text")
    kind = raw.get("kind")
    caption = raw.get("caption")
    anchor = raw.get("anchor")
    return Paragraph(
        text="" if text is None else str(text),
        kind="body" if kind is None else str(kind),
        caption="" if caption is None else str(caption),
        width=_as_int(raw.get("width"), 0),
        height=_as_int(raw.get("height"), 0),
        anchor="" if anchor is None else str(anchor),
        refs=_normalize_refs(raw.get("refs")),
        rows=normalize_rows(raw.get("rows")),
        ruby=_normalize_ruby(raw.get("ruby")),
        align=_normalize_align(raw.get("align")),
    )


def _chapter_to_dict(ch: Chapter) -> dict:
    out = {
        "title": ch.title,
        "number": ch.number,
        "level": ch.level,
        "start_line": ch.start_line,
        "paragraphs": [_paragraph_to_dict(p) for p in ch.paragraphs],
    }
    # 只在真有标题注时才写这个键：老树（含 TXT 那条线的产物）重新序列化后
    # 除 version 外应当逐字节不变。
    if ch.refs:
        out["refs"] = [_ref_to_dict(r) for r in ch.refs]
    if ch.toc_depth is not None:
        out["toc_depth"] = ch.toc_depth
    return out


def _as_list(value) -> list:
    """把 JSON 里的值当作列表用。

    为什么需要它：手改 JSON 时很容易把 "chapters": [] 写成 "chapters": {} 或
    手滑留成字符串。直接 for 遍历 dict 会遍历键、遍历字符串会遍历字符，
    结果是"读进来一堆莫名其妙的空章节"，比报错更难查。这里一律退化成空列表，
    让 validate() 能报"没有任何章节"，问题一眼可见。
    """
    return value if isinstance(value, list) else []


def _as_int(value, default: int = 0) -> int:
    """把 JSON 里的值转成 int；转不了就用默认值。

    脏 JSON 不该让整本书读不进来 —— 一个 level 写错不该毁掉 500 章的内容。
    bool 要单独挡掉，因为 Python 里 True 是 int 的子类，会悄悄变成 1。
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value) -> int | None:
    """同 _as_int，但保留 None 的语义（"没解析出序号"和"序号是 0"是两回事）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chapter_from_dict(raw) -> Chapter:
    if not isinstance(raw, dict):
        return Chapter(title=str(raw))
    title = raw.get("title")
    depth = _as_optional_int(raw.get("toc_depth"))
    return Chapter(
        title="" if title is None else str(title),
        number=_as_optional_int(raw.get("number")),
        level=_as_int(raw.get("level"), 1),
        start_line=_as_int(raw.get("start_line"), 0),
        paragraphs=[_paragraph_from_dict(x) for x in _as_list(raw.get("paragraphs"))],
        refs=_normalize_refs(raw.get("refs")),
        toc_depth=depth if depth is not None and depth >= 0 else None,
    )


def to_dict(book: Book) -> dict:
    """把章节树转成可以直接 json.dumps 的普通字典。

    为什么返回 dict 而不是 JSON 字符串：缩进几位、中文要不要转义、
    要不要写文件，各家调用方的要求不一样。序列化策略留给调用方，
    本函数只保证"结构稳定、字段齐全、每次键的顺序一致"。
    返回的是新构造的对象，调用方随便改都污染不到原来那本书。
    """
    return {
        "version": SCHEMA_VERSION,
        "title": book.title,
        "author": book.author,
        "encoding": book.encoding,
        "source": book.source,
        "front_matter": [_paragraph_to_dict(p) for p in book.front_matter],
        "chapters": [_chapter_to_dict(ch) for ch in book.chapters],
        "tail_matter": [_paragraph_to_dict(p) for p in book.tail_matter],
    }


def from_dict(d: dict) -> Book:
    """从 to_dict() 的产物还原章节树。

    为什么缺字段宽容、版本号严格：
    缺字段通常意味着旧文件或手写 JSON，用默认值补齐还能用；
    但主版本号对不上意味着"字段含义可能已经变了"（比如 level 的编号方式改了），
    照着旧语义读会读出一本结构错位的书，还不如当场报错让人去看。

    为什么段落允许是纯字符串：手写样本和早期草稿里常见这种写法，
    兼容它不会带来歧义。
    """
    if not isinstance(d, dict):
        raise TypeError(f"章节树应该是 dict，实际是 {type(d).__name__}")

    version = str(d.get("version", SCHEMA_VERSION))
    major = version.split(".")[0].strip()
    # 历次 schema 升级只增加可选字段，旧文件按新版本读入都无损；其余版本仍然拒绝，
    # 宁可当场报错，也不按不认识的语义读出一本结构错位的书。
    # 支持范围从 SCHEMA_VERSION 派生：以前这里写死一个元组，升版本时忘了同步
    # 就会出现"自己刚写出来的文件自己读不进去"（本次 v7 就踩了一次）。
    if major not in {str(n) for n in range(1, int(SCHEMA_VERSION.split(".")[0]) + 1)}:
        raise ValueError(
            f"不认识的章节树版本 {version!r}（当前支持 1 至 {SCHEMA_VERSION}）："
            "字段含义可能已变，拒绝按旧语义读出错误的章节结构"
        )

    def _text(key: str) -> str:
        v = d.get(key)
        return "" if v is None else str(v)

    return Book(
        title=_text("title"),
        author=_text("author"),
        encoding=_text("encoding"),
        source=_text("source"),
        front_matter=[_paragraph_from_dict(x) for x in _as_list(d.get("front_matter"))],
        chapters=[_chapter_from_dict(x) for x in _as_list(d.get("chapters"))],
        tail_matter=[_paragraph_from_dict(x) for x in _as_list(d.get("tail_matter"))],
    )


def save(book: Book, path: Path) -> None:
    """把章节树写成 JSON 文件（UTF-8）。

    为什么固定 ensure_ascii=False：这份文件是给人排查用的，
    中文全变成 \\uXXXX 就失去了"能直接打开看"的价值；
    为什么固定 indent=2：diff 时一行一个字段，改了什么一眼能看出来。
    父目录不存在就顺手建：调用方写的常是 outputs/<书名>/book.json 这种新目录。
    """
    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_dict(book), ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def load(path: Path) -> Book:
    """读回 save() 写出的 JSON 文件。

    只接受 UTF-8：save 只会写 UTF-8，读到别的东西说明文件被换过，
    该报错而不是猜编码（猜编码是 encoding.py 的职责，不是这里的）。
    """
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------- 校验 ----------


def _iter_groups(book: Book) -> list[tuple[str, list[tuple[int, Chapter]]]]:
    """把章节按"卷"分组，供序号检查使用。

    为什么必须分组：每一卷的章号都从 1 重新开始。如果把全书章节当一条线
    比较递增，卷二的第一章必然被报成"从 300 倒退到 1"，全是假警报。
    分组规则：
    - 所有卷标题归同一组，卷与卷之间比较序号（一卷二卷三卷必须递增）；
    - 章归到"当前所在的卷"名下，每卷的章号从 1 重新开始；
    - 第一个卷之前的章归到"全书（无卷）"；
    - 没编号的章（序章、番外）留在组里但跳过比较 —— 没判定就别瞎猜。

    返回 [(组名, [(章节下标, 章节), ...]), ...]，组名只用于给人看的提示。
    """
    # 组 key 用 (层级, 卷序号) 而不是卷号：两卷都写成"第1卷"（常见于盗版合并本）
    # 时，按卷号做 key 会把两卷的章混成一组，然后互相误报序号重复。
    groups: dict[tuple[int, int], list[tuple[int, Chapter]]] = {}
    group_names: dict[tuple[int, int], str] = {}
    # 卷序号用递增计数器，不用章节序号：卷标题没编号（"正文卷"）时也要能分开。
    volume_seq = 0
    for idx, ch in enumerate(book.chapters):
        if ch.level >= 2:
            volume_seq += 1
            # 卷标题全部并进同一个组（key 的第二个分量固定 0），
            # 这样"第1卷 → 第2卷 → 第3卷"之间才能比较递增。
            key: tuple[int, int] = (2, 0)
            group_names[key] = "卷/部/集标题"
        else:
            key = (1, volume_seq)
            if key not in group_names:
                group_names[key] = (
                    "全书（第一个卷之前）" if volume_seq == 0 else f"第 {volume_seq} 个卷"
                )
        groups.setdefault(key, []).append((idx, ch))
    return [(group_names[key], items) for key, items in groups.items()]


def chapter_groups(book: Book) -> list[tuple[str, list[tuple[int, Chapter]]]]:
    """公开入口：把章节按卷分组，供序号比较复用（详见 _iter_groups）。

    为什么要有公开的这一层：复核页要按组比章号，不该自己再写一条
    "全书一条线比较"的循环 —— 每卷章号都从 1 重新开始，全局比较必然
    把卷二的第一章全报成"编号倒退"。分组规则只在这里维护一份。
    """
    return _iter_groups(book)


# 告警分三级（决定32）。三级不是三种颜色，是三种"该谁管"：
#   loss   东西丢了 —— 得回去看原文件，光看成品看不出所以然；
#   order  结构错了 —— 目录会跳错位置、调整没生效，还能改；
#   layout 排版瑕疵 —— 书照读，留意即可。
# 级别由产生告警的这一层判定，界面不猜；界面只负责把同级的合成一行。
LEVEL_LOSS = "loss"
LEVEL_ORDER = "order"
LEVEL_LAYOUT = "layout"


def validate_items(book: Book) -> list[dict]:
    """检查章节树是否健康，返回结构化的问题清单；空列表代表没发现问题。

    为什么返回 dict 而不是字符串：同一类问题会一条一章地出现（十三个空章就是
    十三行），界面要按 code 合并计数才压得到三行以内（决定31）。text 是给人读的
    整句，一句一章、带可核对的位置；code 是合并用的类别；level 是严重度（见上面
    三个 LEVEL_* 常量）。排序、计数、措辞归并都在界面那一侧做 —— 这一层只保证
    "同类同 code"。

    为什么返回清单而不是抛异常：
    这些问题大多不致命（脏书的中间态本来就带病），先渲染出来看一眼
    往往比"整个流程挂掉"更有用。清单要能贴进清洗报告给人核对，
    所以每条都带"第几个章节、什么标题、原文第几行"这类可核对的信息。

    为什么序号检查要分组（见 _iter_groups）：每卷章号重新开始，
    全局比较必然把卷二的第一章误报成倒退。

    检查项：
    1. 编码字段为空（丢失原始编码 = 以后无法复现解码）；
    2. 一个章节都没有（打包后没有目录）；
    3. 章节标题为空（渲染出空白目录项）；
    4. level 不是 1/2（契约外取值，渲染层无法排版）；
    5. 段落全空（多半是清洗把正文丢了）。**level=2 的卷/部/集标题豁免这一条**：
       它是容器，正文在下面的章里，空段落是正常形态；
    6. 段落 kind 不在 PARAGRAPH_KINDS 之内（渲染层没法定排版）；
    7. 序号重复、序号非递增（同组内比较，缺号不算问题 —— 没判定就别瞎猜）；
    8. 图片段（kind="image"）的 text 不是安全的图片文件名（空 / 带路径分隔符 /
       带 .. / 扩展名不在白名单）—— 渲染层会把它拼进 ../Images/<name>，
       脏名字等于路径穿越；
    9. 非图片段带了 caption（图说只属于 image 段，渲染层会忽略它 —— 不报就悄悄丢了）；
    10. 表格段（kind="table"）的网格为空，或者单元格的 anchor / refs 不合法。
        单元格锚点和引用与段落对应字段一样会进入 XHTML 的 id / href，
        引用偏移也必须相对本格文字有效。
    """
    issues: list[dict] = []

    def _add(code: str, level: str, text: str) -> None:
        issues.append({"code": code, "level": level, "text": text})

    def _check_refs(refs, text: str, where: str) -> None:
        """行内引用的契约检查（段落与表格单元格共用）。

        at 的上界按**它自己的那段文字**算：单元格里的偏移只相对那一格，
        拿整张表的长度去比会把每一格都报成越界。
        """
        for ref in refs:
            note = str(ref.get("note") or "")
            own_text = str(ref.get("text") or "")
            if not note:
                # 自带注文的引用没有落点：文字本身就是渲染层要写进属性的东西，
                # 不写 text 又没 note 的条目才是坏的（渲染层拼不出任何东西）。
                if not own_text.strip():
                    _add("bad_ref", LEVEL_LAYOUT, f"{where}行内引用既没有目标也没有注文：渲染层拼不出东西")
            elif not _SAFE_ID.match(note):
                _add("bad_ref", LEVEL_LAYOUT, f"{where}行内引用目标 {note!r} 不是安全的 HTML id")
            at = ref.get("at")
            if not isinstance(at, int) or isinstance(at, bool) or at < 0:
                _add("bad_ref", LEVEL_LAYOUT, f"{where}行内引用的位置 {at!r} 不是非负整数")
            elif at > len(text):
                _add("bad_ref", LEVEL_LAYOUT, f"{where}行内引用的位置 {at} 超出这段文字的长度 {len(text)}")

    def _check_paragraph(p: Paragraph, label: str) -> None:
        """单段的契约检查（图片文件名白名单 / caption 归属 / 表格网格）。"""
        if p.kind == "image":
            name = p.text.strip()
            if not name:
                _add("image_name", LEVEL_LAYOUT, f"{label}有图片段的文件名为空：渲染层拼不出图片路径")
            elif "/" in name or "\\" in name or ".." in name:
                _add("image_name", LEVEL_LAYOUT,
                    f"{label}图片文件名 {name!r} 含路径分隔符或 ..（只许纯文件名）"
                )
            elif Path(name).suffix.lower() not in IMAGE_EXTS:
                _add("image_name", LEVEL_LAYOUT,
                    f"{label}图片文件名 {name!r} 的扩展名不在白名单"
                    f"（只允许 {'/'.join(IMAGE_EXTS)}）"
                )
        elif p.caption:
            # caption 是图说，只属于 image 段；出现在正文段上说明上游标错了，
            # 渲染层会直接忽略它 —— 不报的话这段文字就悄悄丢了。
            _add("caption_on_text", LEVEL_LAYOUT, f"{label}非图片段落带了 caption（图说只属于 image 段）")
        # anchor / refs 会直接拼进 xhtml 的 id 与 href：脏值等于注入。
        if p.anchor and not _SAFE_ID.match(p.anchor):
            _add("bad_anchor", LEVEL_LAYOUT, f"{label}段落 anchor {p.anchor!r} 不是安全的 HTML id（含空白或引号等）")
        # align 决定这一段套哪个 CSS 类（见 epub.py 的 p.align-right）：
        # 契约外的取值渲染层只能当正文排，等于用户标好的靠右静默失效。
        if p.align not in PARAGRAPH_ALIGNS:
            _add("bad_align", LEVEL_LAYOUT,
                f"{label}段落的 align={p.align!r} 不在白名单"
                f"（只允许 right 或空串）：渲染层会退回正文样式"
            )
        _check_refs(p.refs, p.text, label)
        if p.kind == "table":
            # 先归一再看：手写的章节树（测试、手改 JSON）里格子可能缺键，
            # 直接下标取值会让校验本身抛 KeyError —— 校验器崩掉比校验不出来更糟。
            rows = normalize_rows(p.rows)
            # 空网格不能靠 p.text 判断：text 是摊平文本，可能与 rows 不一致。
            # rows 中没有任何单元格时渲染层会跳过表格，因此这里必须报告。
            if not any(rows):
                _add("table_empty", LEVEL_LAYOUT,
                    f"{label}表格段一个单元格都没有（rows 为空）：渲染层会跳过这张表"
                )
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    pos = f"{label}表格第 {r + 1} 行第 {c + 1} 格"
                    if cell["anchor"] and not _SAFE_ID.match(cell["anchor"]):
                        _add("bad_anchor", LEVEL_LAYOUT,
                            f"{pos}的 anchor {cell['anchor']!r} 不是安全的 HTML id（含空白或引号等）"
                        )
                    _check_refs(cell["refs"], cell["text"], pos)

    if not book.encoding.strip():
        _add("encoding_empty", LEVEL_LAYOUT, "encoding 字段为空：清洗时没记录原始编码，日后无法复核解码是否正确")

    if not book.chapters:
        _add("no_chapters", LEVEL_LOSS, "没有任何章节：全书内容都会落在 front_matter，打包后没有目录可跳转")

    for idx, ch in enumerate(book.chapters):
        label = f"第 {idx + 1} 个章节"
        title = ch.title.strip()
        if not title:
            _add("empty_title", LEVEL_ORDER,
                f"{label}标题为空（start_line={ch.start_line}）：渲染后目录里会出现一个空白条目"
            )
        if ch.level not in (1, 2):
            _add("bad_level", LEVEL_ORDER,
                f"{label}「{ch.title}」的 level={ch.level} 不在 1/2 之内（1=章级，2=卷/部/集级）"
            )
        # 标题注的偏移相对标题文字：越界的条目渲染层会跳过，而它指向的注文
        # 早已按"这条引用在"从正文里搬走 —— 不报就等于悄悄丢一条注。
        _check_refs(ch.refs, ch.title, f"{label}「{ch.title}」标题")
        # 卷/部/集（level=2）是**容器标题**，正文都在它下面的章里，本身没有
        # 段落是正常形态。不豁免的话每本分卷的书都会报一条"正文可能丢了"，
        # 天天喊狼来了，真丢正文的时候人反而不看了。
        if ch.level != 2 and not ch.paragraphs:
            _add("chapter_empty", LEVEL_LOSS, f"{label}「{ch.title}」一个段落都没有：正文可能在清洗时丢了")
        elif ch.level != 2 and all(not p.text.strip() for p in ch.paragraphs):
            _add("chapter_blank", LEVEL_LOSS,
                f"{label}「{ch.title}」段落全空（{len(ch.paragraphs)} 段都没字）：正文可能在清洗时丢了"
            )
        # kind 是渲染层唯一的排版依据，契约外的取值会让它不知道该缩进还是该落空行。
        bad_kinds = sorted({p.kind for p in ch.paragraphs if p.kind not in PARAGRAPH_KINDS})
        if bad_kinds:
            _add("bad_kinds", LEVEL_LAYOUT,
                f"{label}「{ch.title}」出现契约外的段落类型 {bad_kinds}"
                f"（只允许 {'/'.join(PARAGRAPH_KINDS)}）：渲染层无法决定怎么排版"
            )
        for p in ch.paragraphs:
            _check_paragraph(p, f"{label}「{ch.title}」")

    for pos, p in enumerate(book.front_matter):
        _check_paragraph(p, f"前言第 {pos + 1} 段")
    for pos, p in enumerate(book.tail_matter):
        _check_paragraph(p, f"后记第 {pos + 1} 段")

    for group_name, items in _iter_groups(book):
        first_seen: dict[int, int] = {}
        prev: tuple[int, int] | None = None
        for idx, ch in items:
            if ch.number is None:
                continue  # 没解析出序号就跳过，不做推测
            if ch.number in first_seen:
                first = first_seen[ch.number]
                _add("number_dup", LEVEL_ORDER,
                    f"章节序号重复：{group_name} 里第 {idx + 1} 个章节「{ch.title}」的序号 "
                    f"{ch.number} 与第 {first + 1} 个章节「{book.chapters[first].title}」相同"
                )
            else:
                first_seen[ch.number] = idx
            if prev is not None and ch.number < prev[0]:
                _add("number_decrease", LEVEL_ORDER,
                    f"章节序号非递增：{group_name} 里第 {idx + 1} 个章节「{ch.title}」序号 "
                    f"{ch.number} 小于第 {prev[1] + 1} 个章节的 {prev[0]}（start_line={ch.start_line}）"
                )
            prev = (ch.number, idx)

    return issues


def validate(book: Book) -> list[str]:
    """问题清单的纯文字版（清洗报告、CLI 用）。结构见 validate_items()。"""
    return [it["text"] for it in validate_items(book)]



# ---------- 统计 ----------


def count_chars(text: str) -> int:
    """数字数：只数中文字符与字母数字，不数空白和标点。

    为什么不直接用 len()：不同来源的书排版习惯不同（全角空格缩进、
    空行隔场景、每句一个换行），len() 会把排版噪声算成内容，
    两本书的字数就没法比了。只数"读者真正看到的字"才可比。
    """
    n = 0
    for ch in text:
        if ch.isspace():
            continue
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF  # 常用汉字
            or 0x3400 <= o <= 0x4DBF  # 扩展 A
            or 0xF900 <= o <= 0xFAFF  # 兼容汉字
            or 0x20000 <= o <= 0x2FA1F  # 扩展 B~F
        ):
            n += 1
        elif ch.isalnum():  # 拉丁字母、阿拉伯数字、假名等
            n += 1
    return n


def stats(book: Book) -> dict:
    """统计这本书的规模：章节数、段数、字数、平均每章字数、最长标题。

    为什么平均每章字数用"章节内字数 / 章节数"，而不是"总字数 / 章节数"：
    front_matter 与 tail_matter 不属于任何一章，混进去会把平均字数虚高，
    这个数是用来看"每章大概多长"的，算错就失去意义。

    为什么最长标题返回字符串而不是长度：给报告看的时候要能对上
    "是不是有个超长标题混进来了"，只给数字还得回去找。

    图片段（kind="image"）整段跳过：它的 text 是图片文件名而不是正文，
    把它算进段落数和字数会让"这本书多少字"虚高，也会让平均每章字数失真。
    """
    def _text_only(paragraphs: list[Paragraph]) -> list[Paragraph]:
        return [p for p in paragraphs if p.kind != "image"]

    total_paragraphs = len(_text_only(book.front_matter)) + len(_text_only(book.tail_matter))
    total_chars = sum(count_chars(p.text) for p in _text_only(book.front_matter))
    total_chars += sum(count_chars(p.text) for p in _text_only(book.tail_matter))

    chapter_chars = 0
    longest_title = ""
    for ch in book.chapters:
        text_paras = _text_only(ch.paragraphs)
        total_paragraphs += len(text_paras)
        chapter_chars += sum(count_chars(p.text) for p in text_paras)
        if len(ch.title) > len(longest_title):
            longest_title = ch.title
    total_chars += chapter_chars

    n = len(book.chapters)
    return {
        "chapters": n,
        "paragraphs": total_paragraphs,
        "chars": total_chars,
        "avg_chars_per_chapter": round(chapter_chars / n, 1) if n else 0.0,
        "longest_title": longest_title,
    }
