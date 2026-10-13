"""登录页「注册按钮点不到」的回归测试（2026-10-13）。

现象：登录页右侧内容（演示身份下拉 + 账号密码 + 展开的注册面板）高于视口时，
容器不可滚动 —— 底部的「注册并登录」按钮永远点不到。

修法：`.lg-right` 变成可滚动的竖向 flex 容器；`.lg-card` 用 `margin:auto 0`
做「安全居中」（内容不满屏时居中，超出时 auto 外边距归零、不被裁切）。
本文件锁住这两条样式契约与展开后的自动滚动行为，防止回退。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()


def _rule(selector: str) -> str:
    """取出某条简单规则的声明块（取第一个匹配）。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", HTML)
    assert m, f"未找到样式规则 {selector}"
    return m.group(1)


def test_log_right_is_scrollable():
    decl = _rule(".lg-right")
    assert "overflow-y:auto" in decl.replace(" ", ""), \
        f".lg-right 必须可纵向滚动，否则底部注册按钮点不到：{decl}"


def test_log_right_uses_safe_centering():
    """不能用 justify-content:center 居中（内容超高时会把顶部裁掉、滚不到）。"""
    decl = _rule(".lg-right").replace(" ", "")
    assert "flex-direction:column" in decl
    assert "justify-content:center" not in decl


def test_log_card_safe_auto_margin():
    decl = _rule(".lg-card").replace(" ", "")
    assert "margin:auto0" in decl, f".lg-card 缺少安全居中写法：{decl}"
    assert "flex:00auto" in decl, "flex 子项会被压缩，需要 flex:0 0 auto 保持自然高度"


def test_toggle_reg_scrolls_panel_into_view():
    m = re.search(r"function\s+toggleReg\s*\(\s*\)\s*\{(.*?)\n\}", HTML, re.S)
    assert m, "未找到 toggleReg()"
    body = m.group(1)
    assert "scrollIntoView" in body, "展开注册面板后必须滚动到可见区域"


def test_chat_quick_questions_include_chitchat():
    """闲聊快捷入口要能引导用户问项目外的问题（如天气）。"""
    m = re.search(r"ai-quick'\)\.innerHTML\s*=\s*\[(.*?)\]", HTML, re.S)
    assert m, "未找到助手快捷问题列表"
    assert "天气" in m.group(1), "快捷问题里应包含天气，证明闲聊兜底已可用"
