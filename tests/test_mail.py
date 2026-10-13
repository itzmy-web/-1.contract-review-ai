"""失败上传邮件通知（PRD 2.4.7 延伸）测试。

全部离线：不发真实邮件，只验证配置降级、状态写入与接口结构。
"""
from backend import mailer, store
from backend import main as M
from backend.models import BizType, ReviewTask
from backend.rules import DEFAULT_RULES  # noqa: F401  (保持与其它用例一致的导入习惯)


def _mk_task(**kw) -> ReviewTask:
    """造一个 blocked 任务（不落库）。"""
    t = ReviewTask(task_id="TMAIL001", contract_name="测试合同.docx",
                   applicant="测试经办人", biz_type=BizType.PROCUREMENT,
                   created_at="2026-09-19 09:00:00", updated_at="2026-09-19 09:00:00")
    t.status = "blocked"
    t.blocked_reason = "Word 文档打开失败（可能加密或损坏）"
    t.meta.department = "法务部"
    for k, v in kw.items():
        setattr(t, k, v)
    return t


# ---------------- 配置与降级 ----------------
def test_mail_default_disabled_and_no_send():
    """未配置时：enabled=False，且绝不发信、不误报为发送失败。"""
    assert mailer.enabled() is False
    t = _mk_task()
    ok, msg = mailer.send_failure(t)
    assert ok is False
    assert "未配置" in msg
    assert t.mail_status is None, "未配置时应保持「未发送」，而不是「发送失败」"
    assert "未配置" in (t.mail_error or "")


def test_mail_config_roundtrip_and_sender_default():
    """写入配置后启用；收件人默认等于发件邮箱。"""
    mailer.set_config(user_="tester@qq.com", password_="abcd1234efgh5678",
                      host_="smtp.qq.com", port_="465", to_="")
    try:
        assert mailer.enabled() is True
        assert mailer.mail_to() == "tester@qq.com", "MAIL_TO 留空应回落到发件邮箱"
        st = mailer.status()
        assert st["enabled"] and st["user"] == "tester@qq.com"
        assert st["pass_masked"], "授权码不应回显明文"
    finally:
        mailer.set_config(user_="", password_="", to_="")


def test_mail_disabled_switch():
    """MAIL_ENABLED=0 时即使配好账号也不发。"""
    mailer.set_config(user_="tester@qq.com", password_="abcd1234efgh5678", enabled_=False)
    try:
        assert mailer.enabled() is False
    finally:
        mailer.set_config(user_="", password_="", enabled_=True)


def test_mail_subject_and_body_contain_key_fields():
    """邮件主题/正文应含合同名、失败原因等关键信息。"""
    t = _mk_task()
    assert "测试合同.docx" in mailer.build_subject(t)
    body = mailer.build_text(t)
    for kw in ["测试合同.docx", "TMAIL001", "法务部", "Word 文档打开失败"]:
        assert kw in body, f"正文缺少 {kw}"
    html = mailer.build_html(t)
    assert "测试合同.docx" in html and "处理建议" in html


def test_mail_send_fails_gracefully_on_bad_host():
    """配置了不可达服务器时：如实返回失败并写入原因，不抛异常。"""
    mailer.set_config(user_="tester@qq.com", password_="abcd1234efgh5678",
                      host_="127.0.0.1", port_="9", to_="tester@qq.com")
    try:
        t = _mk_task()
        ok, msg = mailer.send_failure(t)
        assert ok is False
        assert t.mail_status == "failed", "配了通道但发不出去，才应标记为发送失败"
        assert t.mail_error
    finally:
        mailer.set_config(user_="", password_="", host_="smtp.qq.com", port_="465")


def test_clean_reason_strips_local_paths():
    """失败原因里的本机绝对路径要剔除（不该写进对外邮件）。"""
    raw = ("Word 文档打开失败（可能加密或损坏）：Package not found at "
           r"'C:\Users\zmy\WorkBuddy\uploads\TA7.docx'")
    out = mailer.clean_reason(raw)
    assert r"C:\Users" not in out, f"路径未清洗：{out}"
    assert "（路径已省略）" in out
    assert "Word 文档打开失败" in out
    # 不含路径的原因应原样保留
    plain = "PDF 解析失败（可能加密）：No /Root object!"
    assert mailer.clean_reason(plain) == plain
    assert mailer.clean_reason(None) == ""


# ---------------- 接口 ----------------
def test_mail_status_endpoint_shape():
    st = M.get_mail_status(_Req())
    import json
    d = json.loads(st.body)
    assert d["enabled"] is False
    assert d["host"] == "smtp.qq.com" and d["port"] == 465
    assert "has_pass" in d and "env_file" in d


def test_mail_failures_lists_blocked_tasks():
    """解析失败的任务应出现在失败列表里，并带邮件状态字段。"""
    t = store.create_task(contract_name="邮件用例合同.docx", biz_type="procurement")
    t.status = "blocked"
    t.blocked_reason = "PDF 解析失败（可能加密）"
    t.meta.department = "采购部"
    try:
        resp = M.mail_failures(_Req())
        import json
        d = json.loads(resp.body)
        row = next(i for i in d["items"] if i["task_id"] == t.task_id)
        assert row["blocked_reason"].startswith("PDF 解析失败")
        assert row["department"] == "采购部"
        assert "mail_status" in row and "mail_error" in row
        assert d["total"] >= 1
        # 统计自洽
        assert d["total"] == d["sent"] + d["failed"] + d["not_sent"]
    finally:
        with store.LOCK:
            store.TASKS.pop(t.task_id, None)


def test_mail_test_endpoint_honest_when_unconfigured():
    """未配置时测试接口应如实返回失败，而不是假装成功。"""
    resp = M.test_mail(_Req())
    import json
    d = json.loads(resp.body)
    assert d["ok"] is False
    assert "未配置" in d["message"]


def test_resend_mail_404_for_missing_task():
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as e:
        M.resend_failure_mail(_Req(), "TNOT_EXIST")
    assert e.value.status_code == 404


class _Req:
    """最小请求替身：携带服务端签发的 Bearer JWT（A1 硬鉴权）。"""

    def __init__(self, role="admin"):
        import backend.main as _M
        self.headers = {"Authorization": f"Bearer {_M.create_token('mail_tester', role)}"}
