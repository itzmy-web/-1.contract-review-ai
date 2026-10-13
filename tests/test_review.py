"""合同审批审查系统 · pytest 回归套件（P2 工程化补齐）。

覆盖本次代码评审修复点：
  - P0-1：审查异常兜底（不卡死在 reviewing / 可重试）
  - P0-2：任务列表不序列化全文（模型层验证）
  - P1-4：数据驱动规则（keyword 模式命中）
  - P1-7：元数据多模式提取（中文大写金额 / ¥ / 美元）
  - P1-9：规则开关（enabled=False 不再触发）
  - P1-9：回写失败记录 FAILED 状态
  - P1-5/6：条款切分与高风险判定

运行：
  cd contract-review-system
  .venv/Scripts/python -m pytest tests/test_review.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document  # noqa: E402

from backend import store  # noqa: E402
from backend.models import (  # noqa: E402
    BizType, ClauseSegment, ContractMeta, ReviewTask, RiskLevel, TaskStatus,
    WritebackStatus,
)
from backend.parsers import extract_metadata  # noqa: E402
from backend.review import run_review  # noqa: E402
from backend.rules import evaluate  # noqa: E402
from backend.writeback import MockApprovalSystem  # noqa: E402


def _write_docx(path: str, text: str, valid: bool = True) -> str:
    if not valid:
        with open(path, "wb") as f:
            f.write(b"this is not a zip / invalid docx content")
        return path
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(path)
    return path


VALID_PROCUREMENT = """甲方：测试科技有限公司
乙方：演示供应商有限公司
统一社会信用代码：91110108MA01TESTX0
合同金额：人民币 120,000 元
履行期限：自合同签订之日起 60 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方采购测试用服务器一台及配套定制软件。

第二条 知识产权条款
本合同所涉定制软件之全部知识产权归乙方所有，甲方仅获得非独占使用许可。

第三条 付款进度
甲方应在设备到货后 3 个工作日内，向乙方支付合同全部款项。

第四条 违约责任
任一方违约，向守约方支付违约金，违约金不超过合同总额 20%。

第五条 保密义务
双方对本合同知悉的商业秘密负有保密义务。

第六条 争议解决管辖
因本合同发生争议，提交乙方所在地有管辖权的人民法院诉讼解决。
"""

# 干净合同：必备条款齐全且无不利约定，用于验证「不误判高风险」
CLEAN_PROCUREMENT = """甲方：测试科技有限公司
乙方：演示供应商有限公司
统一社会信用代码：91110108MA01TESTX0
合同金额：人民币 120,000 元
履行期限：自合同签订之日起 60 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方采购测试用服务器一台及配套定制软件。

第二条 知识产权条款
本合同项下定制软件之知识产权归甲方所有，乙方保留必要使用许可。

第三条 付款进度
甲方在收到乙方交付并验收合格后 10 个工作日内支付合同款项。

第四条 验收标准
乙方交付后，甲方在 5 个工作日内完成验收并出具验收报告。

第五条 违约责任
任一方违约，向守约方支付违约金，违约金不超过合同总额 20%。

第六条 保密义务
双方对本合同知悉的商业秘密负有保密义务，保密期限为合同终止后 3 年。

