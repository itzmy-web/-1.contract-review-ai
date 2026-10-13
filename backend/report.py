"""审查报告 PDF 导出（PRD 2.4.8）。"""
from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .models import ReviewTask
from .writeback import build_writeback_comment

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
FONT = "STSong-Light"
MARGIN = 20 * mm


def _wrap_text(text: str, size: int, max_width: float) -> list[str]:
    """按真实字符宽度折行（CID 字体中英文混排宽度不一）。

    P2：替掉旧的「每 32 字硬切」，避免长英文/数字被拦腰截断、也避免中文行尾大块空白。
    """
    lines: list[str] = []
    cur = ""
    for ch in text:
        if pdfmetrics.stringWidth(cur + ch, FONT, size) <= max_width:
            cur += ch
        else:
            if cur:
                lines.append(cur)
            # 超长单字符（罕见）直接落单行
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def build_pdf(task: ReviewTask, path: str) -> None:
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    max_width = w - 2 * MARGIN
    y = h - 25 * mm

    def line(text: str, size=11, gap=7 * mm):
        nonlocal y
        c.setFont(FONT, size)
        # 显式换行符拆分后，再按宽度折行
        for seg in text.split("\n"):
            for sub in _wrap_text(seg, size, max_width):
                if y < 18 * mm:
                    c.showPage()
                    y = h - MARGIN
                    c.setFont(FONT, size)
                c.drawString(MARGIN, y, sub)
                y -= gap

    c.setFont(FONT, 16)
    line(f"合同审查报告 · {task.contract_name}", size=16, gap=12 * mm)
    line("【合同基本信息】（PRD 2.4.8）")
    line(f"合同编号：{task.meta.contract_no or '（未提取）'}    送审部门：{task.meta.department or '（未提取）'}")
    line(f"申请人：{task.applicant}    业务类型：{task.biz_type.value}")
    line(f"相对方主体名称：{task.meta.counterparty or '（未提取）'}")
    line(f"合同金额：{task.amount} {task.currency or ''}    履约周期：{task.meta.performance_period or '（未提取）'}")
    line(f"综合风险等级：{task.risk_level.value if task.risk_level else '无'}")
    line("")
    line("")
    line("【综合审查结论】")
    for s in (task.summary or "无").split("\n"):
        line(s)
    line("")
    line("【风险清单明细】")
    for i, r in enumerate(task.risks, 1):
        line(f"{i}. 【{r.level.value}】{r.title}（{r.clause_type}）")
        line(f"   成因：{r.reason}")
        line(f"   依据：{r.legal_basis}")
        line(f"   建议：{r.suggestion}")
    line("")
    line("【法务审批意见】（PRD 审批意见导出）")
    line(task.legal_note or "（未填写整体批注）")
    for i, r in enumerate(task.risks, 1):
        if r.lawyer_comment:
            line(f"· 风险{i}「{r.title}」：{r.lawyer_comment}")
    line("")
    line("【审批回写评论文本】")
    for s in build_writeback_comment(task).split("\n"):
        line(s, size=10, gap=6 * mm)
    c.save()
