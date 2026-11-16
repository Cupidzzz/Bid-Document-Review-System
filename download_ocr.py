#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
download_ocr.py —— 从清华镜像下载 OCR 依赖 wheel

背景：pip 与 urllib 直连 files.pythonhosted.org（PyPI 文件 CDN）TLS 握手超时，
但 pypi.tuna.tsinghua.edu.cn 索引与文件均正常（14 MB 约 4 秒）。
故改为解析镜像索引页、直接从镜像取 wheel，再离线安装。
"""
import os
import re
import sys
import time
import urllib.request

BASE = 'https://pypi.tuna.tsinghua.edu.cn/simple/%s/'
PKGS = ['rapidocr-onnxruntime', 'pyclipper', 'opencv-python-headless',
        'numpy', 'six', 'shapely', 'PyYAML', 'Pillow', 'onnxruntime',
        'tqdm', 'requests', 'colorama', 'packaging']
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ver_key(fn):
    """版本号取文件名第二段（PyYAML-6.0.3-cp39-... -> 6.0.3），
    不能从整个文件名抓数字——那会把 cp39 当版本，导致 win32 压过 win_amd64。"""
    parts = fn.split('-')
    s = parts[1] if len(parts) >= 2 else ''
    nums = re.findall(r'\d+', s)
    return tuple(int(x) for x in nums[:4])


def platform_rank(fn):
    n = fn.lower()
    if 'win_amd64' in n:
        return 2
    if 'win32' in n:
        return 1
    return 0


def usable(fn):
    n = fn.lower()
    if not n.endswith('.whl'):
        return False
    if 'py3-none-any' in n or 'py2.py3-none-any' in n:
        return True
    return 'cp39' in n and ('win_amd64' in n or 'win32' in n)


def main(out):
    os.makedirs(out, exist_ok=True)
    for pkg in PKGS:
        try:
            t = time.time()
            html = op.open(BASE % pkg, timeout=30).read().decode('utf-8', 'replace')
            links = re.findall(r'href=[\'"]([^\'"]+\.whl)[#\'"]', html)
            cand = []
            for u in links:
                fn = os.path.basename(u.split('#')[0])
                if usable(fn):
                    cand.append((ver_key(fn), platform_rank(fn), u, fn))
            if not cand:
                print('SKIP %-24s 无适用 wheel' % pkg, flush=True)
                continue
            cand.sort()
            _v, _p, u, fn = cand[-1]
            if u.startswith('/'):
                u = 'https://pypi.tuna.tsinghua.edu.cn' + u
            elif not u.startswith('http'):
                u = (BASE % pkg) + u
            dst = os.path.join(out, fn)
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                print('HAVE %-40s %6.1f MB' % (fn[:40],
                                              os.path.getsize(dst) / 1048576),
                      flush=True)
                continue
            data = op.open(u, timeout=300).read()
            with open(dst, 'wb') as f:
                f.write(data)
            print('OK   %-40s %6.1f MB  %.1fs'
                  % (fn[:40], len(data) / 1048576, time.time() - t), flush=True)
        except Exception as e:
            print('FAIL %-24s %s' % (pkg, type(e).__name__ + ': ' + str(e)[:60]),
                  flush=True)
    print('DONE', flush=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get('TEMP', '.'), 'whldl3'))
