"""合规审查规则引擎（PRD 2.4.4 + 2.4.6）。

P1-4 改造：规则引擎数据驱动。
  - 简单规则通过 mode/pattern 即可配置，无需改代码：
      mode=keyword  pattern="词1|词2"       命中其一即风险
      mode=regex    pattern="正则"            正则命中即风险
      mode=absence  pattern="条款类型"         该条款类型缺失即风险
      mode=absence_text pattern="关键词"       全文未出现该关键词即风险（用于「不可抗力」等无独立条款类型者）
  注意：absence / absence_text / keyword / regex 四类模式的 pattern **不得为空**（空 pattern
  会让 absence 恒触发、keyword 永不触发），`_generic` 已对该情况做显式短路保护。
  - 复杂规则（需跨字段判断）走 CUSTOM_DETECTORS 注册表，按 rule_id 分发。
新增规则只需在 DEFAULT_RULES 加一条数据（或经 /api/rules 接口），不必动逻辑。
"""
from __future__ import annotations

import re
from typing import Optional

from .models import BizType, ClauseSegment, ContractMeta, RiskItem, RiskLevel, ReviewRule

# 各业务类型必备条款（缺失即风险）
REQUIRED_CLAUSES = {
    "procurement": ["标的物条款", "付款进度", "验收标准", "违约责任", "保密义务", "争议解决管辖", "知识产权"],
    "sales": ["标的物条款", "付款进度", "验收标准", "违约责任", "争议解决管辖"],
    "service": ["标的物条款", "付款进度", "验收标准", "违约责任", "保密义务", "争议解决管辖"],
    "labor": ["标的物条款", "付款进度", "违约责任", "保密义务", "争议解决管辖"],
}

