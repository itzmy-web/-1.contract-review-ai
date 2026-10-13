"""分片上传 / 断点续传回归。

覆盖：
  - 会话创建与进度计算
  - 分片写入 + 幂等覆盖
  - 「同账号 + 同 file_key」命中已有会话 → 断点续传（含部分分片）
  - 分片不全不能合并；补齐后可合并且内容与源文件逐字节一致
  - abort（保留会话可续）/ discard（彻底删除）
  - 会话持久化到磁盘后可重新加载（服务重启仍能续传）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from backend import chunkupload as CU  # noqa: E402


def _payload(n: int) -> bytes:
    return bytes((i * 7 + 3) % 256 for i in range(n))


def test_session_and_progress():
    sess = CU.create_session("a.docx", 2500, 1000, owner="alice", file_key="k1")
    p = CU.progress(sess)
    assert p["total_chunks"] == 3 and p["received_chunks"] == 0 and p["percent"] == 0.0
    CU.add_chunk(sess["upload_id"], 0, _payload(1000))
    p = CU.progress(CU.get(sess["upload_id"]))
    assert p["received_chunks"] == 1 and p["received_bytes"] == 1000
    assert abs(p["percent"] - 40.0) < 0.01     # 1000/2500 = 40%


def test_resume_hits_same_session():
    s1 = CU.create_session("b.docx", 3000, 1000, owner="bob", file_key="fkb")
    CU.add_chunk(s1["upload_id"], 0, _payload(1000))
    # 再次 init：同账号 + 同 file_key → 复用同一会话，已收分片保留
    s2 = CU.create_session("b.docx", 3000, 1000, owner="bob", file_key="fkb")
    assert s2["upload_id"] == s1["upload_id"]
    assert CU.progress(s2)["received"] == [0]
    # 不同账号 / 不同文件指纹 → 新会话
    s3 = CU.create_session("b.docx", 3000, 1000, owner="carol", file_key="fkb")
    assert s3["upload_id"] != s1["upload_id"]


def test_chunk_idempotent_and_bounds():
    s = CU.create_session("c.bin", 2000, 1000, owner="u", file_key="kc")
    CU.add_chunk(s["upload_id"], 0, b"X" * 1000)
    p, err = CU.add_chunk(s["upload_id"], 0, b"Y" * 1000)   # 重复上传同序号 → 覆盖
    assert err == "" and p["received_chunks"] == 1
    _, err2 = CU.add_chunk(s["upload_id"], 5, b"Z")
    assert "越界" in err2


def test_assemble_requires_all_then_matches(tmp_path):
    data = _payload(2500)
    s = CU.create_session("d.bin", len(data), 1000, owner="u", file_key="kd")
    CU.add_chunk(s["upload_id"], 0, data[0:1000])
    CU.add_chunk(s["upload_id"], 1, data[1000:2000])
    with pytest.raises(ValueError):
        CU.assemble(s["upload_id"], str(tmp_path / "d.bin"))   # 缺第 2 片
    CU.add_chunk(s["upload_id"], 2, data[2000:2500])
    out = str(tmp_path / "d.bin")
    CU.assemble(s["upload_id"], out)
    assert open(out, "rb").read() == data


def test_abort_keeps_session_discard_removes():
    s = CU.create_session("e.bin", 1000, 1000, owner="u", file_key="ke")
    CU.add_chunk(s["upload_id"], 0, b"A" * 1000)
    assert CU.abort(s["upload_id"]) is True
    assert CU.get(s["upload_id"])["status"] == CU.STATUS_ABORTED
    assert CU.find_resumable("u", "ke") is None            # 已放弃 → 不再被续传命中
    assert CU.discard(s["upload_id"]) is True
    assert CU.get(s["upload_id"]) is None


def test_sessions_persist_across_reload(tmp_path, monkeypatch):
    s = CU.create_session("f.bin", 4000, 1000, owner="persist_me", file_key="kf")
    CU.add_chunk(s["upload_id"], 0, _payload(1000))
    # 模拟重启：清空内存后从磁盘重新加载
    CU.SESSIONS.clear()
    CU._load()
    got = CU.get(s["upload_id"])
    assert got is not None and got["filename"] == "f.bin"
    assert CU.progress(got)["received"] == [0]
    assert CU.find_resumable("persist_me", "kf") is not None


def test_list_sessions_filters_owner_and_status():
    s = CU.create_session("g.bin", 1000, 1000, owner="lister", file_key="kg")
    lst = CU.list_sessions(owner="lister")
    assert any(x["upload_id"] == s["upload_id"] for x in lst)
    assert all(x["status"] == CU.STATUS_UPLOADING for x in lst)
    assert not any(x["upload_id"] == s["upload_id"] for x in CU.list_sessions(owner="other"))
