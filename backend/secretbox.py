"""配置项加密存储（零第三方依赖，仅用 Python 标准库）。

用途：把**敏感配置**（QQ 邮箱 SMTP 授权码、收件邮箱等）以密文形式落盘，
避免它们以明文出现在 `.env` / 日志 / 截图中；界面上只回显掩码。

设计要点
--------
- **密钥**：首次使用时自动生成 32 字节随机主密钥，存放在
  `<data 目录>/.secrets.key`（权限 0600，已被 .gitignore 排除）。可通过
  环境变量 `APP_SECRET_KEY` 显式指定，便于多实例共享同一密钥。
- **算法**：scrypt 派生（N=2^14, r=8, p=1）+ HMAC-SHA256 计数器流加密
  + HMAC-SHA256 认证标签（encrypt-then-MAC）。全部来自 hashlib / hmac，
  不需要 cryptography / pycryptodome，部署零负担。
- **格式**：`enc:v1:<salt_hex>:<nonce_hex>:<ct_hex>:<tag_hex>`，自描述、
  可平滑轮换版本。解密失败（换过密钥 / 被篡改）返回 None，调用方按"未配置"处理。

定位说明：这是**静态数据保护（encryption at rest）**，防的是明文泄露
（截图、发仓库、拷配置文件），不是防拥有主机权限的攻击者——主密钥就在同机。
需要更强保证时应换成 KMS / 云密钥托管，接口保持不变即可。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_DIR = Path(os.getenv("CR_DATA_DIR") or (BASE_DIR / "data"))
KEY_FILE = SECRET_DIR / ".secrets.key"

_PREFIX = "enc:v1:"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_BLOCK = 32


def _master_key() -> bytes:
    """取主密钥：环境变量优先，否则从密钥文件读取（不存在则生成）。"""
    env = (os.getenv("APP_SECRET_KEY") or "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    try:
        if KEY_FILE.exists():
            raw = KEY_FILE.read_text(encoding="utf-8").strip()
            if raw:
                return bytes.fromhex(raw)
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        SECRET_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key.hex(), encoding="utf-8")
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass          # Windows 上 chmod 语义有限，失败不影响功能
    except Exception:
        pass
    return key


def _derive(salt: bytes) -> tuple[bytes, bytes]:
    """由主密钥 + salt 派生 (加密密钥, 认证密钥)。"""
    dk = hashlib.scrypt(_master_key(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=64)
    return dk[:32], dk[32:]


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 计数器模式密钥流。"""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def is_encrypted(token: str | None) -> bool:
    return bool(token) and str(token).startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    """明文 → 密文串；空串原样返回。"""
    if plaintext is None or plaintext == "":
        return ""
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    enc_key, mac_key = _derive(salt)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(enc_key, nonce, len(pt))))
    tag = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    return f"{_PREFIX}{salt.hex()}:{nonce.hex()}:{ct.hex()}:{tag.hex()}"


def decrypt(token: str | None) -> str | None:
    """密文串 → 明文；非密文 / 解密失败返回 None。"""
    if not is_encrypted(token):
        return None
    try:
        salt_hex, nonce_hex, ct_hex, tag_hex = str(token)[len(_PREFIX):].split(":")
        salt, nonce, ct, tag = (bytes.fromhex(salt_hex), bytes.fromhex(nonce_hex),
                                bytes.fromhex(ct_hex), bytes.fromhex(tag_hex))
        enc_key, mac_key = _derive(salt)
        expect = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(expect, tag):
            return None
        pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
        return pt.decode("utf-8")
    except Exception:
        return None


def read_value(value: str | None) -> str:
    """便捷读取：密文则解密，明文原样返回（兼容历史明文配置）。"""
    if not value:
        return ""
    if is_encrypted(value):
        return decrypt(value) or ""
    return str(value).strip()


def masked(value: str | None, keep_head: int = 2, keep_tail: int = 2) -> str:
    """掩码显示：保留首尾少量字符，中间打点。"""
    s = value or ""
    if not s:
        return ""
    if "@" in s:                      # 邮箱：保留首字符与域名
        head, _, domain = s.partition("@")
        return (head[:2] + "****@" + domain) if len(head) > 2 else ("*@" + domain)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]
