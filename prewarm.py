# -*- coding: utf-8 -*-
"""prewarm.py —— OCR 预热层：挑关键页 + 后台批量识别 + 进度状态。

从 server.py 拆出（P2-1 第四步）：HEAVY_LOCK / pick_key_pages / _prewarm_worker /
prewarm_summary / wait_prewarm / start_prewarm / _prune_prewarm_state，
以及锚点关键词与预热状态常量。
"""
import datetime
import json
import os
import threading
import time
import traceback

import ocr_text
from storage import OCR_STATS, now

# 全进程 OCR 单飞锁：预热与 read_bid 两路 OCR 共用，同一时刻只识别一页。
# 动机：8 逻辑核上两路各自开满 onnxruntime 线程会互相抢核，总吞吐反而下降；
# 同一页还可能被两路各算一遍（缓存只在算完之后才生效）。
HEAVY_LOCK = threading.Lock()

# 这些关键词出现的页，其后若干页通常是扫描件形式的证据材料
_ANCHOR_KWS = ('业绩一览表', '类似业绩', '业绩表', '获奖', '奖项',
               '工法', '科学技术', '科技奖', '研发能力',
               '科技创新', '第三方评价', '资信',
               '人员信息表', '主要人员', '项目管理机构', '简历表',
               '职称', '注册', '社保', '证书')

_EVIDENCE_TAIL = 30          # 仅在限制了上限时用于优先选取锚点附近的页
_PREWARM_DEFAULT_CAP = 120   # 0 = 全部缺文字层的页；>0 = 按锚点区间轮流取样


def pick_key_pages(bid_txt, pdir, extra_pages=(), cap=_PREWARM_DEFAULT_CAP):
    """
    挑选"需要 OCR 的扫描页"：
      1) 正文里出现锚点关键词的页，其后的 _EVIDENCE_TAIL 页（证据多为扫描件）
      2) 调用方显式给出的页（如复核结果里引用过的页码）
    仅保留确实存在图片的页。返回按页号升序的图片路径列表。
    """
    if not pdir or not os.path.isdir(pdir):
        return []
    # 页号 → 图片路径（统一入口：包含每页的全部图片，不限于 i0）
    _info = ocr_text.collect_page_images(pdir)
    idx = _info['页']
    if not idx:
        return []

    lines = []
    if bid_txt and os.path.isfile(bid_txt):
        try:
            with open(bid_txt, encoding='utf-8') as f:
                lines = f.read().split('\n')
        except Exception:
            lines = []

    def has_text(page_no):
        """该页已有足够文字层（>40 字）→ 直接读文本即可，无需 OCR"""
        if 1 <= page_no <= len(lines):
            return len((lines[page_no - 1] or '').strip()) > 40
        return False

    # 默认（cap<=0）：预热全部缺文字层的扫描页
    if not cap or cap <= 0:
        out = []
        for p in sorted(idx):
            if has_text(p):
                continue
            out.extend(idx[p])
        return out

    # 指定了上限：按「锚点区间轮流取样」，避免被文档前半部分占满
    ranges = []
    for p in (extra_pages or ()):
        try:
            q = int(p)
        except Exception:
            continue
        ranges.append((q, q))
    for i, ln in enumerate(lines, 1):
        if any(k in ln for k in _ANCHOR_KWS):
            ranges.append((i + 1, i + _EVIDENCE_TAIL))
    if not ranges:
        ranges = [(1, len(lines) or 1)]
    ranges.sort()
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    # 按区间平均分配配额，避免靠前的区段把上限吃满
    quota = max(2, int(cap / max(1, len(merged))))   # 保证总配额不超过 cap
    picked = []
    seen = set()
    for a, b in merged:
        n = 0
        for p in range(a, b + 1):
            if n >= quota:
                break
            if p in idx and p not in seen and not has_text(p):
                seen.add(p)
                picked.append(p)
                n += 1
    # 还有余量则按原顺序补齐
    if len(picked) < cap:
        for a, b in merged:
            for p in range(a, b + 1):
                if len(picked) >= cap:
                    break
                if p in idx and p not in seen and not has_text(p):
                    seen.add(p)
                    picked.append(p)
    out = []
    for p in sorted(picked[:cap]):
        out.extend(idx[p])
    return out


_PREWARM_STATE = {}


