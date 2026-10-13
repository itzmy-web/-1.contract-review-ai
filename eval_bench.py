# -*- coding: utf-8 -*-
"""OCR 识别自动评测脚本。

用法：
  1. 编辑同目录 benchmark_answers.json，为每张图填写标准答案（expect 字段，留空=不判）
  2. 把图片放到 IMG_DIR（默认本机桌面）
  3. 运行：.\.venv\Scripts\python.exe eval_bench.py
  4. 输出每张图逐字段结果 + 字段准确率 + 汇总表
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend.parsers import parse_contract  # noqa: E402

IMG_DIR = os.environ.get("BENCH_DIR", r"C:\Users\zmy\Desktop")
ANS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmark_answers.json")

FIELDS = [
    ("contract_no", "合同编号", lambda m: m.contract_no or ""),
    ("department", "送审部门", lambda m: m.department or ""),
    ("subject_name", "甲方主体", lambda m: m.subject_name or ""),
    ("counterparty", "乙方相对方", lambda m: m.counterparty or ""),
    ("credit_code", "信用代码", lambda m: m.credit_code or ""),
    ("amount", "合同金额", lambda m: m.amount or 0),
    ("currency", "币种", lambda m: m.currency or ""),
    ("performance_period", "履行期限", lambda m: m.performance_period or ""),
]


def match(kind, got, want):
    """字段比对：金额按数值，其余按精确相等（strip）。"""
    if kind == "amount":
        try:
            return abs(float(got or 0) - float(want or 0)) < 0.5
        except Exception:
            return False
    g = (got or "").strip()
    w = (want or "").strip()
    if not w:
        return None  # 标准答案为空 → 该字段不计入
    if kind == "performance_period":
        # 期限字段按"包含关键子串"放宽：任一非空片段命中即可
        return bool(g and w) and (g in w or w in g)
    return g == w


def main():
    data = json.load(open(ANS_FILE, encoding="utf-8"))
    samples = data["samples"]
    print("=" * 72)
    print(f"OCR 识别测评 · 样本 {len(samples)} 张")
    print("=" * 72)

    total_hit = 0
    total_valid = 0
    rows = []

    for s in samples:
        fname = s["file"]
        path = os.path.join(IMG_DIR, fname)
        if not os.path.exists(path):
            print(f"\n[跳过] 找不到图片：{path}")
            continue
        t0 = time.time()
        r = parse_contract(path)
        m = r.meta
        hit = 0
        valid = 0
        detail = []
        for key, label, getter in FIELDS:
            got = getter(m)
            want = s["expect"].get(key, "")
            res = match(key, got, want)
            if res is None:
                detail.append(f"  · {label}：跳过（未标注）")
                continue
            valid += 1
            if res:
                hit += 1
                detail.append(f"  ✓ {label}：{got or '(空)'}")
            else:
                detail.append(f"  ✗ {label}：识别「{got or '(空)'}」≠ 标准「{want}」")
        acc = hit / valid if valid else 1.0
        total_hit += hit
        total_valid += valid
        level = "S" if acc >= 0.95 else "A" if acc >= 0.85 else "B" if acc >= 0.70 else "C"
        rows.append((fname, s.get("level", "-"), acc, level))
        print(f"\n■ {fname}  [{s.get('level','-')}级]  {r.note}")
        print(f"  字段准确率 {hit}/{valid} = {acc:.0%}  → 评级 {level}   ({time.time()-t0:.0f}s)")
        for d in detail:
            print(d)

    print("\n" + "=" * 72)
    print("汇总表")
    print(f"{'样本':<44}{'级别':<6}{'字段准确率':<10}{'评级'}")
    for fname, lv, acc, lvl in rows:
        print(f"{fname:<44}{lv:<6}{acc:.0%}{'':<6}{lvl}")
    print("-" * 72)
    overall = total_hit / total_valid if total_valid else 0
    print(f"总体字段准确率：{total_hit}/{total_valid} = {overall:.0%}")
    ov_level = "S" if overall >= 0.95 else "A" if overall >= 0.85 else "B" if overall >= 0.70 else "C"
    print(f"总体评级：{ov_level}")
    print("=" * 72)


if __name__ == "__main__":
    main()
