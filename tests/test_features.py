"""需求补齐回归套件（对照 PRD 2.4.1–2.4.10 核验新增能力）。

覆盖：
  - 2.4.4  管辖约定不利判高危（R-H5）
  - 2.4.8  合同编号 / 送审部门 / 相对方主体名称 提取
  - 2.4.1  扫描件（图片）解析（真实 OCR 或 sidecar 模拟）
  - 2.4.6  附件去重哈希表
  - 2.4.6/2.4.7  审批待办拉取（含去重跳过）
  - 2.4.2  角色权限矩阵（X-Role 软校验 → 403）
  - 2.4.10-5 法务编辑建议 / 采纳
  - 2.4.7  阻塞告警
  - 2.4.1  分类审查清单
  - 2.4.5  报告内联预览

运行：
  cd contract-review-system
  .venv/Scripts/python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

import backend.main as M  # noqa: E402
from backend import store  # noqa: E402
from backend.models import BizType, ClauseSegment, ContractMeta, PullReq, RiskEditReq  # noqa: E402
from backend.parsers import extract_metadata, parse_contract  # noqa: E402
from backend.review import run_review  # noqa: E402
from backend.rules import evaluate  # noqa: E402
from backend.samples import seed_samples  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "samples")


class _Req:
    """最小 Request 替身：携带服务端签发的 Bearer JWT（A1 硬鉴权）。"""
    def __init__(self, role=None, token=None):
        if token is not None:
            self.headers = {"Authorization": f"Bearer {token}"}
        elif role is not None:
            self.headers = {"Authorization": f"Bearer {M.create_token('tester', role)}"}
        else:
            self.headers = {}


@pytest.fixture(autouse=True)
def _ensure_samples():
    seed_samples(SAMPLE_DIR)


# ---------------- 2.4.4 管辖约定不利 ----------------
def test_r_h5_jurisdiction_unfavorable():
    doc = "第六条 争议解决管辖\n因本合同发生争议，提交乙方所在地有管辖权的人民法院诉讼解决。"
    seg = [ClauseSegment(clause_type="争议解决管辖", text=doc, page=1,
                         paragraph_index=0, start_char=0, end_char=len(doc))]
    risks = evaluate(doc, seg, ContractMeta(), BizType.PROCUREMENT, store.RULES)
    hit = next((r for r in risks if r.title == "争议管辖约定不利"), None)
    assert hit is not None and hit.level.value == "high" and hit.highlight

    # 约定我方(甲方)所在地 → 不应触发
    doc2 = "第六条 争议解决管辖\n因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决。"
    seg2 = [ClauseSegment(clause_type="争议解决管辖", text=doc2, page=1,
                          paragraph_index=0, start_char=0, end_char=len(doc2))]
    risks2 = evaluate(doc2, seg2, ContractMeta(), BizType.PROCUREMENT, store.RULES)
    assert not any(r.title == "争议管辖约定不利" for r in risks2)


def test_r_h5_excludes_labor():
    """劳动类合同不走 R-H5（劳动争议仲裁合法）。"""
    doc = "第五条 争议解决管辖\n提交乙方所在地劳动争议仲裁委员会仲裁。"
    seg = [ClauseSegment(clause_type="争议解决管辖", text=doc, page=1,
                         paragraph_index=0, start_char=0, end_char=len(doc))]
    risks = evaluate(doc, seg, ContractMeta(), BizType.LABOR, store.RULES)
    assert not any(r.title == "争议管辖约定不利" for r in risks)


# ---------------- 2.4.8 元数据扩展 ----------------
def test_metadata_contract_no_and_department():
    doc = ("甲方：示例科技有限公司\n乙方：云软信息技术有限公司\n"
           "合同编号：HT-2026-CG-001\n送审部门：采购部\n合同金额：人民币 300,000 元")
    meta = extract_metadata(doc)
    assert meta.contract_no == "HT-2026-CG-001"
    assert meta.department == "采购部"
    assert meta.subject_name == "示例科技有限公司"
    assert meta.counterparty == "云软信息技术有限公司"


# ---------------- 2.4.1 扫描件解析 ----------------
def test_scan_image_parsed_via_ocr_or_sidecar():
    p = os.path.join(SAMPLE_DIR, "scan_procurement.png")
    assert os.path.exists(p), "应生成扫描件样例图片"
    r = parse_contract(p)
    assert r.doc_text.strip()
    assert r.note  # 真实 OCR 或模拟 OCR 均需给出备注
    assert r.meta.amount == 680000.0
    assert r.meta.contract_no == "HT-2026-CG-058"


# ---------------- 2.4.6 附件去重 ----------------
def test_attachment_hash_registry():
    h = store.content_sha256(b"same-bytes")
    assert store.find_by_attachment_hash(h) is None
    store.register_attachment_hash(h, "T_DEDUP")
    assert store.find_by_attachment_hash(h) == "T_DEDUP"


# ---------------- 2.4.6/2.4.7 待办拉取 ----------------
def test_approval_inbox_and_pull_with_dedup():
    import json
    inbox = json.loads(M.approval_inbox(_Req("registered")).body)
    assert len(inbox) >= 4
    # 首次拉取全部 → 新建任务
    r1 = json.loads(M.approval_pull(_Req("registered"), PullReq(ticket_ids=[]), BackgroundTasks()).body)
    assert len(r1["created"]) >= 1
    # 再次拉取 → 已拉取，不再新建
    r2 = json.loads(M.approval_pull(_Req("registered"), PullReq(ticket_ids=[]), BackgroundTasks()).body)
    assert len(r2["created"]) == 0
    # 清空「已拉取」但保留附件哈希 → 应命中「附件重复」而去重跳过
    M.approval.pulled.clear()
    r3 = json.loads(M.approval_pull(_Req("registered"), PullReq(ticket_ids=[]), BackgroundTasks()).body)
    assert len(r3["created"]) == 0
    assert any("重复" in s["reason"] for s in r3["skipped"])


# ---------------- 2.4.2 角色权限（A1 服务端硬鉴权） ----------------
def test_role_permission_matrix():
    # 无令牌 → 401（A1：不再放行演示模式）
    with pytest.raises(HTTPException) as e0:
        M.require_perm(_Req(), "view")
    assert e0.value.status_code == 401
    # 注册用户（只读）：可提交审查，但不可管理规则
    M.require_perm(_Req("registered"), "submit")
    with pytest.raises(HTTPException) as e1:
        M.require_perm(_Req("registered"), "manage_rules")
    assert e1.value.status_code == 403
    # 法务可写回
    M.require_perm(_Req("legal"), "writeback")
    # 经办人不可写回
    with pytest.raises(HTTPException) as e1:
        M.require_perm(_Req("applicant"), "writeback")
    assert e1.value.status_code == 403
    # 经办人不可管理规则
    with pytest.raises(HTTPException):
        M.require_perm(_Req("applicant"), "manage_rules")
    # 管理员可管理规则、不可写回
    M.require_perm(_Req("admin"), "manage_rules")
    with pytest.raises(HTTPException):
        M.require_perm(_Req("admin"), "writeback")


# ---------------- 2.4.10-5 法务编辑建议 ----------------
def test_edit_risk_suggestion_and_adopt():
    spec = store.SAMPLES["procurement_risk.docx"]
    task = store.create_task(contract_name="编辑用例", biz_type=spec["biz_type"])
    run_review(task, store.RULES, spec["path"])
    assert task.risks, "应有风险项"
    rid = task.risks[0].risk_id
    M.edit_risk(_Req("legal"), task.task_id, rid,
                RiskEditReq(suggestion="双方各承担 50% 责任", adopted=True, lawyer_comment="已核"))
    risk = next(r for r in task.risks if r.risk_id == rid)
    assert risk.suggestion == "双方各承担 50% 责任"
    assert risk.edited is True and risk.adopted is True and risk.lawyer_comment == "已核"


# ---------------- 2.4.7 阻塞告警 ----------------
def test_blocked_generates_alert():
    a = store.add_alert("T_ALERT", "告警用例", "解析异常：示例")
    assert a["alert_id"] and a["acked"] is False
    assert any(x["alert_id"] == a["alert_id"] for x in store.list_alerts())
    assert store.ack_alert(a["alert_id"]) is True


# ---------------- 2.4.1 分类审查清单 ----------------
def test_checklist_endpoint():
    cl = M.get_checklist(_Req("registered"))
    data = __import__("json").loads(cl.body)
    assert any(x["biz_type"] == "procurement" for x in data)
    proc = next(x for x in data if x["biz_type"] == "procurement")
    assert "验收标准" in proc["required_clauses"]


# ---------------- 2.4.5 报告内联预览 ----------------
def test_report_inline_preview_returns_html():
    spec = store.SAMPLES["procurement_std.docx"]
    task = store.create_task(contract_name="预览用例", biz_type=spec["biz_type"])
    run_review(task, store.RULES, spec["path"])
    resp = M.export_report(_Req("registered"), task.task_id, "md", 1)
    assert "text/html" in resp.media_type


# ---------------- 大模型：可选能力 + 安全降级 ----------------
from backend import llm  # noqa: E402


@pytest.fixture
def _clean_llm_env():
    keys = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROXY",
            "LLM_TIMEOUT", "LLM_SUGGEST")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_llm_disabled_by_default(_clean_llm_env):
    assert llm.enabled() is False
    assert llm.chat_json("hi") is None          # 未启用 → 不发任何请求
    st = llm.status()
    assert st["enabled"] is False and st["has_key"] is False
    ok, msg = llm.test_connection()
    assert ok is False and "LLM_API_KEY" in msg


def test_llm_recommend_noop_when_disabled(_clean_llm_env):
    """未启用大模型时，建议生成直接跳过（保留规则库预置文本）。"""
    spec = store.SAMPLES["sales_risk.docx"]
    task = store.create_task(contract_name="建议降级用例", biz_type=spec["biz_type"])
    run_review(task, store.RULES, spec["path"])
    assert task.risks
    assert all(r.by_llm is False for r in task.risks), "未启用时不应标记为 AI 生成"
    assert task.llm_used is False
    assert all("不超过合同总额" in r.suggestion or r.suggestion for r in task.risks)


def test_llm_config_set_and_env_roundtrip(_clean_llm_env, tmp_path):
    """set_config 生效 + save/load .env 往返（含 GBK 兜底）。"""
    st = llm.set_config(api_key_="sk-test-1234567890", base_url_="https://x.example/v1/chat/completions",
                        model_="test-model", suggest_=False)
    assert st["enabled"] is True and st["model"] == "test-model" and st["suggest"] is False
    assert llm.api_key() == "sk-test-1234567890"
    assert "***" in st["key_masked"]

    # 写入/读取 .env 往返
    envf = tmp_path / ".env"
    orig_env_path = llm.ENV_PATH
    llm.ENV_PATH = envf
    try:
        llm.save_env({"LLM_MODEL": "saved-model", "LLM_SUGGEST": "0"})
        assert "LLM_MODEL=saved-model" in envf.read_text(encoding="utf-8")
        # 以 GBK 重写，验证编码兜底
        envf.write_text("LLM_MODEL=gbk-model\n", encoding="gbk")
        os.environ.pop("LLM_MODEL", None)
        llm._loaded = False
        llm.load_env()
        assert os.environ.get("LLM_MODEL") == "gbk-model"
    finally:
        llm.ENV_PATH = orig_env_path
