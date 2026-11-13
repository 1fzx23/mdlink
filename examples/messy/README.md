# 示例项目（故意写坏版）

这个目录里的链接**大部分是坏的**，用来演示 `mdlink` 能发现哪些问题。

真正干净的示例见 [`../clean`](../clean)。

## 正常的链接

- [快速安装](docs/guide.md#快速安装) ✓ 文件和锚点都存在
- [API 概览](docs/api.md) ✓
- [返回上级](../clean) ✓ 目录链接

## 坏掉的链接

- [部署文档](docs/deploy.md) ✗ 文件根本不存在
- [API 说明](docs/API.md) ✗ 大小写不一致（Linux / GitHub 上会 404）
- [配置项](docs/guide.md#配置文件参考) ✗ 锚点在目标文档里不存在
- [目录里的锚点](docs/guide.md#nope) ✗
- [空链接]() ✗ 目标为空
- [指向目录](docs) ⚠ 打开的是目录列表
- ![Logo](assets/logo.PNG) ⚠ 图片扩展名大小写不一致
- [引用式链接][missing-doc] ✗ 引用定义指向不存在的文件
- <img src="assets/missing.png" alt="缺失的图片"> ✗ HTML 图片不存在

[missing-doc]: ./docs/nonexistent.md
[ok-ref]: ./docs/api.md

## 代码块里的假链接不该被当成链接

```markdown
[这是一个假链接](does-not-exist.md)
![假图片](also-missing.png)
```

行内代码同理：`[示例](still-not-real.md)` 也应该被忽略。

## 自动链接

直接写地址也可以：<https://example.com>

## 表格里的链接

| 名称 | 链接 |
| --- | --- |
| 指南 | [guide](docs/guide.md) |
| 坏的 | [broken](./nope.md) |