# 默认规则集（初始化规则与模板管理页）
DEFAULT_RULES = [
    ReviewRule(rule_id="R-H1", name="主体资质缺失/经营异常", level=RiskLevel.HIGH,
               clause_types=["其他条款"], mode="custom",
               trigger="签约主体名称或统一社会信用代码缺失，或文本含「经营异常」",
               reason="主体信息缺失或相对方被列入经营异常，合同效力与履约能力存疑。",
               legal_basis="《民法典》第143条（民事法律行为有效要件）；市场主体登记相关规定。",
               suggestion="补充完整签约主体名称与统一社会信用代码，并核实相对方经营状态后签署。"),
    ReviewRule(rule_id="R-H2", name="违约责任严重不对等", level=RiskLevel.HIGH,
               clause_types=["违约责任"], mode="keyword",
               pattern="无限责任|由甲方承担全部责任|甲方承担无限|甲方承担全部赔偿责任|无上限的赔偿责任",
               trigger="出现「无限责任」「甲方承担全部责任」「无上限的赔偿责任」等表述",
               reason="违约责任严重不对等，我方单方承担无限/全部责任，显失公平。",
               legal_basis="《民法典》第6条（公平原则）、第497条（格式条款无效情形）。",
               suggestion="修改为对等责任，如「双方对因各自过错造成的损失分别承担责任，任一方赔偿责任不超过合同总额的 20%」。"),
    ReviewRule(rule_id="R-H3", name="争议管辖条款缺失", level=RiskLevel.HIGH,
               clause_types=["争议解决管辖"], mode="absence", pattern="争议解决管辖",
               trigger="合同未约定争议解决/管辖条款",
               reason="缺少争议解决与管辖约定，维权时管辖法院不明，增加诉讼成本与不确定性。",
               legal_basis="《民事诉讼法》第35条（协议管辖）。",
               suggestion="约定由我方所在地有管辖权的人民法院诉讼，或约定境内仲裁机构仲裁。"),
    ReviewRule(rule_id="R-H4", name="付款无前置验收条件", level=RiskLevel.HIGH,
               biz_types=["procurement", "sales", "service"], mode="custom",
               clause_types=["付款进度"],
               trigger="存在付款条款但缺少验收标准条款（付款前置验收缺失）",
               reason="未设置付款前置验收条件，付款后即丧失质量抗辩权，资金风险高。",
               legal_basis="《民法典》第626条（买受人检验义务）、第628条。",
               suggestion="增加「买方在验收合格并出具验收报告后 X 个工作日内支付对应款项」的付款前置条件。"),
    ReviewRule(rule_id="R-H5", name="争议管辖约定不利", level=RiskLevel.HIGH,
               biz_types=["procurement", "sales", "service"], mode="custom",
               clause_types=["争议解决管辖"],
               trigger="管辖约定为相对方所在地或境外法院/仲裁机构（未约定我方所在地）",
               reason="管辖地约定对我方不利（约定相对方所在地或境外机构），诉讼/仲裁成本与结果不确定性显著增加。",
               legal_basis="《民事诉讼法》第35条（协议管辖应选择与争议有实际联系地点的法院）。",
               suggestion="修改为「因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决」。"),
    ReviewRule(rule_id="R-H6", name="知识产权归属不利", level=RiskLevel.HIGH,
               clause_types=["知识产权"], mode="keyword",
               pattern="知识产权归乙方|知识产权由乙方|知识产权归属乙方|知识产权归供应商|归乙方所有",
               trigger="知识产权约定归乙方/供应商所有（如「知识产权归乙方」「归乙方所有」）",
               reason="核心知识产权（含定制开发源代码）归属相对方，我方投入无法沉淀资产。",
               legal_basis="《民法典》第123条（知识产权客体）；委托开发合同相关规定。",
               suggestion="修改为「定制开发部分之源代码及全部知识产权归甲方所有，乙方保留为履行本合同所必需的使用许可」。"),
    ReviewRule(rule_id="R-H7", name="合同倒签疑点", level=RiskLevel.HIGH,
               biz_types=["procurement", "sales", "service", "labor", "lease", "tech", "other"],
               clause_types=["其他条款"], mode="custom",
               trigger="签订日期晚于合同履行起始日期（倒签）",
               reason="合同签订日期晚于约定的履行起始日期，存在倒签疑点，影响合同效力认定与内控合规。",
               legal_basis="《民法典》第490条（合同成立时间）；企业内部合同管理制度。",
               suggestion="核实实际签订时间与履约起始时间，补充倒签说明或重新约定履行期限。"),
    ReviewRule(rule_id="R-M1", name="保密义务缺期限", level=RiskLevel.MEDIUM,
               clause_types=["保密义务"], mode="custom",
               trigger="存在保密条款但未约定保密期限（年/月）",
               reason="保密义务未明确期限，约束期不明，存在泄密后无法追责风险。",
               legal_basis="《反不正当竞争法》第9条（商业秘密保护）。",
               suggestion="明确保密期限，如「保密义务在本合同终止后持续 3 年」。"),
    ReviewRule(rule_id="R-M2", name="不可抗力未约通知时效", level=RiskLevel.MEDIUM,
               clause_types=["其他条款"], mode="custom",
               trigger="文本含「不可抗力」但未约定通知时限（日/小时/书面通知）",
               reason="不可抗力条款未明确通知时效，争议时难以举证免责。",
               legal_basis="《民法典》第590条（不可抗力通知与证明）。",
               suggestion="补充「受影响方应在不可抗力发生后 5 个工作日内书面通知对方并提供证明」。"),
    ReviewRule(rule_id="R-M3", name="违约金比例超合理范围", level=RiskLevel.MEDIUM,
               clause_types=["违约责任"], mode="custom",
               trigger="违约金比例超过合同总额 20%，或约定「赔偿全部损失」",
               reason="违约金比例超出法定合理范围或约定无上限，可能被认定过高/显失公平。",
               legal_basis="《民法典》第585条（违约金调整：过分高于损失的可请求酌减）。",
               suggestion="将违约金调整为「不超过合同总额的 20%」，并以实际损失为赔偿上限。"),
    ReviewRule(rule_id="R-M4", name="验收标准与期限缺失", level=RiskLevel.MEDIUM,
               # 仅适用于有「交付—验收」环节的合同；劳动合同没有验收概念（与 REQUIRED_CLAUSES
               # 中 labor 不含「验收标准」保持一致），否则会稳定误报。
               biz_types=["procurement", "sales", "service"],
               clause_types=["验收条款"], mode="absence", pattern="验收标准",
               trigger="全文未发现「验收」相关条款",
               reason="缺少验收标准与验收期限，交付质量与付款节点将无从认定，极易产生扯皮。",
               legal_basis="《民法典》第620条（买受人应在约定或合理期间内检验）、第621条（检验期间内通知义务）。",
               suggestion="补充：「乙方交付后 10 个工作日内，甲方按《功能需求说明书》完成验收并出具书面验收报告；逾期未提出书面异议视为验收合格。」"),
    ReviewRule(rule_id="R-M5", name="保密范围约定不明", level=RiskLevel.MEDIUM,
               clause_types=["保密条款"], mode="custom",
               trigger="保密条款未界定「保密信息」的范围",
               reason="保密范围未界定，守约方难以举证、违约方易于抗辩，保密条款形同虚设。",
               legal_basis="《反不正当竞争法》第9条（商业秘密须「不为公众所知悉、经权利人采取保密措施」）；《民法典》第501条。",
               suggestion="补充定义：「保密信息包括本合同及其附件、技术资料、商业数据与双方往来文件，书面标注「保密」或依其性质应视为保密的信息均属之。」"),
    ReviewRule(rule_id="R-M6", name="数据安全与个人信息保护缺失", level=RiskLevel.MEDIUM,
               clause_types=["数据条款"], mode="custom",
               trigger="全文未发现「数据安全 / 个人信息」相关约定",
               reason="涉及数据处理却未约定安全义务与合规责任，双方均可能面临行政责任。",
               legal_basis="《数据安全法》第27条（数据安全保护义务）；《个人信息保护法》第21条（委托处理个人信息的约定要求）。",
               suggestion="补充：「乙方处理甲方数据应遵守《数据安全法》《个人信息保护法》，采取加密与访问控制措施，不得超出本合同目的使用；发生数据泄露应在 24 小时内通知甲方并承担相应责任。」"),
    # 注意：rule_id 必须全局唯一（规则页的启停/编辑/删除都按 rule_id 定位）。
    # 本条原误用 R-H7（与「合同倒签疑点」重号），会导致两条规则互相覆盖，故改为 R-H8。
    ReviewRule(rule_id="R-H8", name="单方解除权不对等", level=RiskLevel.HIGH,
               clause_types=["合同解除"], mode="keyword",
               pattern="仅甲方有权解除|仅乙方有权解除|甲方有权单方解除|乙方有权单方解除"
                       "|甲方有权随时解除|乙方有权随时解除|甲方可随时解除合同|乙方可随时解除合同",
               trigger="仅一方享有任意解除权（解除权|有权解除 与 仅甲方|仅乙方 同时命中近似表述）",
               reason="解除权仅约定单方享有且无对等约束，相对方履约预期极不稳定，构成重大权利失衡。",
               legal_basis="《民法典》第562条（协议解除）、第563条（法定解除权应系于法定事由）；第497条（排除对方主要权利的格式条款无效）。",
               suggestion="改为对等：「任何一方均可在对方根本违约且经书面催告后 15 日内仍未纠正时，书面通知解除本合同。」"),
    ReviewRule(rule_id="R-L1", name="不可抗力条款缺失", level=RiskLevel.LOW,
               clause_types=["不可抗力"], mode="absence_text", pattern="不可抗力",
               trigger="全文未发现「不可抗力」条款",
               reason="未约定不可抗力条款，虽可适用法定规则，但范围、通知与后果处理缺乏明确预期。",
               legal_basis="《民法典》第180条（不可抗力免责）、第590条（通知与证明义务）。",
               suggestion="补充：「因不可抗力不能履约的一方应在 7 日内书面通知对方并提供证明，双方按影响程度协商变更或解除合同。」"),
]


