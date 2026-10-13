"""审批回写模块（PRD 2.4.1 / 2.4.6 审批回写模块 + 2.4.8 审批回写评论文本）。

实现「审批回写模拟模块」：将格式化审查意见写回审批系统评论区（此处为内存模拟），
并记录回写日志。真实对接时替换 post_comment 为审批系统 OpenAPI 调用即可。
"""
from __future__ import annotations

from datetime import datetime

from .models import ReviewTask, WritebackStatus


def build_writeback_comment(task: ReviewTask) -> str:
    """构建格式化 Markdown 回写评论（PRD 2.4.8 审批回写评论文本）。"""
    lvl = task.risk_level.value if task.risk_level else "无"
    lines = [
        f"## 合同智能审查意见 · {task.contract_name}",
        f"**综合风险等级**：{lvl}",
        "",
        "### 一、风险总评",
        task.summary or "无",
        "",
        "### 二、需重点关注事项",
    ]
    for i, r in enumerate(task.risks, 1):
        lines.append(f"{i}. 【{r.level.value}】{r.title} —— {r.reason}")
        if r.highlight:
            lines.append(f"   原文：{r.snippet}")
        lines.append(f"   建议：{r.suggestion}")
    lines += ["", "### 三、法务批注", task.legal_note or "（无）", ""]
    return "\n".join(lines)


class MockApprovalSystem:
    """内存版审批系统，模拟评论区写回 + 待办任务队列（PRD 2.4.6 接入与流转）。"""

    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.inbox: list[dict] = []      # 审批系统待办任务（附件待审查）
        self.pulled: set[str] = set()    # 已拉取的工单，避免重复建任务

    # ---------- 待办队列（模拟审批系统事件/待办拉取） ----------
    def seed_inbox(self, samples: dict) -> list[dict]:
        """用示例合同模拟审批系统的待办任务队列。"""
        plan = [
            ("procurement_risk.docx", "软件采购合同审批", "采购部"),
            ("sales_risk.docx", "大额销售合同审批", "销售部"),
            ("procurement_std.docx", "办公设备采购审批", "行政部"),
            ("labor_std.docx", "新员工劳动合同审批", "人力部"),
        ]
        self.inbox = []
        for i, (name, title, dept) in enumerate(plan, 1):
            spec = samples.get(name)
            if not spec:
                continue
            self.inbox.append({
                "ticket_id": f"AP-INBOX-{i:03d}",
                "title": title,
                "applicant": spec.get("applicant", "业务经办人"),
                "department": dept,
                "biz_type": spec.get("biz_type", "procurement"),
                "attachment": name,
                "status": "pending",
                "received_at": datetime.now().isoformat(timespec="seconds"),
            })
        return self.inbox

    def list_inbox(self) -> list[dict]:
        """返回待办列表，附带是否已被拉取。"""
        return [{**t, "pulled": t["ticket_id"] in self.pulled} for t in self.inbox]

    def get_inbox_ticket(self, ticket_id: str) -> dict | None:
        return next((t for t in self.inbox if t["ticket_id"] == ticket_id), None)

    def mark_pulled(self, ticket_id: str) -> None:
        self.pulled.add(ticket_id)

    # ---------- 写回 ----------
    def ensure_ticket(self, task: ReviewTask) -> str:
        tid = f"AP-{task.task_id[-8:].upper()}"
        if tid not in self.tickets:
            self.tickets[tid] = {
                "ticket_id": tid,
                "contract_name": task.contract_name,
                "applicant": task.applicant,
                "comments": [],
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        return tid

    def post_comment(self, task: ReviewTask) -> tuple[WritebackStatus, str]:
        """写回评论区。返回 (状态, 日志)。"""
        try:
            tid = self.ensure_ticket(task)
            comment = build_writeback_comment(task)
            self.tickets[tid]["comments"].append({
                "author": "智能审查系统",
                "content": comment,
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            log = f"[{datetime.now().isoformat(timespec='seconds')}] 已写回审批单 {tid} 评论区，共 {len(task.risks)} 条风险意见。"
            return WritebackStatus.SUCCESS, log
        except Exception as e:  # pragma: no cover
            return WritebackStatus.FAILED, f"回写失败：{e}"


approval = MockApprovalSystem()
