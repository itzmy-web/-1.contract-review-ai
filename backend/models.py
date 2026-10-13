"""数据模型与状态枚举（对应 PRD 2.4.2 / 2.4.4 / 2.4.6）。"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 状态枚举（PRD 2.4.4） ----------
class TaskStatus(str, Enum):
    PENDING = "pending"      # 待处理
    PARSING = "parsing"      # 解析中
    REVIEWING = "reviewing"  # 审查中
    BLOCKED = "blocked"      # 解析受阻
    COMPLETED = "completed"  # 审查完成


class WritebackStatus(str, Enum):
    NOT_WRITTEN = "not_written"  # 未回写
    WRITING = "writing"          # 回写中
    SUCCESS = "success"          # 回写成功
    FAILED = "failed"            # 回写失败


class RiskLevel(str, Enum):
    HIGH = "high"      # 高风险
    MEDIUM = "medium"  # 中风险
    LOW = "low"        # 低风险


class BizType(str, Enum):
    PROCUREMENT = "procurement"  # 采购
    SALES = "sales"              # 销售
    SERVICE = "service"          # 服务
    LABOR = "labor"              # 劳动
    LEASE = "lease"              # 租赁
    TECH = "tech"                # 技术
    OTHER = "other"              # 其他/未分类


# ---------- 合同元数据（PRD 2.4.4 条款提取与元数据解析） ----------
class ContractMeta(BaseModel):
    subject_name: Optional[str] = None        # 签约主体名称
    credit_code: Optional[str] = None          # 统一社会信用代码
    amount: Optional[float] = None             # 合同金额
    currency: Optional[str] = None             # 币种
    performance_period: Optional[str] = None   # 履行期限
    effective_condition: Optional[str] = None  # 生效条件
    contract_no: Optional[str] = None          # 合同编号（PRD 2.4.8 报告基本信息）
    department: Optional[str] = None           # 送审部门（PRD 2.4.8 报告基本信息）
    counterparty: Optional[str] = None         # 相对方主体名称（PRD 2.4.8）
    warranty_period: Optional[str] = None      # 质保期（审计规格书：结构化元数据）
    payment_terms: Optional[str] = None        # 付款节点/付款方式（审计规格书）
    sign_date: Optional[str] = None            # 签订日期（用于倒签检测）


# ---------- 条款切分（PRD 2.4.4 溯源要求） ----------
class ClauseSegment(BaseModel):
    clause_type: str                 # 条款类型（标的物/付款进度/违约责任...）
    text: str
    page: int = 1
    paragraph_index: int = 0
    start_char: int = 0              # 全文起止字符位置
    end_char: int = 0


# ---------- 风险条目（PRD 2.4.4 合规审查判定） ----------
class RiskItem(BaseModel):
    risk_id: str
    title: str                       # 风险项名称
    clause_type: str                 # 涉及条款类型
    level: RiskLevel                 # 高/中/低
    reason: str                      # 违规原因
    legal_basis: str                 # 法律合规依据
    snippet: str                     # 原文片段
    suggestion: str                  # 推荐修改示范条款
    # 原文定位（页码/段落/起止字符），highlight=False 表示缺失类风险不定位
    page: int = 1
    paragraph_index: int = 0
    start_char: int = 0
    end_char: int = 0
    highlight: bool = True
    # 建议来源：True 表示该条建议由大模型实时生成（未启用大模型时为规则库预置文本）
    by_llm: bool = False
    # 法务复核状态（PRD 2.4.10-5 支持法务编辑修改建议 / 2.4.2 采纳或调整）
    adopted: bool = False            # 法务是否已采纳该建议
    edited: bool = False             # 建议文本是否被法务人工修改过
    lawyer_comment: str = ""         # 法务对该风险的补充意见


# ---------- 审查任务（PRD 2.4.3 大盘 / 2.4.6 任务状态机） ----------
class ReviewTask(BaseModel):
    task_id: str
    contract_name: str
    applicant: str = "业务经办人"
    biz_type: BizType = BizType.PROCUREMENT
    amount: Optional[float] = None
    currency: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    risk_level: Optional[RiskLevel] = None
    writeback_status: WritebackStatus = WritebackStatus.NOT_WRITTEN
    created_at: str
    updated_at: str
    file_type: Optional[str] = None
    source_path: Optional[str] = None   # 源附件路径（重试/批量重审时复用）
    blocked_reason: Optional[str] = None
    parse_note: Optional[str] = None   # 解析备注（如：扫描件未装 OCR 引擎，采用模拟识别）
    doc_text: str = ""                 # 全文文本（前端原文渲染与高亮定位）
    meta: ContractMeta = Field(default_factory=ContractMeta)
    segments: list[ClauseSegment] = []
    risks: list[RiskItem] = []
    summary: str = ""               # 综合审查结论摘要
    llm_used: bool = False          # 本次审查是否实际调用过大模型
    legal_note: str = ""            # 法务人工批注
    writeback_log: Optional[str] = None
    # 失败告警邮件（PRD 2.4.7 延伸：解析失败 → 发送到指定邮箱）
    mail_status: Optional[str] = None   # None 未发送 | sent 已发送 | failed 发送失败
    mail_to: Optional[str] = None       # 收件人
    mail_sent_at: Optional[str] = None  # 发送成功时间
    mail_error: Optional[str] = None    # 发送失败原因
    # 归属账号（登录用户名 / 角色键）：用于「同一账号每次登录都能找回自己的历史任务」
    owner: str = ""
    # 人工修改留痕（管理员修改合同正文 / 审查意见后置 True，合同名追加「（已修改）」）
    modified: bool = False
    modified_at: Optional[str] = None
    modified_by: Optional[str] = None
    original_name: Optional[str] = None  # 修改前的原始合同名（避免重复追加标注）
    # 附件内容哈希：随任务持久化，使「附件去重」跨重启依然有效
    attach_hash: Optional[str] = None


# ---------- 规则与模板（PRD 2.4.1 规则与模板管理） ----------
class ReviewRule(BaseModel):
    rule_id: str
    name: str
    biz_types: list[str] = Field(default_factory=lambda: ["procurement", "sales", "service", "labor"])
    clause_types: list[str] = Field(default_factory=list)
    level: RiskLevel = RiskLevel.MEDIUM
    trigger: str                    # 触发说明（人类可读）
    reason: str
    legal_basis: str
    suggestion: str
    enabled: bool = True
    # P1-4 数据驱动：简单规则可用通用判定，免改代码
    # mode: custom(走注册表) | keyword(命中其一即风险) | regex(正则命中) | absence(该条款类型缺失即风险)
    mode: str = "custom"
    pattern: str = ""               # keyword 用 | 分隔；regex 为正则；absence 为条款类型名


class ClauseTemplate(BaseModel):
    template_id: str
    clause_type: str
    biz_type: str
    content: str                    # 标准示范条款


# ---------- 请求体 ----------
class AnnotateReq(BaseModel):
    legal_note: str = ""


class ImportSampleReq(BaseModel):
    name: str


class RiskEditReq(BaseModel):
    """法务编辑某条风险的建议 / 采纳 / 补充意见（PRD 2.4.10-5）。"""
    suggestion: Optional[str] = None
    adopted: Optional[bool] = None
    lawyer_comment: Optional[str] = None


class BatchReq(BaseModel):
    """大盘批量操作（PRD 2.4.5 批量操作）。"""
    task_ids: list[str] = Field(default_factory=list)
    action: str  # retry | delete


class PullReq(BaseModel):
    """从审批系统拉取待办任务（PRD 2.4.6/2.4.7）。"""
    ticket_ids: list[str] = Field(default_factory=list)


class LLMConfigReq(BaseModel):
    """大模型配置（可选能力）。字段为 None 表示不修改该项。"""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    proxy: Optional[str] = None
    timeout: Optional[str] = None
    suggest: Optional[bool] = None
    persist: bool = False   # True 则同时写入项目根 .env
