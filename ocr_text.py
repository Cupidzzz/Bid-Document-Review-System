#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ocr_text.py —— 本地中文 OCR（RapidOCR + onnxruntime，装在 vendor/）

能力边界
--------
* 纯本地推理，不需要 API key，不依赖网络
* 速度约 3~7 秒/张（原图 3300×4700，缩到长边 1600）
* 对印刷体中文识别良好；手写、印章、模糊影印会有错字

缓存
----
识别结果按「图片所在目录\\.ocr_cache\\<图片名>.json」持久化。
同一张图（大小与修改时间未变、max_side 相同）第二次调用直接读缓存，耗时接近 0。
复核过程中会反复回看同一批页，缓存能消掉绝大部分重复识别。

用法
----
    from ocr_text import ocr_image, cache_stats, clear_cache

    txt = ocr_image('p0020_i0.jpg')          # 自动用缓存
    txt = ocr_image('p0020_i0.jpg', use_cache=False)   # 强制重识别

    print(cache_stats(r'D:\\...\\xxx.pdf.pages'))
    clear_cache(r'D:\\...\\xxx.pdf.pages')
"""
import hashlib
import json
import re
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VEND = os.path.join(HERE, 'vendor')
if os.path.isdir(VEND) and VEND not in sys.path:
    sys.path.insert(0, VEND)

# 可选的 OpenVINO 引擎目录（独立安装，不污染 vendor；见 变更记录.md 2026-10-30 条）
VEND_OV = os.path.join(HERE, 'vendor_ov')
if os.path.isdir(VEND_OV) and VEND_OV not in sys.path:
    sys.path.append(VEND_OV)

_ENG = None
_ENG_GATE = threading.Lock()   # G7：engine 构造的双检锁（独立于 _ENG_LOCK，避免死锁）
_MAX_SIDE = 1600
CACHE_DIRNAME = '.ocr_cache'

# 推理引擎：onnxruntime（默认）| openvino
# 两个包 API 相同、模型相同（PP-OCRv4），换的只是推理后端。
# 默认引擎：显式 OCR_ENGINE 优先；否则只要 vendor_ov 在就直接用 openvino。
# 实测同一批扫描页（2026-10-30）：onnxruntime 6.80 秒/页，openvino 0.29–1.4 秒/页，
# 文字结果完全一致 —— 用错引擎会让整条复核链慢 5–20 倍，所以在代码里兜住。
_OV_DIR = os.path.join(HERE, 'vendor_ov')
_DEFAULT_ENGINE = 'openvino' if os.path.isdir(_OV_DIR) else 'onnxruntime'
_ENGINE_NAME = (os.environ.get('OCR_ENGINE') or _DEFAULT_ENGINE).strip().lower()

# 运行时统计（仅本进程内）
_STAT = {'命中': 0, '未命中': 0, '写入': 0, '让路': 0}

# ---------------- 前后台让路 ----------------
# 复核（前台）识别时刷新心跳文件；后台预热（ocr_many(yield_to_foreground=True)）
# 看到心跳新鲜就整段让路，把 CPU 让给前台。
# 动机：实测同一份活，独占时 2.4~3.2 秒/页；与另一路 OCR 并行时劣化到 8.4 秒/页，
# 8 逻辑核上多路 onnxruntime 互相抢线程，总吞吐反而下降。
#
# 除了心跳，还看队列：只要有复核任务处于「处理中」，后台就整段停住。
# 心跳只覆盖"正在识别"的那几秒，复核读文件的两分钟里心跳是冷的，
# 后台会趁机恢复满速，下一轮前台再识别又得重新抢——用任务状态做闸门才彻底。
_FG_FILE = os.path.join(HERE, 'work', '.ocr_fg')
_FG_YIELD_SECS = 20.0
_FG_LAST = [0.0]

QUEUE_DIR = os.path.join(HERE, 'work', 'queue')
_BG_HOLD_MAX_SECS = 1800.0     # 单次让路最多等 30 分钟，避免复核异常卡死后台永不恢复
_REVIEW_CACHE = {'t': 0.0, 'v': False}
_REVIEW_TTL = 5.0


def _fg_touch():
    """前台每识别一页刷新一次心跳（最多每秒写一次，成本可忽略）"""
    t = time.time()
    if t - _FG_LAST[0] < 1.0:
        return
    _FG_LAST[0] = t
    try:
        d = os.path.dirname(_FG_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(_FG_FILE, 'a'):
            pass
        os.utime(_FG_FILE, None)
    except Exception:
        pass


def _fg_age():
    """距上次前台识别过去多少秒；没有心跳文件时返回一个很大的数"""
    try:
        return time.time() - os.path.getmtime(_FG_FILE)
    except Exception:
        return 1e9


def review_active(refresh=False):
    """队列里是否有复核任务处于「处理中」（跨进程可用的闸门信号）。

    结果缓存 5 秒，避免后台轮询时反复读队列目录。
    """
    t = time.time()
    if not refresh and t - _REVIEW_CACHE['t'] < _REVIEW_TTL:
        return _REVIEW_CACHE['v']
    val = False
    try:
        for fn in os.listdir(QUEUE_DIR):
            if not fn.endswith('.json'):
                continue
            try:
                with open(os.path.join(QUEUE_DIR, fn), encoding='utf-8') as f:
                    if json.load(f).get('状态') == '处理中':
                        val = True
                        break
            except Exception:
                continue
    except Exception:
        val = False
    _REVIEW_CACHE['t'] = t
    _REVIEW_CACHE['v'] = val
    return val


def background_hold():
    """后台 OCR 该不该停手；返回原因字符串（空串=可以跑）"""
    if _fg_age() < _FG_YIELD_SECS:
        return '前台识别中'
    if review_active():
        return '复核处理中'
    return ''


# 「停手」的墙钟统计：多个工作线程同时停手时只计一次墙钟，避免
# 3 线程各等 10 秒被算成 30 秒（上一版就是这么把有效秒算成负数的）。
_HOLD = {'n': 0, 'since': 0.0, 'wall': 0.0}
_HOLD_LOCK = threading.Lock()


def _hold_begin():
    with _HOLD_LOCK:
        if _HOLD['n'] == 0:
            _HOLD['since'] = time.time()
        _HOLD['n'] += 1


def _hold_end():
    with _HOLD_LOCK:
        _HOLD['n'] = max(0, _HOLD['n'] - 1)
        if _HOLD['n'] == 0 and _HOLD['since']:
            _HOLD['wall'] += time.time() - _HOLD['since']
            _HOLD['since'] = 0.0


def _hold_wall_snapshot():
    with _HOLD_LOCK:
        v = _HOLD['wall']
        if _HOLD['n'] > 0 and _HOLD['since']:
            v += time.time() - _HOLD['since']
        return v


def foreground_busy(within=_FG_YIELD_SECS):
    """外部可直接调用：最近 within 秒内是否有前台 OCR 在跑"""
    return _fg_age() < within


def mark_foreground():
    """前台开始一段重活（如导出扫描页）时调用，后台预热会据此让路"""
    _fg_touch()


def engine():
    global _ENG
    if _ENG is None:
        # G7：双检锁——首启时预热线程 + 前台请求同时到达会各建一个 RapidOCR，
        # OpenVINO 编译要数秒、几十 MB，内存瞬时翻倍。
        with _ENG_GATE:
            if _ENG is None:
                if _ENGINE_NAME == 'openvino':
                    try:
                        import ocr_ov_engine
                        ocr_ov_engine.patch()
                    except Exception as e:
                        raise RuntimeError(
                            'OpenVINO 后端不可用（需 vendor_ov 里的 openvino）：%r' % (e,))
                from rapidocr_onnxruntime import RapidOCR
                _ENG = RapidOCR()
    return _ENG


def available():
    try:
        engine()
        return True
    except Exception:
        return False


# ---------------- 内存保护与引擎回收（2026-11-02） ----------------
# 背景：RapidOCR+OpenVINO 每处理一张**不同尺寸**的页图，提交内存会涨几十 MB
# （实测 25 页从 994 MB 涨到 3338 MB，约 +95 MB/页；同一张图重复跑几乎不涨）。
# 服务端连续跑几百页后会吃掉 5 GB 上下，叠加其它 python 进程，
# 系统提交内存触顶 → 新起的 python.exe 报 0xc0000142（DLL 初始化失败）。
# 对策：每处理 N 页把引擎整个释放重建（重建约 1.5 秒），把提交内存压回基线；
#      并对外提供「剩余提交内存」查询，供入口处提前判断而不是崩在启动阶段。
_ENG_USE = {'active': 0}
_ENG_LOCK = threading.Lock()
_PAGE_COUNT = {'n': 0}


def _recycle_pages():
    try:
        return int(os.environ.get('OCR_RECYCLE_PAGES') or 0)
    except Exception:
        return 0


def _max_commit_mb():
    """本进程提交内存上限（超过就回收引擎）。8 GB 机器上默认 1800 MB。"""
    try:
        return int(os.environ.get('OCR_MAX_COMMIT_MB') or 1800)
    except Exception:
        return 1800


def own_commit_mb():
    """本进程已提交内存（MB），拿不到返回 None。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [('cb', wt.DWORD), ('fc', wt.DWORD)] + \
                       [(n, ctypes.c_size_t) for n in
                        ('peakws', 'ws', 'qppp', 'qpp', 'qpnpp', 'qpnp',
                         'pf', 'peakpf', 'priv')]

        psapi = ctypes.WinDLL('psapi')
        k32 = ctypes.WinDLL('kernel32')
        k32.GetCurrentProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC),
                                               wt.DWORD]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(c), ctypes.sizeof(PMC)):
            return None
        return int(c.priv / 1048576)
    except Exception:
        return None


