"""文档解析引擎（PRD 2.4.1 / 2.4.6 文档解析引擎）。

支持 DOCX / PDF / 图片扫描件(OCR) 解析，输出：
  - doc_text: 全文文本（用于前端原文渲染与高亮定位）
  - segments: 条款切分（带页码/段落索引/全文起止字符）
  - meta: 合同核心元数据
异常时抛出 BlockedError 进入 blocked 状态（PRD 2.4.4）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import ClauseSegment, ContractMeta

# 图片扩展名（需 OCR）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif"}


class BlockedError(Exception):
    """解析受阻，对应 PRD 任务状态 blocked。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------- 条款类型识别 ----------
CLAUSE_KEYWORDS = {
    "标的物条款": ["标的", "货物", "软件", "硬件", "服务内容", "采购内容", "产品"],
    "付款进度": ["付款", "支付", "价款", "金额支付", "分期", "结算"],
    "验收标准": ["验收", "交付标准", "交付验收"],
    "违约责任": ["违约", "赔偿", "滞纳金", "违约金", "连带责任"],
    "保密义务": ["保密"],
    "数据安全": ["数据", "个人信息", "数据安全", "隐私"],
    "争议解决管辖": ["争议", "管辖", "仲裁", "诉讼", "法院", "适用法律"],
    "知识产权": ["知识产权", "著作权", "专利", "源代码", "商标"],
    "生效条件": ["生效", "生效条件"],
    "履行期限": ["履行期限", "工期", "服务期限", "合同期限"],
}


def classify_clause(text: str) -> str:
    """按关键词判定条款类型。

    P1-5：改为「按出现次数计分 + 标题行加权」，避免仅凭正文里的一个词
    （如采购合同正文出现「软件」）把「知识产权条款」误判为「标的物条款」。
    """
    title, _, rest = text.partition("\n")
    best, score = "其他条款", 0
    for ctype, kws in CLAUSE_KEYWORDS.items():
        s = 0
        for k in kws:
            s += rest.count(k)            # 正文出现次数
            s += title.count(k) * 3       # 标题行权重更高
        if s > score:
            best, score = ctype, s
    return best


def _split_paragraphs(raw: str):
    """将原始文本切分为段落列表（保留空段落以维持索引）。"""
    paras = re.split(r"\n+", raw)
    return [p for p in paras]


HEADER_RE = re.compile(
    r"^(第[一二三四五六七八九十百零0-9]+条|[一二三四五六七八九十]+、|\d+[.、]|[（(][一二三四五六七八九十\d]+[)）])",
    re.M,
)


def segment_text(doc_text: str) -> list[ClauseSegment]:
    """按条款标题（第X条 / 一、 / 数字. / （一））切分并归类。

    P1-5：偏移直接由正则匹配位置得到，避免重建查找导致的错位。
    """
    matches = list(HEADER_RE.finditer(doc_text))
    if not matches:
        ctype = classify_clause(doc_text[:40])
        return [ClauseSegment(clause_type=ctype, text=doc_text.strip(),
                              page=1, paragraph_index=0, start_char=0, end_char=len(doc_text))]
    segs: list[ClauseSegment] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(doc_text)
        body = doc_text[start:end].strip()
        if not body:
            continue
        page = doc_text[:start].count("\n") // 45 + 1
        segs.append(ClauseSegment(clause_type=classify_clause(body[:40]), text=body,
                                  page=page, paragraph_index=i, start_char=start, end_char=end))
    return segs


# ---------- 元数据提取（PRD 2.4.4） ----------
# 中文大写数字映射
_CN_DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
              "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000, "万": 10000, "亿": 100000000}


