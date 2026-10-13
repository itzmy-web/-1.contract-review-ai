"""存储层：审查任务、审查规则、示范条款模板、示例合同注册表。

任务（含归属账号、修改留痕、正文）持久化到 data/tasks.json，保证「同一账号每次登录
都能找回自己之前做过的任务」，且服务重启不丢历史。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .models import ClauseTemplate, ReviewRule, ReviewTask
from .rules import DEFAULT_RULES

# 数据目录：默认 <项目根>/data；测试可用 CR_DATA_DIR 指向临时目录，避免污染真实数据
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CR_DATA_DIR") or (BASE_DIR / "data"))

TASKS: dict[str, ReviewTask] = {}
RULES: list[ReviewRule] = [r.model_copy(deep=True) for r in DEFAULT_RULES]

# 附件内容哈希 -> task_id（PRD 2.4.6 附件去重下载）
ATTACH_HASHES: dict[str, str] = {}
# 阻塞告警队列（PRD 2.4.7 解析异常/接口超时推送告警）
# 上限：只保留最近 ALERTS_MAX 条，避免长跑服务内存无界增长（与 AUDIT_MAX 同口径）
ALERTS: list[dict] = []
ALERTS_MAX = 500

# P1-10：并发保护。后台审查线程会原地修改 task，列表/插入/规则变更需加锁，
# 避免 dict 扩容期间迭代（RuntimeError）与规则集读写竞争。
LOCK = threading.Lock()

# 审计日志（查看面板与审计）：{time, role, method, path, status}
AUDIT: list[dict] = []
AUDIT_MAX = 500
AUDIT_FILE = DATA_DIR / "audit.json"

# 审查任务持久化文件（含归属账号 / 修改留痕 / 正文）。
# 存储量上限：超出后按创建时间淘汰最旧的已完成任务，避免文件无限膨胀。
TASKS_FILE = DATA_DIR / "tasks.json"
TASKS_MAX = 2000


def _load_audit():
    """启动时从文件加载审计日志（持久化，满足内审/外部审计取证要求）。"""
    global AUDIT
    try:
        if AUDIT_FILE.exists():
            AUDIT = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
            AUDIT = AUDIT[-AUDIT_MAX:]
    except Exception:
        AUDIT = []


def _save_audit():
    try:
        AUDIT_FILE.parent.mkdir(exist_ok=True)
        AUDIT_FILE.write_text(json.dumps(AUDIT, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_audit()


def add_audit(role: str, method: str, path: str, status: int) -> None:
    """记录一条操作审计（写操作）。仅保留最近 AUDIT_MAX 条，并持久化到文件。"""
    with LOCK:
        AUDIT.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "role": role or "演示模式",
            "method": method,
            "path": path,
            "status": status,
        })
        if len(AUDIT) > AUDIT_MAX:
            del AUDIT[:len(AUDIT) - AUDIT_MAX]
        _save_audit()


def list_audit(limit: int = 200) -> list[dict]:
    """按时间倒序返回最近审计记录。"""
    with LOCK:
        return list(reversed(AUDIT))[:limit]


# ---------------- 任务持久化（历史记录 / 重启不丢） ----------------
def _load_tasks():
    """启动时从文件恢复历史任务，并重建「附件哈希 → 任务」去重索引。"""
    global TASKS
    try:
        if TASKS_FILE.exists():
            raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            loaded: dict[str, ReviewTask] = {}
            for d in raw:
                try:
                    t = ReviewTask.model_validate(d)
                except Exception:
                    continue  # 单条损坏不影响其余历史任务
                loaded[t.task_id] = t
                if t.attach_hash:
                    ATTACH_HASHES[t.attach_hash] = t.task_id
            TASKS = loaded
    except Exception:
        TASKS = {}


def _save_tasks_locked():
    """写盘（调用方需已持有 LOCK，或明确无并发写）。"""
    try:
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        items = sorted(TASKS.values(), key=lambda t: t.created_at)
        if len(items) > TASKS_MAX:
            # 超限：优先淘汰最旧的「已完成且未修改」任务，保底再按时间淘汰
            droppable = [t for t in items if t.status == "completed" and not t.modified]
            drop_ids = {t.task_id for t in droppable[:len(items) - TASKS_MAX]}
            items = [t for t in items if t.task_id not in drop_ids]
        TASKS_FILE.write_text(
            json.dumps([t.model_dump() for t in items], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def save_tasks() -> None:
    """把当前任务集写盘（创建/审查完成/人工修改后调用）。"""
    with LOCK:
        _save_tasks_locked()


_load_tasks()


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_by_attachment_hash(h: str) -> str | None:
    with LOCK:
        return ATTACH_HASHES.get(h)


def register_attachment_hash(h: str, task_id: str) -> None:
    with LOCK:
        ATTACH_HASHES[h] = task_id


def add_alert(task_id: str, contract_name: str, reason: str) -> dict:
    with LOCK:
        alert = {
            "alert_id": "AL" + uuid.uuid4().hex[:8].upper(),
            "task_id": task_id,
            "contract_name": contract_name,
            "reason": reason,
            "level": "warning",
            "at": datetime.now().isoformat(timespec="seconds"),
            "acked": False,
        }
        ALERTS.append(alert)
        if len(ALERTS) > ALERTS_MAX:
            del ALERTS[:len(ALERTS) - ALERTS_MAX]
        return alert


def list_alerts() -> list[dict]:
    with LOCK:
        return list(reversed(ALERTS))


def ack_alert(alert_id: str) -> bool:
    with LOCK:
        for a in ALERTS:
            if a["alert_id"] == alert_id:
                a["acked"] = True
                return True
    return False
TEMPLATES: list[ClauseTemplate] = [
    ClauseTemplate(template_id="T-IP", clause_type="知识产权", biz_type="procurement",
                   content="本合同项下定制开发之源代码及全部知识产权归甲方所有；乙方仅保留为履行本合同所必需的使用许可。"),
    ClauseTemplate(template_id="T-PAY", clause_type="付款进度", biz_type="procurement",
                   content="甲方在乙方交付并经验收合格、出具验收报告后 10 个工作日内，支付该阶段对应款项。"),
    ClauseTemplate(template_id="T-BREACH", clause_type="违约责任", biz_type="sales",
                   content="任一方违约的，应向守约方支付违约金，违约金不超过合同总额的 20%；造成实际损失的，以实际损失为赔偿上限。"),
    ClauseTemplate(template_id="T-CONF", clause_type="保密义务", biz_type="service",
                   content="双方对本合同知悉的商业秘密负有保密义务，保密期限自本合同签署之日起至合同终止后 3 年。"),
    ClauseTemplate(template_id="T-JUR", clause_type="争议解决管辖", biz_type="sales",
                   content="因本合同发生争议，协商不成的，提交甲方所在地有管辖权的人民法院诉讼解决。"),
]

# 示例合同：name -> (文件路径, 业务类型, 申请人)
SAMPLES: dict[str, dict] = {}


def new_task_id() -> str:
    return "T" + uuid.uuid4().hex[:12].upper()


def create_task(contract_name: str, biz_type: str, applicant: str = "业务经办人",
                owner: str = "") -> ReviewTask:
    tid = new_task_id()
    now = datetime.now().isoformat(timespec="seconds")
    task = ReviewTask(
        task_id=tid, contract_name=contract_name,
        applicant=applicant, biz_type=biz_type, owner=owner,
        created_at=now, updated_at=now,
    )
    with LOCK:
        TASKS[tid] = task
        _save_tasks_locked()
    return task


def list_tasks() -> list[ReviewTask]:
    with LOCK:
        return sorted(TASKS.values(), key=lambda t: t.created_at, reverse=True)


def tasks_of_owner(owner: str) -> list[ReviewTask]:
    """某账号的历史任务（按创建时间倒序）；owner 为空表示不限定（演示模式）。"""
    with LOCK:
        items = [t for t in TASKS.values() if (not owner or t.owner == owner)]
        return sorted(items, key=lambda t: t.created_at, reverse=True)


def get_task(tid: str) -> ReviewTask | None:
    return TASKS.get(tid)
