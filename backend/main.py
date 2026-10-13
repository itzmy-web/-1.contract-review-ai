"""合同审批审查系统 后端主服务（PRD 2.4 全模块）。

启动：uvicorn backend.main:app --reload --port 8010
"""
from __future__ import annotations

import os
import threading
import json
import hashlib
import hmac
import base64
import secrets
import uuid
import time
from datetime import datetime
from pathlib import Path

import html as _htmllib
from urllib.parse import quote as _urlquote

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import assistant, chunkupload, llm, mailer, store
from .models import (
    BatchReq, BizType, ClauseTemplate, LLMConfigReq, PullReq, ReviewRule, ReviewTask, RiskEditReq,
    TaskStatus, WritebackStatus,
)
from .parsers import IMAGE_EXTS
from .review import run_review, run_review_on_text
from .rules import REQUIRED_CLAUSES
from .samples import seed_demo_tasks, seed_samples
from .writeback import MockApprovalSystem, approval

# ---------------- 角色与权限（PRD 2.4.2） ----------------
# 未携带 X-Role 时视为演示模式（放行），携带则按矩阵校验 → 403。
# 每个角色绑定一位具体用户（人名），便于在界面上明确"当前操作人是谁"。
ROLE_PERMS: dict[str, dict] = {
    "admin": {"name": "系统管理员", "person": "张三",
              # 管理员可修改合同正文（edit_contract）、修改审查意见/建议（edit_suggestion）、填写批注（annotate）
              "perms": ["view", "submit", "export", "manage_rules", "monitor", "retry", "config_approval",
                        "manage_audit", "audit_finding", "audit_review", "rectify",
                        "annotate", "edit_suggestion", "edit_contract"]},
    "applicant": {"name": "业务经办人", "person": "李四",
                  "perms": ["submit", "view", "export"]},
    "legal": {"name": "法务审查人", "person": "王五",
              "perms": ["view", "adopt", "edit_suggestion", "annotate", "writeback", "export"]},
    # 审计角色（PRD 五：权限与角色体系）
    "audit_lead": {"name": "审计项目负责人", "person": "赵六",
                   "perms": ["view", "submit", "export", "monitor",
                             "manage_audit", "audit_finding", "audit_review", "rectify"]},
    "audit_member": {"name": "审计组员", "person": "钱七",
                     "perms": ["view", "audit_finding", "rectify", "export"]},
    "reviewer": {"name": "业务/法务查阅人", "person": "孙八",
                 "perms": ["view", "rectify", "export"]},
    # 自行注册 / 扫码登录的用户：默认只读（可提交审查与导出），
    # 管理类权限（规则编辑/合同编辑等）仅管理员持有（A1 服务端硬鉴权）
    "registered": {"name": "注册用户", "person": "",
                   "perms": ["view", "submit", "export"]},
}
ALL_PERMS = ["view", "submit", "export", "manage_rules", "monitor", "retry",
             "config_approval", "adopt", "edit_suggestion", "annotate", "writeback",
             "manage_audit", "audit_finding", "audit_review", "rectify", "edit_contract"]

# 演示身份登录开关（默认开启）：登录页可直接选定「登录身份」进入，
# 后端按 X-Role 解析角色，不依赖 JWT 签名密钥。设为 0 即关闭，只允许账号登录。
DEMO_ROLE_LOGIN = str(os.getenv("DEMO_ROLE_LOGIN", "1")).strip().lower() not in ("0", "false", "no", "off")


def _role_label(role: str) -> str:
    r = ROLE_PERMS.get(role)
    return f"{r['name']} {r['person']}" if r else role


# ---------------- A1：JWT 服务端硬鉴权（替代 X-Role 软权限） ----------------
# JWT_SECRET 在 llm.load_env() 之后校验并注入（见下方启动检查），
# 由 .env / 部署环境提供；拒绝无密钥启动，禁止任何测试占位。
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(username: str, role: str, name: str = "", expires_minutes: int = 480) -> str:
    """签发 HS256 JWT：角色由服务端根据账号类型决定，客户端不可自选（A1）。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": username, "role": role, "name": name,
               "iat": now, "exp": now + expires_minutes * 60}
    seg = f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}." \
          f"{_b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
    sig = hmac.new(JWT_SECRET.encode("utf-8"), seg.encode("utf-8"), hashlib.sha256).digest()
    return f"{seg}.{_b64url(sig)}"


def decode_token(token: str):
    """校验签名与有效期；失败返回 None。"""
    try:
        h, p, s = token.split(".")
        seg = f"{h}.{p}"
        expect = _b64url(hmac.new(JWT_SECRET.encode("utf-8"), seg.encode("utf-8"),
                                  hashlib.sha256).digest())
        if not hmac.compare_digest(expect, s):
            return None
        payload = json.loads(_b64url_decode(p))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def get_current_user(request: Request):
    """解析当前用户，支持两种通道（按可用性降级）：

    ① **账号通道**：Authorization: Bearer <JWT> —— 注册/登录用户，角色由服务端签发；
    ② **演示通道**：X-Role: <角色键> —— 登录页「登录身份」直接选定身份进入
       （演示/评审场景，不依赖 JWT 签名密钥）。

    ⚠ 为什么令牌解不开时不能直接 401：
    部分部署网关（本项目实测遇到过）会**自行给请求注入 Authorization 头**，
    把客户端携带的真令牌覆盖掉，于是「服务端刚签发的令牌，下一次请求就被拒绝」。
    若此时直接 401，整个应用在该环境下彻底不可用、且所有页面静默变空。
    因此令牌解码失败时降级到演示通道；只有"既没有有效令牌、也没有合法身份"
    才返回 401。演示通道可用 `DEMO_ROLE_LOGIN=0` 关闭，关闭后即为纯 JWT 硬鉴权。
    """
    auth = (request.headers.get("Authorization") or "").strip()
    has_bearer = auth.startswith("Bearer ")
    if has_bearer:
        payload = decode_token(auth[7:].strip())
        if payload:
            return payload
    if DEMO_ROLE_LOGIN:
        role = (request.headers.get("X-Role") or "").strip()
        if role in ROLE_PERMS:
            return {"sub": role, "role": role,
                    "name": ROLE_PERMS[role].get("person") or ROLE_PERMS[role]["name"],
                    "demo": True}
    if has_bearer:
        raise HTTPException(401, "登录已失效，请重新登录")
    raise HTTPException(401, "未登录：请先选择登录身份，或使用账号登录")


def require_perm(request: Request, perm: str) -> None:
    """服务端硬鉴权：角色取自服务端签发的 JWT，无令牌 401、无权限 403（A1）。"""
    user = get_current_user(request)
    role = user.get("role") or ""
    if role not in ROLE_PERMS:
        raise HTTPException(403, f"未知角色：{role}")
    if perm not in ROLE_PERMS[role]["perms"]:
        raise HTTPException(403, f"当前用户「{_role_label(role)}」无权执行该操作"
                                 f"（需权限 {perm}）")

BASE = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE / "uploads"
SAMPLE_DIR = BASE / "samples"
STATIC_DIR = BASE / "static"
for d in (UPLOAD_DIR, SAMPLE_DIR, STATIC_DIR):
    d.mkdir(exist_ok=True)

# ---------------- 用户注册表 ----------------
# 密码存储：scrypt 加盐哈希（标准库 hashlib.scrypt，内存硬 → 抗彩虹表与 GPU 暴力破解）。
#   新格式（自描述，参数可随标准演进）：scrypt$N$r$p$<salt_hex>$<hash_hex>
#   旧格式：裸 sha256 hex（无盐）——仅用于兼容历史账号，登录成功后自动升级为新格式。
# 与任务/审计共用数据目录（测试可用 CR_DATA_DIR 重定向，避免污染真实数据）
USERS_FILE = store.DATA_DIR / "users.json"
USERS: dict[str, dict] = {}

# scrypt 参数（n=2^14, r=8, p=1 → 单次约 16MB 内存 / 0.1s，登录体验与抗暴破的平衡点）
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_SCRYPT_DKLEN = 32
_PW_PREFIX = "scrypt$"


def hash_password(password: str) -> str:
    """用随机盐 + scrypt 计算密码哈希，返回自描述的存储串。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return f"{_PW_PREFIX}{_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(stored: str, password: str) -> bool:
    """校验明文密码与存储串是否匹配；兼容旧的裸 sha256 格式。"""
    stored = stored or ""
    if stored.startswith(_PW_PREFIX):
        try:
            _, n, r, p, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), hash_hex)
    # 旧格式：裸 sha256（无盐），仅用于兼容历史数据
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)


