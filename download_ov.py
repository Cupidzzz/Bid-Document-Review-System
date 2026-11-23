#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
download_ov.py —— 从清华镜像下载 OpenVINO 引擎 wheel（可选加速引擎）

OpenVINO 是 OCR 的加速推理后端（实测比 onnxruntime 快 5~20 倍），但不是必需：
不装时服务端自动回退 onnxruntime（慢但能跑）。
下载到的 wheel 请装进 `vendor_ov\` 目录（与 vendor\ 分开，避免污染默认依赖）。

用法：
    python download_ov.py <输出目录>
    # 例：python download_ov.py vendor_ov
"""
import hashlib
import os
import re
import sys
import time
import urllib.request

BASE = 'https://pypi.tuna.tsinghua.edu.cn/simple/%s/'
PKGS = ['openvino', 'openvino-telemetry', 'packaging']
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ver_key(fn):
    """版本号取文件名第二段，不能从整个文件名抓数字（会把 cp39 当版本）。"""
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


def _parse_link(href):
    url = href.split('#')[0]
    sha = ''
    m = re.search(r'#sha256=([0-9a-fA-F]{64})', href)
    if m:
        sha = m.group(1).lower()
    return url, sha


def _sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def main(out):
    os.makedirs(out, exist_ok=True)
    for pkg in PKGS:
        try:
            t = time.time()
            html = op.open(BASE % pkg, timeout=30).read().decode('utf-8', 'replace')
            links = re.findall(r'href=[\'"]([^\'"]*?\.whl[^\'"]*)[\'"]', html)
            cand = []
            for href in links:
                u, sha = _parse_link(href)
                fn = os.path.basename(u)
                if usable(fn):
                    cand.append((ver_key(fn), platform_rank(fn), u, fn, sha))
            if not cand:
                print('SKIP %-24s 无适用 wheel' % pkg, flush=True)
                continue
            cand.sort()
            _v, _p, u, fn, sha = cand[-1]
            if u.startswith('/'):
                u = 'https://pypi.tuna.tsinghua.edu.cn' + u
            elif not u.startswith('http'):
                u = (BASE % pkg) + u
            dst = os.path.join(out, fn)
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                try:
                    with open(dst, 'rb') as f:
                        _have = f.read()
                    if not sha or _sha256_of(_have) == sha:
                        print('HAVE %-40s %6.1f MB（哈希一致）'
                              % (fn[:40], os.path.getsize(dst) / 1048576),
                              flush=True)
                        continue
                    print('BAD  %-40s 哈希不符，重新下载' % fn[:40], flush=True)
                except Exception:
                    pass
            data = op.open(u, timeout=300).read()
            if sha and _sha256_of(data) != sha:
                print('FAIL %-24s SHA256 不符（官方 %s，实际 %s），已拒用'
                      % (pkg, sha, _sha256_of(data)), flush=True)
                continue
            with open(dst, 'wb') as f:
                f.write(data)
            print('OK   %-40s %6.1f MB  %.1fs（SHA256 已校验）'
                  % (fn[:40], len(data) / 1048576, time.time() - t), flush=True)
        except Exception as e:
            print('FAIL %-24s %s' % (pkg, type(e).__name__ + ': ' + str(e)[:60]),
                  flush=True)
    print('DONE  —— 把下载的 wheel 用 pip 装进 vendor_ov\\：', flush=True)
    print('  python -m pip install --no-deps --target vendor_ov vendor_ov\\*.whl',
          flush=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get('TEMP', '.'), 'ovwhl'))
