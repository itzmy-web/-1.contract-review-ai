"""内置智能助手（PRD 延伸：答疑 / 法律咨询 / 闲聊兜底 / 操作引导）。

三层职责：
  1) 教用户使用本系统（页面导航、上传格式、角色权限…）
  2) 解答法律专业问题（走 LLM，附条款号与免责提醒）
  3) 闲聊 / 打招呼兜底，并自然引导到合同审查操作

LLM 未配置时自动降级为本地知识库（关键词匹配），系统开箱可用。
"""
from __future__ import annotations

import logging

from . import llm

logger = logging.getLogger(__name__)

ALLOWED_VIEWS = {"dashboard", "workbench", "rules", "mailfail", "guide"}


# ---------------- 本地知识库（LLM 不可用时的兜底） ----------------
_KB = [
    (["怎么用", "如何使用", "上手", "教我", "使用流程", "怎么操作"],
     "四步上手：\n① 「审查大盘」上传合同（支持 Word / PDF / 扫描件图片）\n"
     "② 审查完成后进入「智能审查工作台」看风险（原文高亮 + 高/中/低分级 + 修改建议）\n"
     "③ 法务在右侧编辑/采纳建议、写批注，然后「写回审批意见」\n"
     "④ 「预览报告 / 导出 MD / 导出 PDF」。\n点侧栏「使用指南」可看完整说明。"),
    (["格式", "文件类型", "支持什么", "docx", "pdf", "图片", "扫描"],
     "支持三类：Word(.docx)、PDF(.pdf)、扫描件图片(.png/.jpg)。\n"
     "图片走本地 OCR：优先 RapidOCR（离线中文识别，模糊件会先做图像增强再重试）；"
     "若配置了大模型，还会对识别结果做一次上下文纠错；都不行时会进入「解析受阻」并给出明确原因，不会静默出错。"),
    (["并发", "同时", "几个任务", "排队", "一起审"],
     "后台默认 4 份合同并发审查（环境变量 REVIEW_CONCURRENCY 可调），多份同时上传会并行推进，不用排队。"),
    (["角色", "权限", "张三", "李四", "王五", "谁能"],
     "登录后由服务端签发令牌决定角色与权限，可在侧栏看到「当前用户」。"
     "管理员（张三）可管理规则、修改合同与意见；业务经办人（李四）上传/查看/导出；"
     "法务（王五）编辑采纳建议、写批注、写回审批；另有审计负责人/组员、查阅人等审计角色。"
     "自行注册或扫码登录的账号为只读（查看/提交/导出）。角色不可在页面切换。"),
    (["邮箱", "邮件", "通知", "qq"],
     "到「失败上传到QQ邮箱」页面配置：发件邮箱 + SMTP 授权码（在 QQ 邮箱 设置→账户→开启 IMAP/SMTP "
     "后生成，不是登录密码）。配置后，解析失败的合同会自动把失败详情发到你的邮箱。"),
    (["法律依据", "凭什么", "哪条法律", "依据", "条款"],
     "每条风险的卡片上都直接展示「合规依据」，例如：违约金过高 →《民法典》第585条；管辖缺失 →"
     "《民事诉讼法》第35条；验收缺失 →《民法典》第620/621条。规则页每条规则单独列出，报告里同样包含。"),
    (["你好", "您好", "hi", "hello", "在吗", "嗨"],
     "你好呀！我是合同审查助手 🤖\n可以教你用系统、解答法律问题，也可以闲聊。\n"
     "试试问我：「怎么上传合同」「违约金约定多高才有效」～"),
]

_GREET = ("你好！我是合同审批审查系统的智能助手 🤖\n"
          "我能帮你：① 上手这个系统（上传、看风险、写回审批）\n"
          "② 解答法律专业问题（违约金、管辖、保密、数据安全…）\n"
          "也可以直接闲聊。试试问我「怎么用」吧。")

_SYSTEM = (
    "你是「合同审批审查系统」的内置助手，三重职责：\n"
    "1) 教用户使用本系统。系统有八个页面：审查大盘（上传/导入/批量/告警/审批待办拉取）、"
    "智能审查工作台（原文高亮、风险卡片、法务编辑建议、写回、报告导出）、我的历史记录、"
    "合规规则与模板、审计工作台、查看面板与审计、法律搜索、失败上传到QQ邮箱，另有使用指南；"
    "上传支持 docx/pdf/png/jpg；审查=规则引擎+大模型语义研判；"
    "登录后由服务端签发令牌决定角色与权限（管理员/业务经办人/法务/审计负责人/审计组员/查阅人，"
    "自行注册或扫码登录为只读），角色不可在页面切换；"
    "默认4份并发审查；解析失败可自动邮件通知。\n"
    "2) 以中国现行法律回答专业问题（民法典、公司法、民事诉讼法、数据安全法、个人信息保护法等），"
    "给出条款号与实务建议，回复结尾加一句「以上为一般性法律信息，重要事项请咨询执业律师」。\n"
    "3) 闲聊与问候友好简短回应，并自然引导用户去上传合同或查看审查结果。\n"
    '只输出 JSON：{"reply": "回复正文（可\\n换行）", "view": null 或 "dashboard/workbench/rules/mailfail/guide 之一"}。\n'
    "只有当回复内容明确对应某个页面时才给 view，否则给 null。"
)


# ---------------- LLM 通道 ----------------
def _llm_reply(message: str) -> dict | None:
    if not llm.enabled():
        return None
    prompt = f"用户消息：{message}\n请按系统指令只输出 JSON。"
    try:
        data = llm.chat_json(prompt, system=_SYSTEM)
    except Exception:
        logger.exception("assistant: LLM 调用失败，转本地兜底")
        return None
    if not isinstance(data, dict):
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    view = data.get("view")
    return {"reply": reply, "mode": "llm",
            "view": view if view in ALLOWED_VIEWS else None}


def _local_reply(message: str) -> dict:
    m = message.strip().lower()
    best, best_hit = None, 0
    for kws, ans in _KB:
        hit = sum(1 for k in kws if k.lower() in m)
        if hit > best_hit:
            best, best_hit = ans, hit
    if best:
        return {"reply": best, "mode": "local", "view": None}
    return {"reply": _GREET, "mode": "local", "view": None}


def chat(message: str) -> dict:
    """统一入口：LLM 优先，失败/未配置降级本地知识库。"""
    message = (message or "").strip()[:2000]
    if not message:
        return {"reply": "请输入你的问题～", "mode": "local", "view": None}
    return _llm_reply(message) or _local_reply(message)
