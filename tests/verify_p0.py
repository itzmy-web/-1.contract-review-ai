"""P0 修复验证：任务不卡死 / 列表不泄露全文 / LLM 失败可降级。"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import store, review as R
from backend.models import ClauseSegment

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "samples", "procurement_risk.docx")


def test_p0_1_no_stuck():
    """evaluate 抛异常时，任务应进入 blocked 而非卡在 reviewing。"""
    orig = R.evaluate
    def boom(*a, **k):
        raise RuntimeError("boom-simulated")
    R.evaluate = boom
    try:
        t = store.create_task(contract_name="异常注入", biz_type="procurement")
        R.run_review(t, store.RULES, SAMPLE)
        assert t.status.value == "blocked", f"应 blocked，实际 {t.status.value}"
        assert "审查异常" in (t.blocked_reason or ""), t.blocked_reason
        print(f"  P0-1 OK  状态={t.status.value} 原因={t.blocked_reason}")
    finally:
        R.evaluate = orig


def test_p0_3_llm_fail_safe():
    """LLM 不可达时应返回 []（降级），不抛异常。"""
    saved = {k: os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROXY")}
    os.environ["LLM_API_KEY"] = "fake"
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/nope"
    os.environ.pop("LLM_PROXY", None)
    try:
        segs = [ClauseSegment(clause_type="其他条款", text="条款", start_char=0, end_char=2)]
        res = R._llm_semantic("合同正文", segs)
        assert res == [], "LLM 失败应降级为空列表"
        print(f"  P0-3 OK  LLM 不可达时返回 {res}（已记日志）")
    finally:
        # 还原环境变量，避免污染同进程其它用例
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    print("== P0 修复验证 ==")
    test_p0_1_no_stuck()
    test_p0_3_llm_fail_safe()
    # P0-2 由运行中的服务接口验证（见下方 curl）
    print("  P0-2 由 /api/tasks 接口验证（不应含 doc_text）")