def _cn_amount_to_float(s: str) -> float | None:
    """把「壹佰贰拾万元整」之类的大写金额转成数字；无法解析返回 None。

    P1-7：支持 万/亿 量级与 拾/佰/仟 进位，覆盖常见合同大写写法。
    """
    s = re.sub(r"[^\u4e00-\u9fa5]", "", s)  # 去非汉字（整/圆/空格等）
    if not s:
        return None
    total = 0.0       # 已结算的“亿/万”以上部分
    section = 0.0     # 当前“万”以内段落累加值
    current = 0.0     # 当前未进位数字（个位）
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit < 10000:           # 拾/佰/仟：进位到段落
                section += current * unit
                current = 0.0
            else:                      # 万/亿：段落结算入总额
                section += current
                total += section * unit
                section = 0.0
                current = 0.0
        else:
            # 角/分等小数位或非数字汉字，简单忽略，避免误算
            continue
    return total + section + current


def _norm_currency(word: str | None) -> str | None:
    if not word:
        return None
    w = word.strip()
    if w in ("人民币", "¥", "￥"):
        return "人民币"
    if w in ("美元", "USD", "usd"):
        return "美元"
    if w in ("EUR", "eur", "欧元"):
        return "EUR"
    return None


def _extract_amount(doc_text: str) -> tuple[float | None, str | None]:
    """返回 (金额, 币种)。优先阿拉伯数字，其次中文大写。"""
    # 1) 阿拉伯数字：合同金额[：:]? 币种? ¥? 数字 元
    #    标签覆盖真实合同常见表述（建设工程类常写「总造价」「包干价」），分隔符兼容「为」。
    m = re.search(
        r"(?:总金额|合同金额|合同总价|总造价|造价|包干价|总价|价款|金额)[：:为]?\s*"
        r"(人民币|美元|USD|EUR|¥|￥)?"
        r"\s*([¥￥]?\s*[\d,]+(?:\.\d+)?)\s*元",
        doc_text,
    )
    if m:
        cur_word, raw = m.group(1), m.group(2)
        try:
            val = float(raw.replace("¥", "").replace("￥", "").replace(",", "").strip())
        except ValueError:
            val = None
        if val is not None:
            return val, _norm_currency(cur_word)
    # 2) 中文大写：币种? 壹贰叁... 元
    m = re.search(r"(人民币|美元|USD|EUR|¥|￥)?\s*([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)\s*元", doc_text)
    if m:
        val = _cn_amount_to_float(m.group(2))
        if val is not None:
            return val, _norm_currency(m.group(1))
    return None, None


