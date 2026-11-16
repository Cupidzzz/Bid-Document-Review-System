#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""py_selftest.py —— 沙箱环境自检（用 py.cmd 运行）

用法：cmd /c py.cmd py_selftest.py
      然后 Get-Content py_last.log -Encoding UTF8

检查项：解释器、工作区读写、中文编码、本地 8767 接口、OCR 依赖
"""
import io
import os
import sys

print('Python:', sys.version.split()[0])
print('工作区:', r'D:\DSH\投标文件复核')
print('  可读:', os.path.isdir(r'D:\DSH\投标文件复核\pipeline\项目'))
try:
    p = r'D:\DSH\投标文件复核\pipeline\_selftest_probe.txt'
    io.open(p, 'w', encoding='utf-8').write('写入正常')
    ok = io.open(p, encoding='utf-8').read()
    os.remove(p)
    print('  可写:', ok)
except Exception as e:
    print('  可写: 失败 %r' % e)

print('编码: 中文输出正常')

try:
    import urllib.request
    import json
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    r = json.loads(op.open('http://127.0.0.1:8767/api/project', timeout=5).read()
                   .decode())
    print('本地接口: 可访问，项目 %d 个' % len(r))
except Exception as e:
    print('本地接口: 不可用 %r' % e)

for m in ('pdf_text', 'ocr_text', 'expert_scores', 'evidence_spec'):
    try:
        __import__(m)
        print('模块 %-14s OK' % m)
    except Exception as e:
        print('模块 %-14s 失败 %r' % (m, e))
