"""失败任务邮件通知（QQ 邮箱 SMTP）。

PRD 2.4.7 异常任务阻塞保护与告警的延伸：合同上传/解析失败时，
自动把失败详情发送到指定邮箱（默认发给自己 = QQ 邮箱）。

配置优先级：**环境变量 > 项目根 .env**（与 llm.py 共用同一份 .env）。
未配置时安全降级：不发信、只记录，绝不阻断审查主流程。

支持的环境变量 / .env 键：
    SMTP_HOST    默认 smtp.qq.com
    SMTP_PORT    默认 465（SSL）
    SMTP_USER    发件邮箱，如 123456@qq.com
    SMTP_PASS    QQ 邮箱 **SMTP 授权码**（不是 QQ 登录密码！）
    MAIL_TO      收件人，默认 = SMTP_USER
    MAIL_ENABLED 1/0，默认 1（配好账号即发送）
"""
from __future__ import annotations

import logging
import re
import smtplib
import threading
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from . import llm

logger = logging.getLogger(__name__)


# ---------------- 配置读取 ----------------
def _cfg(key: str, default: str | None = None) -> str | None:
    """复用 llm 的 .env 加载逻辑，保证两个模块读同一份配置。"""
    return llm._cfg(key, default)


def host() -> str:
    return (_cfg("SMTP_HOST", "smtp.qq.com") or "").strip()


def port() -> int:
    try:
        return int(_cfg("SMTP_PORT", "465") or 465)
    except ValueError:
        return 465


def user() -> str:
    return (_cfg("SMTP_USER") or "").strip()


def password() -> str:
    return (_cfg("SMTP_PASS") or "").strip()


def mail_to() -> str:
    return (_cfg("MAIL_TO") or "").strip() or user()


def enabled() -> bool:
    """配了账号+授权码，且没有被显式关掉。"""
    if str(_cfg("MAIL_ENABLED", "1")).lower() in ("0", "false", "no", "off"):
        return False
    return bool(user() and password())


def status() -> dict:
    u, p = user(), password()
    return {
        "enabled": enabled(),
        "host": host(),
        "port": port(),
        "user": u,
        "user_masked": (u[:3] + "***" + u[u.find("@"):]) if "@" in u and len(u) > 6 else (u or ""),
        "has_pass": bool(p),
        "pass_masked": ("*" * 8) if p else "",
        "to": mail_to(),
        "env_file": str(llm.ENV_PATH),
        "env_file_exists": llm.ENV_PATH.exists(),
        "last_error": _LAST_ERROR,
        "last_at": _LAST_AT,
    }


def set_config(host_: str | None = None, port_: str | None = None, user_: str | None = None,
               password_: str | None = None, to_: str | None = None,
               enabled_: bool | None = None, persist: bool = False) -> dict:
    """更新配置（写进程环境变量；persist=True 同时写回 .env）。"""
    llm._loaded = True  # 阻止后续被 .env 覆盖
    updates: dict[str, str | None] = {}
    if host_ is not None:
        updates["SMTP_HOST"] = host_.strip()
    if port_ is not None:
        updates["SMTP_PORT"] = str(port_).strip()
    if user_ is not None:
        updates["SMTP_USER"] = user_.strip()
    if password_ is not None:
        updates["SMTP_PASS"] = password_.strip()
    if to_ is not None:
        updates["MAIL_TO"] = to_.strip()
    if enabled_ is not None:
        updates["MAIL_ENABLED"] = "1" if enabled_ else "0"
    import os
    for k, v in updates.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if persist:
        llm.save_env(updates)
    return status()


# ---------------- 运行时状态 ----------------
_LAST_ERROR = ""
_LAST_AT = ""
_LOCK = threading.Lock()


def _mark(ok: bool, msg: str) -> None:
    global _LAST_ERROR, _LAST_AT
    with _LOCK:
        _LAST_ERROR = "" if ok else msg
        _LAST_AT = datetime.now().isoformat(timespec="seconds")


# ---------------- 正文组装 ----------------
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"，。）)]*|(?:/[^\s'\"，。）)/]+){2,}/?")