def extract_metadata(doc_text: str) -> ContractMeta:
    meta = ContractMeta()

    # 统一社会信用代码（18 位）；真实合同也常写作「纳税人识别号 / 税号」
    m = re.search(r"(?:统一社会信用代码|纳税人识别号|纳税人识别码|税号|信用代码)"
                  r"[：:]\s*([0-9A-Z]{15,20})", doc_text)
    if m:
        meta.credit_code = m.group(1)

    # 合同编号 / 项目编号（PRD 2.4.8 报告基本信息）
    m = re.search(r"(?:合同编号|项目编号|合同编码|编号)[：:]\s*([A-Za-z0-9\-_/（）()]{3,40})", doc_text)
    if m:
        meta.contract_no = m.group(1).strip()

    # 送审部门 / 申请部门（PRD 2.4.8 报告基本信息）
    m = re.search(r"(?:送审部门|申请部门|发起部门|部门)[：:]\s*([^\n，。,；;]{2,30})", doc_text)
    if m:
        meta.department = m.group(1).strip()

    # 主体名称：甲方=我方(签约主体)，乙方=相对方（PRD 2.4.8 报告基本信息）
    # 兼容「甲方名称」「甲方：」「甲方（单位）：」「甲方 」等写法；
    # 兼容真实合同的别名写法：委托方（甲方）/ 承揽方（乙方）/ 出租方 / 承租方 等。
    _PARTY_ALIASES = {
        "甲方": ["委托方（甲方）", "委托方(甲方)", "甲方", "出租方", "购买方"],
        "乙方": ["承揽方（乙方）", "承揽方(乙方)", "乙方", "承租方", "受托方", "销售方", "供货方"],
    }

    # 名称合理性校验（实测必要）：
    #   「甲方 / 乙方」在真实合同里同样频繁出现在条款正文中（「甲方应按…」），
    #   若照原样截取，会把整句当成主体名 —— 实测抓到「在工作中受伤害」「同专用章」
    #   「有权终止合作协议」等噪声，同时也漏掉「甲方：（发包方）」换行后才是公司名的版式。
    #   因此：先剥掉行首括注 → 同行/次行取候选 → 必须「像名称」才采用。
    _NAME_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9·．.&＆（）()\-]{2,40}")
    _NAME_SUFFIX = re.compile(
        r"(公司|集团|厂|店|中心|事务所|银行|医院|学校|大学|学院|研究院|研究所|合作社"
        r"|商行|超市|酒店|宾馆|物业|工程队|门市部|经营部|服务部|工作室|委员会|管理局)$")
    _NAME_NOISE = re.compile(
        r"[，。；;、:：]|不承担|不负责|应当|应按|有权|承担|负责|任何|义务|权利"
        r"|专用章|盖章|签字|签章|发包方|承包方|委托方|承租方|受托方|供货方|销售方"
        r"|受伤害|工作中|签订|之日起|第一|第二|第三")

    def _clean_name(s: str) -> str:
        """去掉行首括注（「（发包方）」「(甲方)」）与两端空白。"""
        return re.sub(r"^[（(][^）)]{0,12}[）)]\s*", "", (s or "").strip()).strip()

    def _ok_name(s: str) -> bool:
        """候选是否像「主体名称」：形制合法 + 无条款噪声 + 带实体后缀或本身够短（人名）。"""
        s = _clean_name(s)
        if not s or not _NAME_RE.fullmatch(s):
            return False
        if _NAME_NOISE.search(s):
            return False
        return bool(_NAME_SUFFIX.search(s)) or len(s) <= 6

    def _party(label: str):
        for lbl in _PARTY_ALIASES.get(label, [label]):
            # 形式一：标签 + 可选括注 + 冒号（「甲方：」「甲方（单位）：」「甲方名称：」）
            for m in re.finditer(
                    rf"{lbl}(?:名称)?(?:[（(][^）)]{{0,10}}[）)])?[：:][ \t]*([^\n]*)", doc_text):
                cand = _clean_name(m.group(1))
                if _ok_name(cand):
                    return cand
                # 冒号后为空 / 只有括注 → 取紧随其后的第一个非空行（真实合同常见版式）
                for line in doc_text[m.end():].split("\n")[1:]:
                    if not line.strip():
                        continue
                    if _ok_name(line):
                        return line
                    break       # 下一非空行也不是名称 → 放弃这处，继续找下一处匹配
            # 形式二：无冒号，标签后接空白（「甲方 某某公司」）
            m = re.search(rf"{lbl}[ \t]+([^\n]*)", doc_text)
            if m and _ok_name(m.group(1)):
                return _clean_name(m.group(1))
        return None

    party_a, party_b = _party("甲方"), _party("乙方")
    meta.subject_name = party_a or party_b
    meta.counterparty = party_b or party_a

    # 金额（多模式）+ 币种
    amount, currency = _extract_amount(doc_text)
    if amount is not None:
        meta.amount = amount
    if currency:
        meta.currency = currency
    if not meta.currency:
        mc = re.search(r"(?:币种|货币)[：:]?\s*(人民币|美元|USD|EUR|欧元)", doc_text)
        if mc:
            meta.currency = _norm_currency(mc.group(1))

    m = re.search(r"(?:履行期限|合同期限|服务期限|工期)[：:]\s*([^\n，。]{2,40})", doc_text)
    if m:
        meta.performance_period = m.group(1).strip()

    m = re.search(r"生效条件[：:]\s*([^\n，。]{2,40})", doc_text)
    if m:
        meta.effective_condition = m.group(1).strip()

    # 质保期（审计规格书：结构化元数据）
    m = re.search(r"(?:质保期|保修期|质量保证期|质量保修期)[：:为]?\s*([^\n，。；;]{1,30})", doc_text)
    if m:
        meta.warranty_period = m.group(1).strip()

    # 付款节点 / 付款方式（审计规格书）
    m = re.search(r"(?:付款节点|付款方式|支付方式|付款进度|付款条件)[：:]\s*([^\n]{2,60})", doc_text)
    if m:
        meta.payment_terms = m.group(1).strip()

    # 签订日期（倒签检测用）
    m = re.search(r"(?:签订日期|签署日期|签订时间|签约日期|合同签订日期)[：:]\s*(\d{4}[年./\-]\d{1,2}[月./\-]\d{1,2}日?)", doc_text)
    if m:
        meta.sign_date = m.group(1).strip()

    return meta


