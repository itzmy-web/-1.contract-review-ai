"""智能审查编排（PRD 2.4.6 智能审查引擎 + 条款推荐引擎）。

流程：解析 → 规则引擎 → [可选]大模型语义研判 → [可选]大模型生成修改建议 → 风险汇总。

大模型为**可选增强**：未配置 LLM_API_KEY（环境变量或 .env）时全程降级为规则驱动，
开箱即跑；配置后由 `backend/llm.py` 统一调用（支持代理 / 自定义端点）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import llm
from .models import ClauseSegment, ReviewRule, ReviewTask, RiskLevel, RiskItem, TaskStatus
from .parsers import BlockedError, ParseResult, extract_metadata, parse_contract, segment_text
from .rules import evaluate, overall_level, _locate

logger = logging.getLogger(__name__)

# 单次送入大模型的风险数上限 / 原文片段长度（控制 token）
MAX_LLM_RISKS = 20
SNIPPET_LIMIT = 120


def _llm_semantic(doc_text: str, segments: list[ClauseSegment]) -> list[RiskItem]:
    """大模型语义研判：产出规则之外的风险（无 Key / 失败时返回空）。

    要求模型返回 locate（原文中可定位的短句），命中即补全 页码/起止字符/证据片段；
    未返回 locate 的风险降级为不可高亮。
    """
    if not llm.enabled():
        return []
    segments_text = "\n".join(f"【{s.clause_type}】{s.text}" for s in segments[:30])
    prompt = (
        "你是企业法务 AI。请审查以下合同，识别法律合规与商业风险，"
        '仅输出 JSON 数组，每项含：title/clause_type/level(high|medium|low)/'
        "reason/legal_basis/suggestion/locate（locate 填合同原文中出现过的、用于定位的短句，"
        "找不到可填空字符串）。\n" + segments_text[:6000]
    )
    items = llm.chat_json(prompt)
    if not isinstance(items, list):
        return []
    out: list[RiskItem] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        locate = (it.get("locate") or "").strip()
        span = _locate(doc_text, segments, locate) if locate else None
        snippet = "（大模型语义研判）"
        if span:
            snippet = doc_text[max(0, span["start"] - 12): span["end"] + 12].replace("\n", " ")
        try:
            lvl = RiskLevel(it.get("level", "medium"))
        except ValueError:
            lvl = RiskLevel.MEDIUM
        out.append(RiskItem(
            risk_id=f"llm-{i:03d}", title=it.get("title", "大模型识别风险"),
            clause_type=it.get("clause_type", "其他条款"), level=lvl,
            reason=it.get("reason", ""), legal_basis=it.get("legal_basis", ""),
            snippet=snippet, suggestion=it.get("suggestion", ""),
            highlight=bool(span), by_llm=True, **(span or {})))
    return out


def _llm_recommend(doc_text: str, risks: list[RiskItem]) -> int:
    """用大模型为每条风险实时生成「可直接采用的示范条款」，原地覆盖 suggestion。

    返回被改写的条数。失败/未启用则返回 0，保留规则库预置建议（确定性兜底）。
    """
    if not llm.enabled() or not llm.use_for_suggest() or not risks:
        return 0
    batch = risks[:MAX_LLM_RISKS]
    payload = [
        {"index": i, "title": r.title, "clause_type": r.clause_type,
         "level": r.level.value, "reason": r.reason[:SNIPPET_LIMIT],
         "原文": (r.snippet or "")[:SNIPPET_LIMIT]}
        for i, r in enumerate(batch)
    ]
    prompt = (
        "你是企业法务 AI。下面是一份合同的**风险清单**。请为每条风险给出**可直接采用的中文示范条款**，"
        "要求具体、可落地（1-2 句，含明确的数字/比例/期限等），不要泛泛而谈，不要复述问题。\n"
        '只输出 JSON：{"suggestions":[{"index":<与输入一致的整数>,"suggestion":"<示范条款文本>"}]}\n\n'
        "风险清单：\n" + json.dumps(payload, ensure_ascii=False)
    )
    data = llm.chat_json(prompt, "你是严格按 JSON 格式输出的企业法务助手。")
    if not isinstance(data, dict):
        return 0
    items = data.get("suggestions")
    if not isinstance(items, list):
        return 0
    changed = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError):
            continue
        sug = (it.get("suggestion") or "").strip()
        if 0 <= idx < len(batch) and sug:
            batch[idx].suggestion = sug
            batch[idx].by_llm = True
            changed += 1
    return changed


def build_summary(task: ReviewTask) -> str:
    highs = [r for r in task.risks if r.level == RiskLevel.HIGH]
    meds = [r for r in task.risks if r.level == RiskLevel.MEDIUM]
    lows = [r for r in task.risks if r.level == RiskLevel.LOW]
    lvl = task.risk_level.value if task.risk_level else "none"
    verdict = {"high": "建议拒绝/重大整改", "medium": "需整改后通过", "low": "原则通过", "none": "通过"}[lvl]
    parts = [
        f"综合风险等级：{lvl}（{verdict}）。",
        f"共识别风险 {len(task.risks)} 项：高风险 {len(highs)}、中风险 {len(meds)}、低风险 {len(lows)}。",
    ]
    if highs:
        parts.append("高风险要点：" + "；".join(r.title for r in highs[:5]) + "。")
    if meds:
        parts.append("中风险要点：" + "；".join(r.title for r in meds[:5]) + "。")
    if task.llm_used:
        parts.append("（本次已调用大模型进行语义研判/建议生成）")
    return "\n".join(parts)


def run_review(task: ReviewTask, rules: list[ReviewRule], file_path: str) -> None:
    """执行一次完整审查，原地更新 task（状态机：parsing -> reviewing -> completed/blocked）。

    P0-1：整段逻辑兜底——任何非预期异常都置为 blocked 并写明原因，
    绝不让任务静默停留在中间态（reviewing）。
    """
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    # 复跑/重试时清理上一轮残留的受阻原因，避免成功完成后仍显示旧的 blocked_reason
    task.blocked_reason = None
    try:
        task.status = TaskStatus.PARSING
        try:
            parsed = parse_contract(file_path)
        except BlockedError as e:
            task.status = TaskStatus.BLOCKED
            task.blocked_reason = e.reason
            return
        _run_with_parsed(task, rules, parsed)
    except Exception as e:
        # 兜底：解析/审查中任何异常 -> blocked + 原因 + 日志，避免任务卡死
        logger.exception("审查任务 %s 异常中止", task.task_id)
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = f"审查异常：{type(e).__name__}: {e}"
    finally:
        task.updated_at = datetime.now().isoformat(timespec="seconds")


def run_review_on_text(task: ReviewTask, rules: list[ReviewRule], text: str) -> None:
    """用用户修改后的正文重新审查（不重新解析文件）。"""
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    task.blocked_reason = None
    try:
        parsed = ParseResult(doc_text=text, segments=segment_text(text),
                             meta=extract_metadata(text), note="用户编辑后重新提交")
        _run_with_parsed(task, rules, parsed, parse_note="用户编辑后重新提交")
    except Exception as e:
        logger.exception("审查任务 %s（文本重提）异常中止", task.task_id)
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = f"审查异常：{type(e).__name__}: {e}"
    finally:
        task.updated_at = datetime.now().isoformat(timespec="seconds")


def _run_with_parsed(task: ReviewTask, rules: list[ReviewRule], parsed: ParseResult,
                     parse_note: str | None = None) -> None:
    """解析完成后：规则引擎 + 大模型研判 + 建议生成 + 风险汇总（run_review / run_review_on_text 共用）。"""
    task.status = TaskStatus.REVIEWING
    task.doc_text = parsed.doc_text
    task.segments = parsed.segments
    task.meta = parsed.meta
    task.amount = parsed.meta.amount
    task.currency = parsed.meta.currency
    task.parse_note = parse_note or parsed.note or None

    # 1) 规则引擎（始终运行，确定性）
    rule_risks = evaluate(parsed.doc_text, parsed.segments, parsed.meta, task.biz_type, rules)
    # 2) 大模型语义研判（可选，补充规则覆盖不到的语义风险）
    llm_risks = _llm_semantic(parsed.doc_text, parsed.segments)
    task.risks = rule_risks + llm_risks
    # 3) 大模型为每条风险实时生成示范条款（可选，失败保留规则库预置建议）
    generated = _llm_recommend(parsed.doc_text, task.risks)

    task.llm_used = bool(llm.enabled()) and (bool(llm_risks) or generated > 0)
    task.risk_level = overall_level(task.risks)
    task.summary = build_summary(task)
    task.status = TaskStatus.COMPLETED