def commit_headroom_mb():
    """返回系统「提交内存」剩余 MB（拿不到时返回 None）。

    Windows 上 0xc0000142 多半就是这里见底造成的。
    注意：MEMORYSTATUSEX 必须按 64 字节传入，否则 GlobalMemoryStatusEx
    返回 ERROR_INVALID_PARAMETER(87)，会被误读成"剩余 0"。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt

        class MS(ctypes.Structure):
            # 字段顺序按 MEMORYSTATUSEX：
            # dwLength, dwMemoryLoad, TotalPhys, AvailPhys, TotalPageFile,
            # AvailPageFile, TotalVirtual, AvailVirtual, AvailExtendedVirtual
            # 「提交上限/可用」= TotalPageFile/AvailPageFile（不是 Virtual！）
            _fields_ = [('len', wt.DWORD), ('mem', wt.DWORD),
                        ('total', ctypes.c_ulonglong),
                        ('avail', ctypes.c_ulonglong),
                        ('commit_total', ctypes.c_ulonglong),
                        ('commit_avail', ctypes.c_ulonglong),
                        ('virt_total', ctypes.c_ulonglong),
                        ('virt_avail', ctypes.c_ulonglong),
                        ('_pad', ctypes.c_ulonglong)]

        ms = MS()
        ms.len = ctypes.sizeof(MS)
        k32 = ctypes.WinDLL('kernel32')
        k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MS)]
        k32.GlobalMemoryStatusEx.restype = wt.BOOL
        if not k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        return int(ms.commit_avail / 1048576)
    except Exception:
        return None


def recycle_engine(force=False):
    """释放并重建 OCR 引擎（清掉大量已提交但不再需要的内存）。

    active>0（有别的线程正在识别）时默认不回收，避免踩到正在用的模型。
    返回是否真的回收了。
    """
    global _ENG
    with _ENG_LOCK:
        if not force and _ENG_USE['active'] > 0:
            return False
        _ENG = None
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    _PAGE_COUNT['n'] = 0
    return True


def _after_page():
    """每页之后判断是否该回收引擎：**看本进程提交内存**，超过阈值就回收。

    阈值默认 1800 MB（`OCR_MAX_COMMIT_MB`），这样无论页图多复杂，
    单进程提交内存都不会无限涨——这正是 0xc0000142 的源头。
    `OCR_RECYCLE_PAGES` 仍保留为"按页数回收"的补充手段（默认关闭）。
    """
    _PAGE_COUNT['n'] += 1
    lim = _recycle_pages()
    if lim > 0 and _PAGE_COUNT['n'] >= lim:
        recycle_engine()
        return
    cap = _max_commit_mb()
    cur = own_commit_mb()
    if cap > 0 and cur is not None and cur >= cap:
        recycle_engine()


# ---------------- 缓存 ----------------

def _cache_dir(img_path):
    return os.path.join(os.path.dirname(os.path.abspath(img_path)), CACHE_DIRNAME)


def _cache_file(img_path):
    stem = os.path.splitext(os.path.basename(img_path))[0]
    return os.path.join(_cache_dir(img_path), stem + '.json')


def _sig(img_path, max_side):
    """缓存有效性签名：大小 + 内容指纹 + 缩放参数。

    加入指纹的目的：图片被重新导出时 mtime 会变但内容不变，
    若只看 mtime，463 张缓存会全部失效（重扫约 16 分钟）。
    指纹只取首尾各 64KB，成本可忽略。
    """
    try:
        st = os.stat(img_path)
    except Exception:
        return None
    h = hashlib.sha1()
    try:
        with open(img_path, 'rb') as f:
            sz = st.st_size
            # M16：首/中/尾三段抽样 + 长度，覆盖"同尺寸、同首尾、仅中间不同"的页图
            # （原来 <131072 只取头部，会误命中旧结果）
            h.update(f.read(65536))          # 首 64KB
            if sz > 131072:
                f.seek(sz // 2)
                h.update(f.read(65536))      # 中 64KB
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))      # 尾 64KB
        h.update(str(sz).encode())
        fp = h.hexdigest()[:16]
    except Exception:
        fp = ''
    return {'size': st.st_size,
            'mtime': int(st.st_mtime),
            'max_side': int(max_side or 0),
            'fp': fp,
            # 只有非默认引擎才带该键：默认档的历史缓存继续有效，
            # 而 OpenVINO 与 ONNX Runtime 的浮点结果不同，两套缓存不得混用。
            **({'engine': _ENGINE_NAME} if _ENGINE_NAME != 'onnxruntime' else {})}


def _read_cache(img_path, max_side):
    fp = _cache_file(img_path)
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return None
    sig = _sig(img_path, max_side)
    if not sig:
        return None
    old = d.get('签名') or {}
    # 不同推理引擎的口径不同，缓存不得互相命中
    if str(old.get('engine') or 'onnxruntime') != str(sig.get('engine') or 'onnxruntime'):
        return None
    ok = (old == sig)
    if not ok and old.get('size') == sig.get('size') \
            and old.get('max_side') == sig.get('max_side'):
        # 兼容旧缓存（无指纹）：mtime 相同即认
        if not old.get('fp') and old.get('mtime') == sig.get('mtime'):
            ok = True
        # 新缓存：内容指纹一致即认（重导出后 mtime 变了也能命中）
        elif old.get('fp') and sig.get('fp') and old['fp'] == sig['fp']:
            ok = True
    if not ok:
        return None
    txt = d.get('文字')
    if not isinstance(txt, str) or not txt:
        # K1（2026-11-11）：空缓存 = 早前"识别为空/读图失败"的残留，
        # 视为未命中并删除，避免把"读不到"固化成"该页无文字依据"。
        try:
            os.remove(fp)
        except Exception:
            pass
        return None
    return txt


def _write_cache(img_path, max_side, text, elapsed):
    sig = _sig(img_path, max_side)
    if not sig:
        return
    d = _cache_dir(img_path)
    try:
        os.makedirs(d, exist_ok=True)
        fp = _cache_file(img_path)
        tmp = fp + '.tmp.%d.%d' % (os.getpid(), threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'图片': os.path.basename(img_path),
                       '签名': sig,
                       '状态': 'ok',
                       '字数': len(text or ''),
                       '耗时秒': round(elapsed, 2),
                       '识别时间': time.strftime('%Y-%m-%d %H:%M:%S'),
                       '文字': text or ''},
                      f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fp)
        _STAT['写入'] += 1
    except Exception:
        # 缓存失败不影响主流程
        pass


def cache_stats(directory):
    """统计某个图片目录的缓存情况"""
    d = os.path.join(directory, CACHE_DIRNAME)
    if not os.path.isdir(d):
        return {'目录': d, '缓存文件数': 0, '缓存字节': 0}
    n, sz = 0, 0
    for f in os.listdir(d):
        if f.endswith('.json'):
            n += 1
            try:
                sz += os.path.getsize(os.path.join(d, f))
            except Exception:
                pass
    imgs = [f for f in os.listdir(directory)
            if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    return {'目录': d, '图片数': len(imgs), '缓存文件数': n,
            '缓存字节': sz, '覆盖率': round(100.0 * n / len(imgs), 1) if imgs else 0}


def clear_cache(directory):
    """删除某个图片目录的 OCR 缓存"""
    d = os.path.join(directory, CACHE_DIRNAME)
    n = 0
    if os.path.isdir(d):
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
                n += 1
            except Exception:
                pass
        try:
            os.rmdir(d)
        except Exception:
            pass
    return n


def runtime_stats():
    return dict(_STAT)


# ---------------- 识别 ----------------

def ocr_image(path, max_side=_MAX_SIDE, use_cache=True, foreground=True):
    """
    返回识别出的文字（多行）。默认优先读缓存。

    foreground=True 时刷新「前台忙」心跳，供后台预热让路（见文件头部 _fg_touch）。

    注意：Windows 下 cv2.imread 无法处理含中文的路径（返回 None），
    必须用 np.frombuffer + cv2.imdecode 从字节解码。
    """
    if foreground:
        _fg_touch()
    if use_cache:
        cached = _read_cache(path, max_side)
        if cached is not None:
            _STAT['命中'] += 1
            return cached
    _STAT['未命中'] += 1

    import cv2
    import numpy as np
    try:
        with open(path, 'rb') as f:
            buf = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        # K1（2026-11-11）：读图失败返回 None，与"识别成功但无字"（返回 ''）区分开；
        # 调用方据此判断是"没读到"而不是"该页没有文字"。
        return None
    h, w = img.shape[:2]
    long_side = max(h, w)
    if max_side and long_side > max_side:
        s = max_side / float(long_side)
        img = cv2.resize(img, (int(w * s), int(h * s)),
                         interpolation=cv2.INTER_AREA)
    t0 = time.time()
    # active 计数：有线程正在推理时不回收引擎（见 recycle_engine）
    with _ENG_LOCK:
        _ENG_USE['active'] += 1
    try:
        res, _elapse = engine()(img)
    finally:
        with _ENG_LOCK:
            _ENG_USE['active'] -= 1
    txt = ''
    if res:
        txt = '\n'.join(str(r[1]) for r in res if len(r) > 1)
    if use_cache and txt:
        # K1：只有识别到非空文字才写缓存；空结果不固化，下次会重新识别
        _write_cache(path, max_side, txt, time.time() - t0)
    try:
        _after_page()          # 每 N 页回收一次引擎，压住提交内存增长
    except Exception:
        pass
    return txt



def ocr_boxes(path, max_side=_MAX_SIDE, use_cache=True):
    """识别并把每行的**坐标框**一并返回，用于表格结构重建。

    返回 [{ 'text': str, 'box': [[x,y],...], 'score': float,
            'x0','y0','x1','y1','cx','cy','h' }, ...]
    坐标已按识别时的缩放还原到原始像素尺度。
    """
    import json as _json
    import os as _os

    cache = _os.path.join(_cache_dir(path),
                          _os.path.splitext(_os.path.basename(path))[0] +
                          '.boxes.json')
    if use_cache and _os.path.isfile(cache):
        try:
            with open(cache, encoding='utf-8') as f:
                d = _json.load(f)
            if d.get('sig') == _sig(path, max_side):
                _STAT['命中'] += 1
                return d.get('lines') or []
        except Exception:
            pass

    import cv2
    import numpy as np
    _STAT['未命中'] += 1
    try:
        with open(path, 'rb') as f:
            buf = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        return []
    h0, w0 = img.shape[:2]
    img2 = img
    scale = 1.0
    long_side = max(h0, w0)
    if max_side and long_side > max_side:
        scale = max_side / float(long_side)
        img2 = cv2.resize(img, (int(w0 * scale), int(h0 * scale)),
                          interpolation=cv2.INTER_AREA)
    t0 = time.time()
    # G7：active 计数——recycle_engine() 会据此跳过正在被 ocr_boxes 使用的引擎
    with _ENG_LOCK:
        _ENG_USE['active'] += 1
    try:
        res, _el = engine()(img2)
    finally:
        with _ENG_LOCK:
            _ENG_USE['active'] -= 1
    lines = []
    for r in res or []:
        try:
            box, text, score = r[0], str(r[1]), float(r[2]) if len(r) > 2 else 0.0
        except Exception:
            continue
        xs = [float(p[0]) / scale for p in box]
        ys = [float(p[1]) / scale for p in box]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        lines.append({'text': text, 'score': round(score, 3),
                      'box': [[round(float(p[0]) / scale, 1),
                               round(float(p[1]) / scale, 1)] for p in box],
                      'x0': round(x0, 1), 'y0': round(y0, 1),
                      'x1': round(x1, 1), 'y1': round(y1, 1),
                      'cx': round((x0 + x1) / 2, 1),
                      'cy': round((y0 + y1) / 2, 1),
                      'h': round(y1 - y0, 1), 'w': round(x1 - x0, 1)})
    if use_cache:
        # G3：原子写（tmp 带线程 id，多线程同图不互相截断），失败不留半截
        try:
            _os.makedirs(_cache_dir(path), exist_ok=True)
            tmp = cache + '.tmp.%d' % threading.get_ident()
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump({'sig': _sig(path, max_side), 'lines': lines,
                            '尺寸': [w0, h0], '耗时秒': round(time.time() - t0, 2)},
                           f, ensure_ascii=False)
            _os.replace(tmp, cache)
        except Exception:
            try:
                if _os.path.isfile(tmp):
                    _os.remove(tmp)
            except Exception:
                pass
    return lines


def ocr_many(paths, on_progress=None, workers=3, should_stop=None,
             lock=None, yield_to_foreground=False):
    """
    批量识别，返回 {'结果': {路径: 文字}, '失败': [...], '耗时秒': float}

    workers > 1 时用线程并发。实测本机 8 逻辑核下 3 线程约 2.3 倍速，
    再往上会因 onnxruntime 内部已多线程而互相争抢，反而变慢。
    必须用线程而非进程：本机沙箱拒绝 multiprocessing（PermissionError 13）。

    lock：可选的进程内锁，同一时刻只识别一页。服务端把 prewarm 与 read_bid
    两路 OCR 共用一把 HEAVY_LOCK，避免同一页被算两遍、也避免两路各自开满线程。

    yield_to_foreground：后台任务用。每页开跑前若发现前台识别在进行，
    先让路 sleep 1 秒；后台自身不刷新心跳，避免自己让路给自己。
    """
    import time as _t
    t0 = _t.time()
    hold0 = _hold_wall_snapshot()
    holds0 = _STAT.get('让路', 0)      # 让路次数按批次计，不累加历史批次
    out, fail = {}, []
    stopped = 0   # M12：停止单独计数，不塞进 fail（否则 '已停止' 恒 False）

    def one(p):
        if should_stop is not None:
            try:
                if should_stop():
                    return p, None, '__STOPPED__'
            except Exception:
                pass
        try:
            if yield_to_foreground and background_hold():
                # 后台整段停手：前台在识别，或有复核任务正在处理（最多停 30 分钟）。
                # 只 sleep 1 秒是不够的——那一页照样和前台抢同一批核心；
                # 实测"整段停住"能把前台恢复到独占水平。
                _STAT['让路'] += 1
                _hold_begin()
                try:
                    deadline = _t.time() + _BG_HOLD_MAX_SECS
                    while _t.time() < deadline and background_hold():
                        if should_stop is not None:
                            try:
                                if should_stop():
                                    return p, None, '__STOPPED__'
                            except Exception:
                                pass
                        _t.sleep(1.0)
                finally:
                    _hold_end()
            kw = {'foreground': not yield_to_foreground}
            if lock is None:
                return p, ocr_image(p, **kw), None
            with lock:
                return p, ocr_image(p, **kw), None
        except Exception as e:
            return p, None, repr(e)

    if workers and workers > 1 and len(paths) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = 0
        with ThreadPoolExecutor(max_workers=int(workers)) as ex:
            futs = [ex.submit(one, p) for p in paths]
            for fu in as_completed(futs):
                try:
                    p, txt, err = fu.result(timeout=300)   # M13：单页挂死 5 分钟兜底
                except Exception as e:
                    # 推理挂住/超时：标记失败继续，别让整个批次被一页拖死
                    fail.append({'文件': '（超时）', '错误': repr(e)[:80]})
                    done += 1
                    if on_progress:
                        try:
                            on_progress(done, len(paths), '')
                        except Exception:
                            pass
                    continue
                if err is None:
                    out[p] = txt
                elif err == '__STOPPED__':
                    stopped += 1
                else:
                    fail.append({'文件': os.path.basename(p), '错误': err})
                done += 1
                if on_progress:
                    try:
                        on_progress(done, len(paths), p)
                    except Exception:
                        pass
    else:
        for i, p in enumerate(paths):
            p2, txt, err = one(p)
            if err is None:
                out[p] = txt
            elif err == '__STOPPED__':
                stopped += 1
            else:
                fail.append({'文件': os.path.basename(p), '错误': err})
            if on_progress:
                try:
                    on_progress(i + 1, len(paths), p2)
                except Exception:
                    pass
    elapsed = _t.time() - t0
    hold = max(0.0, _hold_wall_snapshot() - hold0)
    # 让路秒：本批次内有工作线程处于「停手等待」的实际墙钟时间（不是各线程相加，
    # 也不是进程累计——否则第二次预热会继承上一次的让路秒，把有效秒算成负数）。
    return {'结果': out,
            '让路秒': round(hold, 1),
            '有效秒': round(max(0.0, elapsed - hold), 1),
            '让路次数': max(0, _STAT.get('让路', 0) - holds0),
            '失败': fail,
            '已停止': stopped > 0,
            '停止页数': stopped,
            '耗时秒': round(elapsed, 2)}



# ============ 每页图片收集（全系统唯一入口） ============
IMG_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff')
_PAGE_RE = re.compile(r'^p(\d+)_i(\d+)', re.I)
_PAGE_RE2 = re.compile(r'^p(\d+)_', re.I)


def collect_page_images(pages_dir):
    """收集一个 .pages 目录下的全部图片，按页、按图序排列。

    返回 dict:
      {
        '页': {页码: [图片路径, ...]},   # 每页的全部图片，已按 i 序号排序
        '全部': [图片路径, ...],          # 扁平列表，页内多图按 i 序
        '页数': int, '图片数': int,
        '多图页': {页码: 图片数},         # 一页多图的页（必须全部读取）
        '扩展名': {'.jpg': n, '.png': n},
        '异常': [...],                    # 无法解析文件名的图片
      }

    规则（硬性）：
      1. 接受所有常见图片扩展名，不得只匹配 .jpg
      2. 同一页的 i0/i1/i2… 必须全部返回，禁止只取第一张
    """
    per = {}
    allp = []
    ext_cnt = {}
    bad = []
    if not pages_dir or not os.path.isdir(pages_dir):
        return {'页': {}, '全部': [], '页数': 0, '图片数': 0,
                '多图页': {}, '扩展名': {}, '异常': []}

    names = sorted(os.listdir(pages_dir))
    tmp = []
    for fn in names:
        low = fn.lower()
        if not low.endswith(IMG_EXTS):
            continue
        if low.endswith('.json'):
            continue
        path = os.path.join(pages_dir, fn)
        ext = os.path.splitext(low)[1]
        m = _PAGE_RE.match(fn)
        if m:
            pg, idx = int(m.group(1)), int(m.group(2))
        else:
            m2 = _PAGE_RE2.match(fn)
            if not m2:
                bad.append(fn)
                continue
            pg, idx = int(m2.group(1)), 0
        tmp.append((pg, idx, fn, path, ext))

    tmp.sort(key=lambda x: (x[0], x[1], x[2]))
    for pg, idx, fn, path, ext in tmp:
        per.setdefault(pg, []).append(path)
        allp.append(path)
        ext_cnt[ext] = ext_cnt.get(ext, 0) + 1

    multi = {p: len(v) for p, v in per.items() if len(v) > 1}
    return {'页': per, '全部': allp, '页数': len(per), '图片数': len(allp),
            '多图页': multi, '扩展名': ext_cnt, '异常': bad}


def audit_images(pages_dir):
    """完整性审计：返回可直接展示给用户的检查结论。"""
    info = collect_page_images(pages_dir)
    warns = []
    if info['图片数'] > info['页数']:
        warns.append('存在 %d 个「一页多图」页（共多出 %d 张图），'
                     '复核时必须逐张读取，不得只读 i0'
                     % (len(info['多图页']), info['图片数'] - info['页数']))
    if info['异常']:
        warns.append('%d 个图片文件名无法解析页号：%s'
                     % (len(info['异常']), '、'.join(info['异常'][:5])))
    exts = info['扩展名']
    if len(exts) > 1:
        warns.append('图片扩展名不止一种（%s），'
                     '按扩展名过滤会漏读'
                     % '、'.join('%s×%d' % (k, v) for k, v in sorted(exts.items())))
    return {'页数': info['页数'], '图片数': info['图片数'],
            '多图页': info['多图页'], '扩展名': exts,
            '异常': info['异常'], '警告': warns,
            '结论': '正常' if not warns else '需注意'}



if __name__ == '__main__':
    target = sys.argv[1]
    t = time.time()
    txt = ocr_image(target)
    print('首次 %.1fs，%d 字' % (time.time() - t, len(txt)))
    t = time.time()
    txt2 = ocr_image(target)
    print('再次 %.3fs（缓存），内容一致: %s' % (time.time() - t, txt == txt2))
    print('统计:', runtime_stats())
    print(txt[:400])