# ---------------- 定位辅助 ----------------
def _locate(doc_text: str, segments: list[ClauseSegment], needle: str) -> Optional[dict]:
    idx = doc_text.find(needle)
    if idx == -1:
        return None
    end = idx + len(needle)
    seg = next((s for s in segments if s.start_char <= idx < s.end_char), None)
    return {"start": idx, "end": end, "page": seg.page if seg else 1,
            "pidx": seg.paragraph_index if seg else 0}


def _locate_span(doc_text: str, segments: list[ClauseSegment], start: int, end: int) -> Optional[dict]:
    seg = next((s for s in segments if s.start_char <= start < s.end_char), None)
    return {"start": start, "end": end, "page": seg.page if seg else 1,
            "pidx": seg.paragraph_index if seg else 0}


# ---------------- 复杂规则注册表 ----------------
def _det_h1(doc_text, segments, meta, rule) -> list[RiskItem]:
    if meta.subject_name is None or meta.credit_code is None or "经营异常" in doc_text:
        reason = "签约主体名称或统一社会信用代码缺失" if (meta.subject_name is None or meta.credit_code is None) else "相对方被列入经营异常"
        return [RiskItem(risk_id="", title=rule.name, clause_type="其他条款", level=rule.level,
                         reason=reason, legal_basis=rule.legal_basis,
                         snippet="（主体信息缺失或含经营异常表述）", suggestion=rule.suggestion, highlight=False)]
    return []