# ---------- 格式解析 ----------
@dataclass
class ParseResult:
    doc_text: str
    segments: list[ClauseSegment] = field(default_factory=list)
    meta: ContractMeta = None  # type: ignore
    note: str = ""              # 解析备注（如扫描件采用模拟 OCR）


def parse_docx(path: str) -> ParseResult:
    try:
        from docx import Document
    except Exception as e:  # noqa
        raise BlockedError(f"缺少 python-docx 依赖：{e}")
    try:
        doc = Document(path)
    except Exception as e:
        raise BlockedError(f"Word 文档打开失败（可能加密或损坏）：{e}")
    paras = [p.text for p in doc.paragraphs if p.text.strip() != ""]
    if not paras:
        raise BlockedError("Word 正文为空")
    doc_text = "\n".join(paras)
    return ParseResult(doc_text=doc_text, segments=segment_text(doc_text), meta=extract_metadata(doc_text))


def parse_pdf(path: str) -> ParseResult:
    try:
        import pdfplumber
    except Exception as e:  # noqa
        raise BlockedError(f"缺少 pdfplumber 依赖：{e}")
    try:
        texts = []
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise BlockedError("PDF 无页面内容")
            for pg in pdf.pages:
                t = pg.extract_text() or ""
                texts.append(t)
    except BlockedError:
        raise
    except Exception as e:
        raise BlockedError(f"PDF 解析失败（可能加密）：{e}")
    doc_text = "\n".join(texts)
    if not doc_text.strip():
        raise BlockedError("PDF 提取文本为空（可能为扫描件/加密）")
    return ParseResult(doc_text=doc_text, segments=segment_text(doc_text), meta=extract_metadata(doc_text))


_OCR_SINGLETON = None


def _get_rapidocr():
    """RapidOCR 懒加载单例（本地 onnx 中文 OCR，纯 pip 安装、离线可用）。"""
    global _OCR_SINGLETON
    if _OCR_SINGLETON is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_SINGLETON = RapidOCR()
    return _OCR_SINGLETON


def _enhance_image(path: str, binarize: bool = False) -> str:
    """模糊/低质扫描件预处理：灰度 → 放大 → 去噪 → 对比度拉伸 → 锐化（可选二值化）。

    binarize=False 适合模糊图（模糊笔画是渐变灰度，硬阈值会抹掉笔画）；
    binarize=True  适合清晰但底色重的扫描件（去灰底噪点）。
    返回增强后临时 PNG 路径，原图不动。任何异常静默失败（返回原路径）。
    """
    try:
        from PIL import Image, ImageOps, ImageFilter
        import tempfile
        img = Image.open(path).convert("L")
        w, h = img.size
        # 小图按需放大：OCR 检测模型对过小图片不敏感，放大到长边约 1600px
        mx = max(w, h)
        if mx < 1600:
            ratio = min(4.0, max(2.0, 1600 / mx))
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        img = img.filter(ImageFilter.MedianFilter(3))   # 去椒盐噪点
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.filter(ImageFilter.SHARPEN)
        if binarize:
            # 轻度二值化，去掉灰底/噪点，突出笔画
            img = img.point(lambda v: 255 if v > 150 else 0)
        out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(out.name)
        out.close()
        return out.name
    except Exception:
        return path


