# mdlink

**Markdown 链接与锚点体检工具** —— 一条命令查完文档里的断链、跑掉的锚点、大小写不一致的文件名，以及失效的外链。

零第三方依赖，纯 Python 标准库，Windows / macOS / Linux 通用。

```
$ mdlink .

mdlink v1.0.0 · 3 个文件 · 19 条链接
  18 本地 / 0 锚点 / 1 外链

✗ 断链 (7)
  examples/messy/README.md:15  missing-target
                               → docs/deploy.md
                               目标不存在：examples/messy/docs/deploy.md
  examples/messy/README.md:17  missing-anchor
                               → docs/guide.md#配置文件参考
                               examples/messy/docs/guide.md 里没有锚点 #配置文件参考
  examples/messy/README.md:19  empty-dest
                               → (空)
                               链接目标为空
  ...

! 警告 (2)
  examples/messy/README.md:16  case-mismatch
                               → docs/API.md
                               大小写与磁盘实际文件名不一致（Linux/GitHub 上会 404）
                               ↳ API.md → api.md

合计 19 · 通过 10 · 断链 7 · 警告 2
耗时 0.12s · 根目录 /home/me/project
```

---

## 为什么又造一个链接检查器？

现成的工具大多只做一件事：联网确认 URL 能不能打开。但写文档时真正坑人的是这些：

| 坑 | 典型场景 | mdlink |
| --- | --- | --- |
| **相对路径写错** | 挪了个目录，`./api.md` 变成 `../api.md` 忘了改 | ✅ 直接定位到 文件:行 |
| **锚点跑掉** | 标题从「安装步骤」改成「快速安装」，`#安装步骤` 全废 | ✅ 按 GitHub slug 规则重新算，还给最接近的建议 |
| **大小写不一致** | Windows/macOS 上完全正常，推到 GitHub 变 404 | ✅ 逐段比对磁盘上的真实文件名 |
| **把代码里的示例当真链接** | README 里写了 `[示例](foo.md)` 的用法演示 | ✅ 自动遮罩代码块与行内代码，不误报 |
| **外链悄悄失效** | 引用的官方文档改版了 | ✅ `-x` 多线程探测，404 与「拒绝爬虫」分开处理 |
| **中文标题锚点** | `## 快速安装` → `#快速安装` | ✅ 支持 CJK，按 GitHub 规则处理 |

另外还有两个体验上的取舍：

- **默认不联网**。不加 `-x` 时一个请求都不发，CI 里跑完全确定、不会因为网络抖动飘红。
- **指向目录不算错**。`[文档](./docs)` 在 GitHub 上是合法的，默认放行（想查加 `--dirs`）。

---

## 安装

不需要安装依赖，直接跑：

```bash
git clone https://github.com/1fzx23/mdlink.git
cd mdlink
python mdlink.py .
```

想加进 PATH：

```bash
pipx install .          # 推荐
# 或者
pip install .
mdlink --version
```

要求 Python 3.9+。

---

## 快速上手

```bash
# 扫当前目录所有 Markdown
mdlink .

# 只查指定文件 / 目录
mdlink README.md docs/

# 顺便探测外部链接（12 并发，8 秒超时）
mdlink . -x

# 出 HTML 报告
mdlink . -f html -o link-report.html

# 忽略本地开发用的地址
mdlink . -x --ignore "localhost|127\.0\.0\.1|example\.com"
```

### 修复前先看建议

```
$ mdlink README.md

✗ 断链 (1)
  README.md:42  missing-anchor
                → docs/guide.md#配置文件
                docs/guide.md 里没有锚点 #配置文件
                ↳ 最接近的锚点是 #配置项
```

文件名写错时同理，会给一个 `是不是 xxx ？` 的提示。

### 调试某个文件有哪些锚点

不确定锚点该怎么写？直接问工具：

```
$ mdlink --list-anchors docs/guide.md
# docs/guide.md
  #使用指南
  #快速安装
  #进阶用法
  #常见问题
  #为什么锚点对不上
  #大小写在-windows-上没问题
```

---

## 能查出的问题类型

| code | 级别 | 含义 |
| --- | --- | --- |
| `missing-target` | 断链 | 相对/绝对路径指向的文件不存在 |
| `missing-anchor` | 断链 | 目标文件里没有这个 `#锚点` |
| `empty-dest` | 断链 | `[文字]()` 目标为空 |
| `empty-anchor` | 警告 | 只有一个 `#` |
| `case-mismatch` | 警告 | 大小写与磁盘实际文件名不一致 |
| `dir-target` | 警告 | 链接指向目录（`--dirs` 时才报） |
| `http-status` | 断链/警告 | 外链返回 404/410 为断链，其余 4xx/5xx 为警告 |
| `unreachable` | 警告 | 连不上（DNS / 超时 / SSL / 被墙），可能是网络问题 |

