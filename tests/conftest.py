"""pytest 全局配置。

测试必须**离线且确定性**：即使项目根存在带真实 Key 的 .env，
也强制在测试期间禁用大模型（也阻止 llm 去读 .env），避免：
  - 单测联网变慢 / 偶发失败
  - 误消耗真实额度
需要验证大模型行为的用例，请自行显式设置环境变量（见 test_features.py）。
"""
import os
import tempfile

import pytest

# 测试隔离：把数据目录（任务 / 审计 / 用户）重定向到临时目录，
# 避免单测写入真实 data/tasks.json、data/users.json，也避免加载用户真实历史任务。
# 必须在任何 backend 模块被 import 之前设置（conftest 最先加载）。
os.environ.setdefault("CR_DATA_DIR", tempfile.mkdtemp(prefix="cr_test_data_"))

# A1：测试环境注入固定 JWT_SECRET（backend.main 顶层启动校验需要），
# 与真实部署密钥隔离；测试自行签发测试令牌，不依赖 .env。
os.environ.setdefault("JWT_SECRET", "cr-test-secret-not-for-production")

LLM_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROXY",
            "LLM_TIMEOUT", "LLM_SUGGEST")
# 邮件通道同理：清掉真实 QQ 授权码，避免单测真的往外发信
MAIL_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
             "MAIL_TO", "MAIL_ENABLED")


@pytest.fixture(autouse=True)
def _disable_real_llm():
    saved = {k: os.environ.get(k) for k in LLM_KEYS + MAIL_KEYS}
    for k in LLM_KEYS + MAIL_KEYS:
        os.environ.pop(k, None)
    try:
        from backend import llm
        llm._loaded = True          # 阻止后续 cfg() 再去读真实 .env
    except Exception:
        pass
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