_OCR_CORRECT_SYSTEM = (
    "你是合同文本校对助手。下面是 OCR 从扫描件识别出的合同文本，可能含有：错别字、形近字误识、"
    "多余换行、标点错误、数字/金额识别错误、句子断裂。请只修正这些明显错误：不要增删任何条款、"
    "不要改写语义、不要补充内容。直接输出修正后的完整全文，不要任何解释、前后缀或代码块。"
)


def _llm_correct(text: str) -> str | None:
    """用大模型对 OCR 结果做上下文纠错（修正错别字/断行/数字/标点），失败返回 None。"""
    try:
        from . import llm
        if not llm.enabled():
            return None
        out = llm.chat_text(text, system=_OCR_CORRECT_SYSTEM)
        if out and len(out.strip()) >= 20:
            return out.strip()
    except Exception:
        pass
    return None


def parse_image(path: str) -> ParseResult:
    """扫描件解析（PRD 2.4.1 扫描件 OCR）。

    多级降级，兼顾「真实能力」与「可验收演示」：
      1) RapidOCR（本地 onnx 中文 OCR，纯 pip 安装、离线可用）—— 首选；
         识别为空/过少时，先做图像增强（灰度/放大/锐化/二值化）重试，解决模糊扫描件痛点；
         若配置了大模型，再对识别结果做上下文纠错（修正错别字/断行/数字）；
      2) 已装 Tesseract：Tesseract OCR（chi_sim+eng）；
      3) 未装任何 OCR 但存在同名 `<path>.txt` sidecar：用其文本作为「模拟 OCR」；
      4) 都没有：抛 BlockedError 进入 blocked（PRD 2.4.4），并给出安装指引。
    """

    def _mk(text: str, note: str) -> ParseResult:
        return ParseResult(doc_text=text, segments=segment_text(text),
                           meta=extract_metadata(text), note=note)

    def _run(path_: str):
        try:
            result, _ = _get_rapidocr()(path_)
            return "\n".join(item[1] for item in (result or []))
        except Exception:
            return ""

    # 1) RapidOCR（首选：本地、离线、中文识别效果好）
    #    模糊/低质图：依次尝试「原图 → 增强(不二值化) → 增强(二值化)」，取识别文本最多的一版
    try:
        best = ""
        used_enhance = False
        for i, cand in enumerate([path, _enhance_image(path, binarize=False), _enhance_image(path, binarize=True)]):
            t = _run(cand)
            if len(t.strip()) > len(best.strip()):
                best = t
                used_enhance = i > 0
            if len(best.strip()) >= 20:
                break
        if len(best.strip()) >= 20:
            note = "RapidOCR 本地识别" + ("（含图像增强）" if used_enhance else "")
            corrected = _llm_correct(best)
            if corrected:
                best = corrected
                note += " + 大模型纠错"
            return _mk(best, note)
    except BlockedError:
        raise
    except Exception:
        pass  # 未安装或识别异常 → 走下一级降级

    # 2) Tesseract OCR（若已安装）
    try:
        import pytesseract
        from PIL import Image as _Image
        img = _Image.open(path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        if text.strip():
            return _mk(text, "Tesseract OCR 真实识别")
        # 识别为空 → 继续尝试 sidecar，仍失败则 blocked
    except Exception:
        pass

    # 3) 模拟 OCR（sidecar 文本）
    sidecar = path + ".txt"
    if os.path.exists(sidecar):
        with open(sidecar, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        if text.strip():
            return _mk(text, "模拟 OCR（本机未安装 OCR 引擎，读取同名 .txt 演示文本）")

    # 4) 无法解析
    raise BlockedError(
        "扫描件无法识别：本机缺少 OCR 引擎。"
        "请在项目虚拟环境中执行 pip install rapidocr-onnxruntime 安装本地 OCR 后重试，或改用 DOCX/PDF 原件。"
    )


def parse_contract(path: str) -> ParseResult:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return parse_docx(path)
    if ext == ".pdf":
        return parse_pdf(path)
    if ext in IMAGE_EXTS:
        return parse_image(path)
    raise BlockedError(f"不支持的文件格式：{ext}")