def _det_h4(doc_text, segments, meta, rule) -> list[RiskItem]:
    has_pay = any(s.clause_type == "付款进度" for s in segments)
    has_acc = any(s.clause_type == "验收标准" for s in segments)
    if has_pay and not has_acc:
        return [RiskItem(risk_id="", title=rule.name, clause_type="付款进度", level=rule.level,
                         reason=rule.reason, legal_basis=rule.legal_basis,
                         snippet="（存在付款条款但未约定验收标准）", suggestion=rule.suggestion, highlight=False)]
    return []


def _det_m1(doc_text, segments, meta, rule) -> list[RiskItem]:
    seg = next((s for s in segments if s.clause_type == "保密义务"), None)
    if seg and not re.search(r"\d+\s*(年|月|日|天|工作日)", seg.text):
        span = _locate(doc_text, segments, "保密")
        return [RiskItem(risk_id="", title=rule.name, clause_type="保密义务", level=rule.level,
                         reason=rule.reason, legal_basis=rule.legal_basis,
                         snippet=seg.text[:40], suggestion=rule.suggestion,
                         highlight=True, **(span or {}))]
    return []


def _det_m2(doc_text, segments, meta, rule) -> list[RiskItem]:
    if "不可抗力" in doc_text:
        idx = doc_text.find("不可抗力")
        window = doc_text[idx: idx + 200]
        if not re.search(r"\d+\s*(个)?\s*(工作日|日|天|小时)", window):
            span = _locate(doc_text, segments, "不可抗力")
            return [RiskItem(risk_id="", title=rule.name, clause_type="其他条款", level=rule.level,
                             reason=rule.reason, legal_basis=rule.legal_basis,
                             snippet="不可抗力…（未约通知时效）", suggestion=rule.suggestion,
                             highlight=True, **(span or {}))]
    return []


def _det_m3(doc_text, segments, meta, rule) -> list[RiskItem]:
    out = []
    m = re.search(r"违约金[^\n]{0,30}?(\d{1,3})\s*%", doc_text)
    if m and int(m.group(1)) > 20:
        span = _locate_span(doc_text, segments, m.start(), m.end())
        out.append(RiskItem(risk_id="", title=rule.name, clause_type="违约责任", level=rule.level,
                            reason=rule.reason, legal_basis=rule.legal_basis,
                            snippet=m.group(0), suggestion=rule.suggestion,
                            highlight=True, **(span or {})))
    # 仅精确「赔偿全部损失」才计，避免与 R-H2「无上限的赔偿责任」重复
    if "赔偿全部损失" in doc_text:
        span = _locate(doc_text, segments, "赔偿全部损失")
        out.append(RiskItem(risk_id="", title=rule.name, clause_type="违约责任", level=rule.level,
                            reason=rule.reason, legal_basis=rule.legal_basis,
                            snippet="赔偿全部损失", suggestion=rule.suggestion,
                            highlight=True, **(span or {})))
    return out