`missing-anchor` 和 `case-mismatch` 都带修复建议。

---

## 支持的链接写法

- 行内链接 `[文字](目标 "标题")`
- 图片 `![alt](图片.png)`，包括嵌套写法 `[![alt](i.png)](页面.md)`
- 引用式定义 `[id]: ./target.md`
- HTML `<a href="...">` / `<img src="...">`
- 自动链接 `<https://example.com>`
- 目标用尖括号包裹 `[x](<路径 带空格.md>)`
- URL 编码的路径（会自动 `unquote`）
- 表格、列表、引用块里的链接

**以下内容会被自动跳过**（不会误报）：

- ` ``` ` / `~~~` 围起来的代码块
- 行内代码 `` `[示例](foo.md)` ``
- `mailto:` / `tel:` / `data:` 等非 HTTP 协议（只统计，不校验）

---

## 锚点是怎么算的

按 GitHub 的规则：

1. 取标题文本，去掉 Markdown 语法（`**加粗**`、`` `代码` ``、链接只留文字）和 HTML 标签
2. 全部转小写
3. 去掉所有标点，**保留**字母、数字、下划线、连字符、空格、中文/日文/韩文
4. 每个空格替换成一个 `-`
5. 同名标题依次追加 `-1`、`-2`……

例如：

| 标题 | 锚点 |
| --- | --- |
| `## What's new?` | `#whats-new` |
| `## 快速安装` | `#快速安装` |
| `## 大小写在 Windows 上没问题` | `#大小写在-windows-上没问题` |
| `## 配置项 {#config}` | `#config` |

显式锚点也认：`<a name="manual"></a>`、`<span id="manual"></span>`、`{#custom-id}`。

---

## 命令行参数

```
mdlink [路径...] [选项]

  -x, --external         额外探测外部 http(s) 链接
  --timeout SEC          外链探测超时（默认 8）
  --workers N            外链探测并发数（默认 12）
  --no-anchors           跳过锚点校验
  --no-case              跳过大小写校验
  --dirs                 把「指向目录」也记为警告
  --no-suggest           不输出修复建议
  --exclude PATTERN      跳过目录名或 glob（可多次）
  --ignore REGEX         忽略匹配的链接（可多次）
  --ignore-file FILE     从文件读忽略规则，每行一条正则
  -f, --format FMT       text（默认）| json | md | html
  -o, --out FILE         报告写入文件
  --only-broken          终端输出不列警告
  --strict               警告也算失败
  -q, --quiet            不打印进度
  --no-color             禁用颜色
  --root DIR             链接解析 / 展示的根目录
  --list-anchors FILE    打印某文件的全部锚点
  -V, --version          版本
```

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 全部通过 |
| `1` | 存在断链（`--strict` 下存在警告也算） |
| `2` | 参数错误 / 报告写入失败 |

---

## 接进 CI

GitHub Actions 里加一步，PR 就会因为断链变红：

```yaml
name: docs
on: [push, pull_request]

jobs:
  links:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - name: 检查文档链接
        run: python mdlink.py . --no-color
```

想把结果贴到 PR 里，用 JSON 格式自己渲染：

```bash
mdlink . -f json -o link-report.json || true
```

或者出一份 HTML 报告挂到 Pages：

```bash
mdlink . -x -f html -o public/report.html
```

### 忽略文件示例

`.mdlinkignore`（每行一条正则，`#` 开头是注释）：

```
# 本地开发地址
localhost
127\.0\.0\.1
# 需要登录才能访问的页面
^https://internal\.example\.com
# 隐私政策等允许失效的旧链接
^http://legacy\.example\.com
```

```bash
mdlink . -x --ignore-file .mdlinkignore
```

---

## 仓库结构

```
mdlink/
├── mdlink.py               # 全部实现（单文件，无依赖）
├── tests/test_mdlink.py    # 28 个单元测试
└── examples/
    ├── clean/              # 所有链接都正确，应该 0 断链
    ├── messy/              # 故意写坏，用来演示各类问题
    └── report-demo.html    # HTML 报告长什么样
```

跑测试：

```bash
python -m unittest discover -s tests -v
```

---

## 已知取舍

- 链接目标里有空格时，CommonMark 要求用尖括号包裹（`[x](<a b.md>)`）或 `%20`，裸空格会被截断 —— 这是 Markdown 规范本身的限制，不是 bug。
- 极罕见的嵌套链接（链接文字里再套一个完整链接之外的复杂结构）可能只识别出内层，真实文档里几乎不会出现。
- 锚点只校验 Markdown 文件内部；指向 HTML 文件的 `#锚点` 不做检查。
- 外链探测会带一个自定义 User-Agent。有些站点（Cloudflare、社交平台）会直接 403，这类情况报「警告」而不是「断链」。

---

## License

[MIT](LICENSE) © 2026 1fzx23
