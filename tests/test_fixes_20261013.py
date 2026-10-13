# -*- coding: utf-8 -*-
"""2026-10-13 评审修复项回归测试（配套 backend/rules.py・backend/main.py 的改动）。

覆盖：
  F4  规则 rule_id 全局唯一（R-H7 与「单方解除权不对等」重号已修正为 R-H8）
  F5  非 custom 模式的 pattern 不得为空（空 pattern 会让 absence 恒真、keyword 恒假）
  F6  absence 规则按「条款类型」判定：合同已有验收标准 → 不再误报缺失
  F7  R-L1 不可抗力改用 absence_text（全文关键词缺失）→ 有不可抗力条款即不报
  F8  R-M5 保密范围约定不明：界定/未界定两种情况判定正确
  A3  GET 接口鉴权：未带令牌 401，带 view 令牌 200
  A4  审计留痕操作人：由 JWT 解析为「姓名（角色）」，不再一律记为「演示模式」
  P1  密码存储：scrypt 加盐（同密码两次哈希不同），兼容旧裸 sha256，登录即自动升级
"""
import hashlib
import json
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as M  # noqa: E402
from backend import store  # noqa: E402
from backend.models import BizType, ReviewRule, RiskLevel  # noqa: E402
from backend.rules import DEFAULT_RULES, evaluate  # noqa: E402
from backend.samples import seed_samples  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "samples")


def _titled(risks, name):
    """只保留指定规则标题的风险项（evaluate 还会附带「必备条款缺失」等通用结果）。"""
    return [r for r in risks if r.title == name]


class _Req:
    """最小 Request 替身：携带服务端签发的 Bearer JWT。"""

    def __init__(self, role=None, name="回归测试员"):
        self.headers = ({"Authorization": f"Bearer {M.create_token('tester', role, name)}"}
                        if role else {})


@pytest.fixture(autouse=True)
def _ensure_samples():
    seed_samples(SAMPLE_DIR)


# ---------------- F4 规则 ID 唯一 ----------------
def test_rule_ids_are_unique():
    ids = [r.rule_id for r in DEFAULT_RULES]
    dup = {i for i in ids if ids.count(i) > 1}
    assert dup == set(), f"存在重复 rule_id：{dup}（规则页的启停/编辑/删除都按 rule_id 定位）"
    assert "R-H8" in ids, "「单方解除权不对等」应有独立 rule_id"


# ---------------- F5 非 custom 模式必须给出 pattern ----------------
def test_non_custom_rules_have_pattern():
    bad = [r.rule_id for r in DEFAULT_RULES
           if r.mode in ("keyword", "regex", "absence", "absence_text") and not r.pattern]
    assert bad == [], f"以下规则声明了匹配模式却未给 pattern（会恒真或恒假）：{bad}"


# ---------------- F6/F7 absence 类规则不再对标准合同误报 ----------------
def test_absence_rules_do_not_false_positive_on_standard_contract():
    """标准采购合同含「验收标准」条款 → R-M4 不得报「验收标准与期限缺失」。"""
    spec = store.SAMPLES["procurement_std.docx"]
    from backend.parsers import parse_contract
    parsed = parse_contract(spec["path"])
    risks = evaluate(parsed.doc_text, parsed.segments, parsed.meta,
                     BizType(spec["biz_type"]), DEFAULT_RULES)
    titles = [r.title for r in risks]
    assert "验收标准与期限缺失" not in titles, f"误报仍未修复：{titles}"


def test_absence_text_rule_triggers_only_when_keyword_absent():
    """absence_text：全文有「不可抗力」→ 不报；没有 → 报。"""
    rule = next(r for r in DEFAULT_RULES if r.rule_id == "R-L1")
    assert rule.mode == "absence_text" and rule.pattern == "不可抗力"

    from backend.parsers import extract_metadata, segment_text
    with_fm = "第一条 不可抗力\n因不可抗力导致无法履行的，受影响方应及时通知对方。"
    risks_with = _titled(evaluate(with_fm, segment_text(with_fm), extract_metadata(with_fm),
                                  BizType.PROCUREMENT, [rule]), rule.name)
    assert risks_with == [], "合同已约定不可抗力，不应报缺失"

    without = "第一条 标的物条款\n甲方向乙方采购设备一批。"
    risks_without = _titled(evaluate(without, segment_text(without), extract_metadata(without),
                                     BizType.PROCUREMENT, [rule]), rule.name)
    assert len(risks_without) == 1, "未约定不可抗力应报缺失"


# ---------------- F8 保密范围判定 ----------------
def test_scope_rule_distinguishes_defined_and_undefined():
    from backend.parsers import extract_metadata, segment_text
    rule = next(r for r in DEFAULT_RULES if r.rule_id == "R-M5")

    defined = "第一条 保密义务\n双方对本合同项下的保密信息负有保密义务，保密范围包括技术资料与商业数据。"
    risks = _titled(evaluate(defined, segment_text(defined), extract_metadata(defined),
                             BizType.PROCUREMENT, [rule]), rule.name)
    assert risks == [], "已界定保密范围，不应报「保密范围约定不明」"

    vague = "第一条 保密义务\n双方对合同内容负有保密义务。"
    risks = _titled(evaluate(vague, segment_text(vague), extract_metadata(vague),
                             BizType.PROCUREMENT, [rule]), rule.name)
    assert len(risks) == 1, "未界定范围应报「保密范围约定不明」"


