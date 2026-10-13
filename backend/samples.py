"""示例合同生成（PRD 2.4.9 示例业务场景 + 交付形式：示例合同文件集）。

覆盖采购/销售/服务/劳动的标准与风险用例，包含 DOCX / PDF / 扫描件图片 三种格式：
  - 采购风险：知识产权归属供应商 + 到货即付全款无验收 + 管辖约定不利（高风险）
  - 销售风险：违约责任严重不对等（高风险）
  - 服务风险：保密无期限 + 不可抗力无通知时效（中风险）
  - 扫描件：图片格式（附 .txt 供未装 OCR 引擎时模拟识别，PRD 2.4.1）
"""
from __future__ import annotations

import os

# ---------------- 合同文本模板 ----------------
CONTRACTS = {
    "procurement_risk": {
        "title": "软件采购合同（风险用例）",
        "biz_type": "procurement",
        "applicant": "张三(采购部)",
        "docx": True, "pdf": True,
        "body": """甲方：示例科技有限公司
乙方：云软信息技术有限公司
统一社会信用代码：91110108MA01ABCDEF
合同编号：HT-2026-CG-001
送审部门：采购部
合同金额：人民币 300,000 元
履行期限：自合同签订之日起 90 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方采购「智审」企业合同审查软件一套，含一年期标准技术支持服务。

第二条 知识产权条款
本合同所涉软件之全部知识产权（含源代码、文档）归乙方所有；甲方仅获得非独占的使用许可。

第三条 付款进度
甲方应在乙方软件到货后 3 个工作日内，向乙方支付合同全部款项人民币 300,000 元。

第四条 违约责任
因乙方原因导致交付延迟的，乙方按日支付合同总额 0.5% 的违约金，上限不超过合同总额 20%。

第五条 保密义务
双方对本合同涉及的商业秘密负有保密义务，保密期限为本合同终止后 3 年。

第六条 争议解决管辖
因本合同发生争议，提交乙方所在地有管辖权的人民法院诉讼解决。
""",
    },
    "sales_risk": {
        "title": "大额销售合同（风险用例）",
        "biz_type": "sales",
        "applicant": "李四(销售部)",
        "docx": True, "pdf": True,
        "body": """甲方：示例制造有限公司
乙方：大客户集团
统一社会信用代码：91310000MA1FL00001
合同编号：HT-2026-XS-014
送审部门：销售部
合同金额：人民币 5,000,000 元
履行期限：自合同签订之日起 180 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方销售工业控制设备一批，具体规格见附件《技术协议》。

第二条 付款进度
乙方应于设备发运前支付 30% 预付款，到货验收合格后支付剩余 70%。

第三条 验收标准
乙方应在设备到货后 15 个工作日内完成验收，逾期未提出书面异议视为验收合格。

第四条 违约责任
乙方延迟付款的，每日按未付金额万分之一支付滞纳金；甲方违约的，应向乙方承担无上限的赔偿责任，并赔偿乙方因此遭受的全部损失。

第五条 争议解决管辖
因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决。
""",
    },
    "procurement_std": {
        "title": "办公设备采购合同（标准用例）",
        "biz_type": "procurement",
        "applicant": "王五(行政部)",
        "docx": True, "pdf": False,
        "body": """甲方：示例科技有限公司
乙方：卓越办公设备有限公司
统一社会信用代码：91110105MA02C3X999
合同编号：HT-2026-CG-027
送审部门：行政部
合同金额：人民币 80,000 元
履行期限：自合同签订之日起 30 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方采购办公电脑及打印机若干，清单见附件。

第二条 知识产权条款
乙方保证所供设备不侵犯第三方知识产权；设备自带软件许可归甲方正常使用。

第三条 付款进度
甲方在设备交付并经验收合格后 10 个工作日内，支付合同全部款项人民币 80,000 元。

第四条 验收标准
甲方在到货后 7 个工作日内验收，验收合格出具验收单后方可付款。

第五条 违约责任
任一方违约，向守约方支付违约金，违约金不超过合同总额的 20%。

第六条 保密义务
双方对合同知悉的商业秘密保密，期限为本合同终止后 2 年。本合同项下保密信息包括附件、
技术资料、报价与双方往来文件，以及书面标注「保密」或依其性质应视为保密的信息。

第七条 争议解决管辖
因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决。

第八条 不可抗力
因不可抗力不能履行本合同的一方，应在不可抗力发生后 5 个工作日内书面通知对方并提供证明，
双方按影响程度协商变更或解除合同。
""",
    },
    "service_risk": {
        "title": "系统实施与运维服务合同（中风险用例）",
        "biz_type": "service",
        "applicant": "孙七(技术部)",
        "docx": True, "pdf": False,
        "body": """甲方：示例科技有限公司
乙方：云服信息技术有限公司
统一社会信用代码：91110108MA01SVC001
合同编号：HT-2026-SVC-009
送审部门：技术部
合同金额：人民币 460,000 元
履行期限：自合同签订之日起 120 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
乙方向甲方提供「智审」系统实施、培训与一年期运维服务。

第二条 付款进度
甲方于各阶段服务验收合格后 15 个工作日内支付对应阶段服务费。

第三条 验收标准
甲方在每阶段服务交付后 10 个工作日内完成验收并出具验收单。

第四条 违约责任
任一方违约，向守约方支付违约金，违约金不超过合同总额的 20%。

第五条 保密义务
双方对服务过程中知悉的商业秘密负有保密义务。

第六条 不可抗力
因不可抗力导致无法履行的，受影响方应及时通知对方。

第七条 数据安全
乙方处理甲方业务数据应遵守数据安全与个人信息保护相关法律法规，不得超范围使用。

第八条 争议解决管辖
因本合同发生争议，提交甲方所在地有管辖权的人民法院诉讼解决。
""",
    },
    "labor_std": {
        "title": "劳动合同（标准用例）",
        "biz_type": "labor",
        "applicant": "赵六(人力部)",
        "docx": True, "pdf": False,
        "body": """甲方：示例科技有限公司
乙方：新员工
统一社会信用代码：91110108MA01ABCDEF
合同编号：HT-2026-LD-003
送审部门：人力部
合同金额：月薪人民币 20,000 元
履行期限：自入职之日起 3 年
生效条件：双方签字后生效

第一条 标的物条款
乙方受聘担任研发工程师，负责产品研发相关工作。

第二条 付款进度
甲方于每月 10 日支付乙方上月工资人民币 20,000 元。

第三条 违约责任
任一方解除合同的，应提前 30 日通知并按法律规定承担相应责任。

第四条 保密义务
乙方在职及离职后 2 年内，对甲方商业秘密负有保密义务。保密信息包括甲方技术方案、
源代码、客户资料、经营数据及书面标注「保密」的其他信息。

第五条 争议解决管辖
因本合同发生争议，提交甲方所在地劳动争议仲裁委员会仲裁。
""",
    },
    # 扫描件图片用例（PRD 2.4.10-1 扫描件解析；附 .txt 供无 OCR 引擎时模拟识别）
    "scan_procurement": {
        "title": "软件采购合同（扫描件·风险用例）",
        "biz_type": "procurement",
        "applicant": "周八(采购部)",
        "docx": False, "pdf": False, "image": True,
        "body": """甲方：示例科技有限公司
乙方：迅捷软件有限公司
统一社会信用代码：91110108MA01SCAN01
合同编号：HT-2026-CG-058
送审部门：采购部
合同金额：人民币 680,000 元
履行期限：自合同签订之日起 150 日
生效条件：双方盖章并签字后生效

第一条 标的物条款
甲方向乙方采购定制化合同管理系统一套及配套实施服务。

第二条 知识产权条款
定制开发部分的源代码及全部知识产权归乙方所有，甲方仅获得使用许可。

第三条 付款进度
甲方应在合同签订后 5 个工作日内支付合同全款的 80%。

第四条 违约责任
甲方逾期付款的，每日按未付金额千分之五支付违约金。

第五条 争议解决管辖
因本合同发生争议，提交新加坡国际仲裁中心仲裁解决。
""",
    },
}


