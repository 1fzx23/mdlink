#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mdlink — Markdown 文档链接与锚点体检工具

扫描 Markdown 文件里的所有链接，回答三个问题：
  1. 指向的文件 / 图片还在吗？          （相对路径、绝对路径、URL 编码、大小写不一致）
  2. 指向的锚点在目标文档里存在吗？      （GitHub 风格 slug、自定义 {#id}、<a name>）
  3. 外部链接还通吗？                    （可选，多线程 HEAD/GET 探测）

设计要点：
  * 零第三方依赖，只用 Python 标准库
  * 先「遮罩」代码块与行内代码，避免把示例代码里的链接当成真链接
  * 锚点按 GitHub 的 slug 规则生成（小写、去标点、空格转 -、重复加 -1/-2）
  * Windows 上大小写不敏感，但 Git/GitHub 敏感 —— 专门检查「本地能跑、上线 404」的坑
  * 退出码：0 全部通过 / 1 存在断链 / 2 使用错误

用法：
    mdlink .                        # 扫描当前目录
    mdlink docs/ README.md -x       # 同时检查外链
    mdlink . --format html --out report.html

作者：1fzx23 · MIT License
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import fnmatch
import html as html_mod
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

__version__ = "1.0.0"
TOOL_NAME = "mdlink"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

MD_EXTS = {".md", ".markdown", ".mdown", ".mkd", ".mdx"}

# 扫描时默认跳过的目录名
DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "venv", ".venv", "env", "__pycache__", "dist", "build", "out",
    "target", ".next", ".nuxt", ".cache", ".tox", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "site-packages", "coverage",
}

UA = f"{TOOL_NAME}/{__version__} (+https://github.com/1fzx23/{TOOL_NAME})"

SEV_BROKEN = "broken"
SEV_WARN = "warn"

# 视为「站点拒绝自动访问」而非真断链的状态码
SOFT_STATUS = {400, 401, 403, 405, 406, 418, 429, 451, 500, 501, 502, 503, 504, 999}


# --------------------------------------------------------------------------
# 终端着色
# --------------------------------------------------------------------------

class Palette:
    """极简 ANSI 着色器；非 TTY 或 NO_COLOR 时自动降级为纯文本。"""

    enabled = False

    CODES = {
        "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
        "gray": "\033[90m", "bred": "\033[91m", "bgreen": "\033[92m",
        "byellow": "\033[93m", "bcyan": "\033[96m",
    }

    @classmethod
    def paint(cls, text: str, *styles: str) -> str:
        if not cls.enabled or not styles:
            return text
        prefix = "".join(cls.CODES.get(s, "") for s in styles)
        return f"{prefix}{text}{cls.CODES['reset']}"