# ---------------- F5 空 pattern 短路保护 ----------------
def test_empty_pattern_is_short_circuited():
    """即便有人手工配了空 pattern，也必须是「不命中」而不是恒真。"""
    from backend.parsers import extract_metadata, segment_text
    text = "第一条 标的物条款\n甲方向乙方采购服务器一台。"
    for mode in ("absence", "absence_text"):
        r = ReviewRule(rule_id="R-TEST", name="空pattern用例", mode=mode, pattern="",
                       trigger="t", reason="r", legal_basis="l", suggestion="s",
                       level=RiskLevel.HIGH)
        hits = _titled(evaluate(text, segment_text(text), extract_metadata(text),
                                BizType.PROCUREMENT, [r]), r.name)
        assert hits == [], f"{mode} 模式空 pattern 不应命中"


# ---------------- A3 接口鉴权 ----------------
def test_protected_get_endpoints_require_token():
    for fn, perm in [(M.list_tasks, "view"), (M.get_rules, "view"), (M.get_checklist, "view"),
                     (M.get_llm_config, "view"), (M.get_mail_status, "view"),
                     (M.mail_failures, "view"), (M.get_alerts, "view"),
                     (M.approval_inbox, "view"), (M.list_samples, "view")]:
        with pytest.raises(HTTPException) as e:
            fn(_Req())                      # 未带令牌 → 401
        assert e.value.status_code == 401, f"{fn.__name__} 未鉴权"


def test_protected_get_endpoints_ok_with_view_token():
    """注册用户（只读）也应有 view 权限，保证登录后页面数据可加载。"""
    from backend import store as _s
    t = _s.create_task(contract_name="鉴权用例.docx", biz_type="procurement")
    lst = M.list_tasks(_Req("registered"))
    assert lst.status_code == 200
    assert M.get_task(_Req("registered"), t.task_id).status_code == 200


# ---------------- A4 审计留痕操作人 ----------------
def test_audit_actor_from_jwt_not_demo_mode():
    who = M._audit_actor(_Req("admin"))
    assert "admin" in who and "回归测试员" in who, f"审计操作人应来自 JWT，实际：{who}"
    assert who != "演示模式"


def test_audit_actor_fallback_without_token():
    assert "匿名" in M._audit_actor(_Req())


# ---------------- F9 上传扩展名白名单 ----------------
def test_upload_rejects_unsupported_extension():
    assert M._check_ext("合同.docx") == ".docx"
    assert M._check_ext("scan.PNG") == ".png"
    for bad in ("说明.txt", "脚本.exe", "数据.csv", "无扩展名"):
        with pytest.raises(HTTPException) as e:
            M._check_ext(bad)
        assert e.value.status_code == 415, f"{bad} 应被拒绝"


# ---------------- F10 告警队列上限（长跑不涨内存） ----------------
def test_alerts_queue_is_capped():
    for i in range(store.ALERTS_MAX + 20):
        store.add_alert(f"T{i}", f"告警{i}", "解析异常")
    assert len(store.ALERTS) <= store.ALERTS_MAX, "告警队列应保留最近 N 条，避免内存无界增长"


# ---------------- P1 密码存储：scrypt 加盐 + 旧格式兼容 ----------------
def test_password_hash_is_salted_scrypt():
    h1, h2 = M.hash_password("1234"), M.hash_password("1234")
    assert h1.startswith("scrypt$") and h2.startswith("scrypt$")
    assert h1 != h2, "同一密码两次哈希必须不同（随机盐），否则存在彩虹表风险"
    assert h1 != hashlib.sha256(b"1234").hexdigest(), "不得退化为无盐 sha256"


def test_verify_password_roundtrip():
    stored = M.hash_password("s3cret-密码")
    assert M.verify_password(stored, "s3cret-密码") is True
    assert M.verify_password(stored, "wrong") is False
    assert M.verify_password("", "x") is False
    assert M.verify_password("scrypt$坏格式", "x") is False


def test_verify_password_accepts_legacy_sha256():
    legacy = hashlib.sha256("1234".encode("utf-8")).hexdigest()
    assert M.verify_password(legacy, "1234") is True, "历史裸 sha256 账号必须仍可登录"
    assert M.verify_password(legacy, "12345") is False


def test_needs_rehash_flags_legacy_and_stale_params():
    assert M.needs_rehash(hashlib.sha256(b"x").hexdigest()) is True   # 旧格式 → 待升级
    assert M.needs_rehash("scrypt$1$1$1$aa$bb") is True               # 参数落后 → 待升级
    assert M.needs_rehash(M.hash_password("x")) is False              # 当前格式 → 无需升级


def test_register_scrypt_and_login_auto_upgrade():
    r = json.loads(M.register({"username": "pw_user", "password": "abcd1234",
                               "name": "密码用例"}).body)
    assert r["ok"] is True
    stored = M.USERS["pw_user"]["password"]
    assert stored.startswith("scrypt$")
    assert stored != hashlib.sha256(b"abcd1234").hexdigest(), "注册不得再落裸 sha256"

    # 历史账号（裸 sha256）登录成功 → 透明升级为 scrypt 加盐格式
    M.USERS["legacy_user"] = {"password": hashlib.sha256(b"oldpass").hexdigest(),
                              "name": "老账号"}
    M.login({"username": "legacy_user", "password": "oldpass"})
    upgraded = M.USERS["legacy_user"]["password"]
    assert upgraded.startswith("scrypt$"), "旧格式账号登录后应自动升级"
    assert M.verify_password(upgraded, "oldpass") is True