def needs_rehash(stored: str) -> bool:
    """存储串是否需要升级（非 scrypt 格式，或 scrypt 参数落后于当前标准）。"""
    if not (stored or "").startswith(_PW_PREFIX):
        return True
    try:
        _, n, r, p, _, _ = stored.split("$")
        return (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    except (ValueError, TypeError):
        return True


def _load_users():
    global USERS
    if USERS_FILE.exists():
        try:
            USERS = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            USERS = {}


def _save_users():
    USERS_FILE.parent.mkdir(exist_ok=True)
    USERS_FILE.write_text(json.dumps(USERS, ensure_ascii=False, indent=2), encoding="utf-8")


_load_users()

# P1-9：上传大小上限（默认 20MB，可用环境变量覆盖）
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024

# 允许上传的扩展名（与 parsers.parse_contract 支持范围严格一致）：
# 不在此列表的文件在入口就 415 拒绝，避免白建一条注定 blocked 的任务、白占存储。
ALLOWED_EXTS = {".docx", ".pdf"} | IMAGE_EXTS


def _check_ext(filename: str) -> str:
    """校验扩展名并返回归一化后缀；不支持则 415。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(415, f"不支持的文件格式：{ext or '（无扩展名）'}；"
                                 f"支持 {' / '.join(sorted(ALLOWED_EXTS))}")
    return ext

app = FastAPI(title="合同审批审查系统", version="1.0")

# 并发审查：同时最多 N 份合同在跑，其余排队。
# 旧实现用全局互斥锁 → 多任务完全串行（上传 5 份要排 5 轮）；
# 现在各 task 对象互不相干，只要规则取快照即可安全并行。
MAX_CONCURRENT_REVIEWS = max(1, int(os.getenv("REVIEW_CONCURRENCY", "4")))
_review_sem = threading.Semaphore(MAX_CONCURRENT_REVIEWS)

# 启动即生成示例合同，并用其初始化「模拟审批系统待办队列」
seed_samples(str(SAMPLE_DIR))
approval.seed_inbox(store.SAMPLES)

# 加载项目根 .env（大模型等可选配置；环境变量优先，不覆盖已有值）
llm.load_env()

# A2：JWT 签名密钥必须由部署环境注入，缺失即拒绝启动（无默认密钥、无测试占位）
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
if not JWT_SECRET:
    raise RuntimeError(
        "缺少 JWT_SECRET 环境变量（.env 或部署环境）：JWT 令牌签名密钥必填，"
        "请参考 .env.example 配置后重启；拒绝无密钥启动")


# P1-9：可选网关鉴权。仅当配置了 API_AUTH_KEY 时开启；
# 未配置则完全开放（保持开箱即跑、演示与既有测试不受影响）。
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    auth_key = os.getenv("API_AUTH_KEY")
    if auth_key:
        path = request.url.path
        if path == "/" or path.startswith("/static") or path.startswith("/docs"):
            return await call_next(request)
        if request.headers.get("X-API-Key") != auth_key:
            return JSONResponse(status_code=401,
                                content={"detail": "未授权：缺少或无效的 X-API-Key"})
    return await call_next(request)


def _audit_actor(request: Request) -> str:
    """审计留痕操作人：优先从 JWT（Authorization: Bearer）解析「姓名（角色）」。

    A1 改造后角色由服务端签发在令牌里，前端不再发送 X-Role；若仍按 X-Role 取值，
    所有操作都会被记成「演示模式」，审计日志失去「谁做了什么」的核心价值。
    """
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        payload = decode_token(auth[7:].strip())
        if payload:
            role = payload.get("role") or ""
            name = (payload.get("name") or "").strip()
            sub = (payload.get("sub") or "").strip()
            who = name or sub or role
            return f"{who}（{role}）" if role else (who or "已登录用户")
    # 演示通道：X-Role 是「登录身份」的直接来源，优先于 X-User——
    # 否则审计留痕只会记下前端账号串（如 wechat_scan），看不出"谁以什么角色做了什么"。
    role = (request.headers.get("X-Role") or "").strip()
    if role in ROLE_PERMS:
        return f"{_role_label(role)}（{role}）"
    u = (request.headers.get("X-User") or "").strip()
    if u:
        return u
    return _role_label(role) if role else "匿名（未携带令牌）"


# 审计中间件：记录所有写操作（POST/PUT/PATCH/DELETE 的 /api 请求），供「查看面板与审计」页使用
@app.middleware("http")
async def _audit_middleware(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api") \
            and not path.startswith("/api/audit"):
        store.add_audit(_audit_actor(request), request.method, path, resp.status_code)
    return resp


# ---------------- 任务状态机驱动 ----------------
def _process(task_id: str, file_path: str):
    task = store.get_task(task_id)
    if not task:
        return
    # 只在本任务上工作：规则取一份快照后立刻释放全局锁，各任务并行推进。
    with store.LOCK:
        rules = [r.model_copy(deep=True) for r in store.RULES]
    with _review_sem:                       # 限流：避免同时打爆大模型配额
        run_review(task, rules, file_path)
    store.save_tasks()                      # 审查结果落盘，重启后历史记录仍在
    # 解析受阻 → 推送告警（PRD 2.4.7 异常任务阻塞保护与告警）
    if task.status == "blocked":
        store.add_alert(task.task_id, task.contract_name,
                        task.blocked_reason or "解析受阻")
        # 失败详情邮件通知（后台线程发送，不阻塞审查主流程）
        mailer.send_failure_async(task)


def _save_upload(task_id: str, filename: str, content: bytes) -> str:
    ext = os.path.splitext(filename or "file.docx")[1] or ".docx"
    path = UPLOAD_DIR / f"{task_id}{ext}"
    with open(path, "wb") as f:
        f.write(content)
    return str(path)


# ---------------- 人工修改留痕（管理员修改合同 / 意见） ----------------
MODIFIED_SUFFIX = "（已修改）"


def _actor(request: Request) -> str:
    """当前操作人标识：账号用户名（X-User）优先，其次角色中文名，最后演示模式。"""
    u = (request.headers.get("X-User") or "").strip()
    if u:
        return u
    role = request.headers.get("X-Role") or ""
    r = ROLE_PERMS.get(role)
    return f"{r['name']} {r['person']}".strip() if r else (role or "演示模式")


def _mark_modified(task: ReviewTask, actor: str) -> None:
    """标记任务被人工修改：合同名后追加「（已修改）」（幂等，仅加一次），并记录修改人/时间。"""
    if not task.original_name:
        task.original_name = task.contract_name
    base = task.original_name or task.contract_name
    if not base.endswith(MODIFIED_SUFFIX):
        task.contract_name = base + MODIFIED_SUFFIX
    task.modified = True
    task.modified_at = datetime.now().isoformat(timespec="seconds")
    task.modified_by = actor
    task.updated_at = task.modified_at


# ---------------- 页面 ----------------
@app.get("/")
def index():
    """首页 HTML 一律不缓存。

    前端是单文件 SPA，样式与脚本都内联在 index.html 里；一旦被浏览器/网关缓存，
    用户会继续看到旧界面（典型症状：「明明改了样式，用户还说没变化」）。
    这里显式声明 no-store，保证刷新即拿到最新版本。
    """
    return FileResponse(str(STATIC_DIR / "index.html"), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


# ---------------- 任务接口 ----------------
@app.post("/api/tasks/upload")
async def upload_contract(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    biz_type: str = Form("procurement"),
    applicant: str = Form("业务经办人"),
    department: str = Form(""),
    owner: str = Form(""),
):
    require_perm(request, "submit")
    if biz_type not in [b.value for b in BizType]:
        raise HTTPException(400, "未知业务类型")
    content = await file.read()
    # P1-9：上传大小限制，超过上限直接拒绝（413）
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大，上传上限为 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    _check_ext(file.filename)
    # PRD 2.4.6：附件去重 —— 相同内容已审查过则复用同一任务，避免重复审查
    h = store.content_sha256(content)
    dup_id = store.find_by_attachment_hash(h)
    if dup_id:
        old = store.get_task(dup_id)
        if old:
            d = old.model_dump(exclude={"doc_text", "segments"})
            d["duplicated"] = True
            d["dup_of"] = dup_id
            return JSONResponse(content=d)
    task = store.create_task(contract_name=file.filename or "未命名合同",
                             biz_type=biz_type, applicant=applicant,
                             owner=(owner.strip() or _actor(request)))
    if department:
        task.meta.department = department
    task.file_type = os.path.splitext(file.filename or "")[1].lower()
    path = _save_upload(task.task_id, file.filename, content)
    task.source_path = path
    task.attach_hash = h
    store.register_attachment_hash(h, task.task_id)
    store.save_tasks()                      # 归属账号/正文落盘，供该账号下次登录找回
    threading.Thread(target=_process, args=(task.task_id, path), daemon=True).start()
    return JSONResponse(content=task.model_dump())


# ---------------- 分片上传 / 断点续传（上传中断可继续，用户可控制进度） ----------------
DEFAULT_CHUNK_BYTES = 1024 * 1024


@app.post("/api/upload/init")
def upload_init(request: Request, req: dict):
    """开启/复用分片上传会话。

    带 file_key（文件名+大小+修改时间）时，若存在同账号同文件的未完成会话，
    直接返回该会话（含已收分片），客户端据此从断点继续。
    """
    require_perm(request, "submit")
    filename = (req.get("filename") or "").strip() or "未命名合同"
    _check_ext(filename)          # 扩展名白名单：与 parse_contract 支持范围一致
    try:
        total_size = int(req.get("total_size") or 0)
        chunk_size = int(req.get("chunk_size") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "total_size / chunk_size 必须为整数")
    if total_size <= 0:
        raise HTTPException(400, "文件大小无效")
    if total_size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大，上传上限为 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    if chunk_size <= 0:
        chunk_size = DEFAULT_CHUNK_BYTES
    if chunk_size > chunkupload.MAX_CHUNK_BYTES:
        raise HTTPException(400, "分片过大")
    owner = (req.get("owner") or "").strip() or _actor(request)
    sess = chunkupload.create_session(
        filename=filename, total_size=total_size, chunk_size=chunk_size,
        owner=owner, file_key=(req.get("file_key") or "").strip(),
        biz_type=(req.get("biz_type") or "procurement"),
        department=(req.get("department") or ""),
        applicant=(req.get("applicant") or "业务经办人"),
    )
    p = chunkupload.progress(sess)
    p["resumed"] = bool(sess.get("received"))   # 是否命中已有分片（续传）
    return JSONResponse(content=p)


@app.post("/api/upload/chunk")
async def upload_chunk(request: Request, upload_id: str = Form(...), index: int = Form(...),
                       chunk: UploadFile = File(...)):
    """上传单个分片（幂等：同序号重复上传会覆盖）。"""
    require_perm(request, "submit")
    data = await chunk.read()
    if len(data) > chunkupload.MAX_CHUNK_BYTES:
        raise HTTPException(413, "分片过大")
    p, err = chunkupload.add_chunk(upload_id, int(index), data)
    if err:
        raise HTTPException(400, err)
    return JSONResponse(content=p)


@app.get("/api/upload/status/{upload_id}")
def upload_status(request: Request, upload_id: str):
    require_perm(request, "submit")
    sess = chunkupload.get(upload_id)
    if not sess:
        raise HTTPException(404, "上传会话不存在")
    return JSONResponse(content=chunkupload.progress(sess))


@app.get("/api/upload/sessions")
def upload_sessions(request: Request, owner: str = "", status: str = chunkupload.STATUS_UPLOADING):
    """列出未完成的上传会话 → 让用户「下次能找到上次没传完的文件」。"""
    require_perm(request, "submit")
    who = (owner or "").strip() or _actor(request)
    return JSONResponse(content=chunkupload.list_sessions(owner=who, status=status))


@app.post("/api/upload/complete")
def upload_complete(request: Request, req: dict):
    """所有分片到齐后合并为正式附件，建立审查任务（含附件去重）。"""
    require_perm(request, "submit")
    upload_id = (req.get("upload_id") or "").strip()
    sess = chunkupload.get(upload_id)
    if not sess:
        raise HTTPException(404, "上传会话不存在")
    if sess.get("status") == chunkupload.STATUS_COMPLETED and sess.get("task_id"):
        old = store.get_task(sess["task_id"])
        if old:
            d = old.model_dump(exclude={"doc_text", "segments"})
            d["duplicated"], d["dup_of"] = True, old.task_id
            return JSONResponse(content=d)
    if not chunkupload.is_complete(sess):
        raise HTTPException(400, "分片未传完，无法提交")

    filename = sess.get("filename") or "未命名合同"
    ext = os.path.splitext(filename)[1] or ".docx"
    tmp_dest = str(UPLOAD_DIR / f"tmp_{upload_id}{ext}")
    chunkupload.assemble(upload_id, tmp_dest)          # 先合并到临时文件算哈希
    h = store.file_sha256(tmp_dest)
    dup_id = store.find_by_attachment_hash(h)          # 附件去重：同内容已审查过则复用
    if dup_id:
        old = store.get_task(dup_id)
        if old:
            try:
                os.remove(tmp_dest)
            except OSError:
                pass
            chunkupload.discard(upload_id)
            d = old.model_dump(exclude={"doc_text", "segments"})
            d["duplicated"], d["dup_of"] = True, dup_id
            return JSONResponse(content=d)

    task = store.create_task(contract_name=filename,
                             biz_type=sess.get("biz_type") or "procurement",
                             applicant=sess.get("applicant") or "业务经办人",
                             owner=sess.get("owner") or _actor(request))
    if sess.get("department"):
        task.meta.department = sess["department"]
    task.file_type = ext.lower()
    dest = str(UPLOAD_DIR / f"{task.task_id}{ext}")
    os.replace(tmp_dest, dest)
    task.source_path = dest
    task.attach_hash = h
    store.register_attachment_hash(h, task.task_id)
    chunkupload.mark_completed(upload_id, task.task_id)
    store.save_tasks()
    threading.Thread(target=_process, args=(task.task_id, dest), daemon=True).start()
    return JSONResponse(content=task.model_dump())


@app.post("/api/upload/abort")
def upload_abort(request: Request, req: dict):
    """取消上传：保留会话（可稍后续传）或彻底丢弃（discard=true）。"""
    require_perm(request, "submit")
    upload_id = (req.get("upload_id") or "").strip()
    ok = chunkupload.discard(upload_id) if req.get("discard") else chunkupload.abort(upload_id)
    return JSONResponse(content={"ok": ok})


@app.post("/api/tasks/{task_id}/retry")
def retry_task(request: Request, task_id: str, background: BackgroundTasks):
    require_perm(request, "retry")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # 找原文件：优先用记录下来的源附件路径，其次回退到上传目录
    path = task.source_path if (task.source_path and os.path.exists(task.source_path)) else None
    if not path:
        candidates = list(UPLOAD_DIR.glob(f"{task_id}.*"))
        path = str(candidates[0]) if candidates else None
    if not path:
        raise HTTPException(400, "原文件缺失，无法重试")
    task.status = TaskStatus.PENDING
    task.blocked_reason = None
    threading.Thread(target=_process, args=(task.task_id, path), daemon=True).start()
    return JSONResponse(content=task.model_dump())


@app.post("/api/tasks/{task_id}/retext")
def retext_task(request: Request, task_id: str, req: dict):
    """修改合同正文后重新提交审查（不重新解析原文件，直接用新文本）。

    需要「修改合同」权限（edit_contract，管理员已具备）；修改后合同名追加「（已修改）」。
    """
    require_perm(request, "edit_contract")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    text = (req.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "正文内容为空，无法提交")
    task.status = TaskStatus.PENDING
    task.blocked_reason = None
    _mark_modified(task, _actor(request))   # 正文被改动 → 标记「已修改」
    store.save_tasks()

    def _do():
        with store.LOCK:
            rules = [r.model_copy(deep=True) for r in store.RULES]
        with _review_sem:
            run_review_on_text(task, rules, text)
        store.save_tasks()

    threading.Thread(target=_do, daemon=True).start()
    return JSONResponse(content=task.model_dump())


@app.get("/api/tasks")
def list_tasks(request: Request, status: str = "", risk_level: str = "", biz_type: str = "",
               q: str = "", amount_min: float = None, amount_max: float = None,
               counterparty: str = "", owner: str = ""):
    """任务列表 + 全文检索（关键词 q / 金额范围 / 相对方主体 / 归属账号 owner）。

    A3：合同台账含合同名/相对方/金额/风险等级，属敏感业务数据，必须登录后（view 权限）访问。
    """
    require_perm(request, "view")
    items = store.tasks_of_owner(owner) if owner else store.list_tasks()
    if status:
        items = [t for t in items if t.status == status]
    if risk_level:
        items = [t for t in items if (t.risk_level and t.risk_level.value == risk_level)]
    if biz_type:
        items = [t for t in items if t.biz_type.value == biz_type]
    if q:
        ql = q.lower()
        items = [t for t in items if ql in t.contract_name.lower()
                 or (t.meta.counterparty and ql in t.meta.counterparty.lower())
                 or (t.meta.subject_name and ql in t.meta.subject_name.lower())
                 or (t.meta.contract_no and ql in t.meta.contract_no.lower())]
    if amount_min is not None:
        items = [t for t in items if t.amount is not None and t.amount >= amount_min]
    if amount_max is not None:
        items = [t for t in items if t.amount is not None and t.amount <= amount_max]
    if counterparty:
        items = [t for t in items if (t.meta.counterparty and counterparty in t.meta.counterparty)]
    # P0-2：列表不序列化全文(doc_text)与条款(segments)，避免合同正文随大盘列表泄露/膨胀
    return JSONResponse(content=[t.model_dump(exclude={"doc_text", "segments", "source_path"}) for t in items])


@app.get("/api/tasks/{task_id}")
def get_task(request: Request, task_id: str):
    """单任务详情（含合同全文 doc_text 与风险明细）→ 必须登录后访问。"""
    require_perm(request, "view")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(content=task.model_dump())


@app.post("/api/tasks/{task_id}/annotate")
def annotate(request: Request, task_id: str, req: dict):
    require_perm(request, "annotate")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task.legal_note = req.get("legal_note", "")
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    store.save_tasks()
    return JSONResponse(content=task.model_dump())


@app.patch("/api/tasks/{task_id}/risks/{risk_id}")
def edit_risk(request: Request, task_id: str, risk_id: str, req: RiskEditReq):
    """法务编辑某条风险的建议/采纳/补充意见（PRD 2.4.10-5 支持法务编辑）。"""
    require_perm(request, "edit_suggestion")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    risk = next((r for r in task.risks if r.risk_id == risk_id), None)
    if not risk:
        raise HTTPException(404, "风险项不存在")
    changed = False
    if req.suggestion is not None and req.suggestion != risk.suggestion:
        risk.suggestion = req.suggestion
        risk.edited = True
        changed = True
    if req.adopted is not None:
        risk.adopted = req.adopted
    if req.lawyer_comment is not None and req.lawyer_comment != risk.lawyer_comment:
        risk.lawyer_comment = req.lawyer_comment
        changed = True
    if changed:
        _mark_modified(task, _actor(request))   # 意见/建议被改动 → 标记「已修改」
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    store.save_tasks()
    return JSONResponse(content=risk.model_dump())


@app.post("/api/tasks/{task_id}/writeback")
def writeback(request: Request, task_id: str):
    require_perm(request, "writeback")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "completed":
        raise HTTPException(400, "仅已完成审查的任务可回写")
    task.writeback_status = WritebackStatus.WRITING
    status, log = approval.post_comment(task)
    task.writeback_status = status
    task.writeback_log = log
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    store.save_tasks()
    return JSONResponse(content={"status": status, "log": log})


# ---------------- 报告导出（PRD 2.4.8） ----------------
@app.get("/api/tasks/{task_id}/report")
def export_report(request: Request, task_id: str, fmt: str = "md", inline: int = 0):
    require_perm(request, "export")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if fmt == "pdf":
        from .report import build_pdf
        out = SAMPLE_DIR.parent / "reports" / f"{task.task_id}.pdf"
        out.parent.mkdir(exist_ok=True)
        build_pdf(task, str(out))
        # inline=1 供浏览器内联预览（PRD 2.4.5 报告导出预览）
        if inline:
            return FileResponse(str(out), media_type="application/pdf")
        return FileResponse(str(out), filename=f"{task.contract_name}.pdf",
                            media_type="application/pdf")
    # Markdown（inline=1 时渲染为 HTML 预览，否则作为附件下载）
    md = _markdown_report(task)
    if inline:
        body = _htmllib.escape(md)
        return HTMLResponse(
            "<!DOCTYPE html><meta charset='utf-8'>"
            "<body style='margin:0;background:#0f1419;color:#e6edf3;"
            "font-family:Consolas,Menlo,monospace'>"
            f"<div style='padding:16px 20px;white-space:pre-wrap;line-height:1.65;font-size:13px'>{body}</div>"
        )
    return Response(content=md, media_type="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename={task.task_id}.md"})


def _markdown_report(task: ReviewTask) -> str:
    from .writeback import build_writeback_comment
    lines = [
        f"# 合同审查报告 · {task.contract_name}",
        "",
        "## 一、合同基本信息",
        f"- 合同标题：{task.contract_name}",
        f"- 合同编号：{task.meta.contract_no or '（未提取）'}",
        f"- 送审部门：{task.meta.department or '（未提取）'}",
        f"- 申请人：{task.applicant}",
        f"- 业务类型：{task.biz_type.value}",
        f"- 相对方主体名称：{task.meta.counterparty or '（未提取）'}",
        f"- 合同金额：{task.amount} {task.currency or ''}",
        f"- 履约周期：{task.meta.performance_period or '（未提取）'}",
        f"- 综合风险等级：{task.risk_level.value if task.risk_level else '无'}",
        "",
        "## 二、综合审查结论",
        task.summary or "无",
        "",
        "## 三、风险清单明细",
    ]
    for i, r in enumerate(task.risks, 1):
        lines += [
            f"### {i}. 【{r.level.value}】{r.title}",
            f"- 涉及条款：{r.clause_type}",
            f"- 原文定位：第 {r.page} 页 / 段落 {r.paragraph_index}" + (f" / 字符 {r.start_char}-{r.end_char}" if r.highlight else "（缺失类风险）"),
            f"- 原文片段：{r.snippet}",
            f"- 风险成因：{r.reason}",
            f"- 合规依据：{r.legal_basis}",
            f"- 推荐修改：{r.suggestion}",
            "",
        ]
    lines += ["## 四、法务审批意见", "", "- 整体批注：" + (task.legal_note or "（未填写）")]
    for i, r in enumerate(task.risks, 1):
        if r.lawyer_comment:
            lines.append(f"- 风险{i}「{r.title}」：{r.lawyer_comment}")
    lines += ["", "## 五、审批回写评论文本", "", "```markdown", build_writeback_comment(task), "```", ""]
    return "\n".join(lines)


# ---------------- 管理员导出：修改意见 / 修改后合同 ----------------
def _export_base(task: ReviewTask) -> str:
    """导出文件名主干：去掉原扩展名，保留「（已修改）」标注（若已修改）。"""
    base = task.contract_name or task.task_id
    marked = base.endswith(MODIFIED_SUFFIX)
    if marked:
        base = base[: -len(MODIFIED_SUFFIX)]
    for ext in (".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base + (MODIFIED_SUFFIX if marked else "")


def _attachment_headers(filename: str) -> dict:
    """附件下载头：filename* 走 UTF-8 编码，保证中文文件名不乱码。"""
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(filename)}"}


def _opinions_markdown(task: ReviewTask) -> str:
    """「修改意见」导出：总体结论 + 逐条风险与最终建议 + 法务批注。"""
    lines = [
        f"# 合同修改意见 · {task.contract_name}",
        "",
        f"- 合同编号：{task.meta.contract_no or '（未提取）'}",
        f"- 申请人：{task.applicant}",
        f"- 相对方：{task.meta.counterparty or '（未提取）'}",
        f"- 综合风险等级：{task.risk_level.value if task.risk_level else '无'}",
        f"- 是否经人工修改：{'是（' + (task.modified_by or '') + ' ' + (task.modified_at or '') + '）' if task.modified else '否'}",
        f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 一、总体审查结论",
        task.summary or "无",
        "",
        "## 二、修改意见明细",
    ]
    if not task.risks:
        lines.append("（未发现风险项）")
    for i, r in enumerate(task.risks, 1):
        tag = "（已人工修改）" if r.edited else ("（已采纳）" if r.adopted else "")
        lines.append(f"### {i}. 【{r.level.value}】{r.title}{tag}")
        lines.append(f"- 涉及条款：{r.clause_type}")
        lines.append(f"- 问题：{r.reason}")
        lines.append(f"- 合规依据：{r.legal_basis}")
        lines.append(f"- 修改建议：{r.suggestion}")
        if r.lawyer_comment:
            lines.append(f"- 法务补充：{r.lawyer_comment}")
        lines.append("")
    lines += ["## 三、法务整体批注", "", task.legal_note or "（未填写）", ""]
    return "\n".join(lines)


@app.get("/api/tasks/{task_id}/export-opinions")
def export_opinions(request: Request, task_id: str, fmt: str = "md"):
    """导出「修改意见」（总体结论 + 逐条建议 + 法务批注）。"""
    require_perm(request, "export")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    md = _opinions_markdown(task)
    base = _export_base(task) + "-修改意见"
    if fmt == "txt":
        return Response(content=md, media_type="text/plain; charset=utf-8",
                        headers=_attachment_headers(f"{base}.txt"))
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers=_attachment_headers(f"{base}.md"))


@app.get("/api/tasks/{task_id}/export-contract")
def export_contract(request: Request, task_id: str, fmt: str = "docx"):
    """导出（可能已被管理员修改的）合同正文；文件名沿用合同名，含「（已修改）」标注。"""
    require_perm(request, "export")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    text = (task.doc_text or "").strip()
    if not text:
        raise HTTPException(400, "该任务暂无合同正文（可能仍在解析或解析受阻）")
    base = _export_base(task)
    if fmt == "txt":
        return Response(content=text, media_type="text/plain; charset=utf-8",
                        headers=_attachment_headers(f"{base}.txt"))
    if fmt == "md":
        return Response(content=f"# {task.contract_name}\n\n{text}",
                        media_type="text/markdown; charset=utf-8",
                        headers=_attachment_headers(f"{base}.md"))
    # 默认 docx：可直接编辑 / 存档的 Word 文档
    from io import BytesIO

    from docx import Document
    doc = Document()
    doc.add_heading(task.contract_name, level=1)
    if task.modified:
        doc.add_paragraph(f"（本文档为人工修改后的版本，修改人：{task.modified_by or '—'}，"
                          f"修改时间：{task.modified_at or '—'}）")
    for para in text.split("\n"):
        doc.add_paragraph(para)
    buf = BytesIO()
    doc.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=_attachment_headers(f"{base}.docx"),
    )


# ---------------- 示例合同（PRD 2.4.9） ----------------
@app.get("/api/samples")
def list_samples(request: Request):
    require_perm(request, "view")
    return JSONResponse(content=[
        {"name": k, **v} for k, v in store.SAMPLES.items()
    ])


@app.post("/api/samples/import")
def import_sample(request: Request, req: dict, background: BackgroundTasks):
    name = req.get("name")
    if name not in store.SAMPLES:
        raise HTTPException(404, "示例不存在")
    spec = store.SAMPLES[name]
    task = store.create_task(contract_name=spec["contract_name"],
                             biz_type=spec["biz_type"], applicant=spec.get("applicant", "业务经办人"),
                             owner=_actor(request))
    task.file_type = os.path.splitext(name)[1].lower()
    task.source_path = spec["path"]
    store.register_attachment_hash(store.file_sha256(spec["path"]), task.task_id)
    store.save_tasks()
    threading.Thread(target=_process, args=(task.task_id, spec["path"]), daemon=True).start()
    return JSONResponse(content=task.model_dump())


# ---------------- 规则与模板管理（PRD 2.4.1 / 2.4.3） ----------------
@app.get("/api/rules")
def get_rules(request: Request):
    require_perm(request, "view")
    return JSONResponse(content=[r.model_dump() for r in store.RULES])


@app.post("/api/rules")
def add_rule(request: Request, rule: ReviewRule):
    require_perm(request, "manage_rules")
    with store.LOCK:
        store.RULES.append(rule)
    return JSONResponse(content=rule.model_dump())


@app.put("/api/rules/{rule_id}")
def update_rule(request: Request, rule_id: str, rule: ReviewRule):
    require_perm(request, "manage_rules")
    with store.LOCK:
        for i, r in enumerate(store.RULES):
            if r.rule_id == rule_id:
                store.RULES[i] = rule
                return JSONResponse(content=rule.model_dump())
    raise HTTPException(404, "规则不存在")


@app.delete("/api/rules/{rule_id}")
def del_rule(request: Request, rule_id: str):
    require_perm(request, "manage_rules")
    with store.LOCK:
        store.RULES[:] = [r for r in store.RULES if r.rule_id != rule_id]
    return JSONResponse(content={"ok": True})


@app.get("/api/templates")
def get_templates(request: Request):
    require_perm(request, "view")
    return JSONResponse(content=[t.model_dump() for t in store.TEMPLATES])


@app.post("/api/templates")
def add_template(t: ClauseTemplate):
    store.TEMPLATES.append(t)
    return JSONResponse(content=t.model_dump())


# ---------------- 审批回写模拟（PRD 审批回写模拟模块） ----------------
@app.get("/api/approval/tickets")
def approval_tickets(request: Request):
    require_perm(request, "view")
    return JSONResponse(content=list(approval.tickets.values()))


# ---------------- 审批系统接入：待办任务拉取（PRD 2.4.6 / 2.4.7） ----------------
@app.get("/api/approval/inbox")
def approval_inbox(request: Request):
    """模拟审批系统的待办任务队列（含附件名与拉取状态）。"""
    require_perm(request, "view")
    return JSONResponse(content=approval.list_inbox())


@app.post("/api/approval/pull")
def approval_pull(request: Request, req: PullReq, background: BackgroundTasks):
    """拉取待办任务 → 校验附件 → （去重）→ 创建审查任务并进入流水线。"""
    require_perm(request, "submit")
    created, skipped = [], []
    if req.ticket_ids:
        targets = req.ticket_ids
    else:
        targets = [t["ticket_id"] for t in approval.list_inbox()
                   if t["ticket_id"] not in approval.pulled]
    for tkid in targets:
        tk = approval.get_inbox_ticket(tkid)
        if not tk:
            skipped.append({"ticket_id": tkid, "reason": "待办不存在"})
            continue
        if tkid in approval.pulled:
            skipped.append({"ticket_id": tkid, "reason": "已拉取"})
            continue
        spec = store.SAMPLES.get(tk["attachment"])
        if not spec or not os.path.exists(spec["path"]):
            skipped.append({"ticket_id": tkid, "reason": "附件缺失"})
            continue
        # 附件去重：同内容已审查过 → 复用
        h = store.file_sha256(spec["path"])
        dup_id = store.find_by_attachment_hash(h)
        if dup_id:
            approval.mark_pulled(tkid)
            skipped.append({"ticket_id": tkid, "reason": "附件重复，复用既有任务", "task_id": dup_id})
            continue
        task = store.create_task(contract_name=tk["title"], biz_type=tk["biz_type"],
                                 applicant=tk["applicant"], owner=_actor(request))
        task.meta.department = tk.get("department")
        task.file_type = os.path.splitext(tk["attachment"])[1].lower()
        task.source_path = spec["path"]
        store.register_attachment_hash(h, task.task_id)
        approval.mark_pulled(tkid)
        threading.Thread(target=_process, args=(task.task_id, spec["path"]), daemon=True).start()
        created.append({"ticket_id": tkid, "task_id": task.task_id,
                        "contract_name": task.contract_name})
    return JSONResponse(content={"created": created, "skipped": skipped})


# ---------------- 大盘批量操作（PRD 2.4.5 批量操作） ----------------
@app.post("/api/tasks/batch")
def batch_tasks(request: Request, req: BatchReq, background: BackgroundTasks):
    require_perm(request, "retry")
    if req.action not in ("retry", "delete"):
        raise HTTPException(400, "不支持的操作（retry | delete）")
    done, missing = [], []
    for tid in req.task_ids:
        task = store.get_task(tid)
        if not task:
            missing.append(tid)
            continue
        if req.action == "delete":
            with store.LOCK:
                store.TASKS.pop(tid, None)
            done.append(tid)
            store.save_tasks()
        else:  # retry
            path = task.source_path if (task.source_path and os.path.exists(task.source_path)) else None
            if not path:
                cands = list(UPLOAD_DIR.glob(f"{tid}.*"))
                path = str(cands[0]) if cands else None
            if not path:
                missing.append(tid)
                continue
            task.status = TaskStatus.PENDING
            task.blocked_reason = None
            threading.Thread(target=_process, args=(tid, path), daemon=True).start()
            done.append(tid)
    return JSONResponse(content={"action": req.action, "done": done, "missing": missing})


# ---------------- 阻塞告警（PRD 2.4.7 推送告警） ----------------
@app.get("/api/alerts")
def get_alerts(request: Request):
    require_perm(request, "view")
    return JSONResponse(content=[a for a in store.list_alerts() if not a["acked"]])


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(request: Request, alert_id: str):
    require_perm(request, "monitor")
    return JSONResponse(content={"ok": store.ack_alert(alert_id)})


# ---------------- 角色权限 / 分类审查清单（PRD 2.4.2 / 2.4.1） ----------------
@app.get("/api/roles")
def get_roles():
    return JSONResponse(content=ROLE_PERMS)


@app.get("/api/auth/mode")
def auth_mode():
    """前端据此决定登录页展示「登录身份」下拉还是纯账号登录。"""
    return JSONResponse(content={"demo_role_login": DEMO_ROLE_LOGIN})


@app.post("/api/login-role")
def login_role(request: Request, req: dict):
    """演示通道登录：登录页选定「登录身份」即进入（无需密码、无需令牌）。

    与账号通道（/api/login → JWT）并列存在：账号通道用于注册用户，
    演示通道用于评审/演示时快速切换 7 种角色查看权限矩阵差异。
    """
    if not DEMO_ROLE_LOGIN:
        raise HTTPException(403, "演示身份登录已关闭，请使用账号密码登录")
    role = (req.get("role") or "").strip()
    if role not in ROLE_PERMS:
        raise HTTPException(400, "无效的登录身份")
    r = ROLE_PERMS[role]
    label = f"{r['name']} {r.get('person', '')}".strip()
    store.add_audit(label, "POST", "/api/login-role", 200)
    return JSONResponse(content={"ok": True, "role": role, "name": r["name"],
                                 "person": r.get("person", ""), "perms": r["perms"],
                                 "demo": True})


# ---------------- 审计（查看面板与审计） ----------------
@app.get("/api/audit")
def get_audit(request: Request, limit: int = 200):
    require_perm(request, "view")
    return JSONResponse(content=store.list_audit(min(limit, 500)))


@app.get("/api/system-check")
def system_check(request: Request):
    """处理看板：任务流转 / 回写状态 / 系统自检 / 关键统计。"""
    require_perm(request, "view")
    items = store.list_tasks()
    tasks = {"pending": 0, "parsing": 0, "reviewing": 0, "completed": 0, "blocked": 0}
    wb = {"success": 0, "not_written": 0, "writing": 0, "failed": 0}
    risk_total = 0
    for t in items:
        if t.status.value in tasks:
            tasks[t.status.value] += 1
        ws = t.writeback_status.value if t.writeback_status else "not_written"
        if ws in wb:
            wb[ws] += 1
        risk_total += len(t.risks or [])
    total = len(items)
    rate = round(tasks["completed"] / total * 100) if total else 0
    rules_on = sum(1 for r in store.RULES if r.enabled)
    # 解析工具自检
    try:
        import rapidocr_onnxruntime  # noqa: F401
        ocr_detail = "OCR 引擎 RapidOCR · 可用"
    except Exception:
        ocr_detail = "OCR 引擎 RapidOCR · 未安装（图片合同将受阻）"
    # 大模型：只反映「是否配置了 Key」，不做实时探活（避免每次自检都产生一次真实调用与费用）。
    # 措辞必须诚实：配置了 Key ≠ 调用可用（Key 失效/欠费/端点错误时会在审查日志里报错并降级）。
    if llm.enabled():
        llm_detail = f"已配置 Key · {llm.model()}（未实时探活，可用性以「测试连接」为准）"
        llm_status = "ok"
    else:
        llm_detail = "未接入 — 规则引擎兜底，不影响主流程"
        llm_status = "warn"
    # 邮件渠道
    smtp_user = os.getenv("SMTP_USER") or ""
    mail_detail = f"qq_mail · 发件 {smtp_user} · 授权码 已配置" if smtp_user else "邮件渠道 未配置"
    checks = [
        {"name": "审批平台连接", "status": "ok", "detail": f"模拟器在线 · 待办 {len(approval.list_inbox())} 条"},
        {"name": "数据存储", "status": "ok", "detail": f"本地 JSON 持久化 · {len(store.list_tasks())} 条任务已落盘"},
        {"name": "规则文件", "status": "ok", "detail": f"内置规则 {len(store.RULES)} 条 · 启用 {rules_on} 条"},
        {"name": "解析工具", "status": "ok" if "可用" in ocr_detail else "warn", "detail": ocr_detail},
        {"name": "大模型", "status": llm_status, "detail": llm_detail},
        {"name": "协同推送渠道", "status": "ok" if smtp_user else "warn", "detail": mail_detail},
    ]
    return JSONResponse(content={
        "tasks": tasks, "writeback": wb, "checks": checks,
        "stats": {"total": total, "success_rate": rate, "risk_total": risk_total,
                  "rules_enabled": rules_on},
    })


# ---------------- 台账导出 / 审计日志导出 / 版本比对 ----------------
@app.get("/api/export/tasks")
def export_tasks_csv(request: Request):
    """合同台账导出为 CSV（Excel 可直接打开，含质保期/付款节点/签订日期等审计字段）。"""
    require_perm(request, "export")
    import csv
    import io
    items = store.list_tasks()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["合同名称", "业务类型", "甲方", "乙方", "合同编号", "金额", "币种",
                "风险等级", "审查状态", "签订日期", "质保期", "付款节点", "履约期限", "创建时间"])
    for t in items:
        w.writerow([t.contract_name, t.biz_type.value, t.meta.subject_name or "", t.meta.counterparty or "",
                    t.meta.contract_no or "", t.amount or "", t.currency or "",
                    t.risk_level.value if t.risk_level else "", t.status,
                    t.meta.sign_date or "", t.meta.warranty_period or "", t.meta.payment_terms or "",
                    t.meta.performance_period or "", t.created_at])
    data = buf.getvalue()
    return Response(content="\ufeff" + data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=contracts.csv"})


@app.get("/api/audit/export")
def export_audit_csv(request: Request):
    """审计日志导出（外部审计取证）。"""
    require_perm(request, "monitor")
    import csv
    import io
    items = store.list_audit(500)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "操作人", "方法", "路径", "状态"])
    for a in items:
        w.writerow([a["time"], a["role"], a["method"], a["path"], a["status"]])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


@app.post("/api/compare")
def compare_texts(request: Request, req: dict):
    """版本比对：返回两个文本的差异操作序列（difflib）。"""
    require_perm(request, "view")
    import difflib
    a = req.get("a") or ""
    b = req.get("b") or ""
    sm = difflib.SequenceMatcher(None, a, b)
    ops = [{"tag": tag, "a0": i1, "a1": i2, "b0": j1, "b1": j2}
           for tag, i1, i2, j1, j2 in sm.get_opcodes()]
    return JSONResponse(content={"a_len": len(a), "b_len": len(b), "ops": ops,
                                 "ratio": round(sm.ratio(), 4)})


# ---------------- 审计项目与底稿（PRD 三：审计作业与底稿管理） ----------------
AUDIT_PROJECTS: list[dict] = []
AUDIT_FINDINGS: list[dict] = []


def seed_audit_demo() -> None:
    """预置一个演示审计项目 + 若干审计发现，让「审计工作台」开箱即有内容。

    背景：审计项目/底稿原先只存在进程内存里，服务一冷启动就是空列表 ——
    页面只剩一个「+ 新建审计项目」按钮，看起来像"功能没做"。这里按真实
    业务流程预置一套闭环数据（登记底稿 → 整改反馈 → 复核闭环），
    既方便演示，也让统计卡（严重/一般/轻微、整改中、已完成、闭环率）有数可看。
    """
    if AUDIT_PROJECTS:
        return
    proj = {
        "project_id": "AP-DEMO0001",
        "name": "2026 年度采购合同专项审计",
        "scope": [],                       # 审计范围（合同 task_id 列表），可由界面勾选补充
        "members": ["赵六", "钱七"],        # 审计项目负责人 / 审计组员
        "start_date": "2026-03-01",
        "end_date": "2026-06-30",
        "status": "active",
        "created_by": "系统预置演示数据",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    AUDIT_PROJECTS.append(proj)
    seed = [
        ("付款条款缺少验收前置条件", "付款结算", "high", "open", "", ""),
        ("违约金约定明显过高（合同总额 50%）", "法律风险", "high", "rectifying",
         "已与对方协商下调至 20%，待法务复核条款文本", "2026-07-15"),
        ("未约定知识产权归属", "法律风险", "medium", "rectifying",
         "补充条款已起草，正在走内部审批", "2026-07-20"),
        ("保密期限约定缺失", "法律风险", "medium", "done",
         "已在补充协议中明确保密期至合同终止后 3 年", "2026-06-20"),
        ("签署页缺少双方盖章位", "内控缺陷", "low", "done",
         "已按模板补全签署页并重新用印", "2026-06-10"),
        ("相对方营业执照未留存", "内控缺陷", "low", "open", "", "2026-07-31"),
    ]
    for title, cat, sev, st, rect, dl in seed:
        AUDIT_FINDINGS.append({
            "finding_id": "AF-" + uuid.uuid4().hex[:8].upper(),
            "project_id": proj["project_id"],
            "task_id": "", "contract_name": "（演示数据）",
            "title": title, "category": cat, "severity": sev,
            "snippet": "", "evidence": "",
            "rectification": rect,
            "deadline": dl,
            "review_result": "复核通过，问题闭环" if st == "done" else "",
            "status": st,
            "created_by": "系统预置演示数据",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })


seed_audit_demo()


@app.get("/api/audit-projects")
def audit_projects(request: Request):
    require_perm(request, "view")
    with store.LOCK:
        items = list(reversed(AUDIT_PROJECTS))
    return JSONResponse(content=items)


@app.post("/api/audit-projects")
def create_audit_project(request: Request, req: dict):
    require_perm(request, "manage_audit")
    name = (req.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "项目名称不能为空")
    proj = {
        "project_id": "AP-" + uuid.uuid4().hex[:8].upper(),
        "name": name,
        "scope": req.get("scope") or [],        # 划定范围：合同 task_id 列表
        "members": req.get("members") or [],    # 分配审计人员
        "start_date": req.get("start_date") or "",
        "end_date": req.get("end_date") or "",
        "status": "active",
        "created_by": request.headers.get("X-Role") or "演示模式",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with store.LOCK:
        AUDIT_PROJECTS.append(proj)
    return JSONResponse(content=proj)


@app.get("/api/audit-projects/{pid}/findings")
def audit_findings(request: Request, pid: str):
    require_perm(request, "view")
    with store.LOCK:
        items = [f for f in AUDIT_FINDINGS if f["project_id"] == pid]
    return JSONResponse(content=list(reversed(items)))


@app.post("/api/audit-projects/{pid}/findings")
def add_finding(request: Request, pid: str, req: dict):
    require_perm(request, "audit_finding")
    title = (req.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "问题标题不能为空")
    finding = {
        "finding_id": "AF-" + uuid.uuid4().hex[:8].upper(),
        "project_id": pid,
        "task_id": req.get("task_id") or "",
        "contract_name": req.get("contract_name") or "",
        "title": title,
        "category": req.get("category") or "法律风险",   # 内控缺陷/法律风险/业务风险
        "severity": req.get("severity") or "medium",     # high/medium/low
        "snippet": req.get("snippet") or "",
        "evidence": req.get("evidence") or "",
        "rectification": "",
        "deadline": req.get("deadline") or "",
        "review_result": "",
        "status": "open",
        "created_by": request.headers.get("X-Role") or "演示模式",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with store.LOCK:
        AUDIT_FINDINGS.append(finding)
    return JSONResponse(content=finding)


@app.patch("/api/findings/{fid}")
def update_finding(request: Request, fid: str, req: dict):
    with store.LOCK:
        f = next((x for x in AUDIT_FINDINGS if x["finding_id"] == fid), None)
    if not f:
        raise HTTPException(404, "审计发现不存在")
    if req.get("rectification") is not None:   # 整改反馈（业务/组员）
        require_perm(request, "rectify")
        f["rectification"] = req["rectification"]
        if req.get("deadline") is not None:
            f["deadline"] = req["deadline"]
        f["status"] = "rectifying"
    if req.get("review_result") is not None:   # 复核（负责人）
        require_perm(request, "audit_review")
        f["review_result"] = req["review_result"]
        f["status"] = "done" if req.get("done", True) else "rectifying"
    return JSONResponse(content=f)


@app.get("/api/audit-stats")
def audit_stats(request: Request):
    """审计看板统计：项目数、发现总数、整改闭环率、疑点等级分布。"""
    require_perm(request, "view")
    with store.LOCK:
        findings = list(AUDIT_FINDINGS)
        projects = len(AUDIT_PROJECTS)
    total = len(findings)
    done = sum(1 for f in findings if f["status"] == "done")
    sev = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
    return JSONResponse(content={
        "projects": projects, "total": total,
        "open": sum(1 for f in findings if f["status"] == "open"),
        "rectifying": sum(1 for f in findings if f["status"] == "rectifying"),
        "done": done,
        "rate": round(done / total * 100) if total else 0,
        "severity": sev,
    })


@app.get("/api/audit-projects/{pid}/report")
def audit_report(request: Request, pid: str):
    """专项审计报告（Markdown，可导出）。"""
    require_perm(request, "view")
    with store.LOCK:
        proj = next((p for p in AUDIT_PROJECTS if p["project_id"] == pid), None)
        items = [f for f in AUDIT_FINDINGS if f["project_id"] == pid]
    if not proj:
        raise HTTPException(404, "审计项目不存在")
    lines = [
        f"# 审计专项报告 · {proj['name']}",
        "",
        "## 一、项目概况",
        f"- 审计范围：{len(proj['scope'])} 份合同",
        f"- 审计人员：{'、'.join(proj['members']) or '（未分配）'}",
        f"- 审计期间：{proj['start_date']} 至 {proj['end_date']}",
        f"- 发现问题：{len(items)} 项",
        "",
        "## 二、审计发现清单",
    ]
    sev_name = {"high": "严重", "medium": "一般", "low": "轻微"}
    cat_stat = {}
    for i, f in enumerate(items, 1):
        cat_stat[f["category"]] = cat_stat.get(f["category"], 0) + 1
        lines += [
            f"### {i}. 【{sev_name.get(f['severity'], '一般')}】{f['title']}",
            f"- 合同：{f['contract_name'] or '（未关联）'}",
            f"- 问题分类：{f['category']}",
            f"- 原文片段：{f['snippet'] or '（无）'}",
            f"- 证据：{f['evidence'] or '（无）'}",
            f"- 整改方案：{f['rectification'] or '（待整改）'}",
            f"- 整改时限：{f['deadline'] or '（未定）'}",
            f"- 复核结果：{f['review_result'] or '（未复核）'}",
            "",
        ]
    lines += ["## 三、问题分类统计", ""]
    for k, v in cat_stat.items():
        lines.append(f"- {k}：{v} 项")
    lines += ["", "## 四、整改情况", "",
              f"- 待整改：{sum(1 for f in items if f['status'] == 'open')} 项",
              f"- 整改中：{sum(1 for f in items if f['status'] == 'rectifying')} 项",
              f"- 已闭环：{sum(1 for f in items if f['status'] == 'done')} 项", ""]
    return Response(content="\n".join(lines), media_type="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename={pid}.md"})


# ---------------- 注册 / 登录（PRD 登录注册） ----------------
@app.post("/api/register")
def register(req: dict):
    username = (req.get("username") or "").strip()
    password = (req.get("password") or "").strip()
    name = (req.get("name") or "").strip() or username
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    if username in USERS:
        raise HTTPException(400, "该用户名已被注册，请更换")
    USERS[username] = {"password": hash_password(password),
                       "name": name}
    _save_users()
    token = create_token(username, "registered", name)
    return JSONResponse(content={"ok": True, "username": username, "name": name,
                                 "role": "registered", "token": token})


@app.post("/api/login")
def login(req: dict):
    username = (req.get("username") or "").strip()
    password = (req.get("password") or "").strip()
    # A1：管理员账号凭据由部署环境注入（ADMIN_USERNAME / ADMIN_PASSWORD），
    # 不在 users.json 明文落盘；匹配成功才签发 admin 角色令牌。
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "")
    if username == admin_user:
        if not admin_pass or password != admin_pass:
            raise HTTPException(401, "用户名或密码错误")
        token = create_token(username, "admin", "系统管理员")
        return JSONResponse(content={"ok": True, "username": username, "name": "系统管理员",
                                     "role": "admin", "token": token})
    u = USERS.get(username)
    if not u or not verify_password(u.get("password", ""), password):
        raise HTTPException(401, "用户名或密码错误")
    # 历史账号若仍是裸 sha256（无盐）或参数落后，登录成功即透明升级为 scrypt 加盐哈希
    if needs_rehash(u.get("password", "")):
        u["password"] = hash_password(password)
        _save_users()
    token = create_token(username, "registered", u.get("name", username))
    return JSONResponse(content={"ok": True, "username": username, "name": u.get("name", username),
                                 "role": "registered", "token": token})


# ---------------- 法律条文库（PRD 规则页搜索 / 上传补充法律） ----------------
# 每条都带「法律名称 + 条款号 + 分类 + 原文」，既支撑规则引擎的合规依据，
# 也作为「法律条文库」页面的知识底座（评审/演示时可搜索、可引用）。
LAWS: list[dict] = [
    # —— 民法典 · 总则 / 合同通则 ——
    {"law_id": "L-C6", "law_name": "民法典", "article": "第6条",
     "content": "民事主体从事民事活动，应当遵循公平原则，合理确定各方的权利和义务。",
     "category": "基本原则"},
    {"law_id": "L-C7", "law_name": "民法典", "article": "第7条",
     "content": "民事主体从事民事活动，应当遵循诚信原则，秉持诚实，恪守承诺。",
     "category": "基本原则"},
    {"law_id": "L-C143", "law_name": "民法典", "article": "第143条",
     "content": "具备下列条件的民事法律行为有效：（一）行为人具有相应的民事行为能力；（二）意思表示真实；（三）不违反法律、行政法规的强制性规定，不违背公序良俗。",
     "category": "合同效力"},
    {"law_id": "L-C153", "law_name": "民法典", "article": "第153条",
     "content": "违反法律、行政法规的强制性规定的民事法律行为无效，但是该强制性规定不导致该民事法律行为无效的除外。违背公序良俗的民事法律行为无效。",
     "category": "合同效力"},
    {"law_id": "L-C464", "law_name": "民法典", "article": "第464条",
     "content": "合同是民事主体之间设立、变更、终止民事法律关系的协议。婚姻、收养、监护等有关身份关系的协议，适用有关该身份关系的法律规定。",
     "category": "合同订立"},
    {"law_id": "L-C470", "law_name": "民法典", "article": "第470条",
     "content": "合同的内容由当事人约定，一般包括下列条款：（一）当事人的姓名或者名称和住所；（二）标的；（三）数量；（四）质量；（五）价款或者报酬；（六）履行期限、地点和方式；（七）违约责任；（八）解决争议的方法。",
     "category": "合同订立"},
    {"law_id": "L-C490", "law_name": "民法典", "article": "第490条",
     "content": "当事人采用合同书形式订立合同的，自当事人均签名、盖章或者按指印时合同成立。在签名、盖章或者按指印之前，当事人一方已经履行主要义务，对方接受时，该合同成立。",
     "category": "合同订立"},
    {"law_id": "L-C497", "law_name": "民法典", "article": "第497条",
     "content": "有下列情形之一的，该格式条款无效：（一）具有本法第一编第六章第三节和本法第五百零六条规定的无效情形；（二）提供格式条款一方不合理地免除或者减轻其责任、加重对方责任、限制对方主要权利；（三）提供格式条款一方排除对方主要权利。",
     "category": "格式条款"},
    {"law_id": "L-C509", "law_name": "民法典", "article": "第509条",
     "content": "当事人应当按照约定全面履行自己的义务。当事人应当遵循诚信原则，根据合同的性质、目的和交易习惯履行通知、协助、保密等义务。",
     "category": "合同履行"},
    {"law_id": "L-C563", "law_name": "民法典", "article": "第563条",
     "content": "有下列情形之一的，当事人可以解除合同：（一）因不可抗力致使不能实现合同目的；（二）在履行期限届满前，当事人一方明确表示或者以自己的行为表明不履行主要债务；（三）当事人一方迟延履行主要债务，经催告后在合理期限内仍未履行；（四）当事人一方迟延履行债务或者有其他违约行为致使不能实现合同目的。",
     "category": "合同解除"},
    {"law_id": "L-C577", "law_name": "民法典", "article": "第577条",
     "content": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
     "category": "违约责任"},
    {"law_id": "L-C584", "law_name": "民法典", "article": "第584条",
     "content": "当事人一方不履行合同义务或者履行合同义务不符合约定，造成对方损失的，损失赔偿额应当相当于因违约所造成的损失，包括合同履行后可以获得的利益；但是，不得超过违约一方订立合同时预见到或者应当预见到的因违约可能造成的损失。",
     "category": "违约责任"},
    {"law_id": "L-C585", "law_name": "民法典", "article": "第585条",
     "content": "当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金，也可以约定因违约产生的损失赔偿额的计算方法。约定的违约金过分高于造成的损失的，人民法院或者仲裁机构可以根据当事人的请求予以适当减少。",
     "category": "违约责任"},
    {"law_id": "L-C590", "law_name": "民法典", "article": "第590条",
     "content": "当事人一方因不可抗力不能履行合同的，根据不可抗力的影响，部分或者全部免除责任，但是法律另有规定的除外。因不可抗力不能履行合同的，应当及时通知对方，以减轻可能给对方造成的损失，并应当在合理期限内提供证明。",
     "category": "违约免责"},
    {"law_id": "L-C626", "law_name": "民法典", "article": "第626条",
     "content": "买受人收到标的物时应当在约定的检验期间内检验。没有约定检验期间的，应当及时检验。",
     "category": "验收检验"},
    {"law_id": "L-C123", "law_name": "民法典", "article": "第123条",
     "content": "民事主体依法享有知识产权。知识产权是权利人依法就下列客体享有的专有的权利：（一）作品；（二）发明、实用新型、外观设计；（三）商标；（四）地理标志；（五）商业秘密；（六）集成电路布图设计；（七）植物新品种；（八）法律规定的其他客体。",
     "category": "知识产权"},
    {"law_id": "L-C807", "law_name": "民法典", "article": "第807条",
     "content": "发包人未按照约定支付价款的，承包人可以催告发包人在合理期限内支付价款。发包人逾期不支付的，除根据建设工程的性质不宜折价、拍卖外，承包人可以与发包人协议将该工程折价，也可以请求人民法院将该工程依法拍卖。建设工程的价款就该工程折价或者拍卖的价款优先受偿。",
     "category": "建设工程"},
    # —— 争议解决 ——
    {"law_id": "L-P35", "law_name": "民事诉讼法", "article": "第35条",
     "content": "合同或者其他财产权益纠纷的当事人可以书面协议选择被告住所地、合同履行地、合同签订地、原告住所地、标的物所在地等与争议有实际联系的地点的人民法院管辖。",
     "category": "争议管辖"},
    # —— 竞争与秘密 ——
    {"law_id": "L-A9", "law_name": "反不正当竞争法", "article": "第9条",
     "content": "经营者不得实施下列侵犯商业秘密的行为：（一）以盗窃、贿赂、欺诈、胁迫、电子侵入或者其他不正当手段获取权利人的商业秘密；（二）披露、使用或者允许他人使用以前项手段获取的权利人的商业秘密；（三）违反保密义务或者违反权利人有关保守商业秘密的要求，披露、使用或者允许他人使用其所掌握的商业秘密。",
     "category": "商业秘密"},
    # —— 劳动用工 ——
    {"law_id": "L-L19", "law_name": "劳动合同法", "article": "第19条",
     "content": "劳动合同期限三个月以上不满一年的，试用期不得超过一个月；劳动合同期限一年以上不满三年的，试用期不得超过二个月；三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月。同一用人单位与同一劳动者只能约定一次试用期。",
     "category": "劳动用工"},
    {"law_id": "L-L47", "law_name": "劳动合同法", "article": "第47条",
     "content": "经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资的标准向劳动者支付。六个月以上不满一年的，按一年计算；不满六个月的，向劳动者支付半个月工资的经济补偿。",
     "category": "劳动用工"},
    {"law_id": "L-L82", "law_name": "劳动合同法", "article": "第82条",
     "content": "用人单位自用工之日起超过一个月不满一年未与劳动者订立书面劳动合同的，应当向劳动者每月支付二倍的工资。用人单位违反本法规定不与劳动者订立无固定期限劳动合同的，自应当订立无固定期限劳动合同之日起向劳动者每月支付二倍的工资。",
     "category": "劳动用工"},
    # —— 招投标 ——
    {"law_id": "L-B43", "law_name": "招标投标法", "article": "第43条",
     "content": "在确定中标人前，招标人不得与投标人就投标价格、投标方案等实质性内容进行谈判。",
     "category": "招标投标"},
    {"law_id": "L-B46", "law_name": "招标投标法", "article": "第46条",
     "content": "招标人和中标人应当自中标通知书发出之日起三十日内，按照招标文件和中标人的投标文件订立书面合同。招标人和中标人不得再行订立背离合同实质性内容的其他协议。",
     "category": "招标投标"},
    # —— 数据合规 ——
    {"law_id": "L-D27", "law_name": "数据安全法", "article": "第27条",
     "content": "开展数据处理活动应当依照法律、法规的规定，建立健全全流程数据安全管理制度，组织开展数据安全教育培训，采取相应的技术措施和其他必要措施，保障数据安全。",
     "category": "数据合规"},
    {"law_id": "L-PIPL13", "law_name": "个人信息保护法", "article": "第13条",
     "content": "符合下列情形之一的，个人信息处理者方可处理个人信息：（一）取得个人的同意；（二）为订立、履行个人作为一方当事人的合同所必需，或者按照依法制定的劳动规章制度和依法签订的集体合同实施人力资源管理所必需；（三）为履行法定职责或者法定义务所必需……",
     "category": "数据合规"},
    # —— 担保 / 付款 ——
    {"law_id": "L-C681", "law_name": "民法典", "article": "第681条",
     "content": "保证合同是为保障债权的实现，保证人和债权人约定，当债务人不履行到期债务或者发生当事人约定的情形时，保证人履行债务或者承担责任的合同。",
     "category": "担保保证"},
    {"law_id": "L-C686", "law_name": "民法典", "article": "第686条",
     "content": "保证的方式包括一般保证和连带责任保证。当事人在保证合同中对保证方式没有约定或者约定不明确的，按照一般保证承担保证责任。",
     "category": "担保保证"},
]


@app.get("/api/laws")
def get_laws(request: Request, q: str = ""):
    require_perm(request, "view")
    items = LAWS
    if q:
        ql = q.lower()
        items = [x for x in LAWS if ql in x["law_name"].lower() or ql in x["article"].lower()
                 or ql in x["content"].lower() or ql in x["category"].lower()]
    return JSONResponse(content=items)


@app.post("/api/laws")
def add_law(request: Request, req: dict):
    """上传 / 补充法律条文到法律库（PRD 规则页上传法律）。"""
    require_perm(request, "manage_rules")
    law_name = (req.get("law_name") or "").strip()
    article = (req.get("article") or "").strip()
    content = (req.get("content") or "").strip()
    if not law_name or not content:
        raise HTTPException(400, "法律名称与条文内容不能为空")
    law = {"law_id": f"L-U{len(LAWS) + 1:03d}", "law_name": law_name,
           "article": article or "（未注条款号）",
           "content": content,
           "category": (req.get("category") or "").strip() or "其他"}
    LAWS.append(law)
    return JSONResponse(content=law)


@app.post("/api/rules/{rule_id}/enable")
def toggle_rule(request: Request, rule_id: str, req: dict):
    """启用 / 停用某条审查规则（PRD 规则页勾选用哪些法律审查）。"""
    require_perm(request, "manage_rules")
    enabled = bool(req.get("enabled"))
    with store.LOCK:
        for r in store.RULES:
            if r.rule_id == rule_id:
                r.enabled = enabled
                return JSONResponse(content=r.model_dump())
    raise HTTPException(404, "规则不存在")


@app.get("/api/checklist")
def get_checklist(request: Request):
    """合同分类审查清单：各业务类型的必备条款（PRD 2.4.1 规则与模板管理）。"""
    require_perm(request, "view")
    return JSONResponse(content=[
        {"biz_type": k, "biz_name": {"procurement": "采购", "sales": "销售",
                                     "service": "服务", "labor": "劳动"}.get(k, k),
         "required_clauses": v}
        for k, v in REQUIRED_CLAUSES.items()
    ])


# ---------------- 大模型配置（可选能力，PRD 2.4.4/2.4.6） ----------------
@app.get("/api/llm/config")
def get_llm_config(request: Request):
    """当前大模型配置状态（不含明文 Key）——含端点/模型名，需登录后查看。"""
    require_perm(request, "view")
    return JSONResponse(content=llm.status())


@app.post("/api/llm/config")
def set_llm_config(request: Request, req: LLMConfigReq):
    """更新大模型配置；persist=true 时同时写入项目根 .env。"""
    require_perm(request, "manage_rules")
    st = llm.set_config(api_key_=req.api_key, base_url_=req.base_url, model_=req.model,
                        proxy_=req.proxy, timeout_=req.timeout, suggest_=req.suggest,
                        persist=req.persist)
    return JSONResponse(content=st)


@app.post("/api/llm/test")
def test_llm(request: Request):
    """连通性自检（会真实发起一次最小请求）。"""
    require_perm(request, "manage_rules")
    ok, msg = llm.test_connection()
    return JSONResponse(content={"ok": ok, "message": msg})


# ---------------- 失败告警邮件（解析失败 → 邮箱通知，PRD 2.4.7 延伸） ----------------
@app.get("/api/mail/status")
def get_mail_status(request: Request):
    """邮件通道配置状态（授权码不回显明文；含收件邮箱，需登录后查看）。"""
    require_perm(request, "view")
    return JSONResponse(content=mailer.status())


@app.post("/api/mail/config")
def set_mail_config(request: Request, req: dict):
    """更新邮件配置。

    敏感项（发件邮箱 / SMTP 授权码 / 收件邮箱）默认**加密落盘**
    （data/mail_secret.json），并清掉 .env 里的明文同名键；
    非敏感项（服务器 / 端口）按 persist 决定是否写回 .env。
    """
    require_perm(request, "manage_rules")
    st = mailer.set_config(host_=req.get("host"), port_=req.get("port"),
                           user_=req.get("user"), password_=req.get("password"),
                           to_=req.get("to"), enabled_=req.get("enabled"),
                           persist=bool(req.get("persist")),
                           encrypt=bool(req.get("encrypt", True)))
    return JSONResponse(content=st)


@app.post("/api/mail/test")
def test_mail(request: Request):
    """发一封测试邮件（真实投递，用于打通通道自检）。"""
    require_perm(request, "manage_rules")
    ok, msg = mailer.test_connection()
    return JSONResponse(content={"ok": ok, "message": msg})


@app.get("/api/mail/failures")
def mail_failures(request: Request):
    """「失败上传到QQ邮箱」页面数据：解析失败的任务 + 其邮件通知状态。

    A3：返回体含收件邮箱明文与失败原因（可能含合同名），必须登录后访问。
    """
    require_perm(request, "view")
    items = []
    for t in store.list_tasks():
        if t.status != "blocked":
            continue
        items.append({
            "task_id": t.task_id, "contract_name": t.contract_name,
            "biz_type": t.biz_type.value, "applicant": t.applicant,
            "department": t.meta.department or "",
            "file_type": t.file_type,
            "blocked_reason": mailer.clean_reason(t.blocked_reason),
            "created_at": t.created_at,
            "mail_status": t.mail_status, "mail_to": t.mail_to,
            "mail_sent_at": t.mail_sent_at, "mail_error": t.mail_error,
        })
    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    sent = sum(1 for i in items if i["mail_status"] == "sent")
    failed = sum(1 for i in items if i["mail_status"] == "failed")
    return JSONResponse(content={
        "configured": mailer.enabled(),
        "config": {"to": mailer.mail_to(), "user": mailer.user(),
                   "host": mailer.host(), "port": mailer.port()},
        "items": items, "total": len(items), "sent": sent, "failed": failed,
        "not_sent": len(items) - sent - failed,
    })


@app.post("/api/tasks/{task_id}/mail")
def resend_failure_mail(request: Request, task_id: str):
    """手动重发某条失败任务的邮件通知。"""
    require_perm(request, "monitor")
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ok, msg = mailer.send_failure(task)
    return JSONResponse(content={"ok": ok, "message": msg,
                                 "mail_status": task.mail_status,
                                 "mail_sent_at": task.mail_sent_at,
                                 "mail_to": task.mail_to})


# ---------------- 内置智能助手（用法答疑 / 法律问答 / 闲聊兜底） ----------------
@app.post("/api/assistant/chat")
def assistant_chat(request: Request, req: dict):
    """对话入口：LLM 优先（法律问答/引导），未配置时本地知识库兜底。"""
    require_perm(request, "view")
    msg = str(req.get("message") or "")[:2000]
    return JSONResponse(content=assistant.chat(msg))


# ---------------- 静态资源 ----------------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 首次启动预置演示任务（后台线程跑真实审查链路，不阻塞启动）。
# 放在这里而不是文件顶部：_process 已定义，且已过 seed_samples 注册样例。
seed_demo_tasks(_process)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