def enable_ansi() -> None:
    """在 Windows 控制台里打开 VT 序列支持。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            kernel32.SetConsoleMode(kernel32.GetStdHandle(handle), 7)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 代码遮罩：把 fenced code block 与行内代码替换成等长空格
# --------------------------------------------------------------------------

def mask_inline_code(line: str) -> str:
    """把一行里的 `code` 遮成空格，保持长度与列位置不变。"""
    out: List[str] = []
    i, n = 0, len(line)
    while i < n:
        if line[i] != "`":
            out.append(line[i])
            i += 1
            continue
        j = i
        while j < n and line[j] == "`":
            j += 1
        ticks = j - i
        k, close = j, -1
        while k < n:
            if line[k] == "`":
                m = k
                while m < n and line[m] == "`":
                    m += 1
                if m - k == ticks:      # 反引号数量必须严格相等
                    close = m
                    break
                k = m
            else:
                k += 1
        if close == -1:                 # 没有闭合，当作普通字符
            out.append(line[i:j])
            i = j
        else:
            out.append(" " * (close - i))
            i = close
    return "".join(out)


def mask_fences(text: str) -> Tuple[str, Set[int]]:
    """遮罩整段 Markdown，返回 (遮罩后文本, 属于代码块的行号集合[0基])。

    遮罩后文本与原文逐字符对齐，因此正则匹配到的位置可以直接用来算行号。
    """
    lines = text.split("\n")
    out: List[str] = []
    code_lines: Set[int] = set()
    fence: Optional[Tuple[str, int]] = None

    for idx, line in enumerate(lines):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                code_lines.add(idx)
                out.append(" " * len(line))
            else:
                out.append(mask_inline_code(line))
        else:
            code_lines.add(idx)
            if (m and m.group(1)[0] == fence[0]
                    and len(m.group(1)) >= fence[1]
                    and not m.group(2).strip()):
                fence = None
            out.append(" " * len(line))

    return "\n".join(out), code_lines


# --------------------------------------------------------------------------
# GitHub 风格锚点
# --------------------------------------------------------------------------

_PUNCT_KEEP = re.compile(r"[^\w\s\-]", re.UNICODE)


def gh_slug(text: str) -> str:
    """按 GitHub 的规则把标题转成锚点 id。

    规则：转小写 → 去掉 HTML 标签与标点（保留字母/数字/下划线/连字符/空白/CJK）
         → 每个空格替换成一个连字符。
    例：``What's new?`` → ``whats-new``；``读取文件`` → ``读取文件``。
    """
    t = text.strip().lower()
    t = _PUNCT_KEEP.sub("", t)
    return t.replace(" ", "-")


def heading_plain(md: str) -> str:
    """把标题里的 Markdown 语法还原成纯文本，用于生成 slug。"""
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", md)      # 图片
    t = re.sub(r"!\[([^\]]*)\]\[[^\]]*\]", r"\1", t)      # 引用式图片
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)        # 行内链接
    t = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", t)       # 引用式链接
    t = re.sub(r"<[^>]+>", "", t)                         # HTML 标签
    t = t.replace("`", "")
    t = re.sub(r"\*\*|__|\*|~~", "", t)
    t = html_mod.unescape(t)
    return t.strip()


_CUSTOM_ID_RE = re.compile(r"\{#([\w\-:.]+)\}\s*$")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")


def extract_headings(text: str, code_lines: Set[int]) -> List[Tuple[str, Optional[str]]]:
    """提取所有标题，返回 [(原始标题文本, 自定义 id 或 None)]。"""
    lines = text.split("\n")
    heads: List[Tuple[str, Optional[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        if i in code_lines:
            i += 1
            continue
        line = lines[i]
        atx = _ATX_RE.match(line)
        if atx:
            raw = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group(2)).strip()
            custom = None
            cm = _CUSTOM_ID_RE.search(raw)
            if cm:
                custom = cm.group(1)
                raw = raw[: cm.start()].strip()
            heads.append((raw, custom))
            i += 1
            continue
        # Setext 标题：标题行 + === / ---
        if (line.strip() and i + 1 < n and (i + 1) not in code_lines
                and _SETEXT_RE.match(lines[i + 1])
                and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line)
                and not line.lstrip().startswith(("|", "#", ">", "<", "    "))):
            heads.append((line.strip(), None))
            i += 2
            continue
        i += 1
    return heads


def build_anchors(text: str, code_lines: Set[int]) -> Tuple[List[str], Set[str]]:
    """生成文档可用的锚点集合。

    返回 (按出现顺序的 slug 列表, 显式锚点集合)。
    重复标题按 GitHub 规则追加 -1 / -2 ...
    """
    anchors: List[str] = []
    counts: Dict[str, int] = {}
    for raw, custom in extract_headings(text, code_lines):
        base = custom if custom else gh_slug(heading_plain(raw))
        if not base:
            continue
        if base not in counts:
            counts[base] = 0
            anchors.append(base)
            continue
        # 同名标题：追加 -1 / -2 ...，并避开已经存在的 id
        counts[base] += 1
        slug = f"{base}-{counts[base]}"
        while slug in counts:
            counts[base] += 1
            slug = f"{base}-{counts[base]}"
        counts[slug] = 0
        anchors.append(slug)

    explicit: Set[str] = set()
    for m in re.finditer(r"<(?:a|span|div|[a-z0-9]+)\b[^>]*?\b(?:name|id)\s*=\s*([\"'])([^\"']+)\1",
                         text, re.IGNORECASE):
        explicit.add(m.group(2))
    for m in _CUSTOM_ID_RE.finditer(text):
        explicit.add(m.group(1))
    return anchors, explicit


# --------------------------------------------------------------------------
# 链接提取
# --------------------------------------------------------------------------

# 目标部分：<尖括号包裹> 或 不含空白的序列（允许成对括号）
_DEST = r"(?:<[^<>\n]*>|(?:[^\s()\\]|\\.|\([^\s()]*\))*)"
_TITLE = r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?"
_IMG_RE = re.compile(r"!\[([^\[\]]*)\]\(\s*(" + _DEST + r")" + _TITLE + r"\s*\)")
_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(\s*(" + _DEST + r")" + _TITLE + r"\s*\)")
_REFDEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*(<[^>\n]*>|\S+)", re.MULTILINE)
_HTML_A_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_AUTOLINK_RE = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>", re.IGNORECASE)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _is_web_url(dest: str) -> bool:
    d = dest.lower()
    return d.startswith(("http://", "https://", "//"))


@dataclass
class Link:
    """一条被抽出来的链接。"""
    line: int                 # 1 基行号
    col: int                  # 1 基列号
    dest: str                  # 原始目标（已去掉尖括号）
    text: str                  # 链接文字 / alt / 引用 id
    kind: str                  # inline | image | reference | html-a | html-img | autolink
    raw: str = ""              # 原始匹配片段（用于报告展示）


def _blank_spans(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    if not spans:
        return text
    buf = list(text)
    for s, e in spans:
        for i in range(s, e):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def extract_links(masked: str, code_lines: Set[int]) -> List[Link]:
    """从遮罩后的文本里提取全部链接。

    分两轮：先抽图片，把图片片段遮掉，再抽普通链接 —— 这样
    ``[![alt](img.png)](url)`` 这种嵌套写法里外层链接也能被识别。
    """
    starts = [0]
    for i, ch in enumerate(masked):
        if ch == "\n":
            starts.append(i + 1)

    def pos(idx: int) -> Tuple[int, int]:
        ln = bisect.bisect_right(starts, idx) - 1
        return ln + 1, idx - starts[ln] + 1

    links: List[Link] = []
    img_spans: List[Tuple[int, int]] = []

    for m in _IMG_RE.finditer(masked):
        alt, dest = m.group(1), m.group(2)
        if not dest and not alt:
            continue
        ln, col = pos(m.start())
        links.append(Link(ln, col, _unwrap(dest), alt, "image", m.group(0)))
        img_spans.append((m.start(), m.end()))

    work = _blank_spans(masked, img_spans)

    for m in _LINK_RE.finditer(work):
        text, dest = m.group(1), m.group(2)
        ln, col = pos(m.start())
        links.append(Link(ln, col, _unwrap(dest), text, "inline", m.group(0)))

    for m in _REFDEF_RE.finditer(work):
        ln, col = pos(m.start())
        links.append(Link(ln, col, _unwrap(m.group(2)), m.group(1), "reference", m.group(0).strip()))

    for regex, kind in ((_HTML_A_RE, "html-a"), (_HTML_IMG_RE, "html-img")):
        for m in regex.finditer(work):
            ln, col = pos(m.start())
            links.append(Link(ln, col, m.group(2).strip(), "", kind, m.group(0)[:120]))

    for m in _AUTOLINK_RE.finditer(work):
        ln, col = pos(m.start())
        links.append(Link(ln, col, m.group(1), "", "autolink", m.group(0)))

    links.sort(key=lambda l: (l.line, l.col))
    return links


def _unwrap(dest: str) -> str:
    d = dest.strip()
    if len(d) >= 2 and d[0] == "<" and d[-1] == ">":
        d = d[1:-1].strip()
    return d


# --------------------------------------------------------------------------
# 结果模型
# --------------------------------------------------------------------------

@dataclass
class Issue:
    severity: str             # broken | warn
    code: str                 # 机器可读的问题类型
    file: str                 # 源文件（相对 root）
    line: int
    col: int
    dest: str
    text: str
    message: str
    hint: str = ""            # 修复建议

    def as_dict(self) -> dict:
        return {
            "severity": self.severity, "code": self.code, "file": self.file,
            "line": self.line, "col": self.col, "dest": self.dest,
            "text": self.text, "message": self.message, "hint": self.hint,
        }


@dataclass
class Doc:
    """一个 Markdown 文档的解析结果（带缓存，被多处引用时只解析一次）。"""
    path: Path
    rel: str
    text: str
    masked: str
    code_lines: Set[int]
    anchors: List[str]
    explicit: Set[str]
    links: List[Link] = field(default_factory=list)

    @property
    def anchor_set(self) -> Set[str]:
        return set(self.anchors) | self.explicit


# --------------------------------------------------------------------------
# 扫描与校验
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
        except OSError:
            break
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def rel_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


class Checker:
    def __init__(self, root: Path, *, check_anchors=True, check_case=True,
                 check_dirs=False, suggest=True, ignores: Optional[List[re.Pattern]] = None):
        self.root = root
        self.check_anchors = check_anchors
        self.check_case = check_case
        self.check_dirs = check_dirs
        self.suggest = suggest
        self.ignores = ignores or []
        self._docs: Dict[str, Doc] = {}

    # ---- 文档解析缓存 ----
    def doc(self, path: Path) -> Doc:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in self._docs:
            text = read_text(path)
            masked, code_lines = mask_fences(text)
            anchors, explicit = build_anchors(text, code_lines)
            doc = Doc(path, rel_to(path, self.root), text, masked, code_lines,
                      anchors, explicit)
            doc.links = extract_links(masked, code_lines)
            self._docs[key] = doc
        return self._docs[key]

    def ignored(self, link: Link, rel_file: str) -> bool:
        target = f"{rel_file}:{link.line} {link.dest}"
        return any(p.search(target) for p in self.ignores)

    # ---- 大小写逐段比对 ----
    def case_diff(self, target: Path) -> Optional[List[Tuple[str, str]]]:
        """若 target 的每段名字与磁盘上的实际大小写不一致，返回差异列表。

        注意：这里刻意**不做 resolve()** —— resolve 会把路径改写成磁盘上的
        真实大小写，那样就永远发现不了 docs/API.md 与 docs/api.md 的差异。
        """
        if not self.check_case:
            return None
        try:
            rel_str = os.path.relpath(os.path.abspath(str(target)), str(self.root))
        except Exception:
            return None
        if rel_str.startswith(".."):
            return None
        cur = self.root
        diffs: List[Tuple[str, str]] = []
        for part in Path(rel_str).parts:
            if part in (".", ".."):
                return None
            try:
                entries = os.listdir(cur)
            except OSError:
                return None
            if part in entries:
                cur = cur / part
                continue
            lowered = {e.lower(): e for e in entries}
            if part.lower() in lowered:
                actual = lowered[part.lower()]
                diffs.append((part, actual))
                cur = cur / actual
            else:
                return None
        return diffs or None

    def file_suggestion(self, target: Path) -> str:
        """目标不存在时，在同级目录里找一个名字最接近的文件。"""
        if not self.suggest:
            return ""
        parent = target.parent
        try:
            names = os.listdir(parent)
        except OSError:
            return ""
        wanted = target.name
        cands = difflib.get_close_matches(wanted, names, n=1, cutoff=0.6)
        if not cands:
            # 也试试忽略大小写
            low = {n.lower(): n for n in names}
            cands = difflib.get_close_matches(wanted.lower(), list(low), n=1, cutoff=0.6)
            if cands:
                return f"是不是 {low[cands[0]]} ？"
            return ""
        return f"是不是 {cands[0]} ？"

    def anchor_suggestion(self, doc: Doc, frag: str) -> str:
        if not self.suggest:
            return ""
        pool = list(doc.anchor_set)
        cands = difflib.get_close_matches(frag, pool, n=1, cutoff=0.5)
        if cands:
            return f"最接近的锚点是 #{cands[0]}"
        return ""

    # ---- 单条链接校验 ----
    def check_link(self, link: Link, doc: Doc) -> List[Issue]:
        dest = link.dest.strip()
        rel_file = doc.rel

        def mk(sev: str, code: str, msg: str, hint: str = "") -> Issue:
            return Issue(sev, code, rel_file, link.line, link.col,
                         link.dest, link.text, msg, hint)

        if not dest:
            return [mk(SEV_BROKEN, "empty-dest", "链接目标为空")]

        if _SCHEME_RE.match(dest):
            return []          # 外部链接交给外链探测阶段
        if dest.startswith("//"):
            return []

        # 纯锚点：#section
        if dest.startswith("#"):
            frag = unquote(dest[1:])
            if not frag:
                return [mk(SEV_WARN, "empty-anchor", "锚点为空（# 后面什么都没有）")]
            if not self.check_anchors:
                return []
            if frag in doc.anchor_set:
                return []
            msg = f"当前文档里没有锚点 #{frag}"
            return [mk(SEV_BROKEN, "missing-anchor", msg,
                       self.anchor_suggestion(doc, frag))]

        path_part, frag = _split_dest(dest)
        decoded = unquote(path_part)

        if decoded.startswith("/"):
            target = self.root / decoded.lstrip("/")
        else:
            target = doc.path.parent / decoded

        issues: List[Issue] = []

        if not target.exists():
            issues.append(mk(SEV_BROKEN, "missing-target",
                             f"目标不存在：{rel_to(target, self.root)}",
                             self.file_suggestion(target)))
            return issues

        if target.is_dir():
            if self.check_dirs:
                issues.append(mk(SEV_WARN, "dir-target",
                                 "链接指向目录，GitHub 上会打开目录列表"))
            return issues

        diffs = self.case_diff(target)
        if diffs:
            pairs = "、".join(f"{a} → {b}" for a, b in diffs)
            issues.append(mk(SEV_WARN, "case-mismatch",
                             "大小写与磁盘实际文件名不一致（Linux/GitHub 上会 404）",
                             pairs))

        # 锚点校验只对 Markdown 目标做
        if self.check_anchors and frag and target.suffix.lower() in MD_EXTS:
            target_doc = self.doc(target)
            want = unquote(frag)
            if want not in target_doc.anchor_set:
                issues.append(mk(SEV_BROKEN, "missing-anchor",
                                 f"{target_doc.rel} 里没有锚点 #{want}",
                                 self.anchor_suggestion(target_doc, want)))

        return issues


def _split_dest(dest: str) -> Tuple[str, str]:
    """拆出 (路径, fragment)，丢掉 query。"""
    d = dest
    frag = ""
    if "#" in d:
        d, frag = d.split("#", 1)
    if "?" in d:
        d = d.split("?", 1)[0]
    return d, frag


# --------------------------------------------------------------------------
# 外链探测
# --------------------------------------------------------------------------

def probe_url(url: str, timeout: float) -> Tuple[Optional[int], str]:
    """探测一个 URL，返回 (状态码 或 None, 备注)。"""
    def fetch(method: str) -> Tuple[Optional[int], str]:
        req = Request(url, method=method, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                if method == "GET":
                    resp.read(2048)      # 只读一点点，避免下载大文件
                return resp.status, ""
        except HTTPError as e:
            return e.code, str(e.reason or "")
        except URLError as e:
            return None, str(getattr(e, "reason", e))
        except Exception as e:                     # 超时 / SSL / 连接重置
            return None, f"{type(e).__name__}: {e}"

    code, note = fetch("HEAD")
    if code is None or code in SOFT_STATUS:
        code2, note2 = fetch("GET")
        if code2 is not None:
            return code2, note2
        return code, note
    return code, note


def _short_note(note: str) -> str:
    """把底层网络异常压缩成人能看懂的一句话。"""
    raw = note.strip()
    low = raw.lower()
    if "certificate_verify" in low or "certificate verify" in low:
        return "SSL 证书校验失败"
    if "timed out" in low or "timeout" in low:
        return "连接超时"
    if ("name or service not known" in low or "getaddrinfo" in low
            or "nodename nor servname" in low or "no address" in low):
        return "域名解析失败"
    if "connection refused" in low:
        return "连接被拒绝"
    if "connection reset" in low or "reset by peer" in low:
        return "连接被重置"
    if "no route to host" in low or "network is unreachable" in low:
        return "网络不可达"
    return _short(raw, 48) or "网络错误"


def classify_status(code: Optional[int], note: str) -> Tuple[str, str]:
    """把探测结果翻译成 (severity, 说明)；severity 为空表示通过。"""
    if code is None:
        return SEV_WARN, f"无法连接（{_short_note(note)}）—— 也可能是网络限制/站点屏蔽"
    if code in (404, 410):
        return SEV_BROKEN, f"HTTP {code}（链接已失效）"
    if code in (401, 403, 405, 429, 503, 999):
        return SEV_WARN, f"HTTP {code}（站点拒绝自动探测，需人工确认）"
    if 400 <= code < 600:
        return SEV_WARN, f"HTTP {code}"
    return "", ""


# --------------------------------------------------------------------------
# 扫描主流程
# --------------------------------------------------------------------------

@dataclass
class Report:
    issues: List[Issue] = field(default_factory=list)
    files: int = 0
    links_total: int = 0
    links_local: int = 0
    links_external: int = 0
    links_anchors: int = 0
    elapsed: float = 0.0
    root: str = ""
    checked_external: bool = False

    @property
    def broken(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == SEV_BROKEN]

    @property
    def warns(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == SEV_WARN]


def collect_files(paths: Sequence[str], excludes: Sequence[str], root: Path) -> Tuple[List[Path], List[str]]:
    """收集待扫描的 Markdown 文件。excludes 支持目录名与 glob。"""
    found: Dict[str, Path] = {}
    missing: List[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            abs_p = Path(os.path.abspath(str(p)))
            found[os.path.normcase(str(abs_p))] = abs_p
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames
                                     if not _excluded_dir(d, Path(dirpath) / d, root, excludes))
                for fn in filenames:
                    if Path(fn).suffix.lower() not in MD_EXTS:
                        continue
                    fp = Path(os.path.abspath(str(Path(dirpath) / fn)))
                    rel = rel_to(fp, root)
                    if any(fnmatch.fnmatch(rel, pat) for pat in excludes if "*" in pat):
                        continue
                    found[os.path.normcase(str(fp))] = fp
        else:
            missing.append(raw)
    return sorted(found.values(), key=lambda x: rel_to(x, root).lower()), missing


def _excluded_dir(name: str, path: Path, root: Path, excludes: Sequence[str]) -> bool:
    if name in DEFAULT_EXCLUDES or name in excludes:
        return True
    rel = rel_to(path, root)
    return any(fnmatch.fnmatch(rel, pat) for pat in excludes if "*" in pat)


def run_scan(paths, excludes, checker: Checker, *, external=False,
             timeout=8.0, workers=12, quiet=False) -> Report:
    t0 = time.time()
    files, missing = collect_files(paths, excludes, checker.root)
    rep = Report(root=str(checker.root), checked_external=external)
    rep.files = len(files)

    for raw in missing:
        if not quiet:
            print(Palette.paint(f"! 路径不存在，已跳过：{raw}", "yellow"), file=sys.stderr)

    ext_map: Dict[str, List[Tuple[Doc, Link]]] = {}

    for fp in files:
        doc = checker.doc(fp)
        for link in doc.links:
            if checker.ignored(link, doc.rel):
                continue
            rep.links_total += 1
            dest = link.dest.strip()
            if dest.startswith("#"):
                rep.links_anchors += 1
            elif _is_web_url(dest):
                rep.links_external += 1
                if dest.lower().startswith(("http://", "https://")):
                    ext_map.setdefault(dest, []).append((doc, link))
                continue
            elif _SCHEME_RE.match(dest) or dest.startswith("//"):
                # mailto: / tel: / data: 等，只统计不校验
                continue
            else:
                rep.links_local += 1
            rep.issues.extend(checker.check_link(link, doc))

    # 外部链接：去重后并发探测
    if external and ext_map:
        urls = list(ext_map.keys())
        if not quiet:
            print(Palette.paint(f"· 正在探测 {len(urls)} 个外部链接（{workers} 并发）…", "gray"),
                  file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda u: probe_url(u, timeout), urls))
        for url, (code, note) in zip(urls, results):
            sev, msg = classify_status(code, note)
            if not sev:
                continue
            for doc, link in ext_map[url]:
                rep.issues.append(Issue(sev, "http-status" if code else "unreachable",
                                        doc.rel, link.line, link.col, url, link.text, msg))

    rep.issues.sort(key=lambda i: (i.severity != SEV_BROKEN, i.file, i.line, i.col))
    rep.elapsed = time.time() - t0
    return rep


# --------------------------------------------------------------------------
# 报告输出
# --------------------------------------------------------------------------

def _short(s: str, n: int = 72) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _pad(s: str, width: int) -> str:
    """按显示宽度右填充（中文按 2 列估算）。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, width - w)