def clean_reason(s: str | None) -> str:
    """清洗失败原因：去掉异常里的本机绝对路径。

    两个目的：① 不把本机目录结构写进对外邮件；② 提示更可读。
    """
    if not s:
        return s or ""
    s = _PATH_RE.sub("（路径已省略）", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _dept(task) -> str:
    """送审部门：优先 task.meta.department（解析器提取 / 上传时填写）。"""
    m = getattr(task, "meta", None)
    return (getattr(m, "department", "") or "") if m is not None else ""


def build_subject(task) -> str:
    return f"【合同审查】上传失败通知 - {task.contract_name}"


def build_text(task) -> str:
    return "\n".join([
        "您的合同未能通过系统解析，审查流程已中止，请按下方提示处理后重新提交。",
        "",
        f"合同名称：{task.contract_name}",
        f"任务编号：{task.task_id}",
        f"业务类型：{task.biz_type.value if hasattr(task.biz_type, 'value') else task.biz_type}",
        f"申请人：{task.applicant}",
        f"送审部门：{_dept(task) or '—'}",
        f"失败原因：{clean_reason(task.blocked_reason) or '解析失败'}",
        f"发生时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "处理建议：",
        "1. 确认文件为 .docx / .pdf / .png / .jpg 格式，且未加密、未损坏；",
        "2. 扫描件请保证清晰、页面完整；",
        "3. 处理后回到系统「审查大盘」重新上传，或在工作台点「重新审查」。",
        "",
        "—— 合同审批审查系统 · 自动通知（请勿直接回复）",
    ])


def build_html(task) -> str:
    bt = task.biz_type.value if hasattr(task.biz_type, "value") else str(task.biz_type)
    rows = [
        ("合同名称", task.contract_name),
        ("任务编号", task.task_id),
        ("业务类型", bt),
        ("申请人", task.applicant),
        ("送审部门", _dept(task) or "—"),
        ("失败原因", clean_reason(task.blocked_reason) or "解析失败"),
        ("发生时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    tr = "".join(
        f'<tr><td style="padding:9px 14px;color:#6b7280;font-size:13px;white-space:nowrap;'
        f'border-bottom:1px solid #f0f2f5">{k}</td>'
        f'<td style="padding:9px 14px;color:#111827;font-size:13px;'
        f'border-bottom:1px solid #f0f2f5"><b>{v}</b></td></tr>'
        for k, v in rows
    )
    return f"""<div style="font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:620px;margin:0 auto;
 background:#f6f8fb;padding:22px">
  <div style="background:#fff;border-radius:12px;overflow:hidden;
   box-shadow:0 2px 10px rgba(16,24,40,.07)">
    <div style="background:linear-gradient(135deg,#b3352f,#8f2521);padding:20px 22px;color:#fff">
      <div style="font-size:16px;font-weight:600;letter-spacing:.02em">合同上传失败通知</div>
      <div style="font-size:12px;opacity:.82;margin-top:5px">合同审批审查系统 · 自动告警</div>
    </div>
    <div style="padding:20px 22px 6px">
      <p style="margin:0 0 15px;font-size:13.5px;color:#374151;line-height:1.75">
        您的合同未能通过系统解析，<b>审查流程已中止</b>，请按下方提示处理后重新提交。
      </p>
    </div>
    <table style="width:100%;border-collapse:collapse">{tr}</table>
    <div style="padding:18px 22px 22px">
      <div style="font-size:12.5px;color:#6b7280;line-height:2">
        <b style="color:#374151">处理建议</b><br>
        1. 确认文件为 .docx / .pdf / .png / .jpg 格式，且未加密、未损坏；<br>
        2. 扫描件请保证清晰、页面完整；<br>
        3. 处理后回到系统「审查大盘」重新上传，或在工作台点「重新审查」。
      </div>
    </div>
  </div>
  <div style="text-align:center;color:#9aa2af;font-size:11.5px;margin-top:14px">
    本邮件由系统自动发送，请勿直接回复
  </div>
</div>"""


# ---------------- 发送 ----------------
def send(subject: str, text: str, html: str | None = None, to: str | None = None) -> tuple[bool, str]:
    """真正投递。返回 (是否成功, 说明)。"""
    if not enabled():
        msg = "未配置 SMTP_USER / SMTP_PASS（QQ 邮箱授权码），已跳过发送"
        _mark(False, msg)
        return False, msg
    rcpt = (to or mail_to()).strip()
    if not rcpt:
        msg = "未配置收件人 MAIL_TO"
        _mark(False, msg)
        return False, msg
    try:
        m = MIMEMultipart("alternative")
        m.attach(MIMEText(text, "plain", "utf-8"))
        if html:
            m.attach(MIMEText(html, "html", "utf-8"))
        m["Subject"] = Header(subject, "utf-8")
        m["From"] = formataddr((str(Header("合同审批审查系统", "utf-8")), user()))
        m["To"] = rcpt
        rcpts = [r.strip() for r in rcpt.replace("；", ";").replace(",", ";").split(";") if r.strip()]
        with smtplib.SMTP_SSL(host(), port(), timeout=15) as s:
            s.login(user(), password())
            s.sendmail(user(), rcpts, m.as_string())
        _mark(True, f"已发送至 {rcpt}")
        logger.info("失败通知邮件已发送：%s -> %s", task_subject_safe(subject), rcpt)
        return True, f"已发送至 {rcpt}"
    except smtplib.SMTPAuthenticationError:
        msg = "SMTP 认证失败：请确认使用的是 QQ 邮箱「SMTP 授权码」，而不是 QQ 登录密码"
        _mark(False, msg)
        logger.warning(msg)
        return False, msg
    except Exception as e:  # 网络/端口/收件人等各种异常
        msg = f"发送失败：{type(e).__name__}: {e}"
        _mark(False, msg)
        logger.warning(msg)
        return False, msg


def task_subject_safe(s: str) -> str:
    return (s or "")[:80]


def send_failure(task, to: str | None = None) -> tuple[bool, str]:
    """向任务写入邮件状态并发送。返回 (是否成功, 说明)。

    未配置时保持 mail_status=None（未发送），只记录原因 —— 避免把"没配通道"
    误报成"发送失败"，让列表上的状态语义准确。
    """
    if not enabled():
        msg = "未配置 SMTP_USER / SMTP_PASS（QQ 邮箱授权码），已跳过发送"
        task.mail_status = None
        task.mail_error = msg
        return False, msg
    ok, msg = send(build_subject(task), build_text(task), build_html(task), to=to)
    task.mail_status = "sent" if ok else "failed"
    task.mail_to = (to or mail_to())
    task.mail_error = "" if ok else msg
    task.mail_sent_at = datetime.now().isoformat(timespec="seconds") if ok else None
    return ok, msg


def send_failure_async(task, to: str | None = None) -> None:
    """后台线程发送，绝不阻塞审查主流程。"""
    def _run():
        try:
            send_failure(task, to=to)
        except Exception:
            logger.exception("后台发信异常")
    threading.Thread(target=_run, daemon=True).start()


def test_connection() -> tuple[bool, str]:
    """自检：发一封测试邮件给自己。"""
    if not enabled():
        return False, "未配置 SMTP_USER / SMTP_PASS，请先填写 QQ 邮箱地址与 SMTP 授权码"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "【合同审查】邮件通道测试"
    text = f"这是一封测试邮件，收到即表示失败告警通道已打通。\n发送时间：{now}"
    html = (f'<div style="font-family:sans-serif;padding:18px;color:#111827">'
            f'<h3 style="margin:0 0 10px">邮件通道测试成功</h3>'
            f'<p style="color:#4b5563;font-size:14px;line-height:1.8">'
            f'收到这封邮件，说明「合同上传失败 → QQ 邮箱告警」通道已打通。<br>'
            f'发送时间：{now}</p>'
            f'<p style="color:#9aa2af;font-size:12px">合同审批审查系统</p></div>')
    return send(subject, text, html)
