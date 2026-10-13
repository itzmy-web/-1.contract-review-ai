"""统一大模型客户端（可选能力）。

配置优先级：**环境变量 > 项目根 .env 文件**。未配置 LLM_API_KEY 时全部安全降级
（返回 None / 空），系统仍可仅靠规则引擎完整运行（开箱即跑、断网可用）。

支持的环境变量 / .env 键：
    LLM_API_KEY    必填，没有则不调用任何大模型
    LLM_BASE_URL   OpenAI 兼容端点，默认 https://api.openai.com/v1/chat/completions
    LLM_MODEL      模型名，默认 gpt-4o-mini
    LLM_PROXY      可选代理，如 http://127.0.0.1:7890（访问 OpenAI 时常用）
    LLM_TIMEOUT    秒，默认 30
    LLM_SUGGEST    1/0，是否用大模型实时生成修改建议，默认 1
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_loaded = False


# ---------------- .env 加载（零依赖） ----------------
def load_env(path: Path | None = None, override: bool = False) -> Path:
    """把 .env 键值读入 os.environ。默认不覆盖已存在的环境变量。

    编码兜底：utf-8(-sig) → gbk（用户用记事本另存为 GBK 也能读）。
    """
    global _loaded
    p = Path(path) if path else ENV_PATH
    if p.exists():
        text = None
        for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
            try:
                text = p.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            logger.error(".env 编码无法识别（尝试 utf-8/gbk 均失败）：%s", p)
        else:
            try:
                for raw in text.splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and (override or k not in os.environ):
                        os.environ[k] = v
            except Exception:
                logger.exception(".env 解析失败：%s", p)
    _loaded = True
    return p


def _cfg(key: str, default: str | None = None) -> str | None:
    if not _loaded:
        load_env()
    return os.getenv(key, default)


# ---------------- 配置读取 ----------------
def api_key() -> str:
    return (_cfg("LLM_API_KEY") or "").strip()


def base_url() -> str:
    return (_cfg("LLM_BASE_URL", "https://api.openai.com/v1/chat/completions") or "").strip()


def model() -> str:
    return (_cfg("LLM_MODEL", "gpt-4o-mini") or "").strip()


def proxy() -> str:
    return (_cfg("LLM_PROXY") or "").strip()


def timeout() -> float:
    try:
        return float(_cfg("LLM_TIMEOUT", "30") or 30)
    except ValueError:
        return 30.0


def enabled() -> bool:
    return bool(api_key())


def use_for_suggest() -> bool:
    return str(_cfg("LLM_SUGGEST", "1")).lower() not in ("0", "false", "no", "off")


def status() -> dict:
    k = api_key()
    return {
        "enabled": bool(k),
        "has_key": bool(k),
        "key_masked": (k[:3] + "***" + k[-4:]) if len(k) > 10 else ("***" if k else ""),
        "base_url": base_url(),
        "model": model(),
        "proxy": proxy(),
        "proxy_direct": not bool(proxy()),  # 未配 LLM_PROXY 时走直连（禁用系统代理）
        "timeout": timeout(),
        "suggest": use_for_suggest(),
        "env_file": str(ENV_PATH),
        "env_file_exists": ENV_PATH.exists(),
    }


def set_config(api_key_: str | None = None, base_url_: str | None = None,
               model_: str | None = None, proxy_: str | None = None,
               timeout_: str | None = None, suggest_: bool | None = None,
               persist: bool = False) -> dict:
    """更新配置（写入进程环境变量；persist=True 同时写回 .env）。"""
    global _loaded
    _loaded = True  # 阻止后续再被 .env 覆盖
    updates: dict[str, str | None] = {}
    if api_key_ is not None:
        updates["LLM_API_KEY"] = api_key_.strip()
    if base_url_ is not None:
        updates["LLM_BASE_URL"] = base_url_.strip()
    if model_ is not None:
        updates["LLM_MODEL"] = model_.strip()
    if proxy_ is not None:
        updates["LLM_PROXY"] = proxy_.strip()
    if timeout_ is not None:
        updates["LLM_TIMEOUT"] = str(timeout_).strip()
    if suggest_ is not None:
        updates["LLM_SUGGEST"] = "1" if suggest_ else "0"
    for k, v in updates.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if persist:
        save_env(updates)
    return status()


def save_env(updates: dict) -> None:
    """把配置写回 .env（保留其它行；值为 None/空表示删除该键）。"""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    keys = set(updates)
    out, seen = [], set()
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in keys:
                seen.add(k)
                v = updates[k]
                if v not in (None, ""):
                    out.append(f"{k}={v}")
                continue
        out.append(ln)
    for k, v in updates.items():
        if k not in seen and v not in (None, ""):
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


# ---------------- 调用 ----------------
def _extract_json(content: str):
    content = (content or "").strip()
    if content.startswith("```"):
        parts = content.split("```")
        if len(parts) >= 2:
            content = parts[1]
        if content.lstrip().lower().startswith("json"):
            content = content.lstrip()[4:]
    return json.loads(content.strip())


# 触发重试的服务端瞬时错误（模型节点过载 / 网关抖动）
_RETRY_CODES = frozenset({500, 502, 503, 504})
_MAX_RETRIES = 3


def _build_opener():
    """构造请求 opener。

    - 配置了 LLM_PROXY：走该代理；
    - 未配置：显式「空」 ProxyHandler，**禁用**代理。这能覆盖 urllib 自动套用的
      HTTP_PROXY/HTTPS_PROXY（Clash 等注入），避免国内 SiliconFlow 端点被绕到不可靠
      的本地代理而偶发 000/500。
    """
    px = proxy()
    if px:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _chat(messages: list[dict]) -> str | None:
    """底层调用：发消息、返回 content 字符串；任何失败返回 None。

    对服务端瞬时 5xx、限流 429、网络抖动做有限重试（带退避），吸收偶发故障，
    避免单次 500 就让整步 OCR 纠错 / 语义研判 / 建议生成失败。
    """
    if not enabled():
        return None
    payload = {"model": model(), "messages": messages, "temperature": 0}
    req = urllib.request.Request(
        base_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = _build_opener()
    last_err: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with opener.open(req, timeout=timeout()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                # 限流：按 Retry-After 退避（封顶 20s）后重试
                wait = 5 * attempt
                try:
                    wait = min(20.0, float(e.headers.get("Retry-After", wait)))
                except (TypeError, ValueError):
                    pass
                logger.warning("大模型限流(429)，%.0fs 后重试（%d/%d）", wait, attempt, _MAX_RETRIES)
                time.sleep(wait)
                continue
            if e.code in _RETRY_CODES and attempt < _MAX_RETRIES:
                logger.warning("大模型返回 %d，%.0fs 后重试（%d/%d）", e.code, 2 * attempt, attempt, _MAX_RETRIES)
                time.sleep(2 * attempt)
                continue
            # 4xx（含 401/400）等确定性错误：不重试，记录响应体便于排查
            logger.error("大模型调用失败(%d)：%s", e.code, e.read().decode("utf-8", "ignore")[:400])
            return None
        except Exception as e:  # 网络抖动 / 超时 / URL 错误等
            last_err = e
            if attempt < _MAX_RETRIES:
                logger.warning("大模型调用异常(%s)，%.0fs 后重试（%d/%d）", type(e).__name__, 2 * attempt, attempt, _MAX_RETRIES)
                time.sleep(2 * attempt)
                continue
            logger.error("大模型调用失败（model=%s url=%s）：%s", model(), base_url(), e)
            return None
    logger.error("大模型调用在 %d 次重试后仍失败：%s", _MAX_RETRIES, last_err)
    return None


def chat_json(prompt: str, system: str = "") -> object | None:
    """调用大模型并解析 JSON；任何失败都返回 None（由调用方降级）。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    content = _chat(messages)
    if content is None:
        return None
    try:
        return _extract_json(content)
    except Exception:
        logger.exception("大模型返回无法解析为 JSON")
        return None


def chat_text(prompt: str, system: str = "") -> str | None:
    """调用大模型返回纯文本（用于 OCR 纠错等非 JSON 场景）。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _chat(messages)


def test_connection() -> tuple[bool, str]:
    """连通性自检，供前端「测试连接」使用。"""
    if not enabled():
        return False, "未配置 LLM_API_KEY"
    r = chat_json('只输出 JSON：{"ok": true}', "你是接口连通性测试助手，只输出 JSON。")
    if isinstance(r, dict) and r.get("ok") is True:
        return True, f"连接成功：{model()}"
    return False, "连接失败：请检查 Key / Base URL / 代理 / 模型名（服务端日志有详细异常）"