def render_text(rep: Report, *, only_broken=False) -> str:
    out: List[str] = []
    P = Palette.paint
    out.append(P(f"{TOOL_NAME} v{__version__}", "bold", "bcyan")
               + P(f" · {rep.files} 个文件 · {rep.links_total} 条链接", "gray"))
    out.append(P(f"  {rep.links_local} 本地 / {rep.links_anchors} 锚点 / "
                 f"{rep.links_external} 外链", "gray"))
    out.append("")

    broken, warns = rep.broken, rep.warns

    # 位置列宽度自适应，保证至少两个空格的分隔
    locs = [f"{i.file}:{i.line}" for i in rep.issues]
    loc_w = min(max([len(s) for s in locs] + [26]) + 2, 64)
    indent = " " * (loc_w + 2)

    def block(items: List[Issue], color: str) -> None:
        for i in items:
            loc = f"{i.file}:{i.line}"
            out.append(f"  {P(_pad(loc, loc_w), 'bcyan')}{P(i.code, color)}")
            out.append(f"{indent}{P('→ ' + (i.dest or '(空)'), 'dim')}")
            out.append(f"{indent}{i.message}")
            if i.hint:
                out.append(f"{indent}{P('↳ ' + i.hint, 'byellow')}")

    if broken:
        out.append(P(f"✗ 断链 ({len(broken)})", "bold", "bred"))
        block(broken, "red")
        out.append("")

    if warns and not only_broken:
        out.append(P(f"! 警告 ({len(warns)})", "bold", "byellow"))
        block(warns, "yellow")
        out.append("")

    passed = rep.links_total - len(broken) - len(warns)
    out.append(f"合计 {rep.links_total} · {P(f'通过 {passed}', 'green')} · "
               f"{P(f'断链 {len(broken)}', 'bred' if broken else 'gray')} · "
               f"{P(f'警告 {len(warns)}', 'byellow' if warns else 'gray')}")
    out.append(P(f"耗时 {rep.elapsed:.2f}s · 根目录 {rep.root}"
                 + ("  ·  已检查外链" if rep.checked_external else ""), "gray"))

    if not broken and not warns:
        out.insert(2, P("✓ 所有链接都正常", "bold", "bgreen") + "\n")
    return "\n".join(out)


