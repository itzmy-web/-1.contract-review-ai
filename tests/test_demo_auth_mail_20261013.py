"""演示身份登录（X-Role）、法规库、审计工作台预置数据、邮件配置加密的回归测试。

背景（2026-10-13）：部署环境里 JWT 签名密钥不稳定，导致「登录后所有接口 401、
页面集体变空」。修复方向是恢复原有的**演示身份登录**（选身份即进，不依赖令牌），
并给敏感配置加上加密落盘。本文件锁住这些行为，防止回退。
"""
import os

import pytest

from backend import mailer, secretbox, store
from backend import main as M
from backend.samples import seed_samples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "samples")


class _DemoReq:
    """最小 Request 替身：不带任何令牌，只用 X-Role 表达登录身份。"""

    def __init__(self, role=None, user=None):
        h = {}
        if role:
            h["X-Role"] = role
        if user:
            h["X-User"] = user
        self.headers = h


@pytest.fixture(autouse=True)
def _ensure_samples():
    seed_samples(SAMPLE_DIR)


# ---------------- 演示身份通道 ----------------
def test_demo_role_login_allows_every_role():
    """7 个角色都能凭 X-Role 通过鉴权（root cause：令牌失效时演示仍要能用）。"""
    assert set(M.ROLE_PERMS) == {"admin", "applicant", "legal", "audit_lead",
                                 "audit_member", "reviewer", "registered"}
    for role in M.ROLE_PERMS:
        M.list_tasks(_DemoReq(role))          # 不抛异常即 200


def test_demo_channel_rejects_missing_or_forged_role():
    """没有身份 → 401；伪造角色 → 401（不是 200，也不是 500）。"""
    from fastapi import HTTPException
    for req in (_DemoReq(), _DemoReq("hacker"), _DemoReq("")):
        with pytest.raises(HTTPException) as ei:
            M.list_tasks(req)
        assert ei.value.status_code == 401


def test_invalid_jwt_does_not_fall_back_to_demo():
    """带了坏令牌就不能靠 X-Role 蒙混过关，必须 401 重新登录。"""
    from fastapi import HTTPException
    class _Bad:
        headers = {"Authorization": "Bearer not.a.token", "X-Role": "admin"}
    with pytest.raises(HTTPException) as ei:
        M.list_tasks(_Bad())
    assert ei.value.status_code == 401