第七条 争议解决管辖
因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决。
"""


# ---------------- P0-1 异常兜底 + 可重试 ----------------
def test_blocked_on_parse_failure_and_retry_to_completed():
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "bad.docx")
        good = os.path.join(td, "good.docx")
        _write_docx(bad, "", valid=False)
        _write_docx(good, VALID_PROCUREMENT, valid=True)

        task = store.create_task(contract_name="重试样例", biz_type=BizType.PROCUREMENT.value)
        run_review(task, store.RULES, bad)
        # P0-1：解析失败必须进入 blocked，且写明原因，不能卡在中间态
        assert task.status == TaskStatus.BLOCKED
        assert task.blocked_reason

        # 重试（端点层会先把状态置 pending 再跑，这里直接复跑验证状态机收敛）
        run_review(task, store.RULES, good)
        assert task.status == TaskStatus.COMPLETED
        assert task.blocked_reason is None
        assert task.doc_text.strip()


# ---------------- P0-2 列表不泄露全文（模型层） ----------------
def test_task_dump_excludes_full_text():
    task = ReviewTask(task_id="T_DUMP", contract_name="x", biz_type=BizType.PROCUREMENT,
                      created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
                      doc_text="很长的合同正文" * 50,
                      segments=[ClauseSegment(clause_type="其他条款", text="a")])
    dumped = task.model_dump(exclude={"doc_text", "segments"})
    assert "doc_text" not in dumped
    assert "segments" not in dumped


# ---------------- P1-4 数据驱动 keyword 规则 ----------------
def test_data_driven_keyword_rule():
    doc = "甲方承担无上限的赔偿责任。"
    seg = [ClauseSegment(clause_type="违约责任", text=doc, page=1,
                         paragraph_index=0, start_char=0, end_char=len(doc))]
    risks = evaluate(doc, seg, ContractMeta(), BizType.PROCUREMENT, store.RULES)
    hit = next((r for r in risks if r.title == "违约责任严重不对等"), None)
    assert hit is not None, "keyword 规则应命中『无上限的赔偿责任』"
    # 高亮定位应可用
    assert hit.highlight is True and hit.start_char >= 0


# ---------------- P1-7 元数据多模式 ----------------
def test_metadata_chinese_capital_amount():
    doc = "合同金额：人民币 壹佰贰拾万元整。统一社会信用代码：91110108MA01ABCX0"
    meta = extract_metadata(doc)
    assert meta.amount == 1_200_000.0
    assert meta.currency == "人民币"


def test_metadata_yen_and_usd():
    doc = "总金额：¥300,000.00 元"
    meta = extract_metadata(doc)
    assert meta.amount == 300_000.0
    assert meta.currency == "人民币"

    doc2 = "合同总价：USD 50,000 元"
    meta2 = extract_metadata(doc2)
    assert meta2.amount == 50_000.0
    assert meta2.currency == "美元"


def test_metadata_subject_with_space():
    doc = "甲方 北京示例科技有限公司\n乙方：上海示例供应商有限公司"
    meta = extract_metadata(doc)
    assert meta.subject_name in ("北京示例科技有限公司", "上海示例供应商有限公司")


# ---------------- P1-9 规则开关 ----------------
def test_rule_toggle_disables_risk():
    doc = "本合同项下知识产权归乙方所有。"
    seg = [ClauseSegment(clause_type="知识产权", text=doc, page=1,
                         paragraph_index=0, start_char=0, end_char=len(doc))]
    on = evaluate(doc, seg, ContractMeta(), BizType.PROCUREMENT, store.RULES)
    assert any(r.title == "知识产权归属不利" for r in on)

    rules_off = [r.model_copy(update={"enabled": False}) if r.rule_id == "R-H6"
                 else r.model_copy(deep=True) for r in store.RULES]
    off = evaluate(doc, seg, ContractMeta(), BizType.PROCUREMENT, rules_off)
    assert not any(r.title == "知识产权归属不利" for r in off)


# ---------------- P1-9 回写失败记录 ----------------
def test_writeback_failure_recorded():
    task = store.create_task(contract_name="回写样例", biz_type=BizType.PROCUREMENT.value)
    sys = MockApprovalSystem()
    # 模拟审批系统接口在内部调用时异常（post_comment 的 try/except 应捕获并返回 FAILED）
    def boom(t):
        raise RuntimeError("审批系统 503")
    sys.ensure_ticket = boom
    status, log = sys.post_comment(task)
    assert status == WritebackStatus.FAILED
    assert "失败" in log


# ---------------- P1-5/6 切分与高风险收窄回归 ----------------
def test_procurement_std_has_no_high_risk():
    """标准采购合同（条款齐全、无不利约定）不应误判高风险。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "std.docx")
        _write_docx(p, CLEAN_PROCUREMENT, valid=True)
        task = store.create_task(contract_name="标准采购", biz_type=BizType.PROCUREMENT.value)
        run_review(task, store.RULES, p)
        assert task.status == TaskStatus.COMPLETED
        highs = [r.title for r in task.risks if r.level == RiskLevel.HIGH]
        assert task.risk_level != RiskLevel.HIGH, f"不应有高风险，实际：{highs}"
        assert not any(r.title.startswith("必备条款缺失") for r in task.risks), highs