def _prewarm_worker(task_id, unit, bid_txt, pdir, pages):
    """后台线程：批量 OCR 关键页，写入缓存

    让路策略（2026-10-30）：默认 2 线程；只要队列里有「处理中」的复核任务，
    或前台刚识别过（心跳），就整段停手，等复核结束再继续。
    可用 OCR_PREWARM_WORKERS 调整并发。
    """
    t0 = time.time()
    # 并发实测（2026-10-30，8 张重扫描页）：
    #   ONNX Runtime：1→8.5、2→15.8、3→16.0、4→14.9、6→15.1 页/分
    #   OpenVINO    ：1→20.2、2→38.1、3→60.8、4→60.9 页/分
    # 两种后端都是 3 最优，再多只与推理库内部线程互相争抢。故默认 3。
    # 2026-11-02：默认 2（原 3）。每线程各自的 InferRequest 会追加提交内存，
    # 3 线程在 8 GB 机器上叠加其它 python 进程容易触顶，触发
    # 「python.exe 无法启动 0xc0000142」。要更快可显式 set OCR_PREWARM_WORKERS=3。
    workers = int(os.environ.get('OCR_PREWARM_WORKERS') or 2)

    def _prog(done, total, path):
        st = _PREWARM_STATE.get(task_id) or {}
        st.update({'已完成': done, '总数': total,
                   '百分比': int(100.0 * done / max(1, total))})
        _PREWARM_STATE[task_id] = st

    _PREWARM_STATE[task_id] = {'投标单位': unit, '状态': '运行中',
                               '页数': len(pages), '总数': len(pages),
                               '已完成': 0, '百分比': 0,
                               '并发': workers, '开始': now()}
    try:
        res = ocr_text.ocr_many(pages, workers=workers, lock=HEAVY_LOCK,
                                yield_to_foreground=True, on_progress=_prog)
        el = time.time() - t0
        st = dict(_PREWARM_STATE.get(task_id) or {})
        # 有效耗时 = 墙钟 − 让路墙钟；两者都由 ocr_many 按"墙钟只计一次"算好，
        # 不会出现负值。全部时间都在让路时有效耗时可能为 0，用 0.1 兜底避免除零。
        _eff = float(res.get('有效秒') if res.get('有效秒') is not None else el)
        if _eff <= 0.05:
            _eff = 0.1
        st.update({
            '状态': '完成', '成功': len(res.get('结果') or {}),
            '让路秒': res.get('让路秒'),
            '有效耗时秒': round(_eff, 1),
            '页每分钟': round(60.0 * len(pages) / _eff, 1),
            '失败': len(res.get('失败') or []),
            '耗时秒': round(el, 1),
            '已完成': st.get('已完成') or len(pages),
            '百分比': 100,
            '让路次数': res.get('让路次数') or 0,
            '完成时间': now()})
        _PREWARM_STATE[task_id] = st
        # 吞吐落盘：/api/engine 重启后也能显示最近一次真实吞吐。
        # 只记录"真的在算"的批次——整批命中缓存时耗时会接近 0，页/分会虚高到几千，
        # 那种数字写进去只会误导人。
        if _eff >= 1.0 and len(pages) >= 3:
            try:
                with open(OCR_STATS, 'w', encoding='utf-8') as fh:
                    json.dump({'投标单位': unit, '页数': len(pages),
                               '耗时秒': round(el, 1),
                               '有效秒': round(_eff, 1),
                               '页每分钟': st.get('页每分钟'),
                               '引擎': getattr(ocr_text, '_ENGINE_NAME', '?'),
                               '并发': workers,
                               '时间': st.get('完成时间')},
                              fh, ensure_ascii=False, indent=1)
            except Exception:
                pass
        print('[预热] %s %s：%d 页，%.1f 秒（让路 %.1f 秒，有效 %.1f 秒，%.1f 页/分）'
              % (task_id, unit, len(pages), el,
                 float(res.get('让路秒') or 0), _eff,
                 st.get('页每分钟') or 0))
    except Exception as e:
        traceback.print_exc()
        _PREWARM_STATE[task_id] = {'投标单位': unit, '状态': '异常',
                                   '错误': repr(e), '页数': len(pages)}


def prewarm_summary(tids):
    """把本批次各单位的预热状态写成一句话，塞进唤醒消息"""
    rows = []
    for t in tids:
        st = _PREWARM_STATE.get(t) or {}
        if not st:
            rows.append('%s 未启动预热' % t)
            continue
        unit = st.get('投标单位') or t
        state = st.get('状态')
        if state == '运行中':
            rows.append('%s 预热中 %s/%s 页（%s%%，并发 %s）'
                        % (unit, st.get('已完成') or 0, st.get('总数') or st.get('页数') or 0,
                           st.get('百分比') or 0, st.get('并发') or '?'))
        elif state == '完成':
            rows.append('%s 预热完成 %s 页，%.0f 秒'
                        % (unit, st.get('成功') or st.get('页数') or 0,
                           float(st.get('耗时秒') or 0)))
        else:
            rows.append('%s 预热%s' % (unit, state or '未知'))
    if not rows:
        return '本批次没有后台预热。'
    return ('；'.join(rows)
            + '。复核期间预热会自动停手（不再抢 CPU），不必等它；'
              '它会在你结单后自行补完。')


def wait_prewarm(tids, timeout):
    """按需等待本批次预热跑完，返回实际等待秒数"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        running = any((_PREWARM_STATE.get(t) or {}).get('状态') == '运行中'
                      for t in tids)
        if not running:
            break
        time.sleep(2)
    return round(time.time() - t0, 1)


def start_prewarm(task_id, unit, bid_txt, pdir, extra_pages=(), cap=None):
    """启动后台预热（非阻塞），返回选中的页数"""
    pages = pick_key_pages(bid_txt, pdir, extra_pages=extra_pages,
                           cap=(_PREWARM_DEFAULT_CAP if cap is None else cap))
    if not pages:
        return 0
    threading.Thread(target=_prewarm_worker,
                     args=(task_id, unit, bid_txt, pdir, pages),
                     daemon=True).start()
    return len(pages)


def _prune_prewarm_state(max_minutes=60):
    """L3：清理已结束且超过 N 分钟的预热状态，防 _PREWARM_STATE 只增不减。"""
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=max_minutes)
        dead = []
        for k, v in list(_PREWARM_STATE.items()):
            if v.get('状态') == '运行中':
                continue
            t = v.get('开始') or ''
            try:
                st = datetime.datetime.strptime(t, '%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
            if st < cutoff:
                dead.append(k)
        for k in dead:
            _PREWARM_STATE.pop(k, None)
    except Exception:
        pass
