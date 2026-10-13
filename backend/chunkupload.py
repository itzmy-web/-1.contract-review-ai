"""分片上传 / 断点续传（P1：上传中断后可继续，用户可控制上传进度）。

设计要点：
  - 文件按固定分片大小切片，逐片写入 uploads/.parts/{upload_id}/{index}.part；
  - 每个上传会话持久化到 data/upload_sessions.json，**服务重启后仍可续传**；
  - 用 file_key（文件名 + 字节数 + 最后修改时间）识别「同一个文件」，
    用户下次重新选择该文件时命中同一会话，从已收到的分片之后继续；
  - 全部分片到齐后按序合并为正式附件，交由 main 创建审查任务。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import store

UPLOAD_DIR = store.BASE_DIR / "uploads"
PARTS_ROOT = UPLOAD_DIR / ".parts"
SESSION_FILE = store.DATA_DIR / "upload_sessions.json"

# 单个分片大小上限（防止客户端传入异常值导致内存/磁盘问题）
MAX_CHUNK_BYTES = 8 * 1024 * 1024
# 会话保留上限（超出按更新时间淘汰最旧的已完成会话）
SESSION_MAX = 500

SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()

STATUS_UPLOADING = "uploading"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"


# ---------------- 持久化 ----------------
def _load():
    global SESSIONS
    try:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            SESSIONS = {s["upload_id"]: s for s in data if s.get("upload_id")}
    except Exception:
        SESSIONS = {}


def _save_locked():
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        items = sorted(SESSIONS.values(), key=lambda s: s.get("updated_at", ""))
        if len(items) > SESSION_MAX:
            items = items[-SESSION_MAX:]
        SESSION_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load()


# ---------------- 内部工具 ----------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parts_dir(upload_id: str) -> Path:
    return PARTS_ROOT / upload_id


def _received_bytes(sess: dict) -> int:
    size, cs = int(sess["total_size"]), int(sess["chunk_size"])
    total = 0
    for i in sess.get("received", []):
        start = i * cs
        total += max(0, min(cs, size - start))
    return total


def progress(sess: dict) -> dict:
    """给前端的进度信息。"""
    total_chunks = int(sess.get("total_chunks") or 0)
    recv = len(sess.get("received") or [])
    tb = int(sess.get("total_size") or 0)
    rb = _received_bytes(sess)
    return {
        "upload_id": sess["upload_id"],
        "filename": sess["filename"],
        "owner": sess.get("owner", ""),
        "total_size": tb,
        "chunk_size": int(sess.get("chunk_size") or 0),
        "total_chunks": total_chunks,
        "received_chunks": recv,
        "received": sorted(sess.get("received") or []),
        "received_bytes": rb,
        "percent": round(rb * 100.0 / tb, 1) if tb else 0.0,
        "status": sess.get("status", STATUS_UPLOADING),
        "task_id": sess.get("task_id"),
        "created_at": sess.get("created_at"),
        "updated_at": sess.get("updated_at"),
        "file_key": sess.get("file_key", ""),
    }


def get(upload_id: str) -> dict | None:
    with LOCK:
        return SESSIONS.get(upload_id)


def find_resumable(owner: str, file_key: str) -> dict | None:
    """查找「同账号 + 同文件指纹」且仍在传的会话 → 用于断点续传。"""
    if not file_key:
        return None
    with LOCK:
        for s in SESSIONS.values():
            if (s.get("file_key") == file_key and s.get("owner", "") == owner
                    and s.get("status") == STATUS_UPLOADING):
                return dict(s)
    return None


def list_sessions(owner: str = "", status: str = STATUS_UPLOADING) -> list[dict]:
    """列出会话（默认只列未完成的），用于「找到上次没传完的文件」。"""
    with LOCK:
        items = [s for s in SESSIONS.values()
                 if (not owner or s.get("owner", "") == owner)
                 and (not status or s.get("status") == status)]
        items.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return [progress(s) for s in items]


def create_session(filename: str, total_size: int, chunk_size: int, owner: str = "",
                   file_key: str = "", biz_type: str = "procurement",
                   department: str = "", applicant: str = "业务经办人") -> dict:
    """新建上传会话；若已有同 file_key 的未完成会话则直接复用（续传）。"""
    existing = find_resumable(owner, file_key)
    if existing:
        return existing
    total_size = int(total_size)
    chunk_size = max(1, min(int(chunk_size or 0) or 1024 * 1024, MAX_CHUNK_BYTES))
    upload_id = "U" + uuid.uuid4().hex[:12].upper()
    sess = {
        "upload_id": upload_id,
        "filename": filename or "未命名合同",
        "owner": owner,
        "file_key": file_key or f"{filename}:{total_size}",
        "total_size": total_size,
        "chunk_size": chunk_size,
        "total_chunks": max(1, math.ceil(total_size / chunk_size)) if total_size else 1,
        "received": [],
        "biz_type": biz_type,
        "department": department,
        "applicant": applicant,
        "status": STATUS_UPLOADING,
        "task_id": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _parts_dir(upload_id).mkdir(parents=True, exist_ok=True)
    with LOCK:
        SESSIONS[upload_id] = sess
        _save_locked()
    return dict(sess)


def add_chunk(upload_id: str, index: int, data: bytes) -> tuple[dict | None, str]:
    """写入一个分片（幂等：同序号重复上传会覆盖）。返回 (会话, 错误信息)。"""
    sess = get(upload_id)
    if not sess:
        return None, "上传会话不存在"
    if sess.get("status") != STATUS_UPLOADING:
        return None, "该上传会话已结束"
    total = int(sess["total_chunks"])
    if index < 0 or index >= total:
        return None, f"分片序号越界（应在 0..{total - 1}）"
    d = _parts_dir(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{index}.part").write_bytes(data)
    with LOCK:
        recv = set(sess.get("received") or [])
        recv.add(index)
        sess["received"] = sorted(recv)
        sess["updated_at"] = _now()
        # 记录实际分片大小便于估算进度（最后一片可能不足）
        sess.setdefault("chunk_sizes", {})[str(index)] = len(data)
        _save_locked()
        return progress(sess), ""


def is_complete(sess: dict) -> bool:
    return len(set(sess.get("received") or [])) >= int(sess.get("total_chunks") or 0)


def assemble(upload_id: str, dest_path: str) -> str:
    """按序合并分片到目标文件，返回目标路径。缺片会抛异常。"""
    sess = get(upload_id)
    if not sess:
        raise FileNotFoundError("上传会话不存在")
    if not is_complete(sess):
        raise ValueError("分片不完整，无法合并")
    d = _parts_dir(upload_id)
    tmp = dest_path + ".merging"
    with open(tmp, "wb") as out:
        for i in range(int(sess["total_chunks"])):
            p = d / f"{i}.part"
            if not p.exists():
                raise ValueError(f"缺少分片 {i}")
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)
    os.replace(tmp, dest_path)
    return dest_path


def mark_completed(upload_id: str, task_id: str) -> dict | None:
    """标记会话完成并清理分片目录。"""
    sess = get(upload_id)
    if not sess:
        return None
    with LOCK:
        sess["status"] = STATUS_COMPLETED
        sess["task_id"] = task_id
        sess["updated_at"] = _now()
        sess["received"] = []          # 完成后不再保留分片清单
        _save_locked()
    shutil.rmtree(_parts_dir(upload_id), ignore_errors=True)
    return progress(sess)


def abort(upload_id: str) -> bool:
    """放弃会话并清理分片（用于「取消上传」）。"""
    sess = get(upload_id)
    if not sess:
        return False
    with LOCK:
        sess["status"] = STATUS_ABORTED
        sess["updated_at"] = _now()
        _save_locked()
    shutil.rmtree(_parts_dir(upload_id), ignore_errors=True)
    return True


def discard(upload_id: str) -> bool:
    """彻底删除会话记录（含已完成），用于清理列表。"""
    with LOCK:
        ok = SESSIONS.pop(upload_id, None) is not None
        if ok:
            _save_locked()
    shutil.rmtree(_parts_dir(upload_id), ignore_errors=True)
    return ok