def _write_docx(path: str, title: str, body: str):
    from docx import Document
    doc = Document()
    doc.add_heading(title, level=0)
    for line in body.split("\n"):
        doc.add_paragraph(line)
    doc.save(path)


def _write_pdf(path: str, title: str, body: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    # 内嵌 Adobe 中文 CID 字体，保证中文可被 pdfplumber 抽取
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("STSong-Light", 14)
    c.drawString(20 * mm, 280 * mm, title)
    c.setFont("STSong-Light", 10)
    y = 265 * mm
    for line in body.split("\n"):
        c.drawString(20 * mm, y, line)
        y -= 6 * mm
        if y < 20 * mm:
            c.showPage()
            y = 280 * mm
    c.save()


def _write_image(path: str, title: str, body: str):
    """用中文字体渲染一张「合同扫描件」图片，并写出 .txt sidecar。

    sidecar（<png>.txt）用于本机未安装 Tesseract 时模拟 OCR（PRD 2.4.1），
    装好 Tesseract 后 parse_image 会自动优先走真实 OCR，忽略 sidecar。
    """
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1000, 1500
    img = Image.new("RGB", (W, H), (252, 252, 248))   # 略带米白的纸张底色
    d = ImageDraw.Draw(img)
    font_path = None
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for fn in ("msyh.ttc", "simsun.ttc", "simhei.ttf", "msyhbd.ttc"):
        fp = os.path.join(windir, "Fonts", fn)
        if os.path.exists(fp):
            font_path = fp
            break
    try:
        title_font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
        body_font = ImageFont.truetype(font_path, 20) if font_path else ImageFont.load_default()
    except Exception:
        title_font = body_font = ImageFont.load_default()
    y = 46
    d.text((46, y), title, fill=(10, 10, 10), font=title_font)
    y += 54
    for line in body.split("\n"):
        d.text((46, y), line, fill=(25, 25, 25), font=body_font)
        y += 30
        if y > H - 40:
            break
    img.save(path)
    # sidecar 模拟 OCR 文本
    with open(path + ".txt", "w", encoding="utf-8") as f:
        f.write(title + "\n" + body)


def seed_samples(samples_dir: str) -> dict:
    """生成示例合同并注册到 store.SAMPLES，返回注册表。"""
    from . import store
    os.makedirs(samples_dir, exist_ok=True)
    for key, spec in CONTRACTS.items():
        body = spec["body"]
        meta = {"contract_name": spec["title"], "biz_type": spec["biz_type"],
                "applicant": spec["applicant"]}
        if spec.get("docx"):
            p = os.path.join(samples_dir, f"{key}.docx")
            _write_docx(p, spec["title"], body)
            store.SAMPLES[f"{key}.docx"] = {"path": p, **meta}
        if spec.get("pdf"):
            p = os.path.join(samples_dir, f"{key}.pdf")
            _write_pdf(p, spec["title"], body)
            store.SAMPLES[f"{key}.pdf"] = {"path": p, **meta}
        if spec.get("image"):
            p = os.path.join(samples_dir, f"{key}.png")
            try:
                _write_image(p, spec["title"], body)
                store.SAMPLES[f"{key}.png"] = {"path": p, **meta}
            except Exception as e:
                # 缺 Pillow / 中文字体时也不能影响启动，只跳过该图片样例
                import sys
                print(f"[samples] 跳过图片样例 {key}.png：{e}", file=sys.stderr)
    return store.SAMPLES