def test_demo_channel_still_enforces_permission_matrix():
    """演示通道并没有放开权限矩阵：该 403 的仍然 403。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        M.create_audit_project(_DemoReq("applicant"), {"name": "越权尝试"})
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        M.add_law(_DemoReq("registered"), {"law_name": "x", "content": "y"})
    assert ei2.value.status_code == 403


def test_login_role_endpoint_returns_role_and_perms():
    d = M.login_role(_DemoReq(), {"role": "legal"}).body
    import json
    d = json.loads(d)
    assert d["role"] == "legal" and d["demo"] is True
    assert "writeback" in d["perms"]


def test_login_role_endpoint_rejects_unknown_role():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        M.login_role(_DemoReq(), {"role": "nobody"})
    assert ei.value.status_code == 400


def test_auth_mode_reports_demo_flag():
    import json
    d = json.loads(M.auth_mode().body)
    assert d["demo_role_login"] is True


def test_audit_actor_prefers_demo_role_over_username():
    """审计留痕要能看出「谁以什么角色做了什么」，不能被前端账号串盖掉。"""
    who = M._audit_actor(_DemoReq("audit_lead", user="wechat_scan"))
    assert "审计项目负责人" in who and "audit_lead" in who


# ---------------- 法规库 ----------------
def test_law_library_is_substantive():
    """法规库要有足够体量、id 唯一、字段完整、覆盖多个部门法。"""
    assert len(M.LAWS) >= 20, "法规库过于单薄，评审/演示时撑不起来"
    ids = [l["law_id"] for l in M.LAWS]
    assert len(ids) == len(set(ids)), "存在重复 law_id"
    for l in M.LAWS:
        assert l["law_name"] and l["article"] and l["content"] and l["category"]
        assert 8 <= len(l["content"]) <= 400
    names = {l["law_name"] for l in M.LAWS}
    assert {"民法典", "劳动合同法", "招标投标法"} <= names


def test_law_search_filters():
    import json
    items = json.loads(M.get_laws(_DemoReq("admin"), q="劳动合同法").body)
    assert items and all(i["law_name"] == "劳动合同法" for i in items)


# ---------------- 审计工作台预置数据 ----------------
def test_audit_demo_seeded_and_consistent():
    assert M.AUDIT_PROJECTS, "审计工作台应预置演示项目，否则页面看起来像没做功能"
    assert M.AUDIT_FINDINGS, "应有审计底稿"
    pid = M.AUDIT_PROJECTS[0]["project_id"]
    sev = {"high", "medium", "low"}
    st = {"open", "rectifying", "done"}
    for f in M.AUDIT_FINDINGS:
        assert f["project_id"] == pid
        assert f["severity"] in sev and f["status"] in st


def test_audit_stats_shape():
    import json
    d = json.loads(M.audit_stats(_DemoReq("audit_lead")).body)
    for k in ("projects", "total", "open", "rectifying", "done", "rate", "severity"):
        assert k in d
    assert sum(d["severity"].values()) == d["total"]
    assert 0 <= d["rate"] <= 100


# ---------------- 未归属任务对所有身份可见 ----------------
def test_unowned_tasks_visible_to_every_owner():
    t = store.create_task(contract_name="公共预置合同.docx", biz_type="procurement",
                          applicant="系统", owner="")
    try:
        for who in ("admin", "legal", "applicant"):
            assert any(x.task_id == t.task_id for x in store.tasks_of_owner(who))
    finally:
        with store.LOCK:
            store.TASKS.pop(t.task_id, None)


# ---------------- 配置加密 ----------------
def test_secretbox_roundtrip_and_tamper_detection():
    token = secretbox.encrypt("slqdrnmqlbdqdacj")
    assert token.startswith("enc:v1:") and "slqdrnmqlbdqdacj" not in token
    assert secretbox.decrypt(token) == "slqdrnmqlbdqdacj"
    assert secretbox.is_encrypted(token)
    # 篡改密文 → 认证标签校验失败，返回 None（而不是解出垃圾）
    head, tail = token.rsplit(":", 1)
    flipped = head + ":" + ("0" * len(tail) if tail[0] != "0" else "1" * len(tail))
    assert secretbox.decrypt(flipped) is None
    # 明文兼容读取
    assert secretbox.read_value("plain-text") == "plain-text"
    assert secretbox.read_value("") == ""


def test_secretbox_mask_keeps_domain():
    assert secretbox.masked("3505895840@qq.com") == "35****@qq.com"
    assert secretbox.masked("") == ""


def test_mail_secrets_encrypted_at_rest(tmp_path, monkeypatch):
    """授权码 / 收件邮箱必须密文落盘，明文不出现在文件里。"""
    f = tmp_path / "mail_secret.json"
    monkeypatch.setattr(mailer, "_SECRET_FILE", f)
    st = mailer.set_config(user_="3505895840@qq.com", password_="slqdrnmqlbdqdacj",
                           to_="3505895840@qq.com", host_="smtp.qq.com",
                           port_="465", encrypt=True)
    assert st["enabled"] is True
    assert st["encrypted"] is True
    raw = f.read_text(encoding="utf-8")
    assert "slqdrnmqlbdqdacj" not in raw, "授权码不得以明文落盘"
    assert "3505895840@qq.com" not in raw, "收件邮箱不得以明文落盘"
    assert "enc:v1:" in raw
    assert st["pass_masked"] and "slqdrn" not in st["pass_masked"]
    assert st["user_masked"] == "35****@qq.com" and st["to_masked"] == "35****@qq.com"


def test_mail_status_never_returns_plaintext_password():
    import json
    st = json.loads(M.get_mail_status(_DemoReq("admin")).body)
    assert "password" not in st and st["has_pass"] in (True, False)
    assert st["pass_masked"] == "" or set(st["pass_masked"]) == {"*"}
