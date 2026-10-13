"""新增能力回归：管理员修改合同 + 已修改标注 + 修改意见/合同导出 + 账号历史记录持久化。

覆盖：
  - 权限：admin 具备 edit_contract / edit_suggestion / annotate；applicant 无 edit_contract
  - 修改正文后：modified=True、合同名追加「（已修改）」、original_name 保留、幂等
  - 编辑意见（风险建议）也会标记已修改；仅批注不标记
  - /api/tasks/{id}/export-contract（docx / txt）与 /export-opinions（md）
  - 任务持久化（tasks.json）与 owner 过滤 / 历史记录
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import backend.main as M  # noqa: E402
from backend import store  # noqa: E402
from backend.models import RiskEditReq  # noqa: E402
from backend.review import run_review  # noqa: E402


class _Req:
    """最小 Request 替身：携带服务端签发的 Bearer JWT（A1 硬鉴权）。"""

    def __init__(self, role=None, user=None):
        self.headers = {}
        if role is not None:
            self.headers["Authorization"] = f"Bearer {M.create_token(user or role, role)}"
        if user is not None:
            self.headers["X-User"] = user


def _completed_task(name="历史用例", owner="alice"):
    spec = store.SAMPLES["procurement_risk.docx"]
    task = store.create_task(contract_name=name, biz_type=spec["biz_type"], owner=owner)
    run_review(task, store.RULES, spec["path"])
    assert task.status == "completed" and task.doc_text
    return task


# ---------------- 权限矩阵 ----------------
def test_admin_has_modify_perms():
    M.require_perm(_Req("admin"), "edit_contract")
    M.require_perm(_Req("admin"), "edit_suggestion")
    M.require_perm(_Req("admin"), "annotate")
    # 经办人不得修改合同正文
    with pytest.raises(HTTPException) as e:
        M.require_perm(_Req("applicant"), "edit_contract")
    assert e.value.status_code == 403
    # 注册用户（只读）不得修改合同正文（A1）
    with pytest.raises(HTTPException) as e2:
        M.require_perm(_Req("registered"), "edit_contract")
    assert e2.value.status_code == 403


# ---------------- 修改正文 → 已修改标注 ----------------
def test_retext_marks_modified_and_suffix():
    task = _completed_task(name="采购合同.docx")
    original = task.contract_name
    M.retext_task(_Req("admin", user="admin_acc"), task.task_id,
                  {"text": task.doc_text + "\n补充：以书面签署为准。"})
    assert task.modified is True
    assert task.modified_by == "admin_acc"
    assert task.modified_at
    assert task.original_name == original
    assert task.contract_name.endswith(M.MODIFIED_SUFFIX)
    assert task.contract_name.startswith("采购合同")
    # 幂等：再次修改不应重复追加
    M.retext_task(_Req("admin"), task.task_id, {"text": task.doc_text + " 二改"})
    assert task.contract_name.count(M.MODIFIED_SUFFIX) == 1


def test_retext_requires_perm():
    task = _completed_task(name="无权修改.docx")
    with pytest.raises(HTTPException) as e:
        M.retext_task(_Req("applicant"), task.task_id, {"text": "改"})
    assert e.value.status_code == 403


def test_edit_risk_marks_modified_but_annotate_not():
    task = _completed_task(name="意见修改.docx")
    rid = task.risks[0].risk_id
    M.edit_risk(_Req("admin"), task.task_id, rid,
                RiskEditReq(suggestion="改为：双方各承担 50% 责任"))
    assert task.modified is True and task.contract_name.endswith(M.MODIFIED_SUFFIX)

    task2 = _completed_task(name="仅批注.docx")
    M.annotate(_Req("legal"), task2.task_id, {"legal_note": "仅整体批注，不算改合同"})
    assert task2.modified is False
    assert not task2.contract_name.endswith(M.MODIFIED_SUFFIX)


# ---------------- 导出：修改后合同 / 修改意见 ----------------
def test_export_contract_docx_and_txt():
    task = _completed_task(name="导出合同.docx")
    M.retext_task(_Req("admin"), task.task_id, {"text": task.doc_text + "\n（人工补充条款）"})
    assert task.contract_name.endswith(M.MODIFIED_SUFFIX)

    r = M.export_contract(_Req("admin"), task.task_id, fmt="docx")
    assert "wordprocessingml" in r.media_type
    assert r.body[:2] == b"PK"                      # docx 为 zip 容器
    cd = r.headers["Content-Disposition"]
    assert "%E5%B7%B2%E4%BF%AE%E6%94%B9" in cd     # 「已修改」UTF-8 百分号编码
    assert ".docx" in cd

    rt = M.export_contract(_Req("admin"), task.task_id, fmt="txt")
    assert rt.body.decode("utf-8").endswith("（人工补充条款）")


def test_export_opinions_markdown():
    task = _completed_task(name="导出意见.docx")
    r = M.export_opinions(_Req("admin"), task.task_id, fmt="md")
    body = r.body.decode("utf-8")
    assert "合同修改意见" in body
    assert task.risks[0].title in body
    assert "修改建议" in body


# ---------------- 持久化 + 历史记录 ----------------
def test_tasks_persist_and_reload():
    task = _completed_task(name="持久化用例.docx", owner="persist_user")
    store.save_tasks()
    assert store.TASKS_FILE.exists()
    raw = store.TASKS_FILE.read_text(encoding="utf-8")
    assert task.task_id in raw
    # 模拟重启：清空内存后重新加载
    store.TASKS.clear()
    store._load_tasks()
    assert task.task_id in store.TASKS
    assert store.TASKS[task.task_id].contract_name == task.contract_name


def test_owner_history_filter():
    a = _completed_task(name="alice的合同.docx", owner="alice")
    b = _completed_task(name="bob的合同.docx", owner="bob")
    alice = {t.task_id for t in store.tasks_of_owner("alice")}
    assert a.task_id in alice and b.task_id not in alice
    everyone = {t.task_id for t in store.tasks_of_owner("")}
    assert {a.task_id, b.task_id} <= everyone
