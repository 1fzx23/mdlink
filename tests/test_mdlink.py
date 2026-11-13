#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mdlink 的单元测试（只用标准库 unittest）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_mdlink.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mdlink  # noqa: E402


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(mdlink.gh_slug("Hello World"), "hello-world")

    def test_punctuation(self):
        self.assertEqual(mdlink.gh_slug("What's new?"), "whats-new")
        self.assertEqual(mdlink.gh_slug("foo.bar/baz"), "foobarbaz")

    def test_underscore_kept(self):
        self.assertEqual(mdlink.gh_slug("A_B C"), "a_b-c")

    def test_cjk(self):
        self.assertEqual(mdlink.gh_slug("读取文件"), "读取文件")
        self.assertEqual(mdlink.gh_slug("大小写在 Windows 上没问题？"),
                         "大小写在-windows-上没问题")

    def test_heading_plain(self):
        self.assertEqual(mdlink.heading_plain("**粗体** 和 `代码`"), "粗体 和 代码")
        self.assertEqual(mdlink.heading_plain("[链接](http://x) 后面"), "链接 后面")


class TestMask(unittest.TestCase):
    def test_inline_code_masked_same_length(self):
        src = "a `b c` d"
        out = mdlink.mask_inline_code(src)
        self.assertEqual(len(src), len(out))
        self.assertNotIn("b c", out)

    def test_unclosed_backtick_kept(self):
        src = "a ` b"
        self.assertEqual(mdlink.mask_inline_code(src), src)

    def test_fence_lines(self):
        text = "before\n```elixir\n[bad](x.md)\n```\nafter\n"
        masked, code_lines = mdlink.mask_fences(text)
        self.assertEqual(code_lines, {1, 2, 3})
        self.assertNotIn("bad", masked)
        self.assertIn("before", masked)


class TestExtractLinks(unittest.TestCase):
    def _links(self, text):
        masked, code_lines = mdlink.mask_fences(text)
        return mdlink.extract_links(masked, code_lines)

    def test_inline(self):
        links = self._links("看 [这里](./a.md) 和 [那些](b.md#sec)。")
        self.assertEqual([l.dest for l in links], ["./a.md", "b.md#sec"])
        self.assertEqual(links[0].text, "这里")
        self.assertEqual(links[0].line, 1)

    def test_image_and_nested(self):
        links = self._links("[![alt](img.png)](page.md)")
        dests = sorted(l.dest for l in links)
        self.assertEqual(dests, ["img.png", "page.md"])
        kinds = {l.dest: l.kind for l in links}
        self.assertEqual(kinds["img.png"], "image")

    def test_reference_and_html(self):
        text = ("[ref][id]\n\n[id]: ./target.md\n"
                '<a href="https://x.com">x</a> <img src="p.png">\n')
        links = self._links(text)
        dests = [l.dest for l in links]
        self.assertIn("./target.md", dests)
        self.assertIn("https://x.com", dests)
        self.assertIn("p.png", dests)

    def test_autolink(self):
        links = self._links("见 <https://example.com/a> 和 <mailto:a@b.c>")
        self.assertEqual([l.dest for l in links],
                         ["https://example.com/a", "mailto:a@b.c"])

    def test_code_block_ignored(self):
        text = "```md\n[fake](nope.md)\n```\n"
        self.assertEqual(self._links(text), [])

    def test_angle_bracket_dest(self):
        links = self._links("[spec](<my file.md>)")
        self.assertEqual(links[0].dest, "my file.md")


class TestAnchors(unittest.TestCase):
    def _anchors(self, text):
        masked, code_lines = mdlink.mask_fences(text)
        return mdlink.build_anchors(text, code_lines)

    def test_headings(self):
        anchors, _ = self._anchors("# 一级\n\n## 二级 标题\n\n### Deep One\n")
        self.assertEqual(anchors, ["一级", "二级-标题", "deep-one"])

    def test_duplicates(self):
        anchors, _ = self._anchors("## A\n## A\n## A\n")
        self.assertEqual(anchors, ["a", "a-1", "a-2"])

    def test_custom_id(self):
        anchors, explicit = self._anchors("## 标题 {#custom-id}\n")
        self.assertEqual(anchors, ["custom-id"])
        self.assertIn("custom-id", explicit)

    def test_html_anchor(self):
        _, explicit = self._anchors('<a name="manual"></a>\n')
        self.assertIn("manual", explicit)

    def test_heading_in_code_block_ignored(self):
        anchors, _ = self._anchors("```\n# not a heading\n```\n# real\n")
        self.assertEqual(anchors, ["real"])


class TestEndToEnd(unittest.TestCase):
    """在临时目录里造一个小仓库，跑完整流程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "assets").mkdir()
        (self.root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n")
        (self.root / "docs" / "guide.md").write_text(
            "# Guide\n\n## 快速安装\n\n## FAQ\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, body, **kw):
        (self.root / "README.md").write_text(body, encoding="utf-8")
        checker = mdlink.Checker(self.root, **kw)
        return mdlink.run_scan([str(self.root)], [], checker, quiet=True)

    def test_all_good(self):
        rep = self._scan(
            "[guide](docs/guide.md)\n"
            "[sec](docs/guide.md#快速安装)\n"
            "[same](#top)\n"
            "![logo](assets/logo.png)\n"
            "\n# top\n")
        self.assertEqual(rep.broken, [])
        self.assertEqual(rep.warns, [])

    def test_missing_target(self):
        rep = self._scan("[gone](docs/nope.md)\n")
        self.assertEqual(len(rep.broken), 1)
        self.assertEqual(rep.broken[0].code, "missing-target")

    def test_missing_anchor(self):
        rep = self._scan("[bad](docs/guide.md#不存在)\n")
        self.assertEqual(len(rep.broken), 1)
        self.assertEqual(rep.broken[0].code, "missing-anchor")

    def test_case_mismatch(self):
        rep = self._scan("[c](docs/GUIDE.md)\n")
        codes = [i.code for i in rep.issues]
        # Windows 能打开，但要提醒 GitHub 上会 404
        if os.name == "nt":
            self.assertIn("case-mismatch", codes)
        self.assertEqual(rep.broken, [])

    def test_empty_dest(self):
        rep = self._scan("[empty]()\n")
        self.assertEqual(rep.broken[0].code, "empty-dest")

    def test_ignore_rule(self):
        (self.root / "README.md").write_text("[gone](nope.md)\n", encoding="utf-8")
        checker = mdlink.Checker(self.root, ignores=[mdlink.re.compile("nope")])
        rep = mdlink.run_scan([str(self.root)], [], checker, quiet=True)
        self.assertEqual(rep.broken, [])

    def test_json_render(self):
        rep = self._scan("[gone](nope.md)\n")
        data = mdlink.json.loads(mdlink.render_json(rep))
        self.assertEqual(data["summary"]["broken"], 1)
        self.assertEqual(data["issues"][0]["code"], "missing-target")

    def test_html_render_escapes(self):
        rep = self._scan('[x]("><script>alert(1)</script>.md)\n')
        out = mdlink.render_html(rep)
        self.assertNotIn("<script>alert(1)</script>", out)

    def test_exclude_dir(self):
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "v.md").write_text("[bad](nope.md)\n", encoding="utf-8")
        rep = self._scan("# ok\n")
        self.assertEqual(rep.files, 2)      # README + docs/guide.md
        self.assertEqual(rep.broken, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
