#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""正确的前端校验：逐块检查页面里【所有】内联 <script>（此前只查了第一个小脚本，
主逻辑脚本从未被校验，导致重复声明的语法错误漏网）。"""
import os
import re
import shutil
import subprocess
import sys

def _find_node():
    """按优先级探测 node：显式环境变量 → PATH → WorkBuddy 托管目录（版本号可变，需通配）。

    此前硬编码 '...\\node\\versions\\22.22.2\\node.exe'，托管目录改名成 22.22.2-6 后
    本脚本直接 FileNotFoundError，形同废置。
    """
    env = os.environ.get("NODE_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("node")
    if found:
        return found
    root = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), reverse=True):
            p = os.path.join(root, name, "node.exe")
            if os.path.exists(p):
                return p
    return None

NODE = _find_node()
if not NODE:
    print("  !! 未找到 node（可用 NODE_BIN 环境变量指定），跳过语法检查")
    sys.exit(1)
print(f"  使用 node: {NODE}")
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
html = open(path, encoding='utf-8').read()
blocks = re.findall(r'<script>(.*?)</script>', html, re.S)
bad = 0
print(f'  {path}: {len(blocks)} 个内联 script 块')
for i, b in enumerate(blocks):
    f = f'_chk{i}.js'
    open(f, 'w', encoding='utf-8').write(b)
    r = subprocess.run([NODE, '--check', f], capture_output=True, text=True,
                       encoding='utf-8', errors='ignore')
    ok = r.returncode == 0
    print(f'    块{i} ({len(b):>6} 字符): ' + ('OK' if ok else '!! 语法错误'))
    if not ok:
        bad += 1
        for ln in (r.stderr or '').strip().splitlines()[:5]:
            print('      ' + ln.strip())
    os.remove(f)
print('  => ' + ('全部通过' if bad == 0 else f'{bad} 个块有语法错误'))

# ---- 运行时检查：无头浏览器真实执行页面 JS，验证关键 DOM 被渲染 ----
# 背景：语法合法但顶层运行时报错（如引用了函数内局部变量）会让主脚本中途死亡，
# node --check 抓不住，必须真实执行。用 --dump-dom 后检查动态渲染的节点。
CHROME_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]
chrome = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
runtime_bad = 0
if chrome:
    import tempfile, urllib.request, threading, socket, functools, http.server
    # 起一个临时静态服务器托管该页面（避免文件协议下 fetch/相对路径问题）
    static_dir = os.path.dirname(os.path.abspath(path))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=static_dir)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    with tempfile.TemporaryDirectory() as prof:
        out = os.path.join(prof, 'dom.html')
        r = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--allow-file-access-from-files',
                            f'--user-data-dir={prof}', '--virtual-time-budget=5000',
                            f'--dump-dom', f'http://127.0.0.1:{port}/index.html'],
                           capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=60)
    httpd.shutdown()
    dom = r.stdout or ''
    checks = [
        ('转盘卡片 .ocard >= 4 张（initOrbit 已执行）', dom.count('class="ocard"') >= 4),
        ('小猫客服 #ai-fab-cat 已填充形象（applyCat 已执行）', bool(re.search(r'id="ai-fab-cat">\s*<(?:img|svg)', dom))),
    ]
    print('  运行时检查（无头浏览器执行页面 JS）：')
    for name, ok in checks:
        print(f'    {"OK" if ok else "!! 失败"} - {name}')
        if not ok:
            runtime_bad += 1
else:
    print('  （未找到 Chrome/Edge，跳过运行时检查）')

total_bad = bad + runtime_bad
print('  => ' + ('全部通过' if total_bad == 0 else f'{total_bad} 项失败'))
sys.exit(1 if total_bad else 0)