def _det_h5(doc_text, segments, meta, rule) -> list[RiskItem]:
    """管辖约定不利：约定相对方所在地或境外法院/仲裁机构 → 高风险。

    与 R-H3（缺失管辖）互斥：无管辖条款时 R-H3 命中，此处天然不触发。
    注：仅以「境外/国外/外国」等表述判定，不涉及中国港澳台。
    """
    seg = next((s for s in segments if s.clause_type == "争议解决管辖"), None)
    if not seg:
        return []
    m = (re.search(r"(?:乙方|对方|供方|客户|买方)所在地", seg.text)
         or re.search(r"(?:境外|国外|外国|新加坡|美国|英国|开曼|维尔京)", seg.text))
    if not m:
        return []
    start, end = seg.start_char + m.start(), seg.start_char + m.end()
    span = _locate_span(doc_text, segments, start, end)
    return [RiskItem(risk_id="", title=rule.name, clause_type="争议解决管辖", level=rule.level,
                     reason=rule.reason, legal_basis=rule.legal_basis,
                     snippet=seg.text[:60].replace("\n", " "), suggestion=rule.suggestion,
                     highlight=True, **(span or {}))]


def _det_backdate(doc_text, segments, meta, rule):
    """合同倒签疑点：签订日期晚于履行起始日期（审计规格书二.1）。"""
    if not meta.sign_date or not meta.performance_period:
        return []

    def _ymd(s):
        m = re.search(r"(\d{4})[年./\-](\d{1,2})[月./\-](\d{1,2})", s)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

    sd = _ymd(meta.sign_date)
    m = re.search(r"(\d{4})[年./\-](\d{1,2})[月./\-](\d{1,2})", meta.performance_period)
    if not sd or not m:
        return []
    start = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if sd <= start:
        return []
    return [RiskItem(risk_id="", title=rule.name, clause_type="其他条款", level=rule.level,
                     reason=rule.reason, legal_basis=rule.legal_basis,
                     snippet=f"签订日期 {meta.sign_date} 晚于履约起始 {meta.performance_period[:20]}",
                     suggestion=rule.suggestion, highlight=False)]


def _det_m6(doc_text, segments, meta, rule) -> list[RiskItem]:
    """数据安全与个人信息保护缺失：**仅当合同确实涉及数据处理时**才判定。

    原实现是 pattern 为空的 absence 规则 → `'' in present` 恒为 False，
    等于对所有采购/销售/劳动合同都恒报「数据安全缺失」，属确定性误报。
    正确口径（与本规则 trigger 描述一致）：合同出现**数据处理类**表述（个人信息、隐私、
    数据安全/处理/共享/交换/泄露/出境、业务数据、用户或客户数据）、且全文没有数据安全
    条款时，才算缺失。刻意不采用过于宽泛的「数据」二字——保密条款里出现「经营数据」
    之类的词并不代表存在个人信息处理活动（否则会误伤普通采购/劳动合同）。
    """
    if any(s.clause_type == "数据安全" for s in segments):
        return []
    if not re.search(r"个人信息|隐私|数据安全|数据处理|数据共享|数据交换|数据泄露|数据出境"
                     r"|业务数据|用户数据|客户数据", doc_text):
        return []
    return [RiskItem(risk_id="", title=rule.name, clause_type="数据条款", level=rule.level,
                     reason=rule.reason, legal_basis=rule.legal_basis,
                     snippet="（合同涉及数据处理，但未约定数据安全 / 个人信息保护义务）",
                     suggestion=rule.suggestion, highlight=False)]


def _det_m5(doc_text, segments, meta, rule) -> list[RiskItem]:
    """保密范围约定不明：存在保密条款，但未界定「保密信息」的范围。

    判定：取保密条款段落文本，若既未出现「保密信息」也未出现「保密范围/保密内容」，
    则认为范围未界定（确定性判定，避免把「有保密条款」误当「已界定范围」）。
    """
    seg = next((s for s in segments if s.clause_type == "保密义务"), None)
    if not seg:
        return []
    if ("保密信息" in seg.text) or ("保密范围" in seg.text) or ("保密内容" in seg.text):
        return []
    span = _locate(doc_text, segments, "保密")
    return [RiskItem(risk_id="", title=rule.name, clause_type="保密条款", level=rule.level,
                     reason=rule.reason, legal_basis=rule.legal_basis,
                     snippet=seg.text[:60].replace("\n", " "), suggestion=rule.suggestion,
                     highlight=True, **(span or {}))]