def render_markdown(rep: Report) -> str:
    lines = [f"# {TOOL_NAME} 链接体检报告", ""]
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 扫描根目录：`{rep.root}`")
    lines.append(f"- 文件：{rep.files} 个 ｜ 链接：{rep.links_total} 条")
    lines.append(f"- 断链：**{len(rep.broken)}** ｜ 警告：{len(rep.warns)}")
    lines.append("")
    if rep.issues:
        lines.append("| 严重度 | 位置 | 类型 | 目标 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i in rep.issues:
            sev = "断链" if i.severity == SEV_BROKEN else "警告"
            dest = (i.dest or "(空)").replace("|", "\\|")
            msg = (i.message + (f"（{i.hint}）" if i.hint else "")).replace("|", "\\|")
            lines.append(f"| {sev} | `{i.file}:{i.line}` | {i.code} | `{_short(dest, 60)}` | {msg} |")
    else:
        lines.append("✓ 没有发现任何问题。")
    lines.append("")
    lines.append(f"> 由 {TOOL_NAME} v{__version__} 生成")
    return "\n".join(lines)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
  :root {{
    --bg:#0b0e14; --card:#151b27; --border:#232b3a; --text:#e6edf3;
    --dim:#9aa7b8; --faint:#66748a; --accent:#5eead4; --red:#fb7185;
    --yellow:#fbbf24; --green:#4ade80;
    --mono: "SFMono-Regular", Consolas, Menlo, monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.6; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:40px 24px 80px; }}
  h1 {{ font-size:26px; margin:0 0 6px; letter-spacing:.3px; }}
  .sub {{ color:var(--faint); font-size:13px; font-family:var(--mono); margin-bottom:28px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; margin-bottom:30px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px 18px; }}
  .card .k {{ font-size:12px; color:var(--faint); letter-spacing:.5px; }}
  .card .v {{ font-size:28px; font-weight:700; font-family:var(--mono); margin-top:4px; }}
  .card.ok .v {{ color:var(--green); }}
  .card.bad .v {{ color:var(--red); }}
  .card.warn .v {{ color:var(--yellow); }}
  h2 {{ font-size:16px; margin:32px 0 12px; color:var(--dim); font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:var(--faint); font-weight:500; padding:8px 10px;
        border-bottom:1px solid var(--border); font-size:12px; letter-spacing:.4px; }}
  td {{ padding:9px 10px; border-bottom:1px solid rgba(35,43,58,.6); vertical-align:top; }}
  tr:hover td {{ background:rgba(94,234,212,.03); }}
  code {{ font-family:var(--mono); font-size:12px; color:var(--accent); }}
  .tag {{ display:inline-block; font-size:11px; padding:1px 8px; border-radius:999px; font-family:var(--mono); }}
  .tag.broken {{ background:rgba(251,113,133,.14); color:var(--red); }}
  .tag.warn {{ background:rgba(251,191,36,.14); color:var(--yellow); }}
  .dest {{ font-family:var(--mono); font-size:12px; color:var(--dim); word-break:break-all; }}
  .hint {{ color:var(--yellow); font-size:12px; }}
  .empty {{ text-align:center; padding:60px 0; color:var(--green); font-size:15px; }}
  footer {{ margin-top:40px; color:var(--faint); font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="cards">
    <div class="card"><div class="k">文件</div><div class="v">{files}</div></div>
    <div class="card"><div class="k">链接总数</div><div class="v">{total}</div></div>
    <div class="card ok"><div class="k">通过</div><div class="v">{passed}</div></div>
    <div class="card bad"><div class="k">断链</div><div class="v">{broken}</div></div>
    <div class="card warn"><div class="k">警告</div><div class="v">{warns}</div></div>
  </div>
  {body}
  <footer>由 {tool} v{version} 生成 · {time}</footer>
</div>
</body>
</html>
"""


def render_html(rep: Report, title: str = "链接体检报告") -> str:
    rows: List[str] = []
    if rep.issues:
        rows.append("<h2>问题清单</h2><table><thead><tr>"
                    "<th>严重度</th><th>位置</th><th>类型</th><th>目标</th><th>说明</th>"
                    "</tr></thead><tbody>")
        for i in rep.issues:
            sev = "broken" if i.severity == SEV_BROKEN else "warn"
            label = "断链" if sev == "broken" else "警告"
            hint = f'<div class="hint">↳ {html_mod.escape(i.hint)}</div>' if i.hint else ""
            rows.append(
                "<tr>"
                f'<td><span class="tag {sev}">{label}</span></td>'
                f"<td><code>{html_mod.escape(i.file)}:{i.line}</code></td>"
                f"<td>{html_mod.escape(i.code)}</td>"
                f'<td><div class="dest">{html_mod.escape(_short(i.dest, 120) or "(空)")}</div></td>'
                f"<td>{html_mod.escape(i.message)}{hint}</td>"
                "</tr>")
        rows.append("</tbody></table>")
    else:
        rows.append('<div class="empty">✓ 没有发现任何断链或可疑链接</div>')

    passed = max(0, rep.links_total - len(rep.broken) - len(rep.warns))
    return HTML_TEMPLATE.format(
        title=html_mod.escape(title),
        subtitle=html_mod.escape(f"{rep.root}  ·  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
        files=rep.files, total=rep.links_total, passed=passed,
        broken=len(rep.broken), warns=len(rep.warns),
        body="\n  ".join(rows),
        tool=TOOL_NAME, version=__version__,
        time=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
    )


def render_json(rep: Report) -> str:
    return json.dumps({
        "tool": TOOL_NAME,
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": rep.root,
        "summary": {
            "files": rep.files,
            "links": rep.links_total,
            "local": rep.links_local,
            "anchors": rep.links_anchors,
            "external": rep.links_external,
            "broken": len(rep.broken),
            "warnings": len(rep.warns),
            "elapsed_seconds": round(rep.elapsed, 3),
            "external_checked": rep.checked_external,
        },
        "issues": [i.as_dict() for i in rep.issues],
    }, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Markdown 链接与锚点体检：断链、锚点、大小写、外链一次查完。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  mdlink .                          # 扫描当前目录所有 Markdown
  mdlink README.md docs/            # 只查指定文件/目录
  mdlink . -x --timeout 10          # 同时探测外部链接
  mdlink . --format html --out r.html
  mdlink . --ignore "localhost|127.0.0.1"
  mdlink . --list-anchors README.md # 调试：看某文件有哪些锚点

退出码: 0 全部通过 / 1 存在断链（或 --strict 下存在警告）/ 2 使用错误
""")
    p.add_argument("paths", nargs="*", default=["."], help="待扫描的文件或目录（默认当前目录）")
    p.add_argument("-x", "--external", action="store_true", help="额外探测外部 http(s) 链接")
    p.add_argument("--timeout", type=float, default=8.0, help="外链探测超时秒数（默认 8）")
    p.add_argument("--workers", type=int, default=12, help="外链探测并发数（默认 12）")
    p.add_argument("--no-anchors", action="store_true", help="跳过锚点校验")
    p.add_argument("--no-case", action="store_true", help="跳过文件名大小写校验")
    p.add_argument("--dirs", action="store_true", help="把「指向目录」的链接也记为警告")
    p.add_argument("--no-suggest", action="store_true", help="不给出修复建议")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="跳过目录名或 glob（可多次使用）")
    p.add_argument("--ignore", action="append", default=[], metavar="REGEX",
                   help="忽略匹配该正则的链接（可多次使用）")
    p.add_argument("--ignore-file", metavar="FILE", help="从文件读取忽略规则（每行一条正则）")
    p.add_argument("-f", "--format", choices=["text", "json", "md", "html"], default="text",
                   help="报告格式（默认 text）")
    p.add_argument("-o", "--out", metavar="FILE", help="把报告写入文件")
    p.add_argument("--only-broken", action="store_true", help="终端输出里不列出警告")
    p.add_argument("--strict", action="store_true", help="把警告也视为失败")
    p.add_argument("-q", "--quiet", action="store_true", help="不输出进度信息")
    p.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    p.add_argument("--root", metavar="DIR", help="链接解析与展示的根目录（默认当前目录）")
    p.add_argument("--list-anchors", metavar="FILE", help="打印某个 Markdown 文件的所有锚点后退出")
    p.add_argument("-V", "--version", action="version", version=f"{TOOL_NAME} {__version__}")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()

    # 着色开关：写文件或非 text 格式时不带颜色码
    if args.no_color or args.out or args.format != "text" or os.environ.get("NO_COLOR"):
        Palette.enabled = False
    else:
        enable_ansi()
        Palette.enabled = sys.stdout.isatty()

    # 调试：列出锚点
    if args.list_anchors:
        path = Path(args.list_anchors)
        if not path.is_file():
            print(f"文件不存在：{path}", file=sys.stderr)
            return 2
        text = read_text(path)
        masked, code_lines = mask_fences(text)
        anchors, explicit = build_anchors(text, code_lines)
        print(f"# {rel_to(path, root)}")
        for a in anchors:
            print(f"  #{a}")
        for a in sorted(explicit - set(anchors)):
            print(f"  #{a}   (显式锚点)")
        return 0

    ignores: List[re.Pattern] = []
    for pat in args.ignore:
        try:
            ignores.append(re.compile(pat))
        except re.error as e:
            print(f"忽略规则不是合法正则：{pat}（{e}）", file=sys.stderr)
            return 2
    if args.ignore_file:
        try:
            for line in Path(args.ignore_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    ignores.append(re.compile(line))
                except re.error as e:
                    print(f"忽略规则不是合法正则：{line}（{e}）", file=sys.stderr)
                    return 2
        except OSError as e:
            print(f"读取忽略文件失败：{e}", file=sys.stderr)
            return 2

    checker = Checker(
        root,
        check_anchors=not args.no_anchors,
        check_case=not args.no_case,
        check_dirs=args.dirs,
        suggest=not args.no_suggest,
        ignores=ignores,
    )

    paths = args.paths or ["."]
    rep = run_scan(paths, args.exclude, checker,
                   external=args.external, timeout=args.timeout,
                   workers=args.workers, quiet=args.quiet)

    if args.format == "text":
        output = render_text(rep, only_broken=args.only_broken)
    elif args.format == "json":
        output = render_json(rep)
    elif args.format == "md":
        output = render_markdown(rep)
    else:
        output = render_html(rep, title=f"{TOOL_NAME} 链接体检报告")

    if args.out:
        try:
            Path(args.out).write_text(output + "\n", encoding="utf-8")
        except OSError as e:
            print(f"写入报告失败：{e}", file=sys.stderr)
            return 2
        if not args.quiet:
            print(Palette.paint(f"报告已写入 {args.out}", "green"))
    else:
        print(output)

    if rep.broken:
        return 1
    if args.strict and rep.warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