CUSTOM_DETECTORS = {
    "R-H1": _det_h1, "R-H4": _det_h4, "R-H5": _det_h5,
    "R-M1": _det_m1, "R-M2": _det_m2, "R-M3": _det_m3, "R-M5": _det_m5, "R-M6": _det_m6,
    "R-H7": _det_backdate,
}


def _generic(r: ReviewRule, doc_text: str, segments: list[ClauseSegment], present: set):
    # 显式短路：这四类模式若 pattern 为空（配置疏漏），一律视为未命中，
    # 避免 absence 恒真（把标准合同也判成缺失）或 keyword 恒假（规则形同虚设）。
    if r.mode in ("absence", "absence_text", "keyword", "regex") and not r.pattern:
        return False, None, None
    if r.mode == "absence":
        if r.pattern in present:
            return False, None, None
        return True, None, f"（未约定「{r.pattern}」条款）"
    if r.mode == "absence_text":
        # 全文关键词缺失判定：适用于「不可抗力」这类没有独立条款类型、只能按文本判定的条款
        if r.pattern and r.pattern in doc_text:
            return False, None, None
        return True, None, f"（全文未出现「{r.pattern}」的约定）"
    if r.mode == "keyword":
        for kw in r.pattern.split("|"):
            if kw and kw in doc_text:
                return True, _locate(doc_text, segments, kw), kw
        return False, None, None
    if r.mode == "regex":
        m = re.search(r.pattern, doc_text)
        if m:
            return True, _locate_span(doc_text, segments, m.start(), m.end()), m.group(0)
        return False, None, None
    return False, None, None


def _build(r: ReviewRule, span: Optional[dict], snippet: str) -> RiskItem:
    base = dict(title=r.name, clause_type=r.clause_types[0] if r.clause_types else "其他条款",
                level=r.level, reason=r.reason, legal_basis=r.legal_basis,
                suggestion=r.suggestion, snippet=snippet or "（未命中定位）")
    if span:
        return RiskItem(risk_id="", highlight=True, start_char=span["start"], end_char=span["end"],
                        page=span["page"], paragraph_index=span["pidx"], **base)
    return RiskItem(risk_id="", highlight=False, **base)


def _required_missing(doc_text: str, segments: list[ClauseSegment], biz_type: BizType) -> list[RiskItem]:
    present = {s.clause_type for s in segments}
    out = []
    for ctype in REQUIRED_CLAUSES.get(biz_type.value, []):
        if ctype not in present:
            out.append(RiskItem(risk_id="", title=f"必备条款缺失：{ctype}", clause_type=ctype,
                                 level=RiskLevel.HIGH, reason=f"合同缺少必备条款「{ctype}」，核心权利义务约定不明。",
                                 legal_basis="合同必备条款完整性要求；相关司法解释。",
                                 snippet=f"（未检测到「{ctype}」条款）",
                                 suggestion=f"补充「{ctype}」标准条款，明确双方权利义务。", highlight=False))
    return out


def evaluate(doc_text: str, segments: list[ClauseSegment], meta: ContractMeta,
             biz_type: BizType, rules: list[ReviewRule]) -> list[RiskItem]:
    """对解析结果执行规则引擎，返回风险条目列表（末尾统一编号）。"""
    risks: list[RiskItem] = []
    bt = biz_type.value
    present = {s.clause_type for s in segments}
    for r in rules:
        if not r.enabled:
            continue
        if bt not in r.biz_types:
            continue
        if r.mode == "custom":
            fn = CUSTOM_DETECTORS.get(r.rule_id)
            if fn:
                risks += fn(doc_text, segments, meta, r)
            continue
        flag, span, snippet = _generic(r, doc_text, segments, present)
        if flag:
            risks.append(_build(r, span, snippet))
    risks += _required_missing(doc_text, segments, biz_type)
    for i, r in enumerate(risks, 1):
        r.risk_id = f"risk-{i:03d}"
    return risks


def overall_level(risks: list[RiskItem]) -> Optional[RiskLevel]:
    if any(r.level == RiskLevel.HIGH for r in risks):
        return RiskLevel.HIGH
    if any(r.level == RiskLevel.MEDIUM for r in risks):
        return RiskLevel.MEDIUM
    if risks:
        return RiskLevel.LOW
    return None
