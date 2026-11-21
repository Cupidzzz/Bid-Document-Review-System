#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模块1：评分标准录入 —— 本地服务（纯标准库，数据落盘 work/rules.json）"""
import datetime
import glob
import hashlib
import http.server
import json
import os
import re
import socketserver
import sys
import shutil
import threading
import time
import traceback
import urllib.parse

from validation import (as_text, check_pending_reason,
                        check_result_shape, total_consistency_errors)
from storage import (atomic_write_json, backup_once, front_log_add,
                     issue_add, issue_list, rescue_log_add,
                     FRONT_LOG, ISSUE_LOG, RESCUE_LOG, WORK, OCR_STATS)
from prewarm import (HEAVY_LOCK, pick_key_pages, start_prewarm,
                     prewarm_summary, wait_prewarm, _prune_prewarm_state,
                     _PREWARM_STATE)

TASKS = {}
TLOCK = threading.Lock()


def task_new():
    tid = datetime.datetime.now().strftime('T%H%M%S%f')
    with TLOCK:
        TASKS[tid] = {'id': tid, '进度': 0, '阶段': '排队中', '日志': [],
                      '完成': False, '失败': None, '结果': None}
    return tid


def task_set(tid, **kw):
    with TLOCK:
        t = TASKS.get(tid)
        if t:
            t.update(kw)


def task_log(tid, msg):
    with TLOCK:
        t = TASKS.get(tid)
        if t:
            t['日志'].append(msg)

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, 'ui')
RULES = os.path.join(WORK, 'rules.json')
# 预计耗时：只识别「必需图片」（约导出图片的 60%），实测约 3.5 秒/张
# 故 预计分钟 = 图片数 × 0.6 × 3.5 / 60 ≈ 图片数 × 0.035
OCR_PER_IMG = 2.1


QUEUE = os.path.join(WORK, 'queue')
# 进程启动时刻：/api/version 用它算"已运行多久"，也是判断"跑的是不是新代码"的依据
_BOOT_TS = time.time()

# ===== 提交批次进度（供前端进度条使用） =====
SUBMIT_STATE = {}
SUBMIT_LOCK = threading.Lock()
# M1：提交闸门临界区锁——包住「闸门检查 + inflight 占位」，
# 防止两次并发 POST 同时通过 submit_gate（TOCTOU）。
SUBMIT_GATE_LOCK = threading.Lock()


def submit_new(project, units):
    bid = 'S' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    st = {'批次': bid, '项目': project, '开始时间戳': time.time(),
          '开始': now(), '总单位': len(units), '已完成单位': 0,
          '当前单位': '', '阶段': '准备中', '进度': 0,
          '已用秒': 0.0, '预计剩余秒': None, '完成': False, '失败': None,
          '页进度': {'已处理': 0, '总页数': 0}, '页速率': None,
          '页开始时间': None,
          '明细': {u: {'阶段': '排队中', '进度': 0, '说明': ''} for u in units},
          '已提交': [], '跳过': [], '已唤醒': False}
    with SUBMIT_LOCK:
        SUBMIT_STATE[bid] = st
    return bid


def submit_set(bid, **kw):
    with SUBMIT_LOCK:
        st = SUBMIT_STATE.get(bid)
        if not st:
            return
        st.update(kw)


def submit_unit(bid, unit, **kw):
    with SUBMIT_LOCK:
        st = SUBMIT_STATE.get(bid)
        if not st:
            return
        st.setdefault('明细', {}).setdefault(unit, {})
        st['明细'][unit].update(kw)


def submit_snapshot(bid=None):
    """返回批次快照（不含不可序列化对象），并实时计算已用/预计剩余。"""
    with SUBMIT_LOCK:
        if bid:
            st = SUBMIT_STATE.get(bid)
            items = [st] if st else []
        else:
            items = sorted(SUBMIT_STATE.values(), key=lambda x: x['开始时间戳'])[-5:]
        out = []
        for st in items:
            d = dict(st)
            el = time.time() - d['开始时间戳']
            pct = max(0.0, min(100.0, float(d.get('进度') or 0)))
            d['已用秒'] = round(el, 1)
            pg = d.get('页进度') or {}
            done, tot = pg.get('已处理') or 0, pg.get('总页数') or 0
            st0 = d.get('页开始时间')
            if d.get('完成'):
                d['预计剩余秒'] = 0
            elif st0 and tot > 0 and 0 < done < tot:
                stage_el = max(0.001, time.time() - st0)
                rate = done / stage_el
                d['预计剩余秒'] = round((tot - done) / rate, 1)
                d['页速率'] = round(rate, 2)
            elif pct >= 3:
                rate = el / pct
                d['预计剩余秒'] = round(max(0.0, rate * (100 - pct)), 1)
            else:
                d['预计剩余秒'] = None
            out.append(d)
        return out[0] if bid and out else out


            # agent 任务队列
PROJ = os.path.join(HERE, '项目')              # 程序目录/项目/{项目名称}/项目信息.json
TRASH = os.path.join(HERE, '_项目回收站')        # 删除的项目移到这里，可手工移回
BIDS = os.path.join(HERE, '投标文件')          # 程序目录下，按 {项目}/{投标人} 分目录

# 专家打分导入任务进度表：批次 -> {进度, 阶段, 完成, 结果, 错误}
EXPERT_JOBS = {}
TENDERS = os.path.join(HERE, '招标文件')      # 程序目录下，按 {项目名称} 分目录


def walk_files(root, suffix):
    """递归找出指定后缀的文件（用于 {项目}/{投标人} 两级目录）"""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(suffix):
                out.append(os.path.join(dirpath, fn))
    return out


def pages_of_body(body):
    if body is None:
        return []
    return body.split('\n') if isinstance(body, str) else []


def guess_bidder_name(text, filename=''):
    """从投标文件正文里取投标人名称"""
    t = text or ''
    for p in (r'投标人(?:名称)?[：:]\s*([^\n，。；：（）()]{4,40}?(?:公司|集团|局))',
              r'投标人[（(]全称[)）][：:]\s*([^\n，。；：]{4,40}?(?:公司|集团|局))',
              r'([\u4e00-\u9fffA-Za-z0-9（）()·]{4,40}?(?:有限公司|股份有限公司|集团有限公司))'):
        for m in re.finditer(p, t[:20000]):
            nm = safe_name(m.group(1), '')
            if nm and '投标人' != nm[:3]:
                return nm
    stem = os.path.splitext(os.path.basename(filename))[0]
    return safe_name(stem, '未识别投标人')


def safe_name(s, default='未命名'):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', '', (s or '')).strip()
    s = re.sub(r'\s+', ' ', s)
    # P1-2：拦路径边界（. 和 .. 会指向项目根/上级目录），
    # 并去掉 Windows 会静默忽略的结尾点/空格，避免目录名歧义。
    if s in ('.', '..'):
        return default
    s = s.rstrip('. ')
    if not s:
        return default
    return s[:80]


def guess_project_name(text, filename=''):
    """
    从招标文件正文里取项目名称，用于建目录。
    要点：必须命中"含工程/项目"的实值，排除"投标人名称："这类相邻标签。
    """
    t = text or ''
    pats = (r'项目名称[：:]\s*([^\n，。；：]{6,80}?(?:工程|项目))',
            r'工程名称[：:]\s*([^\n，。；：]{6,80}?(?:工程|项目))',
            r'本招标项目[^\n]{0,12}?([^\n，。；：]{6,60}?工程)',
            r'([\u4e00-\u9fffA-Za-z0-9（）()·\-]{6,60}?(?:装修|装饰|幕墙)[^\n]{0,20}?工程)')
    for p in pats:
        for m in re.finditer(p, t):
            nm = safe_name(m.group(1), '')
            if nm and '名称' not in nm and len(nm) >= 6:
                return nm
    # 退回文件名：取"—"或"《》"之后、扩展名之前的片段
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r'^.*?[—\-–]{1,2}', '', stem)
    stem = re.sub(r'^附件\d*[：:]*', '', stem)
    return safe_name(stem, '未命名项目')


def project_dir_for(name, create=False):
    """按项目名称取目录（不依赖"最近保存"，避免多项目时写错位置）"""
    nm = safe_name(name or '', '')
    if not nm:
        return None
    d = os.path.join(PROJ, nm)
    if create:
        try:
            os.makedirs(d, exist_ok=True)
            for s in ('投标文件', '招标文件', '评委打分', '复核报告'):
                os.makedirs(os.path.join(d, s), exist_ok=True)
        except Exception:
            traceback.print_exc()
    return d


def queue_list():
    out = []
    if not os.path.isdir(QUEUE):
        return out
    for fn in os.listdir(QUEUE):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(QUEUE, fn), encoding='utf-8') as f:
                out.append(json.load(f))
        except Exception as e:
            # P2-2：坏队列文件不再静默跳过，告警
            print('[告警] 队列文件损坏，已跳过：%s（%r）' % (fn, e))
            try:
                front_log_add('error', '队列文件损坏：%s' % fn)
            except Exception:
                pass
    out.sort(key=lambda x: x.get('提交时间') or '', reverse=True)
    # 状态标注（只标注，不自动改状态）
    for _t in out:
        if not isinstance(_t, dict):
            continue
        _st = _t.get('状态') or ''
        _t['唤醒次数'] = int(_t.get('唤醒次数') or 0)
        _t['可重发唤醒'] = False
        _t['唤醒未响应'] = False
        _t['疑似僵死'] = False
        _t['无进展'] = False
        if _st == '待处理':
            # 唤醒后多久没人认领
            _beg = _t.get('唤醒时间') or _t.get('提交时间') or ''
            _mins = minutes_since(_beg)
            if _mins is not None:
                _t['已等待分钟'] = _mins
                _t['唤醒未响应'] = _mins >= WAKE_GRACE_MIN
                _t['可重发唤醒'] = _t['唤醒次数'] < WAKE_MAX_TRIES
        elif _st == '处理中':
            # 优先用 agent 心跳（最后活动），没有才退回开始时间
            _beg = _t.get('最后活动') or _t.get('开始时间') or _t.get('更新时间') or ''
            _mins = minutes_since(_beg)
            if _mins is not None:
                _t['无进展分钟'] = _mins
                _t['已处理分钟'] = _mins
                _t['无进展'] = _mins >= max(15, WAKE_GRACE_MIN)
                _t['疑似僵死'] = _mins >= STALE_MIN
    return out


def rescue_once(do_wake=True):
    """兜底巡检一轮（看门狗主体，也可手动调用测试）。

    规则：
      · 待处理 且 已等待 ≥ WAKE_GRACE_MIN 且 唤醒次数 < WAKE_MAX_TRIES
        → 自动重发一次唤醒（每次巡检最多处理 2 个任务，避免刷屏）；
      · 处理中 且 无进展 ≥ STALE_MIN
        → 只记日志并标「疑似僵死」，**绝不自动重置**（自动重置会造成重复复核）。
    返回本轮的处置清单。
    """
    acted = []
    try:
        tasks = queue_list()
    except Exception:
        traceback.print_exc()
        return acted
    for t in tasks:
        tid = t.get('id')
        st = t.get('状态')
        if st == '待处理' and t.get('唤醒未响应') and t.get('可重发唤醒'):
            if len([a for a in acted if a['动作'] == '重发唤醒']) >= 2:
                continue
            tries = int(t.get('唤醒次数') or 0)
            msg = ('任务已等待 %s 分钟仍未认领（已唤醒 %d 次）'
                   % (t.get('已等待分钟'), tries))
            woke, winfo = False, ''
            if do_wake:
                try:
                    import wake as wake_mod
                    woke, winfo = wake_mod.wake(
                        '【自动唤醒·兜底重发】项目「%s」的任务 %s（%s）已等待 %s 分钟'
                        '仍无人接手（第 %d 次唤醒）。'
                        '【第一步必做】调接口 POST /api/run '
                        '{"任务":"stopcheck","参数":{"id":"%s"}}，返回 "已停止": true 就停手。'
                        '请立刻认领（claim）并复核，完成后写入 项目\\%s\\复核报告\\。'
                        % (t.get('项目') or '', tid, t.get('投标人') or '',
                           t.get('已等待分钟'), tries + 1, tid, t.get('项目') or ''),
                        project=t.get('项目') or '')
                except Exception as e:
                    winfo = '唤醒异常：%r' % e
            task_patch(tid, 唤醒次数=tries + 1,
                       **({'唤醒时间': now()} if woke else {}))
            rescue_log_add('重发唤醒', tid, '%s；结果：%s'
                           % (msg, '已唤醒' if woke else ('失败 ' + str(winfo)[:80])))
            acted.append({'动作': '重发唤醒', '任务': tid, '已唤醒': woke,
                          '说明': msg})
        elif st == '处理中' and t.get('疑似僵死'):
            key = '%s|疑似僵死' % tid
            tried = False
            try:
                if os.path.isfile(RESCUE_LOG):
                    with open(RESCUE_LOG, encoding='utf-8') as f:
                        _start = t.get('开始时间') or ''
                        tried = any((x.get('任务') == tid
                                     and x.get('类型') == '疑似僵死'
                                     and (x.get('起始') or '') == _start)
                                    for x in (json.load(f) or []))
            except Exception:
                pass
            if tried:
                continue
            rescue_log_add('疑似僵死', tid,
                           '处理中已 %s 分钟无进展（阈值 %s 分钟），需人工确认：'
                           '任务行点「重置」，或让 agent 继续。'
                           % (t.get('无进展分钟'), STALE_MIN),
                           起始=t.get('开始时间') or '')
            acted.append({'动作': '记录僵死', '任务': tid,
                          '说明': '无进展 %s 分钟' % t.get('无进展分钟')})
    return acted


_RESCUE_STARTED = [False]


def rescue_loop():
    """后台看门狗：每 RESCUE_TICK 秒巡检一次。"""
    while True:
        try:
            time.sleep(RESCUE_TICK)
            if AUTO_RESCUE:
                rescue_once(do_wake=True)
        except Exception:
            traceback.print_exc()
            time.sleep(30)


def start_rescue():
    if _RESCUE_STARTED[0] or not AUTO_RESCUE:
        return False
    _RESCUE_STARTED[0] = True
    threading.Thread(target=rescue_loop, daemon=True,
                     name='rescue-watchdog').start()
    return True


def project_bid_files(pname):
    """项目投标文件目录下的原始文件（含各单位子目录，排除 .txt/.json 等派生物）"""
    d = project_dir_for(pname)
    bdir = os.path.join(d, '投标文件') if d else None
    files = []
    if bdir and os.path.isdir(bdir):
        for dirpath, _dirs, names in os.walk(bdir):
            for fn in sorted(names):
                if fn.endswith(('.txt', '.json')):
                    continue
                fp = os.path.join(dirpath, fn)
                if os.path.isfile(fp):
                    files.append(fp)
    return files


def pick_bid_files(up):
    """某投标单位目录下的原始文件（按文件名排序，排除 .txt/.json 派生物）。返回路径列表。"""
    if not up or not os.path.isdir(up):
        return []
    return [os.path.join(up, x) for x in sorted(os.listdir(up))
            if not x.endswith(('.txt', '.json'))
            and os.path.isfile(os.path.join(up, x))]


def bid_file_info(fp):
    """一个投标文件的详情（供 OCR 勾选面板）。册名 = 文件名去扩展名。"""
    base = os.path.basename(fp)
    rec = {'文件': base, '册名': os.path.splitext(base)[0],
           '大小MB': round(os.path.getsize(fp) / 1048576.0, 1)
                     if os.path.isfile(fp) else 0,
           '页数': 0, '无文字页': 0, '已OCR页': 0, '预计分钟': 0.0,
           '已抽取': os.path.isfile(fp + '.txt'),
           '已导页': os.path.isdir(fp + '.pages')}
    try:
        if fp.lower().endswith('.pdf'):
            import pdf_images as _pi
            ps = _pi.page_summary(fp)
            rec['页数'] = len(ps)
            rec['无文字页'] = sum(1 for p in ps
                                  if (not p.get('有文字')) or p.get('乱码'))
            rec['预计分钟'] = round(rec['无文字页'] * OCR_PER_IMG / 60.0, 1)
            cache = os.path.join(fp + '.pages', '.ocr_cache')
            if os.path.isdir(cache):
                rec['已OCR页'] = len([x for x in os.listdir(cache)
                                      if x.endswith('.json')])
    except Exception:
        pass
    return rec


def current_project_dir():
    """返回最近保存的项目目录（以 项目信息.json 的修改时间为准）"""
    if not os.path.isdir(PROJ):
        return None
    best, best_t = None, -1
    for d in os.listdir(PROJ):
        fp = os.path.join(PROJ, d, '项目信息.json')
        if not os.path.isfile(fp):
            continue
        t = os.path.getmtime(fp)
        if t > best_t:
            best_t, best = t, os.path.join(PROJ, d)
    return best


def project_file(name):
    """项目内文件路径；无项目时返回 None"""
    d = current_project_dir()
    return os.path.join(d, name) if d else None


def load_project_rules(pname):
    """
    读项目级评分标准。**不回退到全局**：项目没有就返回 (None, None)，
    由调用方报错，避免把上一个项目的规则套到本项目。
    """
    d = project_dir_for(pname)
    if not d:
        return None, None
    fp = os.path.join(d, '评分标准.json')
    if not os.path.isfile(fp):
        return None, fp
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f), fp
    except Exception:
        traceback.print_exc()
        return None, fp


def project_bid_txts(pname):
    """项目目录下的投标正文（.pdf.txt / .docx.txt）"""
    d = project_dir_for(pname)
    if not d:
        return []
    return walk_files(os.path.join(d, '投标文件'), '.txt')


def latest_project():
    """返回最近创建的招标文件项目目录名"""
    if not os.path.isdir(TENDERS):
        return None
    subs = [d for d in os.listdir(TENDERS)
            if os.path.isdir(os.path.join(TENDERS, d))]
    if not subs:
        return None
    subs.sort(key=lambda d: os.path.getmtime(os.path.join(TENDERS, d)),
              reverse=True)
    return subs[0]
os.makedirs(UI, exist_ok=True)
os.makedirs(WORK, exist_ok=True)

EMPTY = {'项目': {'名称': '', '编号': ''}, '分组': [], '更新时间': ''}
MIME = {'.html': 'text/html; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8'}


# ============ 评分标准原文结构化拆分 ============
# 评分标准文本高度公式化，按句切分后按关键词归类即可，不依赖模型
_SENT_SPLIT = re.compile(r'[。；;\n]')

_PROV_PAT = re.compile(
    r'(须提供|须同时提供|应提供|需提供|须提交|应提交|须附|应附|须上传'
    r'|提供[^。；]{0,40}(?:扫描件|截图|复印件|证明|证书))')

_NOTE_PAT = re.compile(
    r'(否则|不予|不得分|不得重复|只计|按最高|为准|不包含|不一致'
    r'|同一项目|同一奖项|同一科技成果|不予评审|不予认定|不予认可|备注)')

_RULE_PAT = re.compile(
    r'(每(?:项|个|次|提供)?[^。；]{0,12}得\s*\d|最多得|最高得|本项最高'
    r'|本小项最高|得\s*\d+(?:\.\d+)?\s*分)')


def split_requirements(text):
    """把一段评分标准拆成 必须提供 / 本项注意 / 计分规则 三类句子"""
    flat = re.sub(r'\s+', '', text or '')
    prov, note, rule = [], [], []
    for seg in _SENT_SPLIT.split(flat):
        s = seg.strip()
        if len(s) < 4:
            continue
        # 互斥归类：一句话只进一个桶，避免同一段文字重复出现在三处
        if _PROV_PAT.search(s):
            prov.append(s)
        elif _NOTE_PAT.search(s):
            note.append(s)
        elif _RULE_PAT.search(s):
            rule.append(s)

    def dedup(xs, limit=6):
        out = []
        for x in xs:
            if x not in out:
                out.append(x)
        return out[:limit]

    return {'必须提供': dedup(prov), '本项注意': dedup(note), '计分规则': dedup(rule)}


def rule_summary(cond):
    """把 mine() 抽出的条件转成中文一句话"""
    if not cond:
        return ''
    bits = []
    if cond.get('时间窗口起'):
        bits.append('%s 起' % cond['时间窗口起'])
    if cond.get('金额门槛万元'):
        bits.append('金额 ≥%s 万元' % cond['金额门槛万元'])
    lv = cond.get('分级档位')
    if lv:
        bits.append('分级计分：' + '、'.join(
            '%s %s 分（封顶 %s）' % (x.get('级别'), x.get('单项分'), x.get('封顶'))
            for x in lv))
    if cond.get('单项分') is not None:
        bits.append('单项 %s 分' % cond['单项分'])
    if cond.get('封顶') is not None:
        bits.append('上限 %s 分' % cond['封顶'])
    if cond.get('职称要求'):
        bits.append('职称要求：' + '/'.join(cond['职称要求']))
    if cond.get('证书要求'):
        bits.append('证书要求：' + '/'.join(cond['证书要求']))
    kws = [k for k in (cond.get('类型关键词') or []) if k]
    if kws:
        bits.append('关键词：' + '/'.join(kws[:6]))
    return '；'.join(bits)

# ===== 会话体检：判断"唤醒目标会话该不该换" =====
# 判据（阈值只用来提醒，绝不中途自动换——同一项目内换会话会让口径漂移）：
#   上下文 ≥50% 注意 / ≥70% 建议更换
#   累计输出 ≥200 万 token 注意 / ≥400 万 建议更换
#   transcript ≥10 MB 注意 / ≥20 MB 建议更换
def _dsh_sessions_root():
    appdata = os.environ.get('APPDATA') or os.path.expanduser(
        os.path.join('~', 'AppData', 'Roaming'))
    return os.path.join(appdata, 'dsh-desktop', 'harness', 'sessions')


def _transcript_bytes(sid):
    """会话 transcript 体积（zstd 压缩后），找不到返回 None"""
    root = _dsh_sessions_root()
    if not sid or not os.path.isdir(root):
        return None
    hits = glob.glob(os.path.join(root, '*', sid, 'session.v*.jsonl.zstd'))
    if not hits:
        return None
    try:
        return max(os.path.getsize(p) for p in hits)
    except OSError:
        return None


PROJCACHE = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming',
                         'dsh-desktop', 'harness', 'storages',
                         'session_projcache', 'sessions')


def session_model(sid):
    """只读：从 DSH 投影缓存读该会话"上次使用的模型"。

    DSH 解析会话模型的顺序是 pending → 该会话日志里的 requestHeader（即上次用过什么）
    → 默认模型服务；全新会话 lastUsed 为 null，就会用默认的
    dsh-agent-default-model 配置（deepseek-official / deepseek-flash）。
    """
    if not sid:
        return {}
    cands = [os.path.join(PROJCACHE, sid + '.json'),
             os.path.join(PROJCACHE, sid.replace('session-', '') + '.json')]
    fp = next((x for x in cands if os.path.isfile(x)), None)
    out = {}
    if not fp:
        return out
    try:
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
        rows = ((d.get('record') or {}).get('rows') or {})
        ms = (rows.get('modelSelection') or {}).get('val') or {}
        last = ms.get('lastUsed') or {}
        if last:
            out['模型'] = last.get('model')
            out['服务商'] = last.get('provider')
            out['推理强度'] = last.get('reasoningEffort')
            out['模型来源'] = '该会话上次使用的模型'
        else:
            out['模型'] = None
            out['模型来源'] = '该会话还没用过：将使用 DSH 默认模型'
    except Exception:
        return {}
    return out


def session_health(project=None):
    """唤醒目标会话体检：上下文压力、累计 token、transcript 体积，并给出该不该换。

    project 给出时按「项目 → 会话」映射取目标（未映射则回落默认会话），
    这样界面能显示"本项目实际会投给哪个会话"。
    """
    out = {'ok': True}
    try:
        import wake as _wake
    except Exception as e:
        return {'ok': False, '错误': repr(e)[:150]}
    proj = (project or '').strip()
    sid, src, title = _wake.resolve_session(project=proj)
    out.update({'项目': proj or None, '目标会话': sid or None,
                '来源': src, '标题': title or None})
    try:
        cfg = _wake._load_cfg()
        out['默认会话'] = cfg.get('default') or None
        out['项目已映射'] = bool(((cfg.get('projects') or {}).get(proj))) if proj else None
        retired_hit = bool(proj and proj in (cfg.get('retired') or {}))
        # 历史项目：会话映射 /「结项退会话」逻辑上线（11-02）之前就归档的，
        # retired 里没有记录，但磁盘上 项目信息.状态=已归档。
        # 这种项目以前会被判成「未分配」，看着像把会话弄丢了，其实只是已归档。
        archived = False
        if proj and not retired_hit:
            try:
                with open(os.path.join(project_dir_for(proj), '项目信息.json'),
                          encoding='utf-8') as f:
                    archived = ((json.load(f) or {}).get('状态') or '') == '已归档'
            except Exception:
                archived = False
        out['项目已归档'] = archived
        out['项目已结项'] = bool(retired_hit or archived)
        out['结项来源'] = ('wake_target.retired' if retired_hit
                          else ('项目信息.状态=已归档' if archived else ''))
        out['自动建会话'] = '开' if _wake.auto_session_enabled() else '关'
    except Exception:
        pass
    rows = []
    try:
        rows = _wake.list_sessions()
    except Exception as e:
        out['列会话失败'] = repr(e)[:150]
    row = next((r for r in rows if r.get('sessionId') == sid), None)
    if row:
        cp, win = row.get('上下文压力'), row.get('上下文窗口')
        out.update({'上下文压力': cp, '上下文窗口': win,
                    '上下文占比': round(100.0 * cp / win, 1) if cp and win else None,
                    '输出token': row.get('输出token'),
                    '输入token': row.get('输入token'),
                    '可唤醒': row.get('agentAvailable'),
                    '空白': row.get('空白'),
                    '最近活动': row.get('更新时间')})
    n = _transcript_bytes(sid)
    out['transcript字节'] = n
    out['transcriptMB'] = round(n / 1e6, 1) if n else None

    pct = out.get('上下文占比') or 0.0
    otok = out.get('输出token') or 0
    mb = out.get('transcriptMB') or 0.0
    bad, warn = [], []
    if pct >= 70:
        bad.append('上下文 %.1f%%（上限 70%%）' % pct)
    elif pct >= 50:
        warn.append('上下文 %.1f%%（50%% 起提醒）' % pct)
    if otok >= 4000000:
        bad.append('累计输出 %d 万 token（上限 400 万）' % (otok // 10000))
    elif otok >= 2000000:
        warn.append('累计输出 %d 万 token（200 万起提醒）' % (otok // 10000))
    if mb >= 20:
        bad.append('transcript %.1f MB（上限 20MB）' % mb)
    elif mb >= 10:
        warn.append('transcript %.1f MB（10MB 起提醒）' % mb)

    if bad:
        level = '建议更换'
        advice = ('已超阈值：%s。**在项目结项时换会话**（不要中途换，同一项目须同一会话保证口径一致）：'
                  '新建会话并发一句话 → python wake.py --set。' % '、'.join(bad + warn))
    elif warn:
        level = '注意'
        advice = ('接近阈值：%s。本项目结项（归档）后换一个新会话再接下一批任务。'
                  % '、'.join(warn))
    else:
        level = '良好'
        advice = '会话负担正常，可继续接单。'
    try:
        out.update(session_model(sid))
    except Exception:
        pass
    out.update({'状态': level, '提示': advice,
                '阈值': {'上下文占比': 50, '输出token': 2000000, 'transcriptMB': 10}})
    return out


# ===== 提交中登记（G2，2026-11-03）=====
# 预处理（抽正文 + 导页图）要几十秒到几分钟，这期间队列里还没有任务，于是：
#   1) 同一家连点两次 → 两次都能过闸门 → 建出两条任务；
#   2) 关掉页面/重启服务 → 预处理白做，队列里什么都没留下（用户以为提交了）。
# 这里用"内存登记 + 磁盘快照"解决：预处理期间占位、拒绝重复、重启留痕。
_SUBMIT_INFLIGHT = {}
_SUBMIT_INFLIGHT_LOCK = threading.Lock()
SUBMIT_INFLIGHT_FILE = os.path.join(WORK, 'submit_inflight.json')
SUBMIT_LAST_FILE = os.path.join(WORK, 'submit_last.json')
SUBMIT_LOG_FILE = os.path.join(WORK, 'submit_log.json')   # 最近若干次提交结果（U4）
STALE_MIN = int(os.environ.get('QUEUE_STALE_MIN') or 120)   # 处理中超过多久算"疑似僵死"
# 唤醒后多久没人认领算"没送到"（v1.8.3）
WAKE_GRACE_MIN = int(os.environ.get('WAKE_GRACE_MIN') or 8)
# 同一任务最多自动/手动重发几次唤醒，超过就提示换会话或重置
WAKE_MAX_TRIES = int(os.environ.get('WAKE_MAX_TRIES') or 3)
# 看门狗：AUTO_RESCUE=0 关闭；RESCUE_TICK 秒一轮
AUTO_RESCUE = (os.environ.get('AUTO_RESCUE') or '1') != '0'
RESCUE_TICK = int(os.environ.get('RESCUE_TICK') or 120)
API_TOKEN = ''   # 访问口令：main() 启动时生成随机值；前端调 /api/* 需带 X-API-Token
_TASK_LOCK = threading.Lock()


def task_path(tid):
    return os.path.join(QUEUE, (tid or '') + '.json')


def task_patch(tid, **kw):
    """改任务文件的若干字段（只用于心跳/唤醒计数这类小改动）。"""
    fp = task_path(tid)
    if not tid or not os.path.isfile(fp):
        return None
    with _TASK_LOCK:
        try:
            with open(fp, encoding='utf-8') as f:
                t = json.load(f)
        except Exception:
            return None
        t.update(kw)
        if not atomic_write_json(fp, t):
            return None
        return t


def task_touch(tid):
    """心跳：agent 每次调接口都刷新「最后活动」，供僵死判定使用。"""
    return task_patch(tid, 最后活动=now())


def minutes_since(s):
    """'2026-11-04 16:00:00' → 距今分钟数；解析不了返回 None。"""
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
            return int((datetime.datetime.now() - dt).total_seconds() // 60)
        except Exception:
            continue
    return None


def _inflight_dump():
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(SUBMIT_INFLIGHT_FILE, 'w', encoding='utf-8') as f:
            json.dump([dict(v) for v in _SUBMIT_INFLIGHT.values()], f,
                      ensure_ascii=False, indent=1)
    except Exception:
        pass


def submit_inflight_begin(pname, unit, batch):
    with _SUBMIT_INFLIGHT_LOCK:
        _SUBMIT_INFLIGHT[(pname, unit)] = {
            '项目': pname, '投标单位': unit, '批次': batch, '开始时间': now()}
        _inflight_dump()


def submit_inflight_end(pname, unit):
    with _SUBMIT_INFLIGHT_LOCK:
        _SUBMIT_INFLIGHT.pop((pname, unit), None)
        _inflight_dump()


def submit_inflight_rows():
    with _SUBMIT_INFLIGHT_LOCK:
        return [dict(v) for v in _SUBMIT_INFLIGHT.values()]


def submit_inflight_busy(pname, unit):
    with _SUBMIT_INFLIGHT_LOCK:
        return (pname, unit) in _SUBMIT_INFLIGHT


def submit_inflight_load():
    """服务启动时调用：把上个进程遗留的"提交中"记成"未完成"，然后清空快照。"""
    rows = []
    try:
        if os.path.isfile(SUBMIT_INFLIGHT_FILE):
            with open(SUBMIT_INFLIGHT_FILE, encoding='utf-8') as f:
                rows = json.load(f) or []
    except Exception:
        rows = []
    if rows:
        try:
            with open(SUBMIT_LAST_FILE, 'w', encoding='utf-8') as f:
                json.dump({'时间': now(), '未完成': rows}, f,
                          ensure_ascii=False, indent=1)
        except Exception:
            pass
    try:
        os.remove(SUBMIT_INFLIGHT_FILE)
    except Exception:
        pass
    return rows


def submit_log_add(pname, unit, result, note='', tid=''):
    """记录一次提交结果（成功/失败），最多 30 条，供界面刷新后回显。"""
    try:
        rows = []
        if os.path.isfile(SUBMIT_LOG_FILE):
            with open(SUBMIT_LOG_FILE, encoding='utf-8') as f:
                rows = json.load(f) or []
        rows.insert(0, {'项目': pname, '投标单位': unit, '结果': result,
                        '说明': note, '任务': tid, '时间': now()})
        os.makedirs(WORK, exist_ok=True)
        with open(SUBMIT_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(rows[:30], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def submit_log_list(pname=''):
    try:
        with open(SUBMIT_LOG_FILE, encoding='utf-8') as f:
            rows = json.load(f) or []
    except Exception:
        rows = []
    if pname:
        rows = [r for r in rows if r.get('项目') == pname]
    return rows[:10]


def submit_last_failed():
    try:
        with open(SUBMIT_LAST_FILE, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


# 从评分标准文本推断「要读材料里的哪些字段」。
# 只用通用关键词，不绑定具体项目；顺序即展示顺序。
READ_FIELD_HINTS = [
    (r'职称|高级工程师|中级职称|副高|正高|初级职称', '职称等级'),
    (r'专业（|专业:|市政|路桥|给水排水|给排水|风景园林|园林|交通|隧道|'
     r'结构|岩土|勘察|测量|造价|电气|暖通|机电', '专业'),
    (r'年(?:或以上)?的?工作经验|工作年限|从事.{0,8}工作|工作经验', '工作年限（毕业证/社保起算）'),
    (r'一级注册造价工程师|注册造价工程师|注册土木工程师|注册建造师|'
     r'注册安全工程师|注册建筑师|注册结构工程师|注册.{0,6}工程师',
     '执业资格证书与注册单位'),
    (r'毕业证|学历|本科|专科|学位', '学历/毕业时间'),
    (r'社保', '社保缴纳证明'),
    (r'业绩|类似工程|类似项目', '业绩（合同金额/竣工时间/项目类型）'),
    (r'合同金额|合同额|金额.{0,8}万元|不低于.{0,6}万元|≥.{0,6}万元', '合同/业绩金额'),
    (r'竣工|验收|完工|交付', '竣工/验收时间'),
    (r'中标通知书|合同关键页|验收报告|竣工验收|建设单位证明', '业绩证明材料'),
    (r'国家级|省级|市级|协会|奖项|获奖|鲁班奖|詹天佑|优质工程', '获奖级别与发证机构'),
    (r'发明专利|实用新型|软件著作权|工法|标准|论文|科技奖|研发', '研发/知识产权材料'),
    (r'ISO|体系认证|质量体系|环境管理体系', '体系认证证书'),
    (r'注册资本|净资产|营业收入|财务|审计报告', '财务指标'),
    (r'设备|机械|仪器|工器具', '设备清单'),
]


def read_fields(text):
    """返回该评分项需要从材料里读出的字段名列表（去重、保序）。"""
    out = []
    t = text or ''
    for pat, label in READ_FIELD_HINTS:
        if re.search(pat, t) and label not in out:
            out.append(label)
    return out


def _reachable(target, vals, cap=None):
    """target 能否由 vals（可重复取用）相加得到（用于分级档位自检）。"""
    try:
        t = round(float(target), 2)
    except Exception:
        return True
    if t <= 0:
        return True
    if cap is not None:
        try:
            if t > round(float(cap), 2) + 0.01:
                return False
        except Exception:
            pass
    units = sorted(set(round(float(v), 2) for v in vals if v > 0))
    if not units:
        return True
    # 金额都放大 100 倍转成整数，做可达性 BFS（有上限，避免无限增长）
    t100 = int(round(t * 100))
    u100 = [int(round(v * 100)) for v in units]
    if t100 % _gcd_list(u100) != 0:
        return False
    seen = {0}
    frontier = [0]
    limit = t100
    while frontier:
        nxt = []
        for cur in frontier:
            for u in u100:
                s = cur + u
                if s > limit or s in seen:
                    continue
                if s == limit:
                    return True
                seen.add(s)
                nxt.append(s)
        frontier = nxt
    return False


def _gcd_list(nums):
    import math
    g = 0
    for n in nums:
        g = math.gcd(g, n)
    return g or 1


def pair_group(group, items, used):
    """把评分标准的某一组项，与复核结果项**一对一**配对（v1.8.1）。

    必须一对一：不同分组可能有同名项（例如"工程研发能力"在商务与技术各有一条），
    只按名称匹配会把商务那条算进技术组，把折算满分抬高。
    匹配顺序：
      ① 名称完全一致且分值一致；
      ② 名称互为前缀/包含且分值一致（"工程研发能力" ↔ "工程研发能力（施工）"）；
      ③ 名称完全一致（分值不同也认，作兜底）；
      ④ 分值一致且该分值只剩一条候选。
    """
    out = []
    pool = [x for x in (items or []) if id(x) not in used]

    def _fv(x):
        try:
            return float(x.get('分值') or 0)
        except Exception:
            return 0.0

    def _nm(x):
        return (x.get('名称') or '').strip()

    def take(pred):
        for x in pool:
            if id(x) in used:
                continue
            if pred(x):
                used.add(id(x))
                return x
        return None

    for it in (group.get('项') or []):
        nm = (it.get('名称') or '').strip()
        try:
            rv = float(it.get('分值') or 0)
        except Exception:
            rv = 0.0
        hit = take(lambda x: _nm(x) == nm and abs(_fv(x) - rv) <= 0.01)
        if hit is None and nm:
            hit = take(lambda x: abs(_fv(x) - rv) <= 0.01
                       and (_nm(x).startswith(nm) or nm.startswith(_nm(x)))
                       and _nm(x))
        if hit is None and nm:
            hit = take(lambda x: _nm(x) == nm)
        if hit is None and rv:
            same = [x for x in pool if id(x) not in used
                    and abs(_fv(x) - rv) <= 0.01]
            if len(same) == 1:
                hit = same[0]
                used.add(id(hit))
        out.append((it, hit))
    return out


def weighted_detail(rules, items):
    """按评分标准里的权重，把一组复核项折成"客观分"（S4）。

    返回 (明细列表, 折算得分合计, 折算满分合计)；未启用权重折算时返回 (None, None, None)。
    """
    if not (rules and rules.get('权重折算')):
        return None, None, None
    out, tot_s, tot_f = [], 0.0, 0.0
    used = set()
    for g in (rules.get('分组') or []):
        if g.get('参与折算') is False:
            continue
        try:
            wv = float(g.get('权重'))
        except Exception:
            continue
        if wv <= 0:
            continue
        gs = gf = 0.0
        for it, hit in pair_group(g, items, used):
            try:
                fv = float(it.get('分值') or 0)
            except Exception:
                fv = 0.0
            gf += fv
            if hit is None:
                continue
            try:
                sv = float(hit.get('得分') or 0)
            except Exception:
                sv = 0.0
            gs += sv
            hit['折算得分'] = round(sv * wv / 100.0, 2)
            hit['折算权重'] = wv
        out.append({'分组': g.get('名称') or '', '权重': wv,
                    '客观得分': round(gs, 2), '客观满分': round(gf, 2),
                    '折算得分': round(gs * wv / 100.0, 2),
                    '折算满分': round(gf * wv / 100.0, 2)})
        tot_s += gs * wv / 100.0
        tot_f += gf * wv / 100.0
    return out, round(tot_s, 2), round(tot_f, 2)


def auto_pending_reason(unit, it):
    """生成待核原因（三段式，简洁）：**要点 / 出处 / 疑问**。

    要点 = 评分项 + 分值 + 得分 + 依据首句
    出处 = 页码与来源（文字层 / OCR）
    疑问 = 到底要人确认什么（优先级：评分规则原文 > 未决项 > 未得满分 > OCR 需核原图）

    只写这三样：不写投标单位名（记录本身就在该单位文件里）、不抄判分标准全文、
    不加任何"自动生成"标记。
    """
    name = (it.get('名称') or '').strip() or '（未填评分项）'
    src = as_text(it.get('出处')).strip()
    conf = (it.get('置信度') or '').strip()
    full = it.get('分值')
    got = it.get('得分')
    basis = re.sub(r'\s+', ' ', (it.get('依据') or '')).strip()
    basis = basis.replace('**', '').replace('*', '')

    # 要点：一句讲清是什么材料、值得多少分
    _b = re.split(r'[。；]', basis)[0] if basis else ''
    if len(_b) > 70:
        _b = _b[:70] + '…'
    key = '%s（%s 分，得 %s）%s' % (
        name,
        ('' if full is None else full),
        ('—' if got is None else got),
        ('——' + _b) if _b else '')

    # 疑问：从"要人判断什么"出发，而不是复述证据
    doubt = ''
    m = re.search(r'(按[^；。]{0,60}(?:规定|要求|注\s*\d|口径|标准)[^；。]{0,50})',
                  basis)
    if m:
        _r = re.sub(r'^按', '', m.group(1)).strip()
        _parts = re.split(r'[，,]', _r)
        _acc = _parts[0]
        for _p in _parts[1:]:
            if len(_acc) >= 12:
                break
            _acc += '，' + _p
        _acc = _acc[:70] + ('…' if len(_acc) > 70 else '')
        doubt = ('是否满足：%s' % _acc) if '「' in _acc else ('是否满足「%s」' % _acc)
    if not doubt:
        m2 = re.search(r'.{0,24}未决项\s*(\d+).{0,60}', basis)
        if m2:
            _frag = re.sub(r'见未决项\s*\d+\s*[。；]?', '', m2.group(0))
            _frag = _frag.strip('，。；、 ')
            doubt = '见未决项 %s，需认定：%s' % (m2.group(1), _frag[:60])
    if not doubt:
        try:
            _full = float(full) if full is not None else None
            _got = float(got) if got is not None else None
        except Exception:
            _full = _got = None
        if _full is not None and _got is not None and _got < _full:
            doubt = ('该项未得满分（%s/%s），缺哪份材料或卡哪条要求需确认'
                     % (_got, _full))
        elif conf in ('中', '低'):
            doubt = ('该项按满分计，但为%s置信度（%s）；需对照原图核对字迹、'
                     '印章与证书编号是否清晰可辨'
                     % (conf, 'OCR' if 'OCR' in src else '间接推定'))
        elif _full is not None and _got is not None:
            doubt = ('该项按满分计（%s/%s），需确认依据所列材料满足该评分项'
                     '全部要求' % (_got, _full))
        else:
            doubt = '该项证据是否成立需人工认定'

    lines = ['要点：' + key]
    if src:
        lines.append('出处：' + src)
    lines.append('疑问：' + doubt)
    return '\n'.join(lines)


def project_archived(pname):
    """项目是否已归档（读 项目信息.json 的状态）。取不到就当未归档。"""
    pd = project_dir_for(pname)
    if not pd:
        return False
    try:
        with open(os.path.join(pd, '项目信息.json'), encoding='utf-8') as f:
            return ((json.load(f) or {}).get('状态') or '') == '已归档'
    except Exception:
        return False


def submit_gate(pname, units, rdir, want, redo):
    """复核提交闸门（2026-11-02；2026-11-03 增加归档拦截）。

    返回 (HTTP 码, 错误信息, 选定的投标单位)。码为 0 表示放行。四条规则：
      0. **已归档的项目不许提交**（归档＝不再复核；要复核先解档）；
      1. 同一时间只允许一家投标单位处于「处理中/待处理」；
      2. 同一家不允许重复提交（有未完成任务 → 拒绝）；
      3. 已有复核结果的不许重复提交（要重做必须显式「重新复核」）。
    未指定单位时：只有恰好一家可复核才自动选中，否则要求显式指定。
    """
    if project_archived(pname):
        return (409, '项目「%s」已归档，不能再提交复核。'
                     '如确需重做，请先点「归档」（此时是解档）把项目恢复为进行中。'
                % pname, '')
    # G2：正在预处理（还没建任务）的提交也必须拦住
    _inflight = submit_inflight_rows()
    if _inflight:
        for _r in _inflight:
            if _r.get('项目') == pname and _r.get('投标单位') in units:
                return (409, '「%s」正在提交（正在抽取正文/导出扫描页），'
                             '请等它完成，不要重复点击。' % _r.get('投标单位'), '')
        _r0 = _inflight[0]
        return (409, '另一家正在提交预处理：%s（%s，%s 开始）。'
                     '预处理期间不接收新的提交。'
                % (_r0.get('投标单位'), _r0.get('项目'), _r0.get('开始时间')), '')
    tasks = []
    for fp in glob.glob(os.path.join(QUEUE, '*.json')):
        try:
            with open(fp, encoding='utf-8') as f:
                tasks.append(json.load(f))
        except Exception:
            continue
    busy = [t for t in tasks if (t.get('状态') or '') in ('待处理', '处理中')]
    if busy:
        mine = [t for t in busy
                if t.get('项目') == pname and t.get('投标人') in units]
        t0 = mine[0] if mine else busy[0]
        if mine:
            return (409, '「%s」已有未完成任务（%s），不允许重复提交。'
                    % (t0.get('投标人'), t0.get('状态')), '')
        return (409, '同一时间只允许复核一家。当前队列里还有未完成任务：'
                '%s（%s，%s），请等它完成后再提交。'
                % (t0.get('投标人'), t0.get('项目'), t0.get('状态')), '')

    if not want:
        free = [u for u in sorted(units)
                if redo or not os.path.isfile(
                    os.path.join(rdir, u + '_复核结果.json'))]
        if len(free) != 1:
            return (400, '请指定一家投标单位（每次只复核一家）。当前可复核：%s'
                    % ('、'.join(free) if free else '（无）'), '')
        want = free[0]

    res = os.path.join(rdir, want + '_复核结果.json')
    if os.path.isfile(res) and not redo:
        sc = ''
        try:
            with open(res, encoding='utf-8') as f:
                d = json.load(f)
            sc = '（现有得分 %s/%s）' % (d.get('小计'), d.get('满分'))
        except Exception:
            pass
        return (409, '「%s」已有复核结果%s，不允许重复提交；如需重做请用「重新复核」。'
                % (want, sc), '')
    return (0, '', want)


def _read_kept_section(pd, start_mark, end_marks):
    """从已有的 项目要点.md 里取出某个手写小节（用于重新生成时保留）"""
    if not pd:
        return ''
    fp = os.path.join(pd, '项目要点.md')
    if not os.path.isfile(fp):
        return ''
    try:
        with open(fp, encoding='utf-8') as f:
            txt = f.read()
    except Exception:
        return ''
    i = txt.find(start_mark)
    if i < 0:
        return ''
    body_start = txt.find('\n', i) + 1
    j = len(txt)
    for m in end_marks:
        k = txt.find(m, body_start)
        if k >= 0:
            j = min(j, k)
    body = txt[body_start:j].strip('\n')
    # 只有占位提示、没有实际内容时视为空
    meaningful = [ln for ln in body.split('\n')
                  if ln.strip() and '【待补】' not in ln
                  and '由复核人补充' not in ln
                  and not ln.strip().startswith('---')]
    if len(meaningful) < 2:
        return ''
    return body

def _vol_page_text(vol_lines, ocr_cache, vol, pg):
    """取某册某页的文本：文字层（>40 字）优先，否则回 OCR 缓存。"""
    ls = vol_lines.get(vol) or []
    if 1 <= pg <= len(ls):
        t = (ls[pg - 1] or '').strip()
        if len(t) > 40:
            return t
    return ocr_cache.get((vol, pg), '')


def verify_result_evidence(d, pname, unit):
    """P1-1：服务端强校验证据——出处页码真实存在 + 依据关键数字在对应页可查。

    返回 {'存疑': [...], '无册正文': bool}；存疑项 {'序号','类型','详情'}。
    类型：'页码缺失'（该页无文字层也无 OCR）／'证据存疑'（依据里的长数字串在该页找不到）。
    这是防「提示词注入诱导编造证据」的最后一道服务端闸门。
    """
    import ocr_text as _ot
    sus = []
    no_bids = True
    pd = project_dir_for(pname)
    if pd:
        bdir = os.path.join(pd, '投标文件')
        up = os.path.join(bdir, unit)
        vol_lines, ocr_cache = {}, {}
        if os.path.isdir(up):
            no_bids = False
            # 每册文字层正文
            for x in sorted(os.listdir(up)):
                if x.endswith('.txt') and not x.endswith('.pages.txt'):
                    vol = os.path.splitext(x)[0]
                    try:
                        with open(os.path.join(up, x), encoding='utf-8') as f:
                            _ls = f.read().split('\n')
                    except Exception:
                        continue
                    vol_lines[vol] = _ls
                    _low = vol.lower()
                    for _ext in ('.pdf', '.docx', '.doc'):
                        if _low.endswith(_ext):
                            vol_lines[vol[:-len(_ext)]] = _ls
                            break
            # 各册 OCR 缓存文本（页号 → 文本）
            for fn in sorted(os.listdir(up)):
                if not fn.endswith('.pages'):
                    continue
                pdir = os.path.join(up, fn)
                if not os.path.isdir(pdir):
                    continue
                vol = fn[:-len('.pages')]
                # 册名别名：出处可能写「技术册p12」（去扩展名）或「技术册.pdfp12」
                _vnames = [vol]
                _vlow = vol.lower()
                for _ext in ('.pdf', '.docx', '.doc'):
                    if _vlow.endswith(_ext):
                        _vnames.append(vol[:-len(_ext)])
                        break
                info = _ot.collect_page_images(pdir)
                per = (info or {}).get('页') or {}
                for pg, imgs in per.items():
                    for img in imgs:
                        cf = _ot._cache_file(img)
                        if not os.path.isfile(cf):
                            continue
                        try:
                            with open(cf, encoding='utf-8') as f:
                                cd = json.load(f)
                            txt = (cd or {}).get('文字') if isinstance(cd, dict) else ''
                        except Exception:
                            txt = ''
                        if txt:
                            for _vn in _vnames:
                                ocr_cache[(_vn, pg)] = txt
                            break
        for it in (d.get('评分') or []):
            cit = as_text(it.get('出处'))
            basis = as_text(it.get('依据'))
            if not cit:
                continue
            digits = re.findall(r'\d{6,}', basis or '')
            for m in re.finditer(
                    r'(?P<vol>[^\s、，,]*?)p(?P<a>\d+)(?:\s*-\s*(?P<b>\d+))?', cit):
                a = int(m.group('a'))
                b = int(m.group('b')) if m.group('b') else a
                vol = m.group('vol') or ''
                for pg in range(a, b + 1):
                    txt = _vol_page_text(vol_lines, ocr_cache, vol, pg)
                    if not txt:
                        sus.append({'序号': it.get('序号'), '类型': '页码缺失',
                                    '详情': '出处 %sp%d 在该册无文字层也无 OCR 文本'
                                    % (vol, pg)})
                        continue
                    for dgt in digits:
                        if dgt not in txt:
                            sus.append({'序号': it.get('序号'), '类型': '证据存疑',
                                        '详情': '依据数字 %s 在 %sp%d 页文本找不到'
                                        % (dgt, vol, pg)})
    return {'存疑': sus, '无册正文': no_bids}


def _dt_parse(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(str(s).strip(), fmt)
        except Exception:
            pass
    return None


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def load_rules():
    if not os.path.isfile(RULES):
        return json.loads(json.dumps(EMPTY))
    try:
        with open(RULES, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        # P2-2：评分标准文件损坏时告警，不再静默返回空标准
        print('[告警] 评分标准文件损坏，返回空标准：%s（%r）' % (RULES, e))
        return json.loads(json.dumps(EMPTY))
    d.setdefault('项目', {})
    d.setdefault('分组', [])
    return d


def save_rules(d):
    d['更新时间'] = now()
    tmp = RULES + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RULES)
    return d


def _read_bid_job(tid, bid_path, pname, ocr_cap=60):
    """
    解读投标文件：
      1) 抽取正文（pdf/docx/doc 按文件头分流）
      2) 统计文字层质量与中文占比
      3) PDF 额外导出扫描页图片（用于读证书）
      4) 定位关键内容、识别投标人
      5) 全部结果写入 <投标文件>.解读.json
    """
    import time
    try:
        task_set(tid, 进度=4, 阶段='抽取正文')
        with open(bid_path, 'rb') as f:
            head = f.read(8)
        tables = None
        if head[:4] == b'PK\x03\x04':
            import docx_text
            r = docx_text.extract(bid_path)
        elif head[:5] == b'%PDF-':
            import pdf_text
            r = pdf_text.extract(bid_path)
        else:
            import doc_text
            r = doc_text.extract(bid_path)
        body = r.pop('正文')
        tables = r.pop('表格', None)
        task_log(tid, '正文抽取完成：%d 字' % len(body))
        with open(bid_path + '.txt', 'w', encoding='utf-8') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        task_set(tid, 进度=30, 阶段='统计文字层')
        lines = body.split('\n')
        pages_blank = sum(1 for l in lines
                          if len(re.findall(r'[\u4e00-\u9fff]', l)) < 20)
        task_log(tid, '中文字符 %d（%.2f%%），无文字页约 %d 页'
                 % (r.get('中文字符', 0), r.get('中文占比', 0), pages_blank))

        imgs = {'图片数': 0, '跳过': {}, '说明': '非 PDF，无页面图片'}
        if head[:5] == b'%PDF-':
            task_set(tid, 进度=45, 阶段='导出扫描页图片')
            try:
                import pdf_images
                import ocr_text as _otg
                pdir = bid_path + '.pages'
                _exist = _otg.collect_page_images(pdir)
                # M10：复用需认"导出完成索引"(.pages.json)——导出中途失败不会写它，
                # 只看"图片数>0"会把半套旧图永久复用
                if _exist['图片数'] > 0 and os.path.isfile(bid_path + '.pages.json'):
                    # 已有扫描页则不再导出：重复导出会改变图片 mtime，
                    # 使 OCR 缓存全部失效（463 张重扫约 16 分钟）。
                    imgs = {'图片数': _exist['图片数'], '跳过': {},
                            '复用': True, '总页数': len(pages_of_body(body))}
                    task_log(tid, '扫描页已存在，复用 %d 张（不重复导出）'
                             % _exist['图片数'])
                else:
                    imgs = pdf_images.export(bid_path, pdir, only_blank_pages=True)
                    with open(bid_path + '.pages.json', 'w',
                              encoding='utf-8') as f:
                        json.dump(imgs, f, ensure_ascii=False, indent=1)
                    task_log(tid, '导出扫描页图片 %d 张（跳过 %s）'
                             % (imgs['图片数'], imgs.get('跳过') or '无'))
            except Exception:
                traceback.print_exc()
                task_log(tid, '图片导出失败（不影响文字解读）')
        task_set(tid, 进度=65, 阶段='定位关键内容')

        KEYS = {'合同金额': ['合同金额', '万元'], '业绩': ['业绩'],
                '获奖': ['获奖', '鲁班', '装饰奖'], '工法': ['工法'],
                '人员简历': ['项目管理机构', '简历表'],
                '职称': ['高级工程师', '中级工程师', '助理工程师'],
                '验收': ['验收'], '社保': ['社保', '参保证明']}
        found = {}
        for label, kws in KEYS.items():
            pages = sorted({i + 1 for i, l in enumerate(lines) for k in kws if k in l})
            found[label] = {'命中页数': len(pages), '前若干页': pages[:8]}
        task_set(tid, 进度=82, 阶段='识别投标人')
        bidder = guess_bidder_name(body, os.path.basename(bid_path))
        task_log(tid, '识别投标人：%s' % bidder)

        # OCR：只识别「关键内容所在页」对应的扫描图，避免全量几百张过慢
        ocr_info = {'启用': False, '张数': 0, '字数': 0, '文件': None, '失败': []}
        pdir = bid_path + '.pages'
        if os.path.isdir(pdir):
            try:
                import ocr_text
                if ocr_text.available():
                    # 按「识别计划」分段识别：先定位证据章节，再按
                    # 分值/图片数 从高到低依次识别，避免全量平铺。
                    import review_plan as _rp
                    import ocr_text as _ot2
                    try:
                        _r, rules_path = load_project_rules(pname)
                        plan = _rp.plan(bid_path, bid_path + '.txt', rules_path) \
                            if rules_path and os.path.isfile(rules_path) \
                            else _rp.plan(bid_path, bid_path + '.txt',
                                          [{'序号': '1', '名称': '全部证据',
                                            '分值': 1}])
                    except Exception:
                        traceback.print_exc()
                        plan = None

                    parts = []
                    if plan and plan.get('顺序'):
                        task_log(tid, '识别计划：%d 个证据章节，必需 %d / 总 %d 张'
                                 % (len(plan['顺序']), plan['必需图片'],
                                    plan['总图片']))
                        done = 0
                        total_need = max(1, plan['必需图片'])
                        by_cat = {s['类别']: s for s in plan['章节']}
                        for cat in plan['顺序']:
                            sec = by_cat[cat]
                            imgs2 = []
                            for pg in range(sec['起'], sec['止'] + 1):
                                imgs2.extend(_ot2.collect_page_images(pdir)['页']
                                             .get(pg, []))
                            if not imgs2:
                                continue
                            task_log(tid, '识别「%s」%s：%d 张（覆盖项 %s，%s 分）'
                                     % (cat, sec['起止'], len(imgs2),
                                        '、'.join(sec['覆盖项']), sec['分值']))
                            for ip in imgs2:
                                try:
                                    t1 = None
                                    with HEAVY_LOCK:
                                        t1 = ocr_text.ocr_image(ip)
                                    if t1 and t1.strip():
                                        m = re.match(r'p(\d+)_', os.path.basename(ip))
                                        pg = int(m.group(1)) if m else 0
                                        parts.append('=== p%d %s ===\n%s'
                                                     % (pg, os.path.basename(ip), t1))
                                except Exception as e:
                                    ocr_info['失败'].append(
                                        {'文件': os.path.basename(ip),
                                         '错误': repr(e)[:80]})
                                done += 1
                                task_set(tid, 进度=min(94, 70 + int(24 * done
                                                                     / total_need)))
                                if done % 10 == 0:
                                    task_log(tid, 'OCR %d/%d（当前「%s」）'
                                             % (done, total_need, cat))
                    else:
                        # 兜底：无计划时按页序识别
                        import ocr_text as _ot2
                        imgs2 = _ot2.collect_page_images(pdir)['全部']
                        cap = int(ocr_cap or 300)
                        imgs2 = imgs2[:cap]
                        for ip in imgs2:
                            try:
                                with HEAVY_LOCK:
                                    t1 = ocr_text.ocr_image(ip)
                                if t1.strip():
                                    parts.append('=== %s ===\n%s'
                                                 % (os.path.basename(ip), t1))
                            except Exception:
                                pass
                    if parts:
                        ocr_path = bid_path + '.ocr.txt'
                        with open(ocr_path, 'w', encoding='utf-8') as f:
                            f.write('\n\n'.join(parts))
                        ocr_info.update({'启用': True, '张数': len(parts),
                                         '字数': sum(len(x) for x in parts),
                                         '文件': ocr_path,
                                         '计划': (plan or {}).get('顺序')})
                        task_log(tid, 'OCR 完成：%d 张 / %d 字'
                                 % (len(parts), ocr_info['字数']))
            except Exception:
                traceback.print_exc()
                task_log(tid, 'OCR 未执行（依赖缺失或异常），已跳过')

        heads = []
        for l in lines:
            s = l.strip()
            m = re.match(r'^[（(]?([一二三四五六七八九十]{1,3})[、）)]\s*(\S{2,30})', s)
            if m and len(s) < 60:
                heads.append(m.group(1) + '、' + m.group(2))
            if len(heads) >= 20:
                break
        task_set(tid, 进度=94, 阶段='汇总并保存')
        res = {'项目': pname, '投标文件': os.path.basename(bid_path),
               '投标人': bidder, '页数': len(lines),
               '总字符': len(body), '中文字符': r.get('中文字符'),
               '中文占比': r.get('中文占比'),
               '文字层质量': r.get('文字层质量'),
               '无文字页约': pages_blank,
               '扫描页图片': {'图片数': imgs.get('图片数', 0),
                            '跳过': imgs.get('跳过') or {},
                            '目录': (bid_path + '.pages'
                                     if imgs.get('图片数') else None)},
               '关键内容定位': found, '章节': heads,
               'OCR': ocr_info}
        if tables:
            res['表格数'] = len(tables)
            res['表格摘要'] = [{'表序': i, '行数': len(tb),
                                '表头': (tb[0] if tb else [])[:10]}
                               for i, tb in enumerate(tables)]
            with open(bid_path + '.tables.json', 'w', encoding='utf-8') as f:
                json.dump(tables, f, ensure_ascii=False)
        out = bid_path + '.解读.json'
        try:
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
            res['解读文件'] = out
        except Exception:
            traceback.print_exc()
        task_set(tid, 进度=100, 阶段='完成', 完成=True, 结果=res)
    except Exception as e:
        traceback.print_exc()
        task_set(tid, 完成=True, 失败=repr(e), 阶段='异常')


class H(http.server.BaseHTTPRequestHandler):
    server_version = 'review/0.1'

    def log_message(self, fmt, *a):
        sys.stderr.write('[%s] %s\n' % (now(), fmt % a))

    # ---------- 基础输出 ----------
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=1)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # P1-3：限制外联，降低 XSS 后果（本前端是内联脚本，需允许 unsafe-inline）
        self.send_header('Content-Security-Policy',
                         "default-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        return self._send(code, {'ok': False, 'error': msg})

    def _check_token(self):
        """校验访问口令：/api/* 需要，首页等静态资源免。未配置口令则不校验。"""
        if not API_TOKEN:
            return True
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith('/api/'):
            return True
        tk = self.headers.get('X-API-Token') or ''
        if not tk:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tk = (qs.get('token') or [''])[0]
        return tk == API_TOKEN

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        # L4：上传体积上限——投标 PDF 一般几十 MB；设 200 MB 防超大文件把内存峰值顶到 ×2
        if n > 200 * 1024 * 1024:
            raise ValueError('请求体 %.1f MB 超过 200 MB 上限，已拒绝'
                             % (n / 1048576.0))
        return self.rfile.read(n) if n else b''

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._do_GET()
        except Exception:
            traceback.print_exc()
            try:
                self._err(500, '服务端处理 GET %s 时出错：%s（详见 server.out.log）'
                          % (self.path.split('?')[0],
                             traceback.format_exc().strip().splitlines()[-1][:200]))
            except Exception:
                pass

    def _do_GET(self):
        if not self._check_token():
            return self._err(401, '访问口令缺失或不正确（见服务启动窗口的「口令」）')
        p = urllib.parse.urlparse(self.path).path
        if p in ('/', '/index.html'):
            fp = os.path.join(HERE, 'ui', 'main.html')
            if not os.path.isfile(fp):
                return self._send(500, '<h1>ui/main.html 缺失</h1>',
                                  'text/html; charset=utf-8')
            with open(fp, encoding='utf-8') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if p == '/api/health':
            return self._send(200, {'ok': True, '时间': now(),
                                    '队列': len(queue_list()),
                                    '项目数': len(os.listdir(PROJ))
                                    if os.path.isdir(PROJ) else 0})
        if p == '/api/rules':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (qs.get('project') or [''])[0].strip()
            # 指定了项目：只读该项目的标准；没有就返回空标准，绝不借用其它项目的
            if want:
                pd = project_dir_for(want)
                pf = os.path.join(pd, '评分标准.json') if pd else None
                if pf and os.path.isfile(pf):
                    try:
                        with open(pf, encoding='utf-8') as f:
                            d = json.load(f)
                        d['_来源'] = pf
                        return self._send(200, d)
                    except Exception:
                        traceback.print_exc()
                return self._send(200, {'项目': {'名称': want, '编号': ''},
                                        '分组': [], '_来源': '(该项目尚无评分标准，'
                                                          '请录入后保存)'})
            pf = project_file('评分标准.json')
            if pf and os.path.isfile(pf):
                try:
                    with open(pf, encoding='utf-8') as f:
                        d = json.load(f)
                    d['_来源'] = pf
                    return self._send(200, d)
                except Exception:
                    pass
            return self._send(200, load_rules())
        if p == '/api/tenders':
            out = []
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            only = (qs.get('project') or [''])[0].strip()
            plist = [only] if only else (
                sorted(os.listdir(PROJ)) if os.path.isdir(PROJ) else [])
            if True:
                for proj in plist:
                    tdir = os.path.join(PROJ, proj, '招标文件')
                    for fp in walk_files(tdir, '.json'):
                        if fp.endswith('.tables.json'):
                            continue
                        try:
                            with open(fp, encoding='utf-8') as f:
                                d = json.load(f)
                            d['项目'] = proj
                            out.append(d)
                        except Exception:
                            pass
            out.sort(key=lambda x: (x.get('项目') or '', x.get('文件') or ''))
            return self._send(200, out)
        if p == '/api/bid_files':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            unit = (qs.get('unit') or qs.get('投标单位') or [''])[0].strip()
            pd = project_dir_for(pname) if pname else None
            up = os.path.join(pd, '投标文件', unit) if pd else None
            files = pick_bid_files(up)
            infos = [bid_file_info(fp) for fp in files]
            return self._send(200, {'ok': True, '项目': pname, '投标单位': unit,
                                    '文件数': len(infos), '文件': infos,
                                    '说明': '册名=文件名去扩展；出处请写「册名+p页码」'
                                            '（如 技术册p45）'})
        if p == '/api/bids':
            out = []
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            only = (qs.get('project') or [''])[0].strip()
            bsearch = (os.path.join(PROJ, only, '投标文件')
                       if only else BIDS)
            for fp in walk_files(bsearch, '.pdf.json'):
                try:
                    with open(fp, encoding='utf-8') as f:
                        d = json.load(f)
                    d['项目'] = os.path.basename(
                        os.path.dirname(os.path.dirname(
                            os.path.dirname(fp))))
                    d['投标人'] = os.path.basename(os.path.dirname(fp))
                    out.append(d)
                except Exception:
                    pass
            out.sort(key=lambda x: x.get('文件') or '')
            return self._send(200, out)
        if p == '/api/project':
            out = []
            if os.path.isdir(PROJ):
                for d in sorted(os.listdir(PROJ)):
                    fp = os.path.join(PROJ, d, '项目信息.json')
                    if os.path.isfile(fp):
                        try:
                            with open(fp, encoding='utf-8') as f:
                                out.append(json.load(f))
                        except Exception:
                            pass
                    else:
                        out.append({'项目名称': d, '项目编号': '',
                                    '目录': os.path.join(PROJ, d)})
            return self._send(200, out)
        if p == '/api/queue':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tid = (qs.get('id') or [''])[0]
            if tid:
                fp = os.path.join(QUEUE, tid + '.json')
                if not os.path.isfile(fp):
                    return self._err(404, '任务不存在：%s' % tid)
                with open(fp, encoding='utf-8') as f:
                    return self._send(200, json.load(f))
            only = (qs.get('project') or [''])[0].strip()
            rows = queue_list()
            if only:
                rows = [x for x in rows if (x.get('项目') or '') == only]
            return self._send(200, rows)
        if p == '/api/busy':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            rows = submit_inflight_rows()
            if pname:
                rows = [r for r in rows if r.get('项目') == pname]
            tasks = queue_list()
            if pname:
                tasks = [t for t in tasks if (t.get('项目') or '') == pname]
            cnt = {}
            running = []
            for t in tasks:
                st = t.get('状态') or ''
                cnt[st] = cnt.get(st, 0) + 1
                if st == '处理中':
                    running.append(t.get('投标人') or '')
            pre = [v for v in (_PREWARM_STATE or {}).values()
                   if isinstance(v, dict) and v.get('状态') == '运行中']
            pd = project_dir_for(pname) if pname else None
            unrev, total_units = [], 0
            summary = False
            summary_mtime = None
            summary_stale = False
            if pd and os.path.isdir(pd):
                fp_sum = os.path.join(pd, '复核结果汇总.md')
                if os.path.isfile(fp_sum):
                    summary = True
                    try:
                        _st_sum = os.path.getmtime(fp_sum)
                        summary_mtime = time.strftime(
                            '%Y-%m-%d %H:%M', time.localtime(_st_sum))
                        # S2：汇总是否落后于结果文件
                        _rdir0 = os.path.join(pd, '复核报告')
                        if os.path.isdir(_rdir0):
                            for _fn0 in os.listdir(_rdir0):
                                if not _fn0.endswith('_复核结果.json'):
                                    continue
                                try:
                                    if os.path.getmtime(
                                            os.path.join(_rdir0, _fn0)) > _st_sum + 1:
                                        summary_stale = True
                                        break
                                except Exception:
                                    pass
                    except Exception:
                        pass
                bdir = os.path.join(pd, '投标文件')
                rdir = os.path.join(pd, '复核报告')
                all_u = []
                if os.path.isdir(bdir):
                    for d in sorted(os.listdir(bdir)):
                        dp = os.path.join(bdir, d)
                        if os.path.isdir(dp) and not d.endswith('.pages'):
                            fs = [x for x in os.listdir(dp)
                                  if not x.endswith(('.txt', '.json'))
                                  and os.path.isfile(os.path.join(dp, x))]
                            if fs:
                                all_u.append(d)
                total_units = len(all_u)
                for u in all_u:
                    if not os.path.isfile(os.path.join(rdir, u + '_复核结果.json')):
                        unrev.append(u)
            return self._send(200, {
                'ok': True, '项目': pname or None,
                '提交中': [r.get('投标单位') or '' for r in rows],
                '待处理': cnt.get('待处理', 0),
                '处理中': cnt.get('处理中', 0),
                '复核中单位': running,
                '未完成任务': cnt.get('待处理', 0) + cnt.get('处理中', 0),
                '预热中': len(pre),
                '汇总': summary, '汇总时间': summary_mtime,
                '汇总过期': summary_stale,
                '总单位数': total_units, '未复核': unrev,
                '完全可导出': bool(summary) and not unrev,
            })
        if p == '/api/submit/last':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname0 = (qs.get('project') or [''])[0].strip()
            left = submit_last_failed()
            rows = (left.get('未完成') or [])
            if pname0:
                rows = [r for r in rows if r.get('项目') == pname0]
            return self._send(200, {'ok': True, '遗留未完成': rows,
                                    '遗留时间': left.get('时间'),
                                    '最近': submit_log_list(pname0)})
        if p == '/api/units':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            if not pname:
                d0 = current_project_dir()
                pname = os.path.basename(d0) if d0 else ''
            pd = project_dir_for(pname) if pname else None
            if not pd or not os.path.isdir(pd):
                return self._err(404, '项目不存在：%s' % pname)
            bdir = os.path.join(pd, '投标文件')
            rdir = os.path.join(pd, '复核报告')
            out = []
            if os.path.isdir(bdir):
                for u in sorted(os.listdir(bdir)):
                    up = os.path.join(bdir, u)
                    if not os.path.isdir(up) or u.endswith('.pages'):
                        continue
                    files = pick_bid_files(up)
                    if not files:
                        continue
                    rec = {'投标单位': u, '投标文件': files[0],
                           '文件数': len(files),
                           '文件大小MB': round(sum(os.path.getsize(x)
                                                  for x in files) / 1048576.0, 1),
                           '已复核': False, '得分': None, '满分': None,
                           '未决项': [], '复核时间': None,
                           '提交中': submit_inflight_busy(pname, u)}
                    # ---- 耗时估算 + OCR 状态：遍历每个文件的 .pages ----
                    try:
                        n_img = 0
                        need_ocr = 0
                        done_ocr = 0
                        for _fp in files:
                            pdir = _fp + '.pages'
                            if os.path.isdir(pdir):
                                for fn in os.listdir(pdir):
                                    if fn.lower().endswith(('.jpg', '.jpeg',
                                                            '.png', '.bmp',
                                                            '.webp', '.tif',
                                                            '.tiff')):
                                        n_img += 1
                                        need_ocr += 1
                                cache = os.path.join(pdir, '.ocr_cache')
                                if os.path.isdir(cache):
                                    done_ocr += len([x for x in os.listdir(cache)
                                                     if x.endswith('.json')])
                        rec['图片数'] = n_img
                        rec['需OCR页'] = need_ocr
                        rec['已OCR页'] = done_ocr
                        rec['已导页'] = n_img > 0
                        rec['OCR完成'] = (need_ocr > 0 and done_ocr >= need_ocr)
                        if n_img:
                            rec['预计分钟'] = round(n_img * OCR_PER_IMG / 60.0, 1)
                        txt = os.path.join(up, files[0] + '.txt')
                        if os.path.isfile(txt):
                            with open(txt, encoding='utf-8', errors='ignore') as f:
                                rec['页数'] = f.read().count('\n') + 1
                    except Exception:
                        pass
                    rf = os.path.join(rdir, u + '_复核结果.json')
                    if os.path.isfile(rf):
                        try:
                            with open(rf, encoding='utf-8') as f:
                                d2 = json.load(f)
                            rec['已复核'] = True
                            rec['得分'] = d2.get('小计')
                            rec['满分'] = d2.get('满分')
                            rec['未决项'] = d2.get('未决项') or []
                            rec['复核时间'] = d2.get('复核时间')
                            try:
                                import glob as _g
                                for qf in _g.glob(os.path.join(QUEUE, '*.json')):
                                    with open(qf, encoding='utf-8') as qfh:
                                        t2 = json.load(qfh)
                                    if t2.get('项目') != pname or \
                                            t2.get('投标人') != u:
                                        continue
                                    a2 = _dt_parse(t2.get('提交时间'))
                                    b2 = _dt_parse(t2.get('完成时间'))
                                    if a2 and b2 and b2 > a2:
                                        rec['实际分钟'] = round(
                                            (b2 - a2).total_seconds() / 60.0, 1)
                                        rec['队列号'] = t2.get('id')
                                    break
                            except Exception:
                                pass
                            bad = check_result_shape(d2)
                            if bad:
                                rec['数据异常'] = '结果文件缺少或类型不符：' + '、'.join(bad)
                        except Exception as e:
                            rec['数据异常'] = '结果文件无法解析：%r' % e
                    out.append(rec)
            return self._send(200, {'项目': pname, '单位数': len(out),
                                    '已复核': len([x for x in out if x['已复核']]),
                                    '单位': out})
        if p == '/api/prewarm':
            _prune_prewarm_state()   # L3：返回前清掉已结束超 60 分钟的旧状态
            return self._send(200, _PREWARM_STATE)
        if p == '/api/issues':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            return self._send(200, {'ok': True, '问题反馈': issue_list(pname),
                                    '数量': len(issue_list(pname))})
        if p == '/api/engine':
            return self._engine_info()
        if p == '/api/version':
            return self._version_info()
        if p == '/api/rescue':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _n = int((qs.get('n') or ['30'])[0] or 30)
            items = []
            try:
                if os.path.isfile(RESCUE_LOG):
                    with open(RESCUE_LOG, encoding='utf-8') as f:
                        items = (json.load(f) or [])[-max(1, min(_n, 300)):]
            except Exception:
                traceback.print_exc()
            return self._send(200, {
                'ok': True, '自动兜底': '开' if AUTO_RESCUE else '关',
                '巡检间隔秒': RESCUE_TICK,
                '唤醒宽限分钟': WAKE_GRACE_MIN,
                '最多唤醒次数': WAKE_MAX_TRIES,
                '僵死阈值分钟': STALE_MIN,
                '记录': items})
        if p == '/api/session/health':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _proj = (qs.get('project') or [''])[0].strip()
            return self._send(200, session_health(_proj or None))
        if p == '/api/doc':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            nm = (qs.get('name') or ['项目要点.md'])[0].strip()
            ALLOW = ('项目要点.md', '复核结果汇总.md', '项目要点_骨架.md',
                     '评分标准.json', '项目信息.json', '复核方法.md')   # L2：补进白名单
            if nm not in ALLOW:
                return self._err(400, '不允许读取该文件：%s' % nm)
            if nm == '复核方法.md':
                fp = os.path.join(HERE, '复核方法.md')
            else:
                pd = project_dir_for(pname) if pname else None
                if not pd or not os.path.isdir(pd):
                    return self._err(404, '项目不存在：%s' % pname)
                fp = os.path.join(pd, nm)
            if not os.path.isfile(fp):
                return self._err(404, '文件不存在：%s（可点「生成要点」/「生成汇总」创建）'
                                 % nm)
            try:
                with open(fp, encoding='utf-8') as f:
                    txt = f.read()
            except Exception as e:
                return self._err(500, '读取失败：%r' % e)
            return self._send(200, {'ok': True, '文件': fp, '名称': nm,
                                    '字节': len(txt), '内容': txt})
        if p == '/api/submit_progress':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bid = ((qs.get('batch') or qs.get('批次') or [''])[0] or '').strip()
            snap = submit_snapshot(bid or None)
            if bid and not snap:
                return self._err(404, '批次不存在或已过期：%s' % bid)
            return self._send(200, {'ok': True, '批次': snap})

        if p == '/api/download':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pname = (qs.get('project') or [''])[0].strip()
            fn = (qs.get('file') or [''])[0].strip()
            disp = (qs.get('name') or [fn])[0].strip()
            ALLOW = ('复核结果汇总.pdf', '复核结果汇总.docx', '复核结果汇总.md',
                     '项目要点.pdf', '项目要点.docx', '项目要点.md')
            if fn not in ALLOW:
                return self._err(400, '不允许下载该文件：%s' % fn)
            pd = project_dir_for(pname) if pname else None
            if not pd or not os.path.isdir(pd):
                return self._err(404, '项目不存在：%s' % pname)
            fp = os.path.join(pd, fn)
            if not os.path.isfile(fp):
                return self._err(404, '文件不存在：%s（请先点「生成汇总」或「导出」）' % fn)
            try:
                with open(fp, 'rb') as f:
                    data = f.read()
            except Exception as e:
                return self._err(500, '读取失败：%r' % e)
            ext = os.path.splitext(fn)[1].lower()
            ctype = {'.pdf': 'application/pdf',
                     '.docx': 'application/vnd.openxmlformats-officedocument.'
                              'wordprocessingml.document',
                     '.md': 'text/markdown; charset=utf-8'}.get(ext, 'application/octet-stream')
            try:
                quoted = urllib.parse.quote(disp, safe='')
            except Exception:
                quoted = 'download' + ext
            # filename= 只能 ASCII（latin-1）；中文名走 RFC 5987 的 filename*
            ascii_name = ''.join(c if 32 <= ord(c) < 127 and c not in '"\\'
                                 else '_' for c in fn) or ('download' + ext)
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition',
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (ascii_name, quoted))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return None
        if p == '/api/method_doc':
            fp = os.path.join(HERE, '复核方法.md')
            if not os.path.isfile(fp):
                return self._err(404, '复核方法.md 不存在')
            with open(fp, encoding='utf-8') as f:
                txt = f.read()
            return self._send(200, {'ok': True, '文件': fp, '名称': '复核方法.md',
                                    '字节': len(txt), '内容': txt})
        if p == '/api/scores':
            out, seen = [], set()
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            only = (qs.get('project') or [''])[0].strip()
            proj_list = [only] if only else (
                sorted(os.listdir(PROJ)) if os.path.isdir(PROJ) else [])
            for proj in proj_list:
                if True:
                    rdir = os.path.join(PROJ, proj, '复核报告')
                    for fp in walk_files(rdir, '.json'):
                        # 重做复核的旧结果归档在 _历史\，不计入当前明细
                        if os.path.sep + '_历史' + os.path.sep in fp:
                            continue
                        try:
                            with open(fp, encoding='utf-8') as f:
                                d = json.load(f)
                            if isinstance(d, dict) and d.get('评分'):
                                d.setdefault('项目', proj)
                                _bad = check_result_shape(d)
                                if _bad:
                                    d['_数据异常'] = _bad
                                out.append(d)
                                seen.add(os.path.basename(fp))
                        except Exception:
                            pass
            sd = os.path.join(WORK, 'scores')
            if not only and os.path.isdir(sd):
                for fn in sorted(os.listdir(sd)):
                    if fn.endswith('.json') and fn not in seen:
                        try:
                            with open(os.path.join(sd, fn), encoding='utf-8') as f:
                                out.append(json.load(f))
                        except Exception:
                            pass
            return self._send(200, out)
        return self._err(404, 'unknown api: %s' % p)

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            traceback.print_exc()
            try:
                self._err(500, '服务端处理 POST %s 时出错：%s（详见 server.out.log）'
                          % (self.path.split('?')[0],
                             traceback.format_exc().strip().splitlines()[-1][:200]))
            except Exception:
                pass

    def _do_POST(self):
        if not self._check_token():
            return self._err(401, '访问口令缺失或不正确（见服务启动窗口的「口令」）')
        p = urllib.parse.urlparse(self.path).path
        if p == '/api/tender':
            return self._upload_tender()
        if p == '/api/bid':
            return self._upload_bid()
        if p == '/api/expert':
            return self._upload_expert()
        if p == '/api/project':
            return self._save_project()
        if p == '/api/request':
            return self._submit_request()
        if p == '/api/run':
            return self._run_command()
        if p == '/api/frontlog':
            return self._frontlog()
        if p != '/api/rules':
            return self._err(404, 'unknown api: %s' % p)
        raw = self._body()
        try:
            d = json.loads(raw.decode('utf-8'))
        except Exception as e:
            return self._err(400, 'JSON 解析失败：%s' % e)
        if not isinstance(d, dict) or not isinstance(d.get('分组'), list):
            return self._err(400, '数据结构不符：需要含「分组」数组的对象')
        saved = save_rules(d)
        n = sum(len(g.get('项') or []) for g in saved['分组'])
        pname = ((d.get('项目') or {}).get('名称') or '').strip()
        pf = None
        if pname:
            pd = project_dir_for(pname, create=True)
            if pd:
                pf = os.path.join(pd, '评分标准.json')
                try:
                    atomic_write_json(pf, saved, backup=True)
                except Exception:
                    traceback.print_exc()
        return self._send(200, {'ok': True, '保存时间': saved['更新时间'],
                                '分组数': len(saved['分组']),
                                '评分项数': n, '路径': RULES,
                                '项目': pname, '项目文件': pf})

    def _frontlog(self):
        """接收前端上报的运行/错误日志，追加到 work\\front_log.json。"""
        raw = self._body()
        try:
            d = json.loads(raw.decode('utf-8'))
        except Exception as e:
            return self._err(400, 'JSON 解析失败：%s' % e)
        if not isinstance(d, dict):
            return self._err(400, '需要 JSON 对象')
        lvl = str(d.get('级别') or 'info')[:20]
        msg = str(d.get('消息') or '')[:2000]
        extra = d.get('上下文')
        kw = {}
        if isinstance(extra, dict):
            kw['上下文'] = extra
        elif extra not in (None, ''):
            kw['上下文'] = str(extra)[:2000]
        front_log_add(lvl, msg, **kw)
        return self._send(200, {'ok': True})

    # ---------- DELETE ----------
    def do_DELETE(self):
        if not self._check_token():
            return self._err(401, '访问口令缺失或不正确（见服务启动窗口的「口令」）')
        p = urllib.parse.urlparse(self.path).path
        if p == '/api/project':
            return self._delete_project()
        if p == '/api/unit':
            return self._delete_unit()
        if p == '/api/queue':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tid = (qs.get('id') or [''])[0]
            if not tid:
                return self._err(400, '缺少 id 参数')
            fp = os.path.join(QUEUE, tid + '.json')
            if not os.path.isfile(fp):
                return self._err(404, '任务不存在：%s' % tid)
            try:
                os.remove(fp)
            except Exception as e:
                return self._err(500, '删除失败：%r' % e)
            return self._send(200, {'ok': True, '已删除': tid})
        return self._err(404, 'unknown api: %s' % p)

    # ---------- 删除投标单位 ----------
    def _delete_project(self):
        """删除项目：整体移入回收站（不直接销毁），并取消未完成任务"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (qs.get('name') or [''])[0].strip()
        confirm = (qs.get('confirm') or [''])[0].strip()
        if not name:
            return self._err(400, '缺少 name 参数')
        pd = project_dir_for(name, create=False)
        if not pd:
            return self._err(404, '项目不存在：%s' % name)
        if confirm != name:
            return self._err(400, '确认名称与项目名不一致，已取消')

        # 取消该项目未完成的队列任务
        cancelled = []
        for qf in glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                with open(qf, encoding='utf-8') as fh:
                    t = json.load(fh)
            except Exception:
                continue
            if t.get('项目') != name:
                continue
            if t.get('状态') in ('待处理', '处理中'):
                t['状态'] = '已取消'
                t['完成时间'] = now()
                with open(qf, 'w', encoding='utf-8') as fh:
                    json.dump(t, fh, ensure_ascii=False, indent=1)
                cancelled.append(t.get('id'))

        # 整体移入回收站
        os.makedirs(TRASH, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        safe = re.sub(r'[\\/:*?"<>|]', '_', name)
        dst = os.path.join(TRASH, '%s_%s' % (safe, ts))
        try:
            shutil.move(pd, dst)
        except Exception as e:
            traceback.print_exc()
            return self._err(500, '移入回收站失败（项目未删除）：%r' % e)

        n_files = 0
        try:
            n_files = sum(len(f) for _, _, f in os.walk(dst))
        except Exception:
            pass
        return self._send(200, {
            'ok': True, '项目': name, '已移入回收站': dst,
            '文件数': n_files, '已取消任务': cancelled,
            '提示': '项目已移入回收站，不会出现在下拉列表；'
                    '如需恢复，把该目录移回 项目\\ 即可'})

    def _delete_unit(self):
        """删除某投标单位：投标文件目录、复核结果、未处理任务。

        只删本项目下的东西；bidder 会做去路径分隔符处理，杜绝越界。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pname = (qs.get('project') or [''])[0].strip()
        bidder = (qs.get('bidder') or [''])[0].strip()
        if not pname:
            return self._err(400, '缺少 project 参数')
        if not bidder:
            return self._err(400, '缺少 bidder 参数')
        if re.search(r'[\\/:*?"<>|]', bidder) or bidder in ('.', '..'):
            return self._err(400, '投标单位名称非法：%s' % bidder)
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)

        bdir = os.path.join(pd, '投标文件')
        up = os.path.join(bdir, bidder)
        # 越界校验：真实路径必须仍在 投标文件 目录下
        if os.path.commonpath([os.path.abspath(up), os.path.abspath(bdir)]) \
                != os.path.abspath(bdir):
            return self._err(400, '目标路径越界，拒绝删除')

        done, missing = [], []
        if os.path.isdir(up):
            try:
                shutil.rmtree(up)
                done.append('投标文件目录')
            except Exception as e:
                return self._err(500, '删除投标文件目录失败：%r' % e)
        else:
            missing.append('投标文件目录')

        rf = os.path.join(pd, '复核报告', bidder + '_复核结果.json')
        if os.path.isfile(rf):
            try:
                os.remove(rf)
                done.append('复核结果')
            except Exception as e:
                return self._err(500, '删除复核结果失败：%r' % e)
        else:
            missing.append('复核结果')

        killed = []
        for fp in glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                with open(fp, encoding='utf-8') as f:
                    t = json.load(f)
            except Exception:
                continue
            if t.get('项目') == pname and t.get('投标人') == bidder:
                try:
                    os.remove(fp)
                    killed.append(t.get('id') or os.path.basename(fp))
                except Exception:
                    pass
        if killed:
            done.append('任务 %d 个' % len(killed))

        if not done:
            return self._err(404, '未找到「%s」的投标文件或复核结果' % bidder)
        return self._send(200, {'ok': True, '项目': pname, '投标单位': bidder,
                                '已删除': done, '未找到': missing,
                                '已删任务': killed})

    # ---------- 项目信息 ----------
    def _save_project(self):
        raw = self._body()
        try:
            d = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception as e:
            return self._err(400, 'JSON 解析失败：%s' % e)
        name = safe_name(d.get('项目名称') or '', '')
        no = (d.get('项目编号') or '').strip()
        if not name:
            return self._err(400, '项目名称不能为空')
        pdir = os.path.join(PROJ, name)
        subs = ['投标文件', '招标文件', '评委打分', '复核报告']
        created = []
        try:
            os.makedirs(pdir, exist_ok=True)
            for s in subs:
                sp = os.path.join(pdir, s)
                if not os.path.isdir(sp):
                    os.makedirs(sp, exist_ok=True)
                    created.append(s)
        except Exception as e:
            return self._err(500, '建目录失败：%r' % e)
        fp = os.path.join(pdir, '项目信息.json')
        old = {}
        if os.path.isfile(fp):
            try:
                with open(fp, encoding='utf-8') as f:
                    old = json.load(f) or {}
            except Exception:
                old = {}
            backup_once(fp)
        info = {'项目名称': name, '项目编号': no, '目录': pdir,
                '子目录': subs,
                '创建时间': old.get('创建时间') or now(),
                '状态': old.get('状态') or '进行中'}
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        return self._send(200, {'ok': True, '项目名称': name, '项目编号': no,
                                '目录': pdir, '子目录': subs,
                                '本次新建': created, '文件': fp})

    # ---------- 招标文件上传 ----------
    def _upload_tender(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = os.path.basename((q.get('name') or ['tender.docx'])[0])
        name = re.sub(r'[\\/:*?"<>|\s]+', '_', name) or 'tender.docx'
        pname = (q.get('project') or [''])[0].strip()
        raw = self._body()
        if not raw:
            return self._err(400, '上传内容为空')
        if not pname:
            return self._err(400, '缺少 project 参数：招标文件必须归档到指定项目')
        pd = project_dir_for(pname, create=False)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在，请先保存项目信息' % pname)

        ext = os.path.splitext(name)[1].lower()
        head = raw[:8]
        if head[:4] == b'PK\x03\x04':
            kind = 'docx'
        elif head[:5] == b'%PDF-':
            kind = 'pdf'
        elif head[:4] == b'\xd0\xcf\x11\xe0':
            kind = 'doc'
        else:
            kind = 'text'

        os.makedirs(WORK, exist_ok=True)
        # M4：临时名带 pid + 线程 id，两个浏览器同时上传不互相截断
        tmp = os.path.join(WORK, '_tmp_tender_upload.%d.%d'
                           % (os.getpid(), threading.get_ident()))
        with open(tmp, 'wb') as f:
            f.write(raw)
        try:
            if kind == 'docx':
                import docx_text
                r = docx_text.extract(tmp)
            elif kind == 'pdf':
                import pdf_text
                r = pdf_text.extract(tmp)
            elif kind == 'doc':
                import doc_text
                r = doc_text.extract(tmp)
            else:
                body0 = raw.decode('utf-8', 'replace')
                cjk = len(re.findall(r'[\u4e00-\u9fff]', body0))
                r = {'段落数': body0.count('\n') + 1, '表格数': 0,
                     '总字符': len(body0), '中文字符': cjk,
                     '中文占比': round(100.0 * cjk / max(1, len(body0)), 2),
                     '文字层质量': '良好' if cjk else '无中文', '正文': body0,
                     '表格': None}
        except Exception as e:
            traceback.print_exc()
            try:
                os.remove(tmp)
            except Exception:
                pass
            return self._err(400, '解析失败：%r' % e)
        body = r.pop('正文')
        tables = r.pop('表格', None)

        tdir = os.path.join(pd, '招标文件')
        os.makedirs(tdir, exist_ok=True)
        h = hashlib.sha256(raw).hexdigest()[:8]
        stem, _e = os.path.splitext(name)
        name = '%s__%s%s' % (stem, h, ext or '.' + kind)
        fp = os.path.join(tdir, name)
        os.replace(tmp, fp)

        with open(fp + '.txt', 'w', encoding='utf-8') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        out = {'文件': name, '项目': pname, '格式': kind, '大小': len(raw),
               '保存路径': fp, '正文路径': fp + '.txt'}
        out.update(r)
        if tables:
            out['表格数'] = len(tables)
            with open(fp + '.tables.json', 'w', encoding='utf-8') as f:
                json.dump(tables, f, ensure_ascii=False)
            out['表格摘要'] = [{'表序': i, '行数': len(tb),
                                '表头': (tb[0] if tb else [])[:12]}
                               for i, tb in enumerate(tables)]
        else:
            out.setdefault('表格数', 0)
        with open(fp + '.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        return self._send(200, {'ok': True, '结果': out})

    # ---------- 投标文件保存（只落盘） ----------
    def _upload_bid(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = os.path.basename((q.get('name') or ['bid.pdf'])[0])
        name = re.sub(r'[\\/:*?"<>|\s]+', '_', name) or 'bid.pdf'
        pname = (q.get('project') or [''])[0].strip()
        bidder = (q.get('bidder') or [''])[0].strip()
        raw = self._body()
        if not raw:
            return self._err(400, '上传内容为空')
        if not pname:
            return self._err(400, '缺少 project 参数：投标文件必须归档到指定项目')
        if not bidder:
            return self._err(400, '缺少 bidder 参数：投标文件按投标单位分文件夹存放，'
                                  '请填写投标单位名称')
        pd = project_dir_for(pname, create=False)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在，请先保存项目信息' % pname)

        head = raw[:8]
        if head[:4] == b'PK\x03\x04':
            kind = 'docx'
        elif head[:5] == b'%PDF-':
            kind = 'pdf'
        elif head[:4] == b'\xd0\xcf\x11\xe0':
            kind = 'doc'
        else:
            return self._err(400, '格式无法识别：只支持 doc / docx / pdf')

        bdir = os.path.join(pd, '投标文件', safe_name(bidder, '未识别投标单位'))
        os.makedirs(bdir, exist_ok=True)
        h = hashlib.sha256(raw).hexdigest()[:8]
        stem, ext = os.path.splitext(name)
        name = '%s__%s%s' % (stem, h, ext or '.' + kind)
        fp = os.path.join(bdir, name)
        with open(fp, 'wb') as f:
            f.write(raw)
        out = {'文件': name, '项目': pname, '投标单位': bidder, '格式': kind,
               '大小': len(raw), '保存路径': fp, '已抽取': False}
        with open(fp + '.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        return self._send(200, {'ok': True, '结果': out})

    # ---------- 提交复核任务（按单位，提交后唤醒 agent） ----------

    # ---------- 提交后台工作线程（抽取正文 / 导出扫描页 / 预热 OCR） ----------
    def _submit_worker(self, bid, pname, units, rpath, req, rdir):
        total_units = max(1, len(units))
        created, skipped = [], []
        woke, winfo = False, None
        try:
            for ui, unit in enumerate(sorted(units)):
                with SUBMIT_LOCK:
                    _st = SUBMIT_STATE.get(bid) or {}
                    _cancel = bool(_st.get('停止请求'))
                if _cancel:
                    submit_set(bid, 阶段='已停止', 已停止=True)
                    steps_stop = '收到停止请求，中断后续单位'
                    skipped.append(steps_stop)
                    break
                base = 100.0 * ui / total_units
                span = 100.0 / total_units
                submit_set(bid, 当前单位=unit, 阶段='抽取正文')
                _bpaths = units[unit] if isinstance(units[unit], (list, tuple)) \
                    else [units[unit]]
                submit_unit(bid, unit, 阶段='抽取正文', 进度=2,
                            说明='读取 %d 个文件：%s'
                             % (len(_bpaths),
                                '、'.join(os.path.basename(x) for x in _bpaths)))

                def setp(frac, stage, note=''):
                    submit_set(bid, 进度=round(base + span * frac, 1), 阶段=stage)
                    submit_unit(bid, unit, 阶段=stage, 进度=round(frac * 100),
                                说明=note)

                bid_paths = units[unit]
                if not isinstance(bid_paths, (list, tuple)):
                    bid_paths = [bid_paths]
                steps = []
                _vols = []   # [(册名, 文件路径, txt, pdir)]
                for _bp in bid_paths:
                    _vol = os.path.splitext(os.path.basename(_bp))[0]
                    bid_path = _bp
                    txt = bid_path + '.txt'
                    steps.append('—— 册「%s」——' % _vol)
                    if not os.path.isfile(txt):
                        try:
                            with open(bid_path, 'rb') as fh:
                                head = fh.read(8)
                            if head[:4] == b'PK\x03\x04':
                                import docx_text as m
                            elif head[:5] == b'%PDF-':
                                import pdf_text as m
                            else:
                                import doc_text as m
                            r = m.extract(bid_path)
                            body = r.pop('正文', '')
                            with open(txt, 'w', encoding='utf-8') as fh:
                                fh.write(body)
                                fh.flush()
                                os.fsync(fh.fileno())
                            if r.get('表格'):
                                with open(bid_path + '.tables.json', 'w',
                                          encoding='utf-8') as fh:
                                    json.dump(r['表格'], fh, ensure_ascii=False)
                            steps.append('抽取正文 %d 字' % len(body))
                        except Exception as e:
                            traceback.print_exc()
                            steps.append('抽取正文失败：%r' % e)
                    else:
                        steps.append('正文已存在，跳过')
                    setp(0.35, '抽取正文完成')

                    pdir = bid_path + '.pages'
                    # M10：复用需认 .pages.json 索引，否则半套旧图被永久复用
                    if bid_path.lower().endswith('.pdf') and \
                            not (os.path.isdir(pdir)
                                 and os.path.isfile(bid_path + '.pages.json')):
                        try:
                            import pdf_images
                            _t0 = time.time()

                            def _prog(done, total, saved):
                                # 导出期间也刷新「前台忙」心跳：导出与后台预热同抢 CPU，
                                # 用户在等导出进度，优先让导出跑完。
                                try:
                                    import ocr_text as _otp
                                    _otp.mark_foreground()
                                except Exception:
                                    pass
                                # 导出占本单位的 35%→92%
                                frac = 0.35 + 0.57 * (float(done) / max(1, total))
                                el = time.time() - _t0
                                if done <= 1:
                                    submit_set(bid, 页开始时间=time.time(),
                                               页进度={'已处理': done, '总页数': total})
                                else:
                                    submit_set(bid,
                                               页进度={'已处理': done, '总页数': total})
                                st0 = (submit_snapshot(bid) or {}).get('页开始时间')
                                note = '已处理 %d/%d 页，导出 %d 张，用时 %.0f 秒' % (
                                    done, total, saved, el)
                                if st0 and done > 1:
                                    rate = done / max(0.001, time.time() - st0)
                                    note += '（%.1f 页/秒，预计还需 %.0f 秒）' % (
                                        rate, (total - done) / rate)
                                setp(frac, '导出扫描页图片', note)

                            idx = pdf_images.export(bid_path, pdir,
                                                    only_blank_pages=True,
                                                    on_progress=_prog)
                            with open(bid_path + '.pages.json', 'w',
                                      encoding='utf-8') as fh:
                                json.dump(idx, fh, ensure_ascii=False, indent=1)
                            steps.append('导出扫描页 %d 张 / 共 %d 页'
                                         % (idx['图片数'], idx.get('总页数') or 0))
                        except Exception as e:
                            traceback.print_exc()
                            steps.append('导出扫描页失败：%r' % e)
                    elif os.path.isdir(pdir):
                        steps.append('扫描页已存在，跳过')
                    _vols.append((_vol, bid_path,
                                  txt if os.path.isfile(txt) else None,
                                  pdir if os.path.isdir(pdir) else None))
                setp(0.94, '导出完成')

                tid = 'Q' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S%f') \
                      + '_' + str(len(created) + 1)
                task = {'id': tid, '类型': '复核', '项目': pname, '投标人': unit,
                        '投标文件': bid_paths[0],
                        '文件清单': [os.path.basename(x) for x in bid_paths],
                        '册详情': [{'册名': v, '文件': os.path.basename(bp),
                                  '正文': t, '扫描页': p}
                                  for v, bp, t, p in _vols],
                        '投标正文': _vols[0][2],
                        '评分标准': rpath,
                        '扫描页目录': _vols[0][3],
                        '提交时间': now(), '状态': '待处理',
                        '预处理': steps, '说明': req.get('说明') or '',
                        '结果文件': None, '错误': None}
                atomic_write_json(os.path.join(QUEUE, tid + '.json'), task)
                pre_n = 0
                for _v, _bp, _t, _pd in _vols:
                    try:
                        pre_n += start_prewarm(
                            tid + '-' + _v, unit,
                            _t if _t and os.path.isfile(_t) else None,
                            _pd if _pd and os.path.isdir(_pd) else None)
                    except Exception:
                        traceback.print_exc()
                if pre_n:
                    steps.append('后台预热 OCR %d 页' % pre_n)
                created.append({'id': tid, '投标单位': unit,
                                '预处理': steps, '预热页数': pre_n})
                submit_log_add(pname, unit, '已提交',
                               '预处理完成，任务 %s' % tid, tid)
                submit_set(bid, 已完成单位=ui + 1, 进度=round(base + span, 1))
                submit_unit(bid, unit, 阶段='已完成', 进度=100,
                            说明='任务 %s 已创建' % tid)

            if created:
                tids = [c['id'] for c in created]
                try:
                    wait_s = float(os.environ.get('WAKE_WAIT_PREWARM') or 0)
                except Exception:
                    wait_s = 0.0
                waited = wait_prewarm(tids, wait_s) if wait_s > 0 else 0.0
                try:
                    import wake as wake_mod
                    # ③ 自动建会话：项目还没有专属会话就新建一个（需 ENABLE_AUTO_SESSION=1）
                    auto_info = ''
                    if wake_mod.auto_session_enabled():
                        try:
                            sid0, why0, made = wake_mod.ensure_project_session(pname)
                            if sid0 and made:
                                auto_info = '已新建项目专属会话 %s' % sid0
                            elif sid0:
                                auto_info = '沿用项目专属会话 %s' % sid0
                            else:
                                auto_info = '自动建会话失败（%s），回落默认会话' % why0
                        except Exception as e2:
                            auto_info = '自动建会话异常：%r（回落默认会话）' % e2
                    names = '、'.join(x['投标单位'] for x in created)
                    pre_txt = prewarm_summary(tids)
                    if waited:
                        pre_txt = '（已等预热 %.0f 秒）%s' % (waited, pre_txt)
                    woke, winfo = wake_mod.wake(
                        '【自动唤醒·前端提交】项目「%s」新增复核任务：%s。'
                        '【会话】%s'
                        '【预热】%s'
                        '【册清单】一家可能有多个投标文件（分册）。任务 JSON 的「册详情」'
                        '列出每册的文件/正文/扫描页路径；复核时逐册都看，'
                        '出处必须写「册名+p页码」，如「技术册p45（OCR）」'
                        '（册名＝文件名去扩展名）；只有一册时也要带册名，保持统一。'
                        '【第一步必做】调接口 POST /api/run '
                        '{"任务":"stopcheck","参数":{"id":"<任务id>"}}，'
                        '返回 "已停止": true 就立刻停手，不得做任何工作、不得写复核结果。'
                        '请立即读取 work/queue 下状态为「待处理」的任务并逐项复核，'
                        '结果写入 项目\\%s\\复核报告\\。'
                        % (pname, names, auto_info or '使用默认会话',
                           pre_txt, pname), project=pname)
                except Exception as e:
                    traceback.print_exc()
                    winfo = '唤醒调用异常：%r' % e
                submit_set(bid, 已唤醒=woke, 唤醒说明=winfo, 预热等待秒=waited,
                           会话=(auto_info or ''))
        except Exception as e:
            traceback.print_exc()
            submit_set(bid, 失败=repr(e))
            try:
                submit_log_add(pname, '', '失败', repr(e)[:200])
            except Exception:
                pass
        finally:
            for _u in list(units):
                try:
                    submit_inflight_end(pname, _u)
                except Exception:
                    pass
            with SUBMIT_LOCK:
                _st2 = SUBMIT_STATE.get(bid) or {}
                _stopped = bool(_st2.get('已停止'))
            submit_set(bid,
                       完成=True,
                       进度=(_st2.get('进度') or 0) if _stopped else 100,
                       阶段='已停止' if _stopped else '完成',
                       已停止=_stopped,
                       已提交=created, 已完成单位=len(created))

    # ---------- 停止 ----------
    def _queue_stop(self, params):
        """停止复核任务：状态改为「已停止」，不删除任务与结果"""
        tid = (params.get('id') or '').strip()
        pname = (params.get('项目') or '').strip()
        reason = (params.get('原因') or '用户在前端停止').strip()
        if not tid and not pname:
            return self._err(400, '需要 id 或 项目 参数')
        targets = []
        if tid:
            fp = os.path.join(QUEUE, tid + '.json')
            if not os.path.isfile(fp):
                return self._err(404, '任务不存在：%s' % tid)
            targets = [fp]
        else:
            for fp in glob.glob(os.path.join(QUEUE, '*.json')):
                try:
                    with open(fp, encoding='utf-8') as fh:
                        t = json.load(fh)
                except Exception:
                    continue
                if t.get('项目') != pname:
                    continue
                if t.get('状态') in ('待处理', '处理中'):
                    targets.append(fp)
        stopped = []
        for fp in targets:
            try:
                with open(fp, encoding='utf-8') as fh:
                    t = json.load(fh)
            except Exception:
                continue
            if t.get('状态') not in ('待处理', '处理中'):
                continue
            t['状态'] = '已停止'
            t['停止时间'] = now()
            t['停止原因'] = reason
            if atomic_write_json(fp, t):
                stopped.append(t.get('id'))
        return self._send(200, {'ok': True, '已停止': stopped,
                                '数量': len(stopped),
                                '说明': '任务状态已置为「已停止」，agent 不会继续处理；'
                                        '任务记录与已产生的复核结果均保留'})

    def _queue_wake(self, params):
        """重发唤醒（U5）：只补一条唤醒消息，不改任务状态。

        场景：任务在「待处理」但上次唤醒失败（会话被删/离线/改过绑定），
        界面上看不出原因，任务就一直躺着。这里给一个"再喊一次"的按钮。
        """
        tid = (params.get('id') or '').strip()
        if not tid:
            return self._err(400, '缺少 id 参数')
        fp = os.path.join(QUEUE, tid + '.json')
        if not os.path.isfile(fp):
            return self._err(404, '任务不存在：%s' % tid)
        try:
            with open(fp, encoding='utf-8') as fh:
                t = json.load(fh)
        except Exception as e:
            return self._err(500, '读取任务失败：%r' % e)
        st = t.get('状态') or ''
        if st != '待处理':
            return self._err(400, '任务当前状态为「%s」，只有「待处理」需要重发唤醒'
                             '（处理中先点「重置」，已完成要重做请点「重置并重审」）' % st)
        _tries = int(t.get('唤醒次数') or 0)
        if _tries >= WAKE_MAX_TRIES:
            return self._err(429, '这个任务已经唤醒 %d 次仍没人接手，继续重发多半无效。\n'
                                  '建议二选一：\n'
                                  '① 换会话：桌面端新建一个会话，发一句话让它可唤醒，'
                                  '再在本项目点「分配会话」；\n'
                                  '② 点「重置」，让任务回到待处理后再试。'
                             % _tries)
        try:
            import wake as wake_mod
            woke, winfo = wake_mod.wake(
                '【重新唤醒】项目「%s」的任务 %s（%s）一直在待处理，'
                '可能上次唤醒没送到。'
                '【第一步必做】调接口 POST /api/run '
                '{"任务":"stopcheck","参数":{"id":"%s"}}，返回 "已停止": true 就停手。'
                '请读取该任务并复核，结果写入 项目\\%s\\复核报告\\。'
                % (t.get('项目') or '', tid, t.get('投标人') or '', tid,
                   t.get('项目') or ''), project=t.get('项目') or '')
        except Exception as e:
            traceback.print_exc()
            return self._err(500, '唤醒调用异常：%r' % e)
        t['重发唤醒时间'] = now()
        t['重发唤醒次数'] = int(t.get('重发唤醒次数') or 0) + 1
        t['唤醒次数'] = int(t.get('唤醒次数') or 0) + 1
        if woke:
            t['唤醒时间'] = now()
        try:
            with open(fp, 'w', encoding='utf-8') as fh:
                json.dump(t, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return self._send(200, {'ok': True, 'id': tid, '已唤醒': woke,
                                '唤醒说明': winfo, '次数': t['重发唤醒次数']})

    def _queue_reset(self, params):
        """一键重置（G3）：把卡住的「处理中/待处理」任务直接置为「待处理」并重新唤醒。

        用途：agent 崩了/会话被删，任务永远停在「处理中」，而闸门会因此拒绝一切新提交。
        期望是用户点一次就恢复，不必自己"先停止再恢复"两步。
        """
        tid = (params.get('id') or '').strip()
        if not tid:
            return self._err(400, '缺少 id 参数')
        fp = os.path.join(QUEUE, tid + '.json')
        if not os.path.isfile(fp):
            return self._err(404, '任务不存在：%s' % tid)
        try:
            with open(fp, encoding='utf-8') as fh:
                t = json.load(fh)
        except Exception as e:
            return self._err(500, '读取任务失败：%r' % e)
        st = t.get('状态') or ''
        if st == '待处理':
            return self._err(400, '任务当前就是「待处理」，不需要重置；'
                                  '若一直没人接手，点「重发唤醒」或检查会话是否在线')
        if st in ('已完成',):
            # 已完成要重做：必须显式确认，且先把旧结果归档，避免误覆盖
            if not params.get('确认覆盖'):
                return self._err(400, '任务「%s」已完成。重置它会重新进入复核队列，'
                                      '并把现有结果覆盖。\n确认重做请传 确认覆盖=true'
                                 % tid)
            _archived = []
            try:
                _rd = os.path.join(project_dir_for(t.get('项目') or '') or '',
                                   '复核报告')
                _unit = t.get('投标人') or ''
                _fp = os.path.join(_rd, _unit + '_复核结果.json')
                if os.path.isfile(_fp):
                    _his = os.path.join(_rd, '_历史')
                    os.makedirs(_his, exist_ok=True)
                    _dst = os.path.join(_his, '%s_复核结果_%s.json'
                                        % (_unit, datetime.datetime.now()
                                           .strftime('%Y%m%d_%H%M%S')))
                    shutil.copy2(_fp, _dst)
                    _archived.append(os.path.basename(_dst))
            except Exception:
                traceback.print_exc()
            t['重置前归档'] = _archived
        hist = t.get('状态历史') or []
        hist.append({'状态': st, '时间': now(),
                     '原因': ('人工重置（已完成→重做）' if st == '已完成'
                             else '人工重置（疑似僵死）')})
        t['状态历史'] = hist
        t['状态'] = '待处理'
        t['重置时间'] = now()
        t['重置次数'] = int(t.get('重置次数') or 0) + 1
        t['唤醒次数'] = 0        # 重置后重新计数，允许再重发
        t.pop('开始时间', None)
        t.pop('最后活动', None)
        t.pop('完成时间', None)
        if not atomic_write_json(fp, t):
            return self._err(500, '写回任务失败')

        woke, winfo = False, None
        if params.get('唤醒', True):
            try:
                import wake as wake_mod
                woke, winfo = wake_mod.wake(
                    '【自动唤醒·重置后继续】项目「%s」的任务 %s（%s）已由用户重置为待处理。'
                    '【第一步必做】调接口 POST /api/run '
                    '{"任务":"stopcheck","参数":{"id":"%s"}}，返回 "已停止": true 就停手。'
                    '请从上次中断处继续复核（已 OCR 的页有缓存），完成后写入 '
                    '项目\\%s\\复核报告\\。'
                    % (t.get('项目') or '', tid, t.get('投标人') or '', tid,
                       t.get('项目') or ''), project=t.get('项目') or '')
            except Exception as e:
                traceback.print_exc()
                winfo = '唤醒调用异常：%r' % e
        try:
            _p = {'唤醒次数': int(t.get('唤醒次数') or 0) + (1 if woke else 0)}
            if woke:
                _p['唤醒时间'] = now()
            task_patch(tid, **_p)
        except Exception:
            pass
        if st == '已完成' and t.get('重置前归档'):
            winfo = (winfo or '') + '（旧结果已归档到 _历史：%s）' % '、'.join(t['重置前归档'])
        return self._send(200, {'ok': True, 'id': tid, '状态': '待处理',
                                '重置次数': t['重置次数'], '已唤醒': woke,
                                '旧结果已归档': t.get('重置前归档') or [],
                                '唤醒说明': winfo})

    def _queue_resume(self, params):
        """继续任务：把「已停止/已取消/失败」的复核任务恢复为「待处理」，并可唤醒 agent"""
        tid = (params.get('id') or '').strip()
        if not tid:
            return self._err(400, '缺少 id 参数')
        fp = os.path.join(QUEUE, tid + '.json')
        if not os.path.isfile(fp):
            return self._err(404, '任务不存在：%s' % tid)
        try:
            with open(fp, encoding='utf-8') as fh:
                t = json.load(fh)
        except Exception as e:
            return self._err(500, '读取任务失败：%r' % e)

        st = t.get('状态') or ''
        if st not in ('已停止', '已取消', '失败'):
            return self._err(400, '任务当前状态为「%s」，无需继续（只有已停止/已取消/失败 可以继续）'
                             % st)

        hist = t.get('状态历史') or []
        hist.append({'状态': st, '时间': t.get('停止时间') or t.get('完成时间') or now(),
                     '原因': t.get('停止原因') or ''})
        t['状态历史'] = hist
        t['状态'] = '待处理'
        t['恢复时间'] = now()
        t['恢复次数'] = int(t.get('恢复次数') or 0) + 1
        t.pop('停止时间', None)
        t.pop('停止原因', None)
        t.pop('完成时间', None)
        t.pop('失败原因', None)
        if not atomic_write_json(fp, t):
            return self._err(500, '写回任务失败')

        woke, winfo = False, None
        if params.get('唤醒', True):
            try:
                import wake as wake_mod
                woke, winfo = wake_mod.wake(
                    '【自动唤醒·继续任务】项目「%s」的复核任务已由用户恢复：%s（任务 %s）。'
                    '【第一步必做】调接口 POST /api/run '
                    '{"任务":"stopcheck","参数":{"id":"%s"}}，返回 "已停止": true 就停手。'
                    '请从上次中断处继续复核（已 OCR 的页有缓存，不必重跑），'
                    '完成后把结果写入 项目\\%s\\复核报告\\。'
                    % (t.get('项目') or '', t.get('投标人') or '', tid, tid,
                       t.get('项目') or ''), project=t.get('项目') or '')
            except Exception as e:
                traceback.print_exc()
                winfo = '唤醒调用异常：%r' % e

        return self._send(200, {'ok': True, 'id': tid, '状态': '待处理',
                                '恢复次数': t['恢复次数'], '已唤醒': woke,
                                '唤醒说明': winfo,
                                '说明': '任务已恢复为「待处理」'
                                        + ('，已唤醒 agent 继续处理' if woke
                                           else '，但唤醒未成功，需在对话里发一句话唤醒')})

    def _submit_stop(self, params):
        """中止正在运行的复核抽取批次"""
        bid = (params.get('批次') or '').strip()
        if not bid:
            return self._err(400, '缺少批次参数')
        with SUBMIT_LOCK:
            st = SUBMIT_STATE.get(bid)
            if not st:
                return self._err(404, '批次不存在或已过期：%s' % bid)
            if st.get('完成'):
                return self._send(200, {'ok': True, '已完成': True,
                                        '说明': '该批次已结束，无需停止'})
            st['停止请求'] = True
            st['停止请求时间'] = now()
        return self._send(200, {'ok': True, '批次': bid,
                                '说明': '已发送停止请求，正在中断当前单位处理'})

    def _submit_request(self):
        raw = self._body()
        try:
            req = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception as e:
            return self._err(400, 'JSON 解析失败：%s' % e)
        pname = (req.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        rules, rpath = load_project_rules(pname)
        if rules is None:
            return self._err(404, '项目「%s」尚未保存评分标准' % pname)

        bdir = os.path.join(pd, '投标文件')
        units = {}
        if os.path.isdir(bdir):
            for d in sorted(os.listdir(bdir)):
                dp = os.path.join(bdir, d)
                if not os.path.isdir(dp) or d.endswith('.pages'):
                    continue
                fs = pick_bid_files(dp)
                if fs:
                    units[d] = fs   # 多文件：整个列表；单文件仍是列表（长度 1）
        want = (req.get('投标单位') or '').strip()
        redo = bool(req.get('重新复核'))
        rdir = os.path.join(pd, '复核报告')
        if want:
            units = {k: v for k, v in units.items() if k == want}
            if not units:
                return self._err(404, '未找到投标单位「%s」的投标文件' % want)
        if not units:
            return self._err(400, '项目「%s」下尚无投标文件，请先上传' % pname)

        # ---- 闸门（2026-11-02）：一次只复核一家 / 同一家不重复 / 不许两家并行 ----
        # M1：闸门检查 + inflight 占位包成临界区，防两次并发 POST 同时通过
        with SUBMIT_GATE_LOCK:
            _code, _msg, _want = submit_gate(pname, units, rdir, want, redo)
            if _code:
                return self._err(_code, _msg)
            want = _want
            units = {want: units[want]}

            os.makedirs(QUEUE, exist_ok=True)
            # 单家提交时无需再算「跳过」（前面已拦掉有结果的单位）
            _skip_pre = []
            _bid = submit_new(pname, list(units))
            for _u in list(units):
                submit_inflight_begin(pname, _u, _bid)   # G2：预处理期间占位
        _thr = threading.Thread(
            target=self._submit_worker,
            args=(_bid, pname, dict(units), rpath, dict(req), rdir),
            daemon=True)
        _thr.start()
        return self._send(200, {
            'ok': True, '异步': True, '批次': _bid, '单位数': len(units),
            '跳过': [{'投标单位': u, '原因': '已有复核结果'} for u in _skip_pre],
            '提示': '已开始抽取投标文件，可在界面查看进度'})

    # ---------- 核对「出处」标注是否与实际来源一致 ----------
    def _audit_citations(self, params):
        pname = (params.get('项目') or '').strip()
        fix = bool(params.get('修正'))
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        rd = os.path.join(pd, '复核报告')
        bdir = os.path.join(pd, '投标文件')
        rows, bad = [], 0
        for fp in walk_files(rd, '_复核结果.json') if os.path.isdir(rd) else []:
            try:
                with open(fp, encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            unit = d.get('投标人') or ''
            # G10：按册加载正文（多册项目每册页码独立，不能只用第一册判断）
            # M35（v1.9.16）：册名口径＝**文件名去扩展名**。PDF/Word 的正文名是
            # `技术册.pdf.txt`，splitext 只去 `.txt` 会得到 `技术册.pdf`，与结果里
            # 按口径写的 `技术册p12` 对不上 → 取不到正文 → 文字层页被误报「实际 OCR」。
            # 现在同一份正文同时登记 `技术册.pdf` 与 `技术册` 两个键：新口径可用，
            # 历史结果里的带扩展写法也照样查得到。
            vol_lines = {}
            up = os.path.join(bdir, unit)
            if os.path.isdir(up):
                for x in sorted(os.listdir(up)):
                    if x.endswith('.txt') and not x.endswith('.pages.txt'):
                        vol = os.path.splitext(x)[0]
                        names = [vol]
                        _low = vol.lower()
                        for _ext in ('.pdf', '.docx', '.doc'):
                            if _low.endswith(_ext):
                                names.append(vol[:-len(_ext)])
                                break
                        try:
                            with open(os.path.join(up, x), encoding='utf-8') as f:
                                _ls = f.read().split('\n')
                        except Exception:
                            continue
                        for _nm in names:
                            vol_lines[_nm] = _ls
            if not vol_lines:
                continue
            lines = next(iter(vol_lines.values()))

            def has_text(pg, vol=None):
                ls = vol_lines.get(vol) if vol else lines
                if not ls:
                    return False
                if 1 <= pg <= len(ls):
                    return len((ls[pg - 1] or '').strip()) > 40
                return False

            changed = False
            for it in (d.get('评分') or []):
                cit = as_text(it.get('出处'))
                if not cit:
                    continue
                for m in re.finditer(
                        r'(?P<vol>[^\s、，,]*?)p(?P<a>\d+)(?:\s*-\s*(?P<b>\d+))?（(?P<lab>文字层|OCR)）',
                        cit):
                    a = int(m.group('a'))
                    b = int(m.group('b')) if m.group('b') else a
                    vol = m.group('vol')
                    lab = m.group('lab')
                    for pg in range(a, b + 1):
                        real = '文字层' if has_text(pg, vol) else 'OCR'
                        if real != lab:
                            bad += 1
                            rows.append({'投标人': unit, '序号': it.get('序号'),
                                         '页': pg, '标注': lab, '实际': real})
                # 未按规范逐页标注的，给出提示
                if not re.search(r'（(文字层|OCR)', cit):
                    rows.append({'投标人': unit, '序号': it.get('序号'),
                                 '页': '—', '标注': '未标注来源',
                                 '实际': '建议改为 pN（文字层）/pN（OCR）'})
            if fix and not rows:
                pass
            if fix:
                # G10：只重写「册名?pN（文字层|OCR）」片段里的来源标签，
                # 保留册名前缀与描述文字，不再整串覆盖（否则会丢掉册名）。
                _cite_re = re.compile(
                    r'(?P<vol>[^\s、，,]*?)p(?P<a>\d+)(?:\s*-\s*(?P<b>\d+))?（(?:文字层|OCR)）')

                def _fix_label(m):
                    vol = m.group('vol') or ''
                    a = int(m.group('a'))
                    b = int(m.group('b')) if m.group('b') else a
                    if b < a:
                        a, b = b, a
                    labs = ['文字层' if has_text(pg, vol) else 'OCR'
                            for pg in range(a, b + 1)]
                    if len(set(labs)) == 1:
                        rng = ('p%d-%d' % (a, b)) if b > a else ('p%d' % a)
                        return '%s%s（%s）' % (vol, rng, labs[0])
                    return '、'.join(
                        '%sp%d（%s）' % (vol, pg,
                                        '文字层' if has_text(pg, vol) else 'OCR')
                        for pg in range(a, b + 1))

                for it in (d.get('评分') or []):
                    cit = as_text(it.get('出处'))
                    if not cit:
                        continue
                    new = _cite_re.sub(_fix_label, cit)
                    if new != cit:
                        it['出处'] = new
                        changed = True
            if fix and changed:
                try:
                    atomic_write_json(fp, d)
                except Exception:
                    traceback.print_exc()
        return self._send(200, {
            'ok': True, '任务': 'audit_citations', '项目': pname,
            '不符条数': bad, '明细': rows[:80],
            '已修正': bool(fix),
            '规范': '出处须逐页标注来源：p76（文字层）、p79（OCR）；'
                    '连续同类页可合并为 p444-455（OCR）'})

    # ---------- 当前 OCR 后端（界面徽标用） ----------
    def _engine_info(self):
        """一次说清：现在跑的是哪个推理后端、什么版本、几路并发、吞吐多少。

        引擎是进程级配置（启动时由 OCR_ENGINE 决定），与「某次预热任务的状态」
        不是一回事，所以单独一个 /api/engine，不塞进 /api/prewarm。
        """
        _svc = self._service_version()
        _wk = {}
        try:
            import wake as _wake
            _sid, _src, _title = _wake.resolve_session()
            _wk = {'sessionId': _sid, '来源': _src, '标题': _title}
        except Exception as e:
            _wk = {'错误': repr(e)[:120]}
        try:
            import ocr_text as _ot
        except Exception as e:
            return self._send(200, {'ok': False, '错误': repr(e)})
        ov = (getattr(_ot, '_ENGINE_NAME', 'onnxruntime') == 'openvino')
        ver = ''
        try:
            if ov:
                import openvino
                ver = getattr(openvino, '__version__', '')
            else:
                import onnxruntime
                ver = onnxruntime.__version__
        except Exception:
            ver = ''
        device = (os.environ.get('OCR_OV_DEVICE') or 'CPU').upper() if ov else 'CPU'
        try:
            # 2026-11-02：默认 2（原 3）。每线程各自的 InferRequest 会追加提交内存，
            # 3 线程在 8 GB 机器上叠加其它 python 进程容易触顶，触发
            # 「python.exe 无法启动 0xc0000142」。要更快可显式 set OCR_PREWARM_WORKERS=3。
            workers = int(os.environ.get('OCR_PREWARM_WORKERS') or 2)
        except Exception:
            workers = 2
        # 最近一次完成的预热（按完成时间取最新，不是取最大值）；没有就回落落盘记录
        last, src = None, '本次进程'
        for st in (_PREWARM_STATE or {}).values():
            if not (isinstance(st, dict) and st.get('状态') == '完成'
                    and st.get('页每分钟')):
                continue
            if last is None or str(st.get('完成时间') or '') > str(last.get('完成时间') or ''):
                last = st
        if last is None:
            try:
                with open(OCR_STATS, encoding='utf-8') as fh:
                    last = json.load(fh)
                src = '落盘记录'
            except Exception:
                last = None
        recent = last.get('页每分钟') if last else None
        # S8（v1.8.0）：参考吞吐不再写死，优先用"本机最近一次真实预热"的数字。
        # 只有拿不到落盘记录（或引擎对不上）时才回落到出厂常量，并注明来源。
        _DEFAULT_TPUT = 51.6 if ov else 16.0
        _ref, _ref_src = _DEFAULT_TPUT, '出厂常量（尚无本机实测）'
        try:
            _tput = float(recent) if recent else None
        except Exception:
            _tput = None
        if _tput and _tput > 0:
            _eng_last = str((last or {}).get('引擎') or '').lower()
            if (not _eng_last) or (_eng_last == ('openvino' if ov else 'onnxruntime')):
                _ref, _ref_src = round(_tput, 1), '本机最近一次实测'
            else:
                _ref_src = ('出厂常量（最近一次实测用的是 %s，与当前引擎不同）'
                            % _eng_last)
        return self._send(200, {
            'ok': True,
            '引擎': 'openvino' if ov else 'onnxruntime',
            '显示': 'OpenVINO' if ov else 'ONNX Runtime',
            '版本': ver, '设备': device, '并发': workers,
            '服务端版本': _svc.get('版本'), '服务端构建': _svc.get('构建'),
            '最近预热页每分': round(float(recent), 1) if recent else None,
            '最近预热单位': (last or {}).get('投标单位') or (last or {}).get('单位'),
            '最近预热时间': (last or {}).get('完成时间') or (last or {}).get('时间'),
            '最近预热来源': src if last else None,
            '参考页每分': _ref,
            '参考页每分来源': _ref_src,
            '唤醒目标': _wk.get('sessionId'), '唤醒来源': _wk.get('来源'),
            '唤醒标题': _wk.get('标题'),
            '说明': '切换引擎后缓存按引擎分开，旧缓存不会命中，会重跑一次'})

    # ---------- 服务端版本 ----------
    def _version_info(self):
        """版本 + 两套构建标识 + 契约号 + 进程信息。

        回答三个问题：
          1) 跑的是哪一版（版本 / 构建）
          2) 磁盘上的服务端代码是否比进程新（服务端需重启？）
          3) 界面需要的接口契约与当前服务端是否匹配
        """
        out = {'ok': True, '服务': '评分复核服务（pipeline/server.py）',
               '端口': self.server.server_address[1] if getattr(self, 'server', None) else None,
               '进程': os.getpid(), 'Python': sys.version.split()[0]}
        out.update(self._service_version())
        # 内存水位（2026-11-02）：0xc0000142 的根因是提交内存见底，
        # 把水位暴露出来，出问题时能直接看出是不是它。
        try:
            import ocr_text as _ot
            ch = _ot.commit_headroom_mb()
            out['提交可用MB'] = ch
            out['本进程提交MB'] = _ot.own_commit_mb()
            if ch is not None and ch < 900:
                out['内存告警'] = ('提交内存只剩 %d MB，新的 python 进程可能无法启动'
                                   '（0xc0000142）。请关闭大程序或重启服务端。' % ch)
        except Exception:
            pass
        boot = globals().get('_BOOT_TS')
        if boot:
            out['进程启动'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(boot))
            out['进程启动时间戳'] = round(boot, 0)
            out['已运行秒'] = round(time.time() - boot, 1)
            # 磁盘上的服务端代码比进程新 → 改了没重启
            m = out.get('服务端修改时刻')
            out['服务端需重启'] = bool(m and boot and m > boot + 1)
            if out['服务端需重启']:
                out['服务端提示'] = ('服务端代码已更新（%s），但进程是 %s 启动的；'
                                     '请运行 stop_server.cmd → start_server.cmd'
                                     % (out.get('服务端构建'),
                                        out.get('进程启动')))
        return self._send(200, out)

    @staticmethod
    def _service_version():
        try:
            import version as _ver
            return _ver.info(HERE)
        except Exception as e:
            return {'版本': None, '构建': None, '错误': repr(e)}

    # ---------- 手动触发 OCR 预热 ----------
    def _ocr_files(self, params):
        """对勾选的文件逐个 抽正文→导页→预热 OCR（多文件 OCR 的核心）。

        参数：项目、投标单位、文件（文件名列表；缺省=该单位全部文件）。
        未勾选的文件不 OCR；返回逐文件处理结果，供界面回显。
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        want = params.get('文件') or params.get('册') or []
        if isinstance(want, str):
            want = [x.strip() for x in want.split(',') if x.strip()]
        if not pname or not unit:
            return self._err(400, '缺少「项目」或「投标单位」参数')
        pd = project_dir_for(pname)
        up = os.path.join(pd, '投标文件', unit) if pd else None
        files = pick_bid_files(up)
        if not files:
            return self._err(404, '项目「%s」下投标单位「%s」没有投标文件'
                             % (pname, unit))
        if want:
            by_name = {os.path.basename(x): x for x in files}
            picked = []
            for w in want:
                w = w.strip()
                fp = by_name.get(w)
                if not fp:
                    fp = next((x for x in files
                               if os.path.splitext(os.path.basename(x))[0] == w),
                              None)
                if fp:
                    picked.append(fp)
        else:
            picked = files
        started = []
        for fp in picked:
            base = os.path.basename(fp)
            vol = os.path.splitext(base)[0]
            steps = []
            txt = fp + '.txt'
            # 抽正文（若缺）
            if not os.path.isfile(txt):
                try:
                    with open(fp, 'rb') as fh:
                        head = fh.read(8)
                    if head[:4] == b'PK\x03\x04':
                        import docx_text as m
                    elif head[:5] == b'%PDF-':
                        import pdf_text as m
                    else:
                        import doc_text as m
                    r = m.extract(fp)
                    body = r.pop('正文', '')
                    with open(txt, 'w', encoding='utf-8') as fh:
                        fh.write(body)
                    steps.append('抽取正文 %d 字' % len(body))
                except Exception as e:
                    traceback.print_exc()
                    steps.append('抽取正文失败：%r' % e)
            else:
                steps.append('正文已存在')
            # 导页（若缺）
            pdir = fp + '.pages'
            # M10：复用需认 .pages.json 索引，否则半套旧图被永久复用
            if fp.lower().endswith('.pdf') and \
                    not (os.path.isdir(pdir)
                         and os.path.isfile(fp + '.pages.json')):
                try:
                    import pdf_images
                    idx = pdf_images.export(fp, pdir, only_blank_pages=True)
                    with open(fp + '.pages.json', 'w', encoding='utf-8') as fh:
                        json.dump(idx, fh, ensure_ascii=False, indent=1)
                    steps.append('导出扫描页 %d 张' % idx.get('图片数', 0))
                except Exception as e:
                    traceback.print_exc()
                    steps.append('导出扫描页失败：%r' % e)
            elif os.path.isdir(pdir):
                steps.append('扫描页已存在')
            # 预热 OCR：cap=0 表示“全部缺文字层的页”（勾选即全跑，不挑锚点）
            n = start_prewarm('ocr-%s-%s' % (unit, vol), unit,
                              txt if os.path.isfile(txt) else None,
                              pdir if os.path.isdir(pdir) else None,
                              cap=0)
            if n:
                steps.append('后台 OCR %d 页' % n)
            started.append({'文件': base, '册名': vol, '预热页数': n,
                            '处理': steps})
        return self._send(200, {'ok': True, '任务': 'ocr_files',
                                '投标单位': unit, '已启动': started,
                                '说明': '后台识别中，可用 /api/prewarm 看进度；'
                                        '未勾选的文件不会 OCR。'})

    def _ocr_prewarm(self, params):
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        cap = params.get('上限')
        cap = _PREWARM_DEFAULT_CAP if cap is None else cap
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        bdir = os.path.join(pd, '投标文件') if pd else None
        if not bdir or not os.path.isdir(bdir):
            return self._err(404, '项目「%s」没有投标文件' % pname)
        units = [unit] if unit else sorted(
            d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d)))
        started = []
        for u in units:
            up = os.path.join(bdir, u)
            if not os.path.isdir(up):
                continue
            bids = pick_bid_files(up)
            if not bids:
                continue
            extra = []
            rf = os.path.join(pd, '复核报告', u + '_复核结果.json')
            if os.path.isfile(rf):
                try:
                    with open(rf, encoding='utf-8') as f:
                        d = json.load(f)
                    for it in (d.get('评分') or []):
                        for m in re.finditer(r'p(\d+)', str(it.get('出处') or '')):
                            extra.append(int(m.group(1)))
                except Exception:
                    pass
            for bid in bids:
                vol = os.path.splitext(os.path.basename(bid))[0]
                txt = bid + '.txt'
                pdir = bid + '.pages'
                n = start_prewarm('manual-' + u + '-' + vol, u,
                                  txt if os.path.isfile(txt) else None,
                                  pdir if os.path.isdir(pdir) else None,
                                  extra_pages=extra, cap=cap)
                started.append({'投标单位': u, '文件': os.path.basename(bid),
                                '预热页数': n})
        return self._send(200, {'ok': True, '任务': 'ocr_prewarm',
                                '已启动': started,
                                '说明': '后台识别中，可用 /api/prewarm 查看进度；'
                                        '识别结果写入各页 .ocr_cache'})

    # ---------- 清理扫描页图片 ----------
    def _clean_pages(self, params):
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        bdir = os.path.join(pd, '投标文件')
        targets = []
        if os.path.isdir(bdir):
            for u in sorted(os.listdir(bdir)):
                if unit and u != unit:
                    continue
                up = os.path.join(bdir, u)
                if not os.path.isdir(up):
                    continue
                for d in os.listdir(up):
                    dp = os.path.join(up, d)
                    if os.path.isdir(dp) and d.endswith('.pages'):
                        targets.append((u, dp))
        if not targets:
            return self._send(200, {'ok': True, '任务': 'clean_pages',
                                    '已清理': [], '释放MB': 0,
                                    '说明': '没有找到扫描页目录'})
        freed = 0
        removed = []
        for u, dp in targets:
            sz = 0
            n = 0
            for f in os.listdir(dp):
                fp = os.path.join(dp, f)
                if os.path.isfile(fp):
                    sz += os.path.getsize(fp)
                    n += 1
            try:
                shutil.rmtree(dp)
                freed += sz
                removed.append({'投标单位': u, '目录': os.path.basename(dp),
                                '张数': n, 'MB': round(sz / 1048576.0, 1)})
            except Exception as e:
                traceback.print_exc()
        return self._send(200, {'ok': True, '任务': 'clean_pages',
                                '已清理': removed,
                                '释放MB': round(freed / 1048576.0, 1),
                                '说明': '扫描页已删除；下次复核该单位时会重新导出'
                                        '（约 30~60 秒/份）'})

    # ---------- 生成项目要点骨架 ----------
    def _gen_points(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        rules, rpath = load_project_rules(pname)
        if rules is None:
            return self._err(404, '项目「%s」尚未保存评分标准' % pname)
        import autoscore
        pd = project_dir_for(pname)
        items = []
        total = 0.0
        for g in rules.get('分组') or []:
            for it in g.get('项') or []:
                v = it.get('分值')
                if isinstance(v, (int, float)):
                    total += v
                items.append((g.get('名称'), it))
        L = []
        L.append('# 项目要点 —— %s\n' % pname)
        L.append('> 本文件由程序按评分标准原文自动生成：计分规则、必须提供、'
                 '本项注意均从原文抽取；标注「原文未写明」的，表示评分标准里'
                 '确实没有对应表述，需人工补充。\n')
        L.append('> 通用方法见程序目录下《复核方法.md》；'
                 '跨项目通用口径见《通用口径.md》（已自动嵌入本文件第〇节）。')
        L.append('> **本项目内多家投标人：每家独立判定，不参考其他投标人的结果。**\n')
        L.append('---\n')
        # ===== 〇、通用口径（全局共享，自动嵌入） =====
        _common_fp = os.path.join(HERE, '通用口径.md')
        if os.path.isfile(_common_fp):
            try:
                _ctxt = open(_common_fp, encoding='utf-8').read()
            except Exception:
                _ctxt = ''
            if _ctxt.strip():
                L.append('## 〇、通用口径（跨项目复用，自动生成）\n')
                L.append('> 本节来自程序目录的《通用口径.md》，对所有项目生效。'
                         '如需修改判断，改那份文件后重新生成要点即可。\n')
                # 去掉原文件的一级标题，降级嵌入
                _lines = _ctxt.split('\n')
                _skipped = False
                for _ln in _lines:
                    if (not _skipped) and _ln.startswith('# '):
                        _skipped = True
                        continue
                    # 二/三级标题降一级，避免与本文档层级冲突
                    if _ln.startswith('### '):
                        _ln = '#### ' + _ln[4:]
                    elif _ln.startswith('## '):
                        _ln = '### ' + _ln[3:]
                    L.append(_ln)
                L.append('')
                L.append('---\n')
        L.append('## 一、评分标准总览\n')
        L.append('| 项目 | 内容 |')
        L.append('|---|---|')
        L.append('| 总分 | **%s 分** |' % (round(total, 2)))
        L.append('| 项数 | %d 项 |' % len(items))
        L.append('| 分组 | %s |' % '、'.join(
            (g.get('名称') or '') for g in (rules.get('分组') or [])))
        L.append('')
        L.append('| 序 | 评分项 | 分值 |')
        L.append('|---|---|---|')
        for gname, it in items:
            L.append('| %s | %s | %s |' % (it.get('序号'), it.get('名称'),
                                           it.get('分值')))
        L.append('')
        L.append('---\n')
        # 汇总已复核结果里的「出处」，用于回填证据位置
        evidence_places = {}
        _rd = os.path.join(pd, '复核报告') if pd else None
        if _rd and os.path.isdir(_rd):
            for _fp in walk_files(_rd, '_复核结果.json'):
                try:
                    with open(_fp, encoding='utf-8') as _f:
                        _d = json.load(_f)
                except Exception:
                    continue
                _unit = (_d.get('投标人') or '').strip()
                for _it in (_d.get('评分') or []):
                    _k = str(_it.get('序号') or '')
                    _v = as_text(_it.get('出处')).strip()
                    if _k and _v:
                        evidence_places.setdefault(_k, [])
                        _pair = (_unit, _v)
                        if _pair not in evidence_places[_k]:
                            evidence_places[_k].append(_pair)
        L.append('## 二、逐项口径与证据位置\n')
        for gname, it in items:
            nm = it.get('名称') or ''
            txt = (it.get('评分标准') or '').strip()
            seq = str(it.get('序号') or '')
            L.append('### 项%s %s（%s 分）\n' % (seq, nm, it.get('分值')))
            parts = split_requirements(txt)
            cond = {}
            if txt:
                try:
                    cond = autoscore.mine(txt)
                except Exception:
                    cond = {}
            rs = rule_summary(cond)
            # 规则句取最短的一条（长句往往混着条件描述）
            _rules = sorted(parts['计分规则'], key=len) if parts['计分规则'] else []
            _r0 = _rules[0] if _rules else ''
            if rs and _r0:
                L.append('- **计分规则**：%s\n  - 原文表述：%s' % (rs, _r0))
            elif rs:
                L.append('- **计分规则**：%s' % rs)
            elif _r0:
                L.append('- **计分规则**：%s' % _r0)
            else:
                L.append('- **计分规则**：原文未写明计分方式，需人工确认')
            # 必须提供
            if parts['必须提供']:
                L.append('- **必须提供**：')
                for x in parts['必须提供']:
                    L.append('  - %s' % x)
            else:
                L.append('- **必须提供**：原文未写明须提供何种材料，需人工确认')
            # 本项注意
            if parts['本项注意']:
                L.append('- **本项注意**：')
                for x in parts['本项注意']:
                    L.append('  - %s' % x)
            else:
                L.append('- **本项注意**：原文未写明限制条件')
            # 证据位置（复核完成后回填）
            places = evidence_places.get(seq) or []
            if places:
                _same = len({p[1] for p in places}) == 1
                if _same:
                    L.append('- **证据位置**：%s（%d 家复核结果的出处一致）'
                             % (places[0][1], len(places)))
                else:
                    seg_txt = '；'.join('%s→%s' % (u or '未知单位', v)
                                        for u, v in places[:6])
                    L.append('- **证据位置**：%s' % seg_txt)
            else:
                L.append('- **证据位置**：尚未复核，复核完成后自动回填')
            L.append('- **原文（备查）**：%s'
                     % (txt if txt else '（评分标准中未填写文本）'))
            L.append('')
        L.append('---\n')
        L.append('## 三、本项目特有的坑\n')
        # 重新生成时保留上一次手写的第三节内容（该节程序无法推导）
        _kept = _read_kept_section(pd, '## 三、', ('## 四、',))
        if _kept:
            L.append(_kept)
            L.append('')
            L.append('> 本节为手工维护内容，重新生成要点时自动保留。\n')
            L.append('---\n')
            _skip_placeholder = True
        else:
            _skip_placeholder = False
        if not _skip_placeholder:
            L.append('（以下为本项目特有、无法从评分标准文本直接推出的经验判断，'
                     '由复核人补充）\n')
            L.append('**【待补】** 逐条列出（示例：奖项地方名称的级别归属、'
                     '评审计数规则、社保月份、职称门槛是"高级"还是"副高级"等）\n')
        L.append('---\n')
        # 已复核结果
        rdir = os.path.join(pd, '复核报告') if pd else None
        rows = []
        if rdir and os.path.isdir(rdir):
            for fp in walk_files(rdir, '_复核结果.json'):
                try:
                    with open(fp, encoding='utf-8') as f:
                        d = json.load(f)
                    rows.append((d.get('投标人'), d.get('小计'),
                                 d.get('满分'), d.get('未决项') or [],
                                 d.get('复核时间') or ''))
                except Exception:
                    pass
        if rows:
            rows.sort(key=lambda x: (x[1] if isinstance(x[1], (int, float)) else -1),
                      reverse=True)
            L.append('## 四、已复核结果（仅供参考，不作判分依据）\n')
            L.append('| 投标单位 | 得分 | 满分 | 未决项 | 复核时间 |')
            L.append('|---|---|---|---|---|')
            for u, s, fu, und, t in rows:
                L.append('| %s | %s | %s | %s | %s |'
                         % (u, s, fu, '、'.join(und) if und else '无', t))
            L.append('')
            L.append('> 复核新投标人时，**不要参考上表结论**，逐项独立取证。')
        else:
            L.append('## 四、已复核结果\n')
            L.append('（暂无）')

        out = os.path.join(pd, '项目要点.md')
        existed = os.path.isfile(out)
        if existed:
            # 覆盖前备份，避免冲掉可能的手写内容
            backup_once(out)
        with open(out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(L))
            f.flush()
            os.fsync(f.fileno())
        # 清理历史遗留的 _骨架 文件，避免用户看到陈旧副本
        stale = os.path.join(pd, '项目要点_骨架.md')
        cleaned = False
        if os.path.isfile(stale):
            try:
                os.remove(stale)
                cleaned = True
            except Exception:
                traceback.print_exc()
        return self._send(200, {
            'ok': True, '任务': 'gen_points', '项目': pname,
            '评分项数': len(items), '总分': round(total, 2),
            '已覆盖': existed, '备份': (out + '.bak') if existed else None,
            '已清理旧骨架': cleaned, '文件': out,
            '说明': ('已重新生成 项目要点.md（旧文件备份为 项目要点.md.bak）'
                     if existed else '已生成 项目要点.md')})

    # ---------- 归档：生成项目复核汇总 ----------
    def _archive(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        rdir = os.path.join(pd, '复核报告') if pd else None
        if not rdir or not os.path.isdir(rdir):
            return self._err(404, '项目「%s」没有复核报告目录' % pname)
        files = walk_files(rdir, '_复核结果.json')
        if not files:
            return self._err(400, '项目「%s」下暂无复核结果' % pname)

        # G4：先看项目下共有多少家投标单位，汇总里要如实写出"未复核"
        _all_units = []
        _bdir = os.path.join(pd, '投标文件')
        if os.path.isdir(_bdir):
            for _d in sorted(os.listdir(_bdir)):
                _dp = os.path.join(_bdir, _d)
                if os.path.isdir(_dp) and not _d.endswith('.pages'):
                    _fs = [x for x in os.listdir(_dp)
                           if not x.endswith(('.txt', '.json'))
                           and os.path.isfile(os.path.join(_dp, x))]
                    if _fs:
                        _all_units.append(_d)
        units = []
        for fp in sorted(files):
            try:
                with open(fp, encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            units.append({
                'unit': d.get('投标人') or os.path.basename(fp).replace('_复核结果.json', ''),
                'score': d.get('小计'), 'full': d.get('满分'),
                'und': d.get('未决项') or [], 'time': d.get('复核时间') or '',
                '结论': d.get('结论') or '', '说明': d.get('复核说明') or '',
                'src': d.get('评分标准来源') or '',
                '评分': d.get('评分') or []})
        units.sort(key=lambda x: (x['score'] if isinstance(x['score'], (int, float))
                                  else -1), reverse=True)

        # ===== 权重折算（v1.7.1）=====
        # 只有在「评分标准」里勾了「启用权重折算」才生效；否则 _won=False，下面全部跳过。
        _rules, _rpath = load_project_rules(pname)
        _won = bool(_rules and _rules.get('权重折算'))
        _groups = []
        if _won:
            for _g in (_rules.get('分组') or []):
                if _g.get('参与折算') is False:
                    continue
                try:
                    _wv = float(_g.get('权重'))
                except Exception:
                    continue
                if _wv <= 0:
                    continue
                _items, _fullsum = [], 0.0
                for _it in (_g.get('项') or []):
                    _nm = (_it.get('名称') or '').strip()
                    if not _nm:
                        continue
                    try:
                        _fv = float(_it.get('分值') or 0)
                    except Exception:
                        _fv = 0.0
                    _items.append((_nm, _fv))      # (名称, 分值)：同名项靠分值区分
                    _fullsum += _fv
                _groups.append({'名称': _g.get('名称') or '', '权重': _wv,
                                '项': _g.get('项') or [],
                                '项名': _items, '项分': round(_fullsum, 2)})

        def _fz_of(u):
            """返回 (折算得分合计, 折算满分合计, 分部明细)"""
            tot_s = tot_f = 0.0
            per = []
            used = set()
            for gr in _groups:
                gs = gf = 0.0
                hit_n = 0
                # 一对一配对：同名项按分值/前缀区分，避免同名项被两组重复计入
                for _r_it, hit in pair_group(gr, u.get('评分') or [], used):
                    if hit is None:
                        continue
                    try:
                        sv = float(hit.get('得分') or 0)
                    except Exception:
                        sv = 0.0
                    try:
                        fv = float(hit.get('分值') or 0)
                    except Exception:
                        fv = 0.0
                    gs += sv
                    gf += fv
                    hit_n += 1
                per.append({'分组': gr['名称'], '权重': gr['权重'],
                            '得分': round(gs, 2), '满分': round(gf, 2),
                            '折算得分': round(gs * gr['权重'] / 100.0, 2),
                            '折算满分': round(gf * gr['权重'] / 100.0, 2),
                            '匹配项数': hit_n})
                tot_s += gs * gr['权重'] / 100.0
                tot_f += gf * gr['权重'] / 100.0
            return round(tot_s, 2), round(tot_f, 2), per

        _fz = {}
        if _won:
            for u in units:
                _fz[u['unit']] = _fz_of(u)
            # 启用折算后按折算得分排序（排名要反映折算后的高低）
            units.sort(key=lambda x: _fz[x['unit']][0], reverse=True)
        _fz_full = round(sum(v[1] for v in _fz.values()) / max(1, len(_fz)), 2) \
            if _fz else 0

        def short(t, n):
            t = (t or '').replace('\n', ' ').replace('|', '/').strip()
            return t if len(t) <= n else t[:n] + '…'

        # 评分项名（取并集，保持顺序）
        names = []
        for u in units:
            for it in u['评分']:
                nm = it.get('名称') or ''
                if nm and nm not in names:
                    names.append(nm)
        full_map = {}
        for u in units:
            for it in u['评分']:
                nm = it.get('名称') or ''
                if nm and nm not in full_map and it.get('分值'):
                    full_map[nm] = it['分值']
        total_full = sum(full_map.values()) if full_map else (units[0]['full'] if units else 0)
        _done_units = set(u['unit'] for u in units)
        _missing = [u for u in _all_units if u not in _done_units]

        L = []
        # ===== 文档头 =====
        if _won:
            _rule_full = round(sum(g['项分'] * g['权重'] / 100.0
                                   for g in _groups), 2)
            _rule_full = ('%g' % _rule_full)
            L.append('# 复核结果汇总（客观分 %s 分制 · 不含主观与价格分）'
                     % _rule_full)
        else:
            L.append('# 复核结果汇总')
        L.append('')
        L.append('| 项目 | 内容 |')
        L.append('|---|---|')
        L.append('| 项目名称 | %s |' % pname)
        L.append('| 生成时间 | %s |' % now())
        L.append('| 投标单位数 | 已复核 %d 家 / 共 %d 家%s |'
                 % (len(units), len(_all_units) or len(units),
                    ('（**未复核：%s**）' % '、'.join(_missing)) if _missing else '（全部已复核）'))
        L.append('| 评分标准 | 合计 %s 分，共 %d 个评分项 |'
                 % (total_full, len(names)))
        if _won:
            L.append('| 计分口径 | **客观分折算**：%s；折算满分合计 %s 分，'
                     '不含主观方案分与价格分 |'
                     % ('；'.join('%s × %g%%' % (g['名称'], g['权重'])
                                  for g in _groups), _fz_full))
        L.append('| 复核依据 | 招标文件所载评分标准'
                 '（逐项对照原文，逐条标注出处页码） |')
        L.append('')
        L.append('> **阅读说明**：本报告每一项得分都标注了证据出处页码与置信度。'
                 '「置信度·高」表示有文字层或清晰证书直接支持；'
                 '「待核」表示材料缺失或口径需认定，已在第五章汇总。')
        L.append('')
        L.append('---')
        L.append('')

        # ===== 一、总排名 =====
        L.append('## 一、总排名与得分概览')
        L.append('')
        _cols = ['排名', '投标单位', '得分', '满分', '得分率']
        if _won:
            _cols += ['折算得分', '折算满分']
        _cols += ['待核项', '复核时间']
        L.append('| ' + ' | '.join(_cols) + ' |')
        L.append('|' + '---|' * len(_cols))
        prev_sc, rank = None, 0
        for i, u in enumerate(units, 1):
            sc, fu = u['score'], u['full'] or total_full
            rate = ('%.1f%%' % (100.0 * sc / fu)) if (isinstance(sc, (int, float))
                                                     and fu) else '—'
            # 同分并列：得分相同则名次相同（启用折算时按折算得分并列）
            _cmp = _fz[u['unit']][0] if _won else sc
            if prev_sc is None or _cmp != prev_sc:
                rank = i
                prev_sc = _cmp
            _row = [str(rank), u['unit'], '**%s**' % sc, str(fu), rate]
            if _won:
                _row += ['**%.2f**' % _fz[u['unit']][0],
                         '%.2f' % _fz[u['unit']][1]]
            _row += [('、'.join(u['und']) if u['und'] else '无'),
                     (u['time'] or '')[:19]]
            L.append('| ' + ' | '.join(_row) + ' |')
        L.append('')
        if _won:
            L.append('> 说明：「得分 / 满分」是评分标准里的原始分（客观项原分值），'
                     '「折算得分 / 折算满分」才是在总分里的实际贡献；名次按折算得分排列。')
            L.append('')

        # ===== 一之二、分部折算明细 =====
        if _won:
            L.append('## 一之二、分部折算明细')
            L.append('')
            L.append('| 投标单位 | 分部 | 权重 | 客观得分 | 客观满分 | 折算得分 | 折算满分 |')
            L.append('|---|---|---|---|---|---|---|')
            for u in units:
                for p in _fz[u['unit']][2]:
                    L.append('| %s | %s | %g%% | %s | %s | **%s** | %s |'
                             % (u['unit'], p['分组'], p['权重'],
                                p['得分'], p['满分'],
                                p['折算得分'], p['折算满分']))
            L.append('')
            L.append('> 折算得分 = 客观得分 × 权重；折算满分 = 客观满分 × 权重。'
                     '主观项（如施工组织设计）与价格分不由本系统复核，未计入。')
            L.append('')
            L.append('---')
            L.append('')

        # ===== 二、逐项得分对照 =====
        L.append('## 二、逐项得分对照')
        L.append('')
        head = '| 序号 | 评分项 | 分值 | ' + ' | '.join(u['unit'] for u in units) + ' |'
        L.append(head)
        L.append('|---' * (3 + len(units)) + '|')
        # 按序号排序
        order = []
        for u in units:
            for it in u['评分']:
                seq = str(it.get('序号') or '')
                if seq and seq not in order:
                    order.append(seq)
        for seq in order:
            nm, val, cells = '', '', []
            for u in units:
                hit = next((x for x in u['评分']
                            if str(x.get('序号')) == seq), None)
                if hit:
                    nm = nm or (hit.get('名称') or '')
                    val = val or hit.get('分值')
                    cells.append(str(hit.get('得分')))
                else:
                    cells.append('—')
            L.append('| %s | %s | %s | %s |' % (seq, nm, val, ' | '.join(cells)))
        L.append('| — | **合计** | **%s** | %s |'
                 % (total_full, ' | '.join('**%s**' % u['score'] for u in units)))
        L.append('')
        L.append('> 未在全部单位中出现的评分项按「—」显示。')
        L.append('')
        L.append('---')
        L.append('')

        # ===== 三、逐单位复核明细 =====
        L.append('## 三、逐单位复核明细')
        L.append('')
        L.append('> 每个单位按相同顺序列四张表：① 得分构成 ② 证据出处 '
                 '③ 计分依据摘要 ④ 待核事项。')
        L.append('')
        for i, u in enumerate(units, 1):
            sc, fu = u['score'], u['full'] or total_full
            L.append('### 3.%d %s' % (i, u['unit']))
            L.append('')
            L.append('**得分：%s / %s**　复核时间：%s　待核项：%s'
                     % (sc, fu, (u['time'] or '')[:19],
                        ('、'.join(u['und']) if u['und'] else '无')))
            L.append('')

            # ① 得分构成
            L.append('#### 3.%d.1 得分构成' % i)
            L.append('')
            L.append('| 序号 | 评分项 | 分值 | 得分 | 置信度 | 状态 |')
            L.append('|---|---|---|---|---|---|')
            for it in u['评分']:
                if it.get('需人工'):
                    st = '待核（%s）' % (it.get('待核类型')
                                          or it.get('待核原因')
                                          or '需人工确认')
                else:
                    st = '已核'
                L.append('| %s | %s | %s | %s | %s | %s |'
                         % (it.get('序号', ''), it.get('名称', ''),
                            it.get('分值', ''), it.get('得分', ''),
                            it.get('置信度') or '—', st))
            L.append('| — | **合计** | **%s** | **%s** | — | — |' % (total_full, sc))
            L.append('')

            # ② 证据出处
            L.append('#### 3.%d.2 证据出处（可按页码回查）' % i)
            L.append('')
            L.append('| 序号 | 评分项 | 证据出处（页码·来源） |')
            L.append('|---|---|---|')
            for it in u['评分']:
                L.append('| %s | %s | %s |'
                         % (it.get('序号', ''), it.get('名称', ''),
                            as_text(it.get('出处')).replace('|', '/') or '—'))
            L.append('')

            # ③ 计分依据明细（有结构化明细的逐条列出）
            L.append('#### 3.%d.3 计分依据明细' % i)
            L.append('')
            for it in u['评分']:
                mx = it.get('明细')
                L.append('**项%s %s（%s/%s 分）**'
                         % (it.get('序号', ''), it.get('名称', ''),
                            it.get('得分', ''), it.get('分值', '')))
                L.append('')
                if isinstance(mx, dict) and mx.get('表头'):
                    if mx.get('标题'):
                        L.append('*%s*' % mx['标题'])
                        L.append('')
                    hdr = [str(x) for x in mx['表头']]
                    L.append('| ' + ' | '.join(hdr) + ' |')
                    L.append('|' + '---|' * len(hdr))
                    for row in (mx.get('行') or []):
                        cells = [str(c).replace('|', '/') for c in row]
                        while len(cells) < len(hdr):
                            cells.append('')
                        L.append('| ' + ' | '.join(cells[:len(hdr)]) + ' |')
                    L.append('')
                else:
                    L.append(short(it.get('依据'), 400))
                    L.append('')
                if it.get('依据'):
                    L.append('> **补充说明**：%s' % short(it['依据'], 260))
                    L.append('')
            L.append('')

            # ④ 待核事项
            pend = [it for it in u['评分'] if it.get('需人工')]
            L.append('#### 3.%d.4 待核事项' % i)
            L.append('')
            if not pend:
                L.append('**无。** 本投标单位 %d 个评分项均已核实，无可待人工确认事项。'
                         % len(u['评分']))
            else:
                L.append('| 序号 | 评分项 | 待核类型 | 原因与建议 |')
                L.append('|---|---|---|---|')
                for it in pend:
                    L.append('| %s | %s | %s | %s |'
                             % (it.get('序号', ''), it.get('名称', ''),
                                it.get('待核类型') or '需人工',
                                short(it.get('待核原因'), 220)))
            L.append('')
            if u['结论']:
                L.append('**复核结论**：%s' % u['结论'])
                L.append('')
            L.append('---')
            L.append('')

        # ===== 四、横向差异 =====
        L.append('## 四、横向差异（仅显示存在差异的评分项）')
        L.append('')
        diff_rows = []
        for seq in order:
            vals = []
            for u in units:
                hit = next((x for x in u['评分'] if str(x.get('序号')) == seq), None)
                vals.append(hit.get('得分') if hit else None)
            uniq = set(vals)
            if len(uniq) > 1:
                nm = ''
                for u in units:
                    hit = next((x for x in u['评分'] if str(x.get('序号')) == seq), None)
                    if hit:
                        nm = hit.get('名称') or ''
                        break
                diff_rows.append((seq, nm, vals))
        if diff_rows:
            L.append('| 序号 | 评分项 | 分值 | ' + ' | '.join(u['unit'] for u in units) + ' |')
            L.append('|---' * (3 + len(units)) + '|')
            for seq, nm, vals in diff_rows:
                L.append('| %s | %s | %s | %s |'
                         % (seq, nm, full_map.get(nm, ''), ' | '.join(
                             '—' if v is None else str(v) for v in vals)))
            L.append('')
            L.append('> 其余评分项各单位得分相同，未在此列出。')
        else:
            L.append('**各单位在所有评分项上得分相同。**')
        L.append('')
        L.append('---')
        L.append('')

        # ===== 四·五、未复核单位（G4）=====
        if _missing:
            L.append('## 五之零、未复核单位（不计入本汇总）')
            L.append('')
            L.append('> 下列单位在本次生成汇总时**尚无复核结果**，本报告未包含它们的得分。'
                     '如需要完整汇总，请先复核这些单位再重新生成本报告。')
            L.append('')
            for _mu in _missing:
                L.append('- %s' % _mu)
            L.append('')
            L.append('---')
            L.append('')

        # ===== 五、待办汇总 =====
        L.append('## 五、待核事项汇总（按单位）')
        L.append('')
        allpend = [(u['unit'], it) for u in units for it in u['评分'] if it.get('需人工')]
        if not allpend:
            L.append('**全部投标单位的全部评分项均已核实，无待核事项。**')
        else:
            by_type = {}
            for unit, it in allpend:
                by_type.setdefault(it.get('待核类型') or '需人工', []).append((unit, it))
            L.append('| 待核类型 | 单位 | 评分项 | 原因 |')
            L.append('|---|---|---|---|')
            for typ in sorted(by_type):
                for unit, it in by_type[typ]:
                    L.append('| %s | %s | 项%s %s | %s |'
                             % (typ, unit, it.get('序号', ''), it.get('名称', ''),
                                short(it.get('待核原因'), 180)))
            L.append('')
            L.append('> **类型说明**：`材料缺失` = 投标文件内未检索到该证明材料；'
                     '`口径认定` = 需评标委员会统一认定；'
                     '`证据不足` = 有相关材料但不足以支撑判定；'
                     '`OCR读数` = 识别结果存疑，需看原图。')
        L.append('')
        L.append('---')
        L.append('')

        # ===== 五之一、文字层乱码页（S9 / v1.8.0）=====
        # 乱码页的文字层不可用，复核是靠整页渲染后 OCR 读的；列出页码方便抽查。
        _garb = []
        try:
            _bdir = os.path.join(pd, '投标文件')
            if os.path.isdir(_bdir):
                for _u in sorted(os.listdir(_bdir)):
                    _up = os.path.join(_bdir, _u)
                    if not os.path.isdir(_up):
                        continue
                    for _fn in sorted(os.listdir(_up)):
                        if not _fn.endswith('.pages.json'):
                            continue
                        try:
                            with open(os.path.join(_up, _fn), encoding='utf-8') as _f:
                                _pj = json.load(_f)
                        except Exception:
                            continue
                        _pages = (((_pj or {}).get('统计') or {}).get('乱码页')) or []
                        if _pages:
                            _garb.append((_u, _fn[:-len('.pages.json')], _pages))
        except Exception:
            traceback.print_exc()
        if _garb:
            L.append('## 五之一、文字层乱码页（已整页渲染并 OCR，建议抽查）')
            L.append('')
            L.append('| 单位 | 投标文件 | 乱码页 |')
            L.append('|---|---|---|')
            for _u, _b, _pages in _garb:
                _show = '、'.join('p%d' % x for x in _pages[:40])
                if len(_pages) > 40:
                    _show += ' …（共 %d 页）' % len(_pages)
                L.append('| %s | %s | %s |' % (_u, _b, _show))
            L.append('')
            L.append('> 判定口径：该页文字层里"非常用字符占比"超过 0.5（子集字体把中文'
                     '重映射成生僻码位），系统已按无文字页处理并整页渲染 + OCR。'
                     '若抽查发现某页内容与结论不符，请指出来。')
            L.append('')
            L.append('---')
            L.append('')

        # ===== 六、复核方法说明 =====
        L.append('## 六、复核方法与说明')
        L.append('')
        L.append('- **判分依据**：招标文件所载的评分标准与评分办法原文，'
                 '每个项目均重新读取原文，不套用其他项目口径。')
        L.append('- **证据来源**：文字层（PDF 可选中文字）优先；'
                 '文字层无内容时，读取该页全部扫描图并作 OCR，'
                 '每页多张图时逐一读取（i0/i1/i2…）。')
        L.append('- **出处标注**：每项标注页码与来源类型（文字层 / OCR / i1 第二张图 / 登记截图）。')
        L.append('- **置信度**：高＝有文字层或清晰证书直接支持；'
                 '中＝有材料但需间接推断；低＝证据不足或存疑。')
        L.append('- **统计口径**：每项得分保留一位小数，合计按评分表封顶规则计算。')
        L.append('- **可追溯性**：完整依据文字（含逐项明细与页码）保存在各单位的'
                 '`复核报告\\{投标单位}_复核结果.json` 中。')
        L.append('')
        L.append('> 本报告为招标人/代理机构自查用途，不替代评标委员会评审结论。')
        L.append('')

        out = os.path.join(pd, '复核结果汇总.md')
        with open(out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(L))
            f.flush()
            os.fsync(f.fileno())
        _resp = {'ok': True, '任务': 'archive', '项目': pname,
                 '单位数': len(units),
                 '总单位数': len(_all_units) or len(units),
                 '未复核': _missing,
                 '完整': not _missing,
                 '文件': out}
        if _won:
            _resp['权重折算'] = True
            _resp['折算满分'] = _fz_full
            _resp['折算'] = [{'投标单位': u['unit'],
                              '折算得分': _fz[u['unit']][0],
                              '折算满分': _fz[u['unit']][1],
                              '明细': _fz[u['unit']][2]} for u in units]
        return self._send(200, _resp)



    # ---------- 专家打分表上传 ----------
    def _upload_expert(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pname = (qs.get('project') or [''])[0].strip()
        name = (qs.get('name') or [''])[0].strip()
        if not pname:
            return self._err(400, '缺少 project 参数')
        if not name:
            return self._err(400, '缺少 name 参数（文件名）')
        name = os.path.basename(name)
        if not name.lower().endswith(('.pdf', '.xlsx', '.xls', '.csv',
                                      '.docx', '.doc', '.jpg', '.jpeg', '.png')):
            return self._err(400, '不支持的文件类型：%s' % name)
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目「%s」不存在，请先保存项目信息' % pname)
        raw = self._body()
        if not raw:
            return self._err(400, '文件为空')

        # ---- 按内容哈希去重：同一份文件不重复落盘 ----
        digest = hashlib.sha256(raw).hexdigest()
        dup_name = None
        try:
            for nm in os.listdir(d):
                fp2 = os.path.join(d, nm)
                if not os.path.isfile(fp2):
                    continue
                if not nm.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg',
                                            '.xlsx', '.csv', '.docx')):
                    continue
                if os.path.getsize(fp2) != len(raw):
                    continue
                with open(fp2, 'rb') as fh:
                    if hashlib.sha256(fh.read()).hexdigest() == digest:
                        dup_name = nm
                        break
        except Exception:
            traceback.print_exc()

        has_result = os.path.isfile(os.path.join(d, '_录入.json'))
        if dup_name:
            return self._send(200, {
                'ok': True, '文件': dup_name, '目录': d, '字节': len(raw),
                '重复': True, '已有结果': has_result,
                '说明': '已存在内容相同的文件（SHA256 %s…），直接复用，未重复保存'
                        % digest[:12]})

        fp = os.path.join(d, name)
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.isfile(fp):
            fp = os.path.join(d, '%s(%d)%s' % (base, i, ext))
            i += 1
        with open(fp, 'wb') as f:
            f.write(raw)
        return self._send(200, {'ok': True, '文件': os.path.basename(fp),
                                '目录': d, '字节': len(raw),
                                '重复': False, '已有结果': has_result,
                                '哈希': digest[:16]})

    # ---------- 项目数据重置 ----------
    def _project_reset(self, params):
        """按项目重置数据：先备份，再删除；须提交项目名确认"""
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname, create=False)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        conf = (params.get('确认') or '').strip()
        if conf != pname:
            return self._err(400, '确认名称与项目名不一致，已取消')

        ALL = ['评分标准', '投标文件', '复核报告', '评委打分', '队列任务']
        scope = params.get('范围') or ALL
        scope = [x for x in scope if x in ALL]
        if not scope:
            return self._err(400, '未选择任何重置范围')

        # 名称 → 路径
        targets = {
            '评分标准': [os.path.join(pd, '评分标准.json'),
                     os.path.join(pd, '评分标准_校验.json'),
                     os.path.join(pd, '评分标准_解读.json')],
            '投标文件': [os.path.join(pd, '投标文件')],
            '复核报告': [os.path.join(pd, '复核报告')] +
                    glob.glob(os.path.join(pd, '复核结果汇总.*')) +
                    [os.path.join(pd, '项目要点.md'),
                     os.path.join(pd, '项目要点.md.bak')],
            '评委打分': [os.path.join(pd, '评委打分')],
        }
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        bk = os.path.join(pd, '_重置备份_' + ts)

        # ① 先备份
        backed = []
        try:
            for name in scope:
                for p in targets.get(name) or []:
                    if not os.path.exists(p):
                        continue
                    os.makedirs(bk, exist_ok=True)
                    dst = os.path.join(bk, os.path.basename(p))
                    if os.path.isdir(p):
                        shutil.copytree(p, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(p, dst)
                    if name not in backed:
                        backed.append(name)
        except Exception as e:
            traceback.print_exc()
            return self._err(500, '备份失败，未执行删除：%r' % e)

        # ② 再删除
        removed, rm_cnt = {}, {}
        for name in scope:
            for p in targets.get(name) or []:
                if not os.path.exists(p):
                    continue
                try:
                    if os.path.isdir(p):
                        n = sum(len(f) for _, _, f in os.walk(p))
                        shutil.rmtree(p, ignore_errors=True)
                        rm_cnt[name] = rm_cnt.get(name, 0) + n
                    else:
                        os.remove(p)
                        rm_cnt[name] = rm_cnt.get(name, 0) + 1
                except Exception as e:
                    traceback.print_exc()
                    removed[name] = '删除失败 %r' % e
        for k, v in rm_cnt.items():
            removed.setdefault(k, '%d 个文件' % v)

        # ③ 队列任务
        cancelled = []
        if '队列任务' in scope:
            for qf in glob.glob(os.path.join(QUEUE, '*.json')):
                try:
                    with open(qf, encoding='utf-8') as fh:
                        t = json.load(fh)
                except Exception:
                    continue
                if t.get('项目') != pname:
                    continue
                if t.get('状态') in ('待处理', '处理中'):
                    t['状态'] = '已取消'
                    t['完成时间'] = now()
                    with open(qf, 'w', encoding='utf-8') as fh:
                        json.dump(t, fh, ensure_ascii=False, indent=1)
                    cancelled.append(t.get('id'))
            removed['队列任务'] = '取消 %d 个未完成任务' % len(cancelled)

        # ④ 项目状态回到进行中
        state_note = None
        ip = os.path.join(pd, '项目信息.json')
        if os.path.isfile(ip):
            try:
                with open(ip, encoding='utf-8') as fh:
                    pj = json.load(fh) or {}
                if pj.get('状态') in ('已归档', '归档中'):
                    pj['状态'] = '进行中'
                    for k in ('归档时间', '归档备注', '归档清单'):
                        pj.pop(k, None)
                    backup_once(ip)
                    with open(ip, 'w', encoding='utf-8') as fh:
                        json.dump(pj, fh, ensure_ascii=False, indent=1)
                    state_note = '项目状态已由「已归档」恢复为「进行中」'
            except Exception:
                traceback.print_exc()

        return self._send(200, {
            'ok': True, '项目': pname, '范围': scope,
            '已删除': removed, '已取消任务': cancelled,
            '备份目录': bk if backed else None,
            '状态说明': state_note,
            '提示': '原数据已备份到「_重置备份_%s」，如误删可从该目录取回' % ts})

    # ---------- 专家打分 ----------
    def _expert_dir(self, pname, create=False):
        pd = project_dir_for(pname, create=create)
        if not pd:
            return None, None
        d = os.path.join(pd, '评委打分')
        if create:
            os.makedirs(d, exist_ok=True)
        return pd, d

    def _expert_extract(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        try:
            import expert_scores
            r = expert_scores.extract(pd)
        except Exception as e:
            traceback.print_exc()
            return self._err(500, '提取失败：%r' % e)
        return self._send(200, {'ok': True, '项目': pname,
                                '文件数': len(r.get('文件') or []),
                                '告警': r.get('告警') or [],
                                '结果': r})

    def _expert_load(self, params):
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        import expert_scores
        ent = expert_scores.load_entries(pd)
        files = []
        if d and os.path.isdir(d):
            for x in sorted(os.listdir(d)):
                if x.startswith('_'):
                    continue
                fp = os.path.join(d, x)
                if os.path.isfile(fp):
                    files.append({'文件': x,
                                  '大小KB': round(os.path.getsize(fp) / 1024, 1)})
        return self._send(200, {'ok': True, '项目': pname, '文件': files,
                                '录入': ent,
                                '有录入': bool(ent.get('专家'))})

    def _expert_save(self, params):
        """保存人工确认后的录入：{专家:[...], 分数:{'投标人|序号':{专家:分}}}"""
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        experts = params.get('专家') or []
        scores = params.get('分数') or {}
        if not isinstance(experts, list) or not isinstance(scores, dict):
            return self._err(400, '数据结构不符：需要 专家 数组与 分数 对象')
        import expert_scores
        ent = expert_scores.save_entries(pd, {'专家': experts, '分数': scores})
        n = sum(len(v or {}) for v in scores.values())
        return self._send(200, {'ok': True, '专家数': len(experts),
                                '分数条数': n, '更新时间': ent.get('更新时间')})

    def _expert_submit(self, params):
        """导入打分表 → 写入队列 → 唤醒 agent 解析"""
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        fname = (params.get('文件') or '').strip()
        fp = os.path.join(d, os.path.basename(fname)) if fname else None
        if not fp or not os.path.isfile(fp):
            # 未指定则取目录里最新的打分表
            cands = [x for x in glob.glob(os.path.join(d, '*'))
                     if x.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))]
            if not cands:
                return self._err(400, '评委打分目录下没有打分表，请先上传')
            fp = max(cands, key=os.path.getmtime)
        tid = 'E' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        task = {'id': tid, '类型': '专家打分解析', '项目': pname,
                '文件': fp, '文件名': os.path.basename(fp),
                '评分标准': os.path.join(pd, '评分标准.json'),
                '提交时间': now(), '状态': '待处理',
                '说明': params.get('说明') or '',
                '结果文件': os.path.join(d, '_录入.json'),
                '错误': None}
        os.makedirs(QUEUE, exist_ok=True)
        with open(os.path.join(QUEUE, tid + '.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(task, fh, ensure_ascii=False, indent=1)
        woke, winfo = False, None
        try:
            import wake as wake_mod
            woke, winfo = wake_mod.wake(
                '【第一步必做】调接口 POST /api/run '
                '{"任务":"stopcheck","参数":{"id":"%s"}}，'
                '返回 "已停止": true 即任务已被停止，此时不得处理。'
                '【自动唤醒·专家打分】项目「%s」导入了评委打分表：%s。'
                '请读取该 PDF，按本项目评分标准逐项解析出各投标单位得分，'
                '写入 项目\\%s\\评委打分\\_录入.json（结构：'
                '{"专家":["评委名"],"分数":{"投标单位|项号":{"评委名":分数}}}），'
                '并把任务 %s 标记为已完成。'
                % (tid, pname, os.path.basename(fp), pname, tid), project=pname)
        except Exception as e:
            traceback.print_exc()
            winfo = '唤醒异常：%r' % e
        return self._send(200, {'ok': True, '任务': tid,
                                '文件': os.path.basename(fp),
                                '已唤醒': woke, '唤醒说明': winfo})

    def _expert_status(self, params):
        """轮询：agent 是否已解析完成；完成则返回比对结果"""
        pname = (params.get('项目') or '').strip()
        tid = (params.get('任务') or '').strip()
        pd, d = self._expert_dir(pname, create=False)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        tf = os.path.join(QUEUE, tid + '.json') if tid else None
        st, err, done_at = None, None, None
        if tf and os.path.isfile(tf):
            try:
                with open(tf, encoding='utf-8') as _fh:
                    t = json.load(_fh)
                st = t.get('状态')
                err = t.get('错误')
                done_at = t.get('完成时间')
            except Exception:
                pass
        # 判完成：任务已完成，或 _录入.json 比任务提交时间新
        ready = (st == '已完成')
        if not ready and tid and tf and os.path.isfile(tf):
            ent = os.path.join(d or '', '_录入.json')
            if os.path.isfile(ent):
                try:
                    t = json.load(io.open(tf, encoding='utf-8'))
                    import time as _tm
                    if os.path.getmtime(ent) > _tm.time() - 3600:
                        ready = True
                except Exception:
                    pass
        out = {'ok': True, '状态': st or '待处理', '完成': bool(ready),
               '错误': err, '完成时间': done_at}
        if ready:
            try:
                import expert_scores
                out['比对'] = expert_scores.compare(pd)
                ent = expert_scores.load_entries(pd)
                out['专家'] = ent.get('专家') or []
            except Exception as e:
                out['错误'] = repr(e)
        return self._send(200, out)

    def _expert_reset(self, params):
        """重置专家打分比对：清空录入与提取结果，可选择性删除打分表"""
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname, create=False)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        removed = []
        keep = ['_录入.json', '_提取.json']
        for nm in keep:
            fp = os.path.join(d, nm)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    removed.append(nm)
                except Exception as e:
                    traceback.print_exc()
                    return self._err(500, '删除 %s 失败：%r' % (nm, e))
        if params.get('删除打分表'):
            for nm in os.listdir(d):
                if nm.startswith('_'):
                    continue
                fp = os.path.join(d, nm)
                if os.path.isfile(fp) and nm.lower().endswith(
                        ('.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.csv')):
                    try:
                        os.remove(fp)
                        removed.append(nm)
                    except Exception:
                        traceback.print_exc()
                elif os.path.isdir(fp) and nm.endswith('.pages'):
                    try:
                        shutil.rmtree(fp, ignore_errors=True)
                        removed.append(nm + '/')
                    except Exception:
                        pass
        # 相关队列任务标记为已取消（保留记录，不删文件）
        cancelled = []
        for qf in glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                t = json.load(io.open(qf, encoding='utf-8'))
            except Exception:
                continue
            if (t.get('类型') == '专家打分解析' and t.get('项目') == pname
                    and t.get('状态') in ('待处理', '处理中')):
                t['状态'] = '已取消'
                t['完成时间'] = now()
                with open(qf, 'w', encoding='utf-8') as fh:
                    json.dump(t, fh, ensure_ascii=False, indent=1)
                cancelled.append(t.get('id'))
        return self._send(200, {'ok': True, '项目': pname,
                                '已清除': removed, '已取消任务': cancelled})

    def _expert_import(self, params):
        """导入即完成：异步解析打分表 → 保存 → 比对；返回批次号供轮询进度

        参数 同步=True 时退化为同步调用（兼容脚本/测试）。
        """
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname, create=True)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)

        if params.get('同步'):
            try:
                import expert_scores
                r = expert_scores.import_and_compare(pd)
            except Exception as e:
                traceback.print_exc()
                return self._err(500, '解析失败：%r' % e)
            r['ok'] = True
            r['项目'] = pname
            return self._send(200, r)

        jid = 'EXP' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        EXPERT_JOBS[jid] = {'进度': 2, '阶段': '排队', '完成': False,
                            '结果': None, '错误': None, '项目': pname}

        def _worker():
            def _prog(msg, pct):
                j = EXPERT_JOBS.get(jid)
                if j:
                    j['阶段'] = msg
                    j['进度'] = max(j.get('进度') or 0, min(99, int(pct)))

            try:
                import expert_scores
                r = expert_scores.import_and_compare(pd, on_progress=_prog)
                r['ok'] = True
                r['项目'] = pname
                EXPERT_JOBS[jid].update({'结果': r, '进度': 100,
                                         '阶段': '完成', '完成': True})
            except Exception as e:
                traceback.print_exc()
                EXPERT_JOBS[jid].update({'错误': repr(e), '完成': True,
                                         '阶段': '失败', '进度': 100})

        threading.Thread(target=_worker, daemon=True).start()
        return self._send(200, {'ok': True, '批次': jid, '项目': pname})

    def _expert_progress(self, params):
        jid = (params.get('批次') or '').strip()
        j = EXPERT_JOBS.get(jid)
        if not j:
            return self._err(404, '批次不存在：%s' % jid)
        return self._send(200, {'ok': True, '批次': jid,
                                '进度': j.get('进度') or 0,
                                '阶段': j.get('阶段') or '',
                                '完成': bool(j.get('完成')),
                                '错误': j.get('错误'),
                                '结果': j.get('结果') if j.get('完成') else None})

    def _expert_compare(self, params):
        pname = (params.get('项目') or '').strip()
        pd, d = self._expert_dir(pname)
        if not pd:
            return self._err(404, '项目不存在：%s' % pname)
        warn = params.get('关注阈值')
        alert = params.get('警示阈值')
        import expert_scores
        r = expert_scores.compare(
            pd,
            warn=float(warn) if isinstance(warn, (int, float)) else 0.5,
            alert=float(alert) if isinstance(alert, (int, float)) else 1.5)
        r['ok'] = True
        r['项目'] = pname
        return self._send(200, r)

    # ---------- 项目归档 ----------
    def _project_facts(self, pname):
        """收集归档所需的项目事实"""
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return None
        rdir = os.path.join(pd, '复核报告')
        bdir = os.path.join(pd, '投标文件')
        units = []
        if os.path.isdir(bdir):
            for d in sorted(os.listdir(bdir)):
                dp = os.path.join(bdir, d)
                if not os.path.isdir(dp) or d.endswith('.pages'):
                    continue
                fs = [x for x in os.listdir(dp)
                      if not x.endswith(('.txt', '.json'))
                      and os.path.isfile(os.path.join(dp, x))]
                if not fs:
                    continue
                rec = {'投标单位': d, '已复核': False, '得分': None,
                       '满分': None, '未决项': []}
                rf = os.path.join(rdir, d + '_复核结果.json')
                if os.path.isfile(rf):
                    try:
                        with open(rf, encoding='utf-8') as f:
                            d2 = json.load(f)
                        rec['已复核'] = True
                        rec['得分'] = d2.get('小计')
                        rec['满分'] = d2.get('满分')
                        rec['未决项'] = d2.get('未决项') or []
                        rec['复核时间'] = d2.get('复核时间')
                    except Exception:
                        rec['数据异常'] = True
                units.append(rec)
        tasks = []
        for fp in glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                with open(fp, encoding='utf-8') as f:
                    t = json.load(f)
            except Exception:
                continue
            if t.get('项目') != pname:
                continue
            tasks.append({'id': t.get('id'), '投标人': t.get('投标人'),
                          '状态': t.get('状态'), '提交时间': t.get('提交时间'),
                          '完成时间': t.get('完成时间')})
        summary = None
        for cand in (os.path.join(pd, '复核结果汇总.md'),
                     os.path.join(rdir, '复核结果汇总.md')):
            if os.path.isfile(cand):
                summary = {'文件': cand, '字节': os.path.getsize(cand)}
                break
        info = {}
        ip = os.path.join(pd, '项目信息.json')
        if os.path.isfile(ip):
            try:
                with open(ip, encoding='utf-8') as f:
                    info = json.load(f)
            except Exception:
                info = {}
        return {'目录': pd, '单位': units, '任务': tasks,
                '汇总': summary, '信息': info}

    def _project_check_data(self, pname):
        """返回体检结果 dict（不发送响应），供 check 与 archive 复用"""
        facts = self._project_facts(pname)
        if facts is None:
            return None
        units = facts['单位']
        done = [u for u in units if u['已复核']]
        pending = [u['投标单位'] for u in units if not u['已复核']]
        open_tasks = [t for t in facts['任务']
                      if t['状态'] in ('待处理', '处理中')]
        bad = [u['投标单位'] for u in units if u.get('数据异常')]

        blocked = {}
        bad_sum = []
        try:
            import evidence_spec as es
            spec = es.load_spec(facts['目录'])
            for u in done:
                rf = os.path.join(facts['目录'], '复核报告',
                                  u['投标单位'] + '_复核结果.json')
                try:
                    with open(rf, encoding='utf-8') as f:
                        d2 = json.load(f)
                except Exception:
                    continue
                # M8：归档前复算数字自洽，拦住被手工改过（小计/满分对不上）的结果
                _bs = total_consistency_errors(d2)
                if _bs:
                    bad_sum.append(u['投标单位'] + '（' + '；'.join(_bs) + '）')
                if spec:
                    try:
                        chk = es.check_result(d2, spec)
                        if not chk['通过']:
                            blocked[u['投标单位']] = chk['阻断']
                    except Exception:
                        pass
        except Exception:
            pass

        gaps = []
        if not units:
            gaps.append('项目下没有投标单位')
        if pending:
            gaps.append('未复核 %d 家：%s'
                        % (len(pending), '、'.join(pending[:3])))
        if open_tasks:
            gaps.append('有 %d 个任务未完成（待处理/处理中）' % len(open_tasks))
        if blocked:
            gaps.append('齐备性校验未通过 %d 家：%s'
                        % (len(blocked), '、'.join(list(blocked)[:3])))
        if bad_sum:
            gaps.append('数字不自洽 %d 家：%s'
                        % (len(bad_sum), '、'.join(bad_sum[:3])))
        if not facts['汇总']:
            gaps.append('尚未生成复核结果汇总')
        else:
            # S2（v1.7.9）：汇总必须比所有结果文件都新，否则是"过期汇总"
            _pdir = facts.get('目录') or ''
            _rdir = os.path.join(_pdir, '复核报告')
            _sum_fp = os.path.join(_pdir, '复核结果汇总.md')
            try:
                _sum_t = os.path.getmtime(_sum_fp)
            except Exception:
                _sum_t = None
            if _sum_t:
                _changed = []
                for _fn in (sorted(os.listdir(_rdir)) if os.path.isdir(_rdir) else []):
                    if not _fn.endswith('_复核结果.json'):
                        continue
                    try:
                        if os.path.getmtime(os.path.join(_rdir, _fn)) > _sum_t + 1:
                            _changed.append(_fn.replace('_复核结果.json', ''))
                    except Exception:
                        pass
                if _changed:
                    gaps.append('汇总已过期：%d 家结果在汇总之后改过（%s），'
                                '请重新生成汇总'
                                % (len(_changed), '、'.join(_changed[:3])))
        if bad:
            gaps.append('结果文件异常 %d 家：%s' % (len(bad), '、'.join(bad[:3])))

        undecided = {u['投标单位']: u['未决项'] for u in done if u['未决项']}
        return {
            '项目': pname, '状态': facts['信息'].get('状态') or '进行中',
            '可归档': not gaps, '缺口': gaps,
            '单位数': len(units), '已复核': len(done),
            '未决项': undecided,
            '任务数': len(facts['任务']), '未完成任务': len(open_tasks),
            '汇总字节': (facts['汇总'] or {}).get('字节'),
            '得分': [{'投标单位': u['投标单位'], '得分': u['得分'],
                      '满分': u['满分']} for u in done],
            '事实': facts,
        }

    def _project_check(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        data = self._project_check_data(pname)
        if data is None:
            return self._err(404, '项目不存在：%s' % pname)
        data.pop('事实', None)
        data['ok'] = True
        data['说明'] = ('「可归档」为真时才能归档；未决项不阻断，'
                        '但归档时须显式传「允许未决」')
        return self._send(200, data)

    def _project_archive(self, params):
        pname = (params.get('项目') or '').strip()
        allow = bool(params.get('允许未决'))
        note = (params.get('备注') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        data = self._project_check_data(pname)
        if data is None:
            return self._err(404, '项目不存在：%s' % pname)
        if not data['可归档']:
            return self._err(400, '尚不满足归档条件：' + '；'.join(data['缺口']))
        if data['未决项'] and not allow:
            return self._err(400, '有未决项：%s。如确认带未决项归档，'
                                  '请传「允许未决」为 true'
                            % '、'.join(data['未决项']))

        facts = data['事实']
        pd = facts['目录']
        now_s = now()
        info = facts['信息']
        info['状态'] = '已归档'
        info['归档时间'] = now_s
        info['归档备注'] = note
        info['归档清单'] = '归档清单.json'
        with open(os.path.join(pd, '项目信息.json'), 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)

        files = []
        for name in ('复核结果汇总.md', '复核结果汇总.docx', '复核结果汇总.pdf'):
            for cand in (os.path.join(pd, name),
                         os.path.join(pd, '复核报告', name)):
                if os.path.isfile(cand):
                    files.append({'文件': name,
                                  '路径': cand,
                                  '字节': os.path.getsize(cand)})
                    break
        for u in facts['单位']:
            p = os.path.join(pd, '复核报告', u['投标单位'] + '_复核结果.json')
            if os.path.isfile(p):
                files.append({'文件': u['投标单位'] + '_复核结果.json',
                              '字节': os.path.getsize(p)})

        archive = {'项目': pname, '项目编号': info.get('项目编号'),
                   '归档时间': now_s, '归档备注': note,
                   '单位数': data['单位数'], '已复核': data['已复核'],
                   '得分': data['得分'], '未决项': data['未决项'],
                   '任务': facts['任务'], '交付文件': files}
        ap = os.path.join(pd, '归档清单.json')
        with open(ap, 'w', encoding='utf-8') as f:
            json.dump(archive, f, ensure_ascii=False, indent=1)

        md = ['# 归档清单', '',
              '- 项目：%s' % pname,
              '- 项目编号：%s' % (info.get('项目编号') or '—'),
              '- 归档时间：%s' % now_s,
              '- 单位数：%s，已复核：%s' % (data['单位数'], data['已复核'])]
        if note:
            md.append('- 归档备注：%s' % note)
        _ar_rules, _ar_path = load_project_rules(pname)
        _ar_won = bool(_ar_rules and _ar_rules.get('权重折算'))
        md += ['', '## 各家得分', '', '| 投标单位 | 得分 | 满分 |', '|---|---|---|']
        for s in data['得分']:
            md.append('| %s | %s | %s |' % (s['投标单位'], s['得分'], s['满分']))
        if _ar_won:
            _ar_g = [g for g in (_ar_rules.get('分组') or [])
                     if g.get('参与折算') is not False and g.get('权重')]
            md += ['', '> 计分口径：上表为**客观项原始分**；计入总分时需折算——'
                   + '；'.join('%s × %s%%' % (g.get('名称') or '', g.get('权重'))
                               for g in _ar_g)
                   + '。主观方案分与价格分不在本系统复核范围。']
        if data['未决项']:
            md += ['', '## 未决项（已确认带未决归档）', '']
            for k, v in data['未决项'].items():
                md.append('- %s：项 %s' % (k, '、'.join(str(x) for x in v)))
        md += ['', '## 交付文件', '', '| 文件 | 字节 |', '|---|---|']
        for f2 in files:
            md.append('| %s | %s |' % (f2['文件'], f2['字节']))
        with open(os.path.join(pd, '归档清单.md'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(md) + '\n')

        # 项目结项 = 会话轮换的硬边界：把该项目专属会话标记失效，
        # 下次该项目再复核会回落到默认会话（界面上会提示"项目已结项"）。
        retired = None
        auto_sess = False
        try:
            import wake as _wake
            retired = _wake.retire_project(pname)
            auto_sess = _wake.auto_session_enabled()
        except Exception:
            pass

        # 给界面一句能直接显示的话：说清「有没有专属会话」「下次复核落到哪」，
        # 不要再说成"用新会话"——自动建会话关着时，实际是回落默认会话。
        if retired:
            sess_note = ('专属会话已退出映射，下次复核会新建专属会话'
                         if auto_sess else '专属会话已退出映射，下次复核回落默认会话')
        else:
            sess_note = ('本项目原无专属会话，下次复核会新建专属会话'
                         if auto_sess else '本项目没有专属会话，复核一直用默认会话')

        return self._send(200, {'ok': True, '项目': pname, '状态': '已归档',
                                '归档时间': now_s, '清单': ap,
                                '单位数': data['单位数'],
                                '文件数': len(files),
                                '会话已结项': retired or '',
                                '会话说明': sess_note,
                                '自动建会话': auto_sess})

    def _project_reopen(self, params):
        pname = (params.get('项目') or '').strip()
        why = (params.get('原因') or '').strip()
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        fp = os.path.join(pd, '项目信息.json')
        info = {}
        if os.path.isfile(fp):
            try:
                with open(fp, encoding='utf-8') as f:
                    info = json.load(f)
            except Exception:
                info = {}
        info['状态'] = '进行中'
        info['解档时间'] = now()
        info['解档原因'] = why
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        # 解档后取消"项目已结项"标记，否则唤醒会一直回落默认会话
        try:
            import wake as _wake
            _wake.set_project_session(pname, '')
        except Exception:
            pass
        return self._send(200, {'ok': True, '项目': pname, '状态': '进行中',
                                '解档原因': why})

    # ---------- 给项目分配专属会话（v1.5.4） ----------
    def _session_assign(self, params):
        """给某个项目创建/指定专属会话，并登记到工作区、置顶。

        场景：历史项目（v1.3.0 之前建的任务）没有会话映射，界面只能显示"未分配"；
        点一次「分配会话」即可为其建一个新会话，之后的唤醒都投给它。
        """
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        # 已归档的项目不再接受分配（归档意味着不再复核）；先解档再加会话
        try:
            with open(os.path.join(pd, '项目信息.json'), encoding='utf-8') as f:
                _st = (json.load(f) or {}).get('状态') or ''
        except Exception:
            _st = ''
        if _st == '已归档':
            return self._err(409, '项目「%s」已归档，请先点「归档」解档，再分配会话。'
                             % pname)
        try:
            import wake as _wake
        except Exception as e:
            return self._err(500, '唤醒模块不可用：%r' % e)
        sid, why, made = _wake.ensure_project_session(pname)
        if not sid:
            return self._err(500, '分配失败：%s' % why)
        # 让 health 立刻反映新状态
        info = session_health(pname)
        return self._send(200, {'ok': True, '项目': pname, '会话': sid,
                                '是否新建': made, '说明': why,
                                '会话状态': info.get('状态'),
                                '上下文占比': info.get('上下文占比')})

    # ---------- 队列状态实时同步（agent 认领任务） ----------
    def _fix_pending(self, params):
        """批量补「待核原因」（默认干跑；写入=true 才落盘）。

        只补 需人工=true 且 待核原因/连带说明 都为空的项；格式固定为三行：
            要点：…
            出处：…
            疑问：…

        `覆盖=true` 时，会把"看起来是自动生成的"旧文本（以 要点/① 谁：开头）
        一并重写，并把历史遗留的 `待核原因_来源` 字段删掉。
        """
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        rd = os.path.join(pd, '复核报告') if pd else None
        if not rd or not os.path.isdir(rd):
            return self._err(404, '项目「%s」没有复核报告目录' % pname)
        want_write = bool(params.get('写入'))
        overwrite = bool(params.get('覆盖'))
        only_unit = (params.get('投标单位') or '').strip()
        files = sorted(fn for fn in os.listdir(rd) if fn.endswith('_复核结果.json'))
        if only_unit:
            target = only_unit + '_复核结果.json'
            files = [fn for fn in files if fn == target]
        if not files:
            return self._err(404, '没有复核结果文件')
        out = []
        for fn in files:
            fp = os.path.join(rd, fn)
            try:
                with open(fp, encoding='utf-8') as f:
                    d = json.load(f)
            except Exception as e:
                out.append({'文件': fn, '错误': repr(e)})
                continue
            unit = d.get('投标人') or fn.replace('_复核结果.json', '')
            filled, preview, dropped_mark = 0, [], 0
            for it in (d.get('评分') or []):
                if not it.get('需人工'):
                    continue
                cur = str(it.get('待核原因') or '').strip()
                if it.pop('待核原因_来源', None) is not None:
                    dropped_mark += 1
                if cur:
                    auto_like = (cur.startswith('要点：')
                                 or cur.startswith('① 谁：'))
                    if not (overwrite and auto_like):
                        continue
                reason = auto_pending_reason(unit, it)
                it['待核原因'] = reason
                filled += 1
                if len(preview) < 2:
                    preview.append({'序号': it.get('序号'),
                                    '名称': it.get('名称'),
                                    '待核原因': reason[:200]})
            if filled and want_write:
                try:
                    atomic_write_json(fp, d)
                except Exception as e:
                    out.append({'文件': fn, '错误': '写入失败：%r' % e})
                    continue
            out.append({'文件': fn, '投标人': unit, '已补': filled,
                        '已写入': bool(filled and want_write),
                        '清掉旧标记': dropped_mark, '样例': preview})
        total = sum(x.get('已补', 0) for x in out)
        return self._send(200, {
            'ok': True, '任务': 'fix_pending', '项目': pname,
            '干跑': not want_write, '共补': total, '明细': out,
            '说明': '格式固定为三行：要点：… / 出处：… / 疑问：…；'
                    '请复核人核对「疑问」那句是否说到了点上。'})

    def _parse_rules(self, params):
        """解析评分标准（结构化：每项判定方式 / 需读字段 / 挖到的条件）。"""
        import autoscore
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数：解读必须指定项目，'
                                  '避免套用其他项目的标准')
        rules, rpath = load_project_rules(pname)
        if rules is None:
            return self._err(404, '项目「%s」尚未保存评分标准（预期文件：%s）'
                                  % (pname, rpath))
        items = []
        for g in rules.get('分组') or []:
            for it in g.get('项') or []:
                txt = (it.get('评分标准') or '').strip()
                rec = {'分组': g.get('名称'), '序号': it.get('序号'),
                       '名称': it.get('名称'), '分值': it.get('分值'),
                       '标准字数': len(txt), '标准': txt}
                rec['需读字段'] = read_fields(txt)
                if it.get('主观项'):
                    rec['判定方式'] = '主观分档（评委打分）'
                    rec['判定方'] = 'agent读文'
                    rec['模式'] = '主观分档（由评委/agent 判断档位）'
                    rec['可自动'] = False
                elif not txt:
                    rec['判定方式'] = '缺文本'
                    rec['判定方'] = '无法判定'
                    rec['模式'] = '缺少评分标准文本'
                    rec['可自动'] = False
                else:
                    c = autoscore.mine(txt)
                    rec['挖出条件'] = {k: v for k, v in c.items() if k != '原文'}
                    if c.get('分级档位'):
                        rec['判定方式'] = '分级计数式'
                        rec['判定方'] = '规则可复算'
                        rec['模式'] = '分级计数式（按级别计数，可程序复算）'
                        rec['可自动'] = True
                    elif c.get('单项分') is not None:
                        rec['判定方式'] = '计数式'
                        rec['判定方'] = '规则可复算'
                        rec['模式'] = '计数式（每项得X分，可程序复算）'
                        rec['可自动'] = True
                    else:
                        rec['判定方式'] = '条件判定式'
                        rec['判定方'] = 'agent读文'
                        rec['模式'] = ('条件判定式（需读材料判断，由复核 agent 完成，'
                                       '不需要人工）')
                        rec['可自动'] = False
                items.append(rec)
        have_score = len([i for i in items
                          if isinstance(i.get('分值'), (int, float))])
        no_score = len(items) - have_score
        graded = len([i for i in items if (i.get('挖出条件') or {}).get('分级档位')])

        def _sum_score(rows):
            t = 0.0
            for r0 in rows:
                try:
                    t += float(r0.get('分值') or 0)
                except Exception:
                    pass
            return round(t, 2)

        auto_items = [i for i in items if i.get('可自动')]
        auto_score = _sum_score(auto_items)
        manual_items = [i for i in items if not i.get('可自动')]
        manual_score = _sum_score(manual_items)
        total_score = _sum_score(items)
        report = {'项目': rules.get('项目'), '生成时间': now(),
                  '评分项数': len(items), '总分': total_score,
                  '有分值项数': have_score, '未填分值项数': no_score,
                  '分级计分项数': graded,
                  # 新口径（v1.7.7）：说的"能不能被程序独立复算"，不是"要不要人"
                  '规则可复算项数': len(auto_items),
                  '规则可复算分': auto_score,
                  '需读文项数': len(manual_items),
                  '需读文分': manual_score,
                  # 旧字段保留（兼容旧界面/旧报告）
                  '可自动项数': len(auto_items),
                  '可自动分': auto_score, '需人工分': manual_score,
                  '说明': ('「规则可复算」= 程序能按"每项X分/分级"直接算出分数，'
                           '用于复核后交叉校验；「需读文判定」= 要读证书、工龄、'
                           '注册证等材料才能判，由复核 agent 完成，'
                           '不需要人工。两者都是自动复核。'),
                  '结果': items}
        pf = None
        if pname:
            pd0 = project_dir_for(pname)
            if pd0:
                pf = os.path.join(pd0, '评分标准_解读.json')
                try:
                    with open(pf, 'w', encoding='utf-8') as f:
                        json.dump(report, f, ensure_ascii=False, indent=1)
                except Exception:
                    traceback.print_exc()
        report.update({'ok': True, '任务': 'parse_rules', '项目': pname,
                       '规则来源': rpath, '解读文件': pf})
        return self._send(200, report)

    def _autoscore_check(self, params):
        """用规则独立复算「规则可复算」的项，与复核得分比对（v1.7.7）。

        只报"规则真的算出来了、且与复核得分不一致"的项；规则算不出来的
        （正文里没有金额+日期行）单列为"无法复算"，不当作差异。
        目的是交叉校验，不替代复核结论。
        """
        import autoscore

        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        rd = os.path.join(pd, '复核报告') if pd else None
        if not rd or not os.path.isdir(rd):
            return self._err(404, '项目「%s」没有复核报告目录' % pname)
        rules, rpath = load_project_rules(pname)
        if rules is None:
            return self._err(404, '项目「%s」尚未保存评分标准' % pname)

        # 规则 → {名称: (判定方式, 评分标准文本)}
        rule_map = {}
        for g in rules.get('分组') or []:
            for it in g.get('项') or []:
                nm = (it.get('名称') or '').strip()
                if not nm:
                    continue
                txt = (it.get('评分标准') or '').strip()
                c = autoscore.mine(txt) if txt else {}
                if c.get('分级档位'):
                    way = '分级计数式'
                elif c.get('单项分') is not None:
                    way = '计数式'
                else:
                    way = '条件判定式'
                rule_map[nm] = (way, txt, c)

        # 投标正文（按单位）：取该单位目录下**信息量最大**的 .txt
        # （.txt 是文字层抽取，.ocr.txt 是整页 OCR 结果，后者常常更全）
        bdir = os.path.join(pd, '投标文件')
        txt_of = {}
        if os.path.isdir(bdir):
            for u in os.listdir(bdir):
                up = os.path.join(bdir, u)
                if not os.path.isdir(up):
                    continue
                cands = []
                for fn in os.listdir(up):
                    if not fn.endswith('.txt'):
                        continue
                    fp = os.path.join(up, fn)
                    if not os.path.isfile(fp):
                        continue
                    try:
                        sz = os.path.getsize(fp)
                    except Exception:
                        sz = 0
                    cands.append((sz, fp))
                if not cands:
                    continue
                cands.sort(reverse=True)
                txt_of[u] = cands[0][1]

        only_unit = (params.get('投标单位') or '').strip()
        files = sorted(fn for fn in os.listdir(rd) if fn.endswith('_复核结果.json'))
        if only_unit:
            target = only_unit + '_复核结果.json'
            files = [fn for fn in files if fn == target]
        if not files:
            return self._err(404, '没有复核结果文件')

        checked, same, diff, cannot, arith = 0, 0, [], [], []
        for fn in files:
            try:
                with open(os.path.join(rd, fn), encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            unit = d.get('投标人') or fn.replace('_复核结果.json', '')
            lines = []
            if txt_of.get(unit):
                try:
                    with open(txt_of[unit], encoding='utf-8',
                              errors='ignore') as f:
                        lines = f.read().split('\n')
                except Exception:
                    lines = []
            for it in (d.get('评分') or []):
                nm = (it.get('名称') or '').strip()
                hit = rule_map.get(nm)
                if not hit:
                    continue
                way, txt, cond = hit
                if way == '条件判定式':
                    continue                      # 只能读文判，不参与规则复算
                checked += 1
                try:
                    a = float(it.get('得分') or 0)
                except Exception:
                    a = 0.0
                try:
                    fv = float(it.get('分值') or 0)
                except Exception:
                    fv = 0.0

                # (1) 算术自检：不需要正文，任何项目都能做
                per = cond.get('单项分')
                cap = cond.get('封顶') or fv
                levels = cond.get('分级档位') or []
                if a > fv + 0.01:
                    arith.append({'投标人': unit, '名称': nm,
                                  '问题': '得分 %s 超过该项分值 %s' % (a, fv)})
                elif per and not levels:
                    if not (abs(a - cap) <= 0.01 or
                            abs(a / float(per) - round(a / float(per))) <= 1e-6):
                        arith.append({'投标人': unit, '名称': nm,
                                      '问题': '得 %s 分不是单项分 %s 的整数倍'
                                              '（也不是封顶 %s）' % (a, per, cap)})
                elif levels:
                    vals = [float(x.get('单项分') or 0) for x in levels]
                    vals = [v for v in vals if v > 0]
                    if vals and not _reachable(a, vals, cap):
                        arith.append({'投标人': unit, '名称': nm,
                                      '问题': '得 %s 分无法由档位分值 %s 相加得到'
                                              % (a, vals)})

                # (2) 正文复算：正文里能找到"金额+日期"数据行时才有结果
                try:
                    r = autoscore.score_item(txt, lines) if lines else \
                        {'分值': 0.0, '命中': 0, '依据': '没有正文'}
                except Exception as e:
                    cannot.append({'投标人': unit, '名称': nm,
                                   '原因': '规则异常：%r' % e})
                    continue
                if not r.get('命中'):
                    cannot.append({'投标人': unit, '名称': nm,
                                   '原因': r.get('依据') or '正文无可识别数据行'})
                    continue
                rv = round(float(r.get('分值') or 0), 2)
                if abs(rv - round(a, 2)) <= 0.01:
                    same += 1
                else:
                    diff.append({'投标人': unit, '名称': nm,
                                 '复核得分': round(a, 2), '规则复算': rv,
                                 '方向': '规则多（复核可能漏算）' if rv > a
                                         else '规则少（多为扫描件数不到）',
                                 '规则依据': (r.get('依据') or '')[:120]})

        return self._send(200, {
            'ok': True, '任务': 'autoscore_check', '项目': pname,
            '规则来源': rpath,
            '检查项数': checked, '一致': same,
            '差异数': len(diff), '差异': diff[:50],
            '算术可疑数': len(arith), '算术可疑': arith[:50],
            '无法复算数': len(cannot), '无法复算': cannot[:30],
            '说明': ('分两层：①「算术自检」不需要正文——得分是否超过分值、'
                     '是否单项分的整数倍、分级项能否由档位相加得到；'
                     '②「正文复算」只在正文能找到"金额+日期"数据行时才有结论，'
                     '算不出来就记为「无法复算」，属正常。'
                     '「差异」里看「方向」：规则多＝复核可能漏算，要重点看；'
                     '规则少＝多为业绩表是扫描件、规则只数到文字层，属常见。'),
        })

    def _stopcheck(self, params):
        """查任务是否已被停止（替代 agent 侧 `python stopcheck.py`）。

        返回 已停止=True 时，agent 必须立刻停手、不得写复核结果。
        """
        tid = (params.get('id') or params.get('任务id') or '').strip()
        if not tid:
            return self._err(400, '缺少 id 参数')
        fp = os.path.join(QUEUE, tid + '.json')
        if not os.path.isfile(fp):
            return self._send(200, {'ok': True, 'id': tid, '状态': None,
                                    '已停止': True,
                                    '说明': '任务不存在（可能已被删除），不要继续'})
        try:
            with open(fp, encoding='utf-8') as f:
                t = json.load(f)
        except Exception as e:
            return self._err(500, '读取任务失败：%r' % e)
        st = t.get('状态') or ''
        allow = ('待处理', '处理中', '复核未完成')
        stop = st not in allow
        return self._send(200, {
            'ok': True, 'id': tid, '状态': st, '已停止': stop,
            '投标人': t.get('投标人') or '', '项目': t.get('项目') or '',
            '说明': ('可以继续复核' if not stop else
                     '不要继续：任务状态为「%s」' % (st or '未知'))})

    def _ocr_pages(self, params):
        """按页 OCR（P0-2）。

        参数：
            项目 / 投标单位（必填）
            页     : [12,13] 或 "12,13"
            范围   : "p120-140" / "120-140"（与 页 二选一）
            全部图 : 默认 true —— 同一页的 i0/i1/i2… 全部识别（漏图是主要风险源）
            强制   : 默认 false —— true 时忽略缓存重新识别
            含文字 : 默认 true —— 是否在返回里带上识别文字
            最多   : 单次页数上限，默认 20（超过请分批）

        返回：结果[每页]、覆盖{请求页数,命中,缺失}、无文字页、疑似空页。
        """
        import ocr_text as _ot

        def _page_set():
            out = []
            raw = params.get('页')
            if isinstance(raw, (list, tuple)):
                for x in raw:
                    try:
                        out.append(int(x))
                    except Exception:
                        pass
            elif raw not in (None, ''):
                for x in re.split(r'[,，\s]+', str(raw)):
                    if x.isdigit():
                        out.append(int(x))
            rng = (params.get('范围') or '').strip()
            if rng:
                m = re.match(r'^[pP]?\s*(\d+)\s*[\-~至]\s*[pP]?\s*(\d+)$', rng)
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    if a > b:
                        a, b = b, a
                    out.extend(range(a, b + 1))
                else:
                    for x in re.split(r'[,，\s]+', rng.lstrip('pP')):
                        if x.isdigit():
                            out.append(int(x))
            return sorted(set(p for p in out if p > 0))

        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        if not unit:
            return self._err(400, '缺少「投标单位」参数')
        pd = project_dir_for(pname)
        up = os.path.join(pd, '投标文件', unit) if pd else None
        if not up or not os.path.isdir(up):
            return self._err(404, '项目「%s」下没有投标单位「%s」' % (pname, unit))
        bids = pick_bid_files(up)
        if not bids:
            return self._err(400, '「%s」目录下没有投标文件' % unit)
        want_file = (params.get('文件') or params.get('册') or '').strip()
        bid = None
        if want_file:
            bid = next((x for x in bids
                        if os.path.basename(x) == want_file
                        or os.path.splitext(os.path.basename(x))[0] == want_file),
                       None)
            if not bid:
                return self._err(404, '「%s」下没有名为「%s」的投标文件（可选：%s）'
                                 % (unit, want_file,
                                    '、'.join(os.path.basename(x) for x in bids)))
        if not bid:
            bid = bids[0]
        pdir = bid + '.pages'
        if not os.path.isdir(pdir):
            return self._err(400, '「%s」还没有导出扫描页（.pages 不存在）'
                             % os.path.basename(bid))

        pages = _page_set()
        if not pages:
            return self._err(400, '缺少页范围：请传 页:[1,2] 或 范围:"p120-140"')
        cap = int(params.get('最多') or 20)
        cap = max(1, min(cap, 60))
        if len(pages) > cap:
            return self._err(400, '一次请求 %d 页，超过上限 %d 页；'
                                  '请分批（每批建议 10~20 页）'
                             % (len(pages), cap))

        info = _ot.collect_page_images(pdir)
        per = info.get('页') or {}
        all_images = bool(params.get('全部图', True))
        force = bool(params.get('强制'))
        with_text = bool(params.get('含文字', True))

        missing = [p for p in pages if p not in per]
        rows, empty_pages, suspect = [], [], []
        for p in pages:
            imgs = per.get(p) or []
            use = imgs if all_images else imgs[:1]
            texts, srcs = [], []
            for img in use:
                try:
                    cached = _ot._cache_file(img)
                    hit = (not force) and os.path.isfile(cached)
                    # M9：与预热带锁路径统一单飞锁（HEAVY_LOCK），
                    # 避免与后台预热并发时各开满线程、同一页算两遍
                    with HEAVY_LOCK:
                        txt = _ot.ocr_image(img, use_cache=not force,
                                            foreground=True)
                except Exception as e:
                    txt = ''
                    srcs.append('失败：%r' % e)
                    texts.append('')
                    continue
                texts.append(txt or '')
                srcs.append('缓存' if hit else '新识别')
            joined = '\n'.join(t for t in texts if t)
            rec = {'页': p, '图数': len(imgs), '读取图数': len(use),
                   '识别字数': len(joined),
                   '来源': '缓存' if srcs and all(x == '缓存' for x in srcs)
                           else ('新识别' if srcs else '无图')}
            if with_text:
                rec['文字'] = joined if len(joined) <= 20000 else joined[:20000]
                if len(joined) > 20000:
                    rec['截断'] = True
            if not imgs:
                missing.append(p)
            elif not joined:
                rec['疑似空页'] = True
                suspect.append(p)
            rows.append(rec)

        covered = [r['页'] for r in rows if r['图数'] > 0]
        return self._send(200, {
            'ok': True, '任务': 'ocr_pages', '项目': pname, '投标单位': unit,
            '页目录': pdir, '结果': rows,
            '覆盖': {'请求页数': len(pages), '命中': len(covered),
                     '缺失': sorted(set(missing))},
            '无文字页': sorted(suspect),
            '说明': ('「疑似空页」= 该页有图但识别不到文字，须回原图核对，'
                     '不得当作"没有该材料"；「覆盖.缺失」= 该页没有导出扫描图。')
        })

    def _page_crop(self, params):
        """裁图：返回某页指定矩形区域放大后的 PNG（base64），供 agent 回原图核对手写/关键字段。

        参数：
            项目 / 投标单位（必填）、页（必填）
            x0,y0,x1,y1（原始像素坐标，缺省整页）
            倍数（默认 2，上限 6）
        返回：png_base64、原图尺寸、裁切范围、放大后尺寸。
        """
        import base64

        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        page = params.get('页')
        if not pname:
            return self._err(400, '缺少「项目」参数')
        if not unit:
            return self._err(400, '缺少「投标单位」参数')
        if page in (None, ''):
            return self._err(400, '缺少「页」参数')
        try:
            page = int(page)
        except Exception:
            return self._err(400, '「页」必须是数字')
        pd = project_dir_for(pname)
        up = os.path.join(pd, '投标文件', unit) if pd else None
        if not up or not os.path.isdir(up):
            return self._err(404, '项目「%s」下没有投标单位「%s」' % (pname, unit))
        bids = pick_bid_files(up)
        if not bids:
            return self._err(400, '「%s」目录下没有投标文件' % unit)
        pdir = bids[0] + '.pages'
        if not os.path.isdir(pdir):
            return self._err(400, '「%s」还没有导出扫描页（.pages 不存在）'
                             % os.path.basename(bids[0]))
        import ocr_text as _ot
        info = _ot.collect_page_images(pdir)
        per = (info or {}).get('页') or {}
        imgs = per.get(page) or []
        if not imgs:
            return self._err(404, '第 %d 页没有扫描图（可能是文字层页，未导出图片）'
                             % page)
        img_path = imgs[0]
        import cv2
        import numpy as np
        try:
            with open(img_path, 'rb') as f:
                buf = np.frombuffer(f.read(), dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None:
            return self._err(500, '读图失败：%s' % os.path.basename(img_path))
        h, w = img.shape[:2]

        def _n(k, default):
            v = params.get(k)
            try:
                return int(float(v))
            except Exception:
                return default

        x0 = max(0, min(w - 1, _n('x0', 0)))
        y0 = max(0, min(h - 1, _n('y0', 0)))
        x1 = max(x0 + 1, min(w, _n('x1', w)))
        y1 = max(y0 + 1, min(h, _n('y1', h)))
        try:
            zoom = float(params.get('倍数') or 2)
        except Exception:
            zoom = 2.0
        zoom = max(1.0, min(6.0, zoom))
        crop = img[y0:y1, x0:x1]
        if zoom != 1.0:
            crop = cv2.resize(crop, None, fx=zoom, fy=zoom,
                              interpolation=cv2.INTER_CUBIC)
        ok, enc = cv2.imencode('.png', crop)
        if not ok:
            return self._err(500, 'PNG 编码失败')
        b64 = base64.b64encode(enc.tobytes()).decode('ascii')
        return self._send(200, {
            'ok': True, '任务': 'page_crop', '页': page,
            '文件': os.path.basename(img_path), '原图尺寸': [w, h],
            '裁切': [x0, y0, x1, y1], '倍数': zoom,
            '尺寸': [crop.shape[1], crop.shape[0]], 'png_base64': b64})

    def _ocr_search(self, params):
        """扫描页 OCR 全文检索（S14）：遍历该册 .ocr_cache，返回关键词命中页/句。

        参数：
            项目 / 投标单位（必填）、关键词（列表或逗号/空格分隔）、可选 文件/册
        返回：命中[{页,关键词,命中句}]、命中页数、已覆盖页数、未覆盖页（有图但无缓存）。
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        if not unit:
            return self._err(400, '缺少「投标单位」参数')
        raw_kw = params.get('关键词')
        if isinstance(raw_kw, (list, tuple)):
            kws = [str(k).strip() for k in raw_kw if str(k).strip()]
        else:
            kws = [k.strip() for k in re.split(r'[,，、\s]+', str(raw_kw or ''))
                   if k.strip()]
        if not kws:
            return self._err(400, '缺少「关键词」参数')
        pd = project_dir_for(pname)
        up = os.path.join(pd, '投标文件', unit) if pd else None
        if not up or not os.path.isdir(up):
            return self._err(404, '项目「%s」下没有投标单位「%s」' % (pname, unit))
        bids = pick_bid_files(up)
        if not bids:
            return self._err(400, '「%s」目录下没有投标文件' % unit)
        want_file = (params.get('文件') or params.get('册') or '').strip()
        bid = None
        if want_file:
            bid = next((x for x in bids
                        if os.path.basename(x) == want_file
                        or os.path.splitext(os.path.basename(x))[0] == want_file),
                       None)
        if not bid:
            bid = bids[0]
        pdir = bid + '.pages'
        if not os.path.isdir(pdir):
            return self._err(400, '「%s」还没有导出扫描页（.pages 不存在）'
                             % os.path.basename(bid))
        import ocr_text as _ot
        cache_dir = os.path.join(pdir, _ot.CACHE_DIRNAME)
        hits, covered = [], set()
        if os.path.isdir(cache_dir):
            for fn in sorted(os.listdir(cache_dir)):
                if not fn.endswith('.json'):
                    continue
                fp = os.path.join(cache_dir, fn)
                try:
                    with open(fp, encoding='utf-8') as f:
                        d = json.load(f)
                except Exception:
                    continue
                txt = (d or {}).get('文字') if isinstance(d, dict) else ''
                if not isinstance(txt, str) or not txt:
                    continue
                m = re.match(r'[pP](\d+)', fn)
                page = int(m.group(1)) if m else None
                if page is not None:
                    covered.add(page)
                for kw in kws:
                    if kw not in txt:
                        continue
                    idx = txt.find(kw)
                    ctx = txt[max(0, idx - 20):idx + len(kw) + 20].replace('\n', ' ')
                    hits.append({'页': page, '关键词': kw, '命中句': ctx})
                    break
        info = _ot.collect_page_images(pdir)
        per = (info or {}).get('页') or {}
        uncovered = sorted(p for p in per if p not in covered)
        return self._send(200, {
            'ok': True, '任务': 'ocr_search', '项目': pname, '投标单位': unit,
            '关键词': kws, '命中': hits,
            '命中页数': len(set(h['页'] for h in hits if h['页'] is not None)),
            '已覆盖页数': len(covered), '未覆盖页': uncovered,
            '说明': '「未覆盖页」= 有扫描图但尚无 OCR 缓存，建议先 ocr_pages 补识别再检索',
        })

    def _report_issue(self, params):
        """复核问题反馈：agent 判分中发现口径/识别/证据/规则问题，分类上报。

        参数：项目、投标单位、类别（口径|识别|证据|规则）、现象（必填）、
              涉及评分项、涉及页、建议。
        落盘 work\\复核问题反馈.json，前端「问题反馈」清单可见。
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        cat = (params.get('类别') or '').strip() or '其他'
        note = (params.get('现象') or '').strip()
        if not note:
            return self._err(400, '缺少「现象」参数')
        issue_add(pname, unit, cat, note,
                  涉及评分项=(params.get('涉及评分项') or '').strip(),
                  涉及页=(params.get('涉及页') or '').strip(),
                  建议=(params.get('建议') or '').strip())
        return self._send(200, {'ok': True, '任务': 'report_issue',
                                '已记录': note[:80]})

    def _verify_evidence(self, params):
        """P1-1：证据强校验命令——agent 结单前自查出处页码与依据数字是否真实可查。

        参数：项目、投标单位。读该单位 `复核报告\<单位>_复核结果.json`，
        返回「存疑」清单（页码缺失/证据存疑）。
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        if not unit:
            return self._err(400, '缺少「投标单位」参数')
        pd = project_dir_for(pname)
        if not pd:
            return self._err(404, '项目「%s」不存在' % pname)
        fp = os.path.join(pd, '复核报告', unit + '_复核结果.json')
        if not os.path.isfile(fp):
            return self._err(404, '未找到「%s」的复核结果（%s）' % (unit, fp))
        try:
            with open(fp, encoding='utf-8') as f:
                d = json.load(f)
        except Exception as e:
            return self._err(500, '读取结果失败：%r' % e)
        evd = verify_result_evidence(d, pname, d.get('投标人') or unit)
        sus = evd['存疑']
        if sus:
            d['证据存疑'] = sus
            try:
                with open(fp, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
            except Exception:
                traceback.print_exc()
        else:
            d.pop('证据存疑', None)
            try:
                with open(fp, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
            except Exception:
                traceback.print_exc()
        return self._send(200, {
            'ok': True, '任务': 'verify_evidence', '项目': pname,
            '投标单位': d.get('投标人') or unit,
            '存疑条数': len(sus), '存疑': sus[:60],
            '说明': '「页码缺失」= 出处该页无文字层也无 OCR；'
                    '「证据存疑」= 依据里的长数字串在该页文本找不到，'
                    '可能是编造证据或 OCR 漏识别，须回原图核对后再结单。'})

    def _claim(self, params):
        """把某单位的「待处理」任务标记为「处理中」，并记录开始时间。

        agent 开始复核时调用，前端「任务总览」即可实时反映在跑状态。
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        hit = []
        for fp in glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                with open(fp, encoding='utf-8') as f:
                    t = json.load(f)
            except Exception:
                continue
            if t.get('项目') != pname:
                continue
            if unit and t.get('投标人') != unit:
                continue
            if t.get('状态') not in ('待处理', '处理中'):
                continue
            t['状态'] = '处理中'
            if not t.get('开始时间'):
                t['开始时间'] = now()
            t['更新时间'] = now()
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(t, f, ensure_ascii=False, indent=1)
            hit.append({'id': t.get('id'), '投标人': t.get('投标人'),
                        '开始时间': t.get('开始时间')})
        return self._send(200, {'ok': True, '已认领': hit,
                                '数量': len(hit),
                                '提示': ('已标记 %d 个任务为处理中' % len(hit))
                                        if hit else '没有可认领的待处理任务'})

    # ---------- 解析体检（发现静默丢数据） ----------
    def _health(self, params):
        """检查投标文件的解析完整性：图片解码、文字层、空页。

        专门用来发现「不报错但少了东西」的问题：
          - 图片对象数 vs 解码成功数（滤镜不支持会静默失败）
          - 无文字页中有多少页其实也没有图片（可能是漏导出或真空白）
        """
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        bdir = os.path.join(pd, '投标文件')
        units = []
        if os.path.isdir(bdir):
            for d in sorted(os.listdir(bdir)):
                dp = os.path.join(bdir, d)
                if not os.path.isdir(dp) or d.endswith('.pages'):
                    continue
                if unit and d != unit:
                    continue
                pdfs = [x for x in os.listdir(dp)
                        if x.lower().endswith(('.pdf', '.docx', '.doc'))]
                if not pdfs:
                    continue
                # L6：遍历全部文件（多册项目每册都要体检，不能只看第一个）
                for pdf in pdfs:
                    units.append((d, dp, os.path.join(dp, pdf)))

        out = []
        for name, dp, pdf in units:
            item = {'投标单位': name, '文件': os.path.basename(pdf),
                   '告警': []}
            txt = pdf + '.txt'
            pdir = pdf + '.pages'
            pages = 0
            no_text = 0
            if os.path.isfile(txt):
                with open(txt, encoding='utf-8', errors='ignore') as f:
                    lines = f.read().split('\n')
                pages = len(lines)
                no_text = sum(1 for x in lines if len(x.strip()) <= 2)
            item['页数'] = pages
            item['无文字页'] = no_text
            nimgs = 0
            if os.path.isdir(pdir):
                for fn in os.listdir(pdir):
                    if fn.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp',
                                            '.webp', '.tif', '.tiff')):
                        nimgs += 1
            item['已导出图片'] = nimgs
            sp = os.path.join(pdir, '.export_stats.json')
            if os.path.isfile(sp):
                try:
                    with open(sp, encoding='utf-8') as f:
                        st = json.load(f)
                except Exception:
                    st = None
                if st:
                    item['图片对象'] = st.get('图片对象')
                    item['解码成功'] = st.get('解码成功')
                    item['解码失败'] = st.get('解码失败') or {}
                    item['滤镜类型'] = st.get('滤镜类型') or {}
                    bad = sum((item['解码失败'] or {}).values())
                    if bad:
                        item['告警'].append(
                            '有 %d 个图片对象解码失败：%s'
                            % (bad, '、'.join(list(item['解码失败'])[:3])))
            if pages and no_text > 0 and nimgs < no_text * 0.5:
                item['告警'].append(
                    '无文字页 %d 页，但只导出 %d 张图——可能有页面未导出'
                    % (no_text, nimgs))
            if pages and no_text > pages * 0.8 and nimgs == 0:
                item['告警'].append('几乎所有页面无文字且无图片，可能解析失败')
            item['结论'] = '正常' if not item['告警'] else '需检查'
            out.append(item)

        return self._send(200, {'ok': True, '项目': pname, '单位': out,
                                '需检查': [x['投标单位'] for x in out
                                          if x['告警']],
                                '说明': '体检项：页数 / 无文字页 / 已导出图片 / '
                                        '图片对象与解码结果 / 滤镜类型'})

    # ---------- 证据齐备性校验 / 结单闸门 ----------
    def _audit_evidence(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        try:
            import evidence_spec as es
        except Exception as e:
            return self._err(500, '证据清单模块不可用：%r' % e)
        spec = es.load_spec(pd)
        if not spec:
            return self._err(400, '项目「%s」尚无证据清单，请先生成' % pname)
        rd = os.path.join(pd, '复核报告')
        units = []
        for fn in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
            if not fn.endswith('_复核结果.json'):
                continue
            try:
                with open(os.path.join(rd, fn), encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            r = es.check_result(d, spec)
            units.append({'投标人': d.get('投标人') or fn, '可结单': r['通过'],
                          '阻断': r['阻断'], '警告': r['警告'],
                          '提示': r['提示'], '结论': r['结论'],
                          '未确认项数': r['未确认项数']})
        blocked = [u['投标人'] for u in units if not u['可结单']]
        return self._send(200, {'ok': True, '任务': 'audit_evidence',
                                '项目': pname, '单位': units,
                                '不可结单': blocked,
                                '说明': '阻断＝明细表缺整列或无明细表，须补表格；'
                                        '警告＝个别单元格漏填，允许结单；'
                                        '提示＝投标人确实未提供材料，已计入得分。'})

    def _finalize(self, params):
        pname = (params.get('项目') or '').strip()
        unit = (params.get('投标单位') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        rd = os.path.join(pd, '复核报告')
        if not unit:
            return self._err(400, '缺少「投标单位」参数：结单必须显式指定，'
                                  '不允许按文件名排序取第一份')
        target = unit + '_复核结果.json'
        if not os.path.isfile(os.path.join(rd, target)):
            # G1（2026-11-11）：精确匹配，防止子串命中别家、或空 unit 取错首份
            cands = ([fn for fn in sorted(os.listdir(rd))
                      if fn.endswith('_复核结果.json')]
                     if os.path.isdir(rd) else [])
            return self._err(404, '未找到「%s」的复核结果（精确文件名 %s）%s'
                             % (unit, target,
                                ('；现有：' + '、'.join(cands)) if cands else ''))
        with open(os.path.join(rd, target), encoding='utf-8') as f:
            d = json.load(f)
        real_unit = d.get('投标人') or unit

        # 字段规整（v1.8.1）：历史上 出处 有写成数组的，统一成文本后再校验/写回
        for _it in (d.get('评分') or []):
            for _k in ('出处', '依据', '待核原因', '名称', '连带说明'):
                if _k in _it and not isinstance(_it[_k], (str, type(None))):
                    _it[_k] = as_text(_it[_k])

        # S1a 数字自洽（v1.7.8；M8 抽公共函数供归档闸门复用）：
        # 小计必须等于逐项得分之和，满分必须等于逐项分值之和。
        _bad_sum = total_consistency_errors(d)
        if _bad_sum:
            return self._err(400, '%s 的结果数字不自洽：%s\n'
                                  '请先改正 小计/满分（或修改相应评分项的得分/分值）'
                                  '再调用 finalize；这次没有改队列状态。'
                             % (real_unit, '；'.join(_bad_sum)))

        # 待核原因必填（v1.7.4）：需人工项若没写清"谁 / 哪份材料 / 卡在哪条要求"，
        # 汇总里只能显示一句空话，等于把解释责任推给读者。
        _miss = []
        for _it in (d.get('评分') or []):
            if not _it.get('需人工'):
                continue
            _ok, _why = check_pending_reason(_it.get('待核原因'))
            if _ok:
                continue
            _miss.append('序%s %s（%s）' % (_it.get('序号') or '?',
                                          (_it.get('名称') or '')[:18], _why))
        if _miss:
            return self._err(400, '有 %d 个「需人工」项的待核原因不合规：\n%s\n'
                                  '格式固定三行：要点：… / 出处：…（含 p12 页码）/ '
                                  '疑问：…（≥12 字，写清要人判断什么）；'
                                  '也可以先调 {"任务":"fix_pending",'
                                  '"参数":{"项目":"%s","覆盖":true,"写入":true}} 生成初稿，'
                                  '再由复核人确认。'
                             % (len(_miss), '\n'.join('· ' + x for x in _miss[:8]),
                                pname))

        # S4（v1.7.9）：启用权重折算的项目，结单时把折算写进结果文件，
        # 这样单看某家的结果也能看到"折算得分"，不必去翻汇总。
        _rules_fin, _rp_fin = load_project_rules(pname)
        _wdet, _wtot_s, _wtot_f = weighted_detail(_rules_fin, d.get('评分') or [])
        if _wdet is not None:
            d['权重折算'] = True
            d['折算'] = _wdet
            d['折算得分'] = _wtot_s
            d['折算满分'] = _wtot_f
            try:
                with open(os.path.join(rd, target), 'w', encoding='utf-8') as _wf:
                    json.dump(d, _wf, ensure_ascii=False, indent=1)
            except Exception:
                traceback.print_exc()

        # P1-1：证据强校验——出处页码真实 + 依据长数字串可查（防注入诱导编造证据）
        _evd = verify_result_evidence(d, pname, real_unit)
        if _evd['存疑']:
            d['证据存疑'] = _evd['存疑']
        else:
            d.pop('证据存疑', None)
        # P1-1：异常高分/全满分告警（提示，不阻断）
        try:
            _rows = d.get('评分') or []
            _full_f = float(d.get('满分') or 0)
            _scored = sum(float(x.get('得分')) or 0 for x in _rows)
            _maxed = _full_f > 0 and _scored >= _full_f - 0.01
            d['高分告警'] = ('全项满分，请人工复核确认' if _maxed else '')
        except Exception:
            d['高分告警'] = ''
        if not d.get('高分告警'):
            d.pop('高分告警', None)
        try:
            with open(os.path.join(rd, target), 'w', encoding='utf-8') as _wf:
                json.dump(d, _wf, ensure_ascii=False, indent=1)
        except Exception:
            traceback.print_exc()

        # 校验
        import evidence_spec as es
        spec = es.load_spec(pd)
        chk = es.check_result(d, spec) if spec else {
            '通过': True, '阻断': [], '警告': [],
            '提示': ['项目尚无证据清单，跳过齐备性校验'], '未确认项数': 0,
            '结论': '未校验'}
        blocked = not chk['通过']

        # 更新队列状态
        new_status = '复核未完成' if blocked else '已完成'
        updated = []
        import glob as _glob
        for fp in _glob.glob(os.path.join(QUEUE, '*.json')):
            try:
                with open(fp, encoding='utf-8') as f:
                    t = json.load(f)
            except Exception:
                continue
            if t.get('项目') != pname or t.get('投标人') != real_unit:
                continue
            t['状态'] = new_status
            t['齐备性校验'] = {'可结单': chk['通过'], '阻断': chk['阻断'],
                              '警告': chk['警告'], '提示': chk['提示']}
            if not blocked:
                t['结果文件'] = os.path.join(rd, target)
                t.setdefault('完成时间', now())
                t['得分'] = d.get('小计')
                t['满分'] = d.get('满分')
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(t, f, ensure_ascii=False, indent=1)
            updated.append(t.get('id'))

        if blocked:
            return self._send(200, {'ok': False, '任务': 'finalize',
                                    '投标人': real_unit, '状态': new_status,
                                    '阻断': chk['阻断'], '警告': chk['警告'],
                                    '提示': chk['提示'],
                                    '错误': '证据表存在结构性缺陷，已置为'
                                            '「复核未完成」，补齐后才能结单'})
        return self._send(200, {'ok': True, '任务': 'finalize',
                                '投标人': real_unit, '状态': new_status,
                                '警告': chk['警告'], '提示': chk['提示'],
                                '队列': updated,
                                '得分': d.get('小计'), '满分': d.get('满分')})

    # ---------- 识别计划（定位 → 按分值优先） ----------
    def _ocr_plan(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        rules_path = os.path.join(pd, '评分标准.json')
        bdir = os.path.join(pd, '投标文件')
        want = (params.get('投标单位') or '').strip()
        units = []
        if os.path.isdir(bdir):
            for name in sorted(os.listdir(bdir)):
                up = os.path.join(bdir, name)
                if not os.path.isdir(up) or name.endswith('.pages'):
                    continue
                if want and name != want:
                    continue
                pdfs = [os.path.join(up, x) for x in sorted(os.listdir(up))
                        if x.lower().endswith('.pdf')]
                if not pdfs:
                    continue
                pdf = pdfs[0]
                txt = pdf + '.txt'
                if not os.path.isdir(pdf + '.pages') or not os.path.isfile(txt):
                    units.append({'投标人': name, '状态': '尚未抽取正文/扫描页'})
                    continue
                try:
                    import review_plan as rp
                    p = rp.plan(pdf, txt,
                                rules_path if os.path.isfile(rules_path) else
                                [{'序号': '1', '名称': '全部证据', '分值': 1}])
                    units.append({'投标人': name, '状态': 'ok',
                                  '计划': p, '估算': rp.estimate(p)})
                except Exception as e:
                    traceback.print_exc()
                    units.append({'投标人': name, '状态': '失败：%r' % e})
        if not units:
            return self._err(404, '项目「%s」下没有可用的投标文件' % pname)
        return self._send(200, {'ok': True, '任务': 'ocr_plan', '项目': pname,
                                '单位': units,
                                '说明': '按「分值/图片数」从高到低识别；'
                                        '先定位证据章节，只识别必需页'})

    # ---------- 扫描页完整性审计 ----------
    def _check_images(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        import ocr_text
        bdir = os.path.join(pd, '投标文件')
        units = []
        if os.path.isdir(bdir):
            for name in sorted(os.listdir(bdir)):
                up = os.path.join(bdir, name)
                if not os.path.isdir(up) or name.endswith('.pages'):
                    continue
                pds = [d for d in sorted(os.listdir(up)) if d.endswith('.pages')]
                if not pds:
                    units.append({'投标人': name, '状态': '未导出扫描页'})
                    continue
                pdir = os.path.join(up, pds[0])
                a = ocr_text.audit_images(pdir)
                units.append({'投标人': name, '状态': a['结论'],
                              '页数': a['页数'], '图片数': a['图片数'],
                              '多图页数': len(a['多图页']),
                              '多图页': sorted(a['多图页'])[:40],
                              '扩展名': a['扩展名'], '警告': a['警告']})
        need = [u['投标人'] for u in units if u.get('警告')]
        return self._send(200, {'ok': True, '任务': 'check_images', '项目': pname,
                                '单位': units, '需注意': need,
                                '说明': '「一页多图」页必须逐张读取，'
                                        '不得只读 i0；扩展名混用时不得按扩展名过滤'})

    # ---------- 汇总导出 ----------
    def _export(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        fmt = (params.get('格式') or 'pdf').strip().lower().lstrip('.')
        if fmt not in ('pdf', 'docx', 'md'):
            return self._err(400, '不支持的格式：%s（可选 pdf / docx / md）' % fmt)
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目「%s」不存在' % pname)
        md_path = os.path.join(pd, '复核结果汇总.md')
        if fmt == 'md':
            if not os.path.isfile(md_path):
                return self._err(400, '尚未生成汇总，请先点「生成汇总」')
            return self._send(200, {'ok': True, '任务': 'export', '项目': pname,
                                    '格式': 'md', '文件': md_path,
                                    '字节': os.path.getsize(md_path)})
        if not os.path.isfile(md_path):
            return self._err(400, '尚未生成汇总，请先点「生成汇总」')
        out = os.path.join(pd, '复核结果汇总.' + fmt)
        try:
            import export_docs
            with open(md_path, encoding='utf-8') as f:
                md = f.read()
            export_docs.convert(md, out, fmt, pname)
        except Exception as e:
            return self._err(500, '导出失败：%r' % e)
        return self._send(200, {'ok': True, '任务': 'export', '项目': pname,
                                '格式': fmt, '文件': out,
                                '字节': os.path.getsize(out)})

    # ---------- 命令分发 ----------
    def _run_command(self):
        raw = self._body()
        try:
            d = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception as e:
            return self._err(400, 'JSON 解析失败：%s' % e)
        task = (d.get('任务') or d.get('task') or '').strip()
        params = d.get('参数') or {}
        # 心跳（v1.8.3）：agent 每次带 id 调接口都算"还活着"，僵死判定据此判断。
        try:
            _tid0 = (params.get('id') or '').strip() if isinstance(params, dict) else ''
            if _tid0:
                task_touch(_tid0)
        except Exception:
            pass
        if not task:
            return self._err(400, '缺少「任务」参数')

        if task == 'list':
            return self._send(200, {'ok': True, '任务': [
                {'名称': 'selfcheck', '说明': '自检投标文件关键材料'},
                {'名称': 'parse_rules', '说明': '解读评分标准'},
                {'名称': 'check_rules', '说明': '对照招标文件校验评分标准'},
                {'名称': 'autoscore', '说明': '按评分标准自动打分（已停用）'},
                {'名称': 'list', '说明': '列出可用命令'}]})

        if task == 'selfcheck':
            return self._selfcheck(params)

        if task == 'parse_rules':
            return self._parse_rules(params)

        if task == 'check_rules':
            pname = (params.get('项目') or '').strip()
            if not pname:
                return self._err(400, '缺少「项目」参数')
            rules, rpath = load_project_rules(pname)
            if rules is None:
                return self._err(404, '项目「%s」尚未保存评分标准' % pname)
            pd = project_dir_for(pname)
            tdir = os.path.join(pd, '招标文件') if pd else None
            txts = walk_files(tdir, '.txt') if tdir else []
            if not txts:
                return self._err(400, '项目「%s」的招标文件目录下没有正文(.txt)，'
                                      '请先上传招标文件' % pname)
            tender = ''
            for f in txts:
                try:
                    with open(f, encoding='utf-8') as fh:
                        tender += fh.read() + '\n'
                except Exception:
                    pass
            issues, items = [], []
            NUM = re.compile(r'(\d+(?:\.\d+)?)\s*分')

            TOTAL_RE = re.compile(
                r'(?:最多得|最高得|本项最高|本项最多得|合计|总分)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分')
            total = 0.0
            for g in rules.get('分组') or []:
                for it in g.get('项') or []:
                    nm = (it.get('名称') or '').strip()
                    v = it.get('分值')
                    if isinstance(v, (int, float)):
                        total += v
                    key = re.sub(r'[（(].*?[)）]', '', nm).strip()
                    pos = tender.find(key) if key else -1
                    rec = {'分组': g.get('名称'), '序号': it.get('序号'),
                           '名称': nm, '分值': v, '在招标文件中': pos >= 0}
                    if pos < 0:
                        issues.append({'级别': '未找到', '项': nm,
                                       '说明': '评分项名称未在招标文件正文中出现，'
                                               '可能是名称写法不同或漏项'})
                    # 用「该项自己的评分标准文本」校验分值是否自相矛盾：
                    # 若文本里写了「最多得 X 分 / 本项最高 X 分」，应与分值一致。
                    # 这样做不受招标文件排版影响，不会把"每项 0.5 分"误当项满分。
                    own = (it.get('评分标准') or '').replace('\n', '')
                    caps = [float(m.group(1)) for m in TOTAL_RE.finditer(own)]
                    if caps:
                        rec['文本中的本项满分'] = caps[0] if len(caps) == 1 else caps
                        # 一项内含多个子项封顶时（如幕墙8分+装修8分=16分），
                        # 子项封顶之和等于分值即属正常
                        ok_val = False
                        if isinstance(v, (int, float)):
                            if len(caps) > 1 and abs(sum(caps) - float(v)) < 1e-6:
                                ok_val = True
                            elif abs(caps[0] - float(v)) < 1e-6:
                                ok_val = True
                        if isinstance(v, (int, float)) and not ok_val:
                            if len(caps) > 1:
                                issues.append({
                                    '级别': '疑似不一致', '项': nm,
                                    '说明': '你填 %s 分；该项文本里的封顶之和为 %s 分'
                                            '（各子项：%s），请确认分值口径'
                                            % (v, round(sum(caps), 2), caps)})
                            else:
                                issues.append({
                                    '级别': '疑似不一致', '项': nm,
                                    '说明': '你填的分值是 %s 分，但该项评分标准文本'
                                            '写的是「%s」= %s 分'
                                            % (v, TOTAL_RE.search(own).group(0).strip(),
                                               caps[0])})
                    else:
                        rec['文本中的本项满分'] = None
                    if it.get('主观项') and it.get('名称'):
                        rec['主观项'] = True
                    items.append(rec)
            nums = [float(x) for x in
                    re.findall(r'评分合计\s*\|?\s*(\d+(?:\.\d+)?)', tender)]
            tender_total = nums[0] if nums else None
            if tender_total is not None and abs(tender_total - total) > 1e-6:
                if tender_total > total:
                    # 常见情形：招标文件总分 100（技术60+商务40），而只录入了其中一部分
                    issues.append({'级别': '提示', '项': '全部',
                                   '说明': '你设置合计 %s 分，招标文件评分合计 %s 分。'
                                           '若你只录入了其中某一部分（例如仅商务部分），'
                                           '属正常；若应录全表则可能漏项。'
                                           % (round(total, 2), tender_total)})
                else:
                    issues.append({'级别': '合计不符', '项': '全部',
                                   '说明': '你设置合计 %s 分，招标文件评分合计 %s 分'
                                           % (round(total, 2), tender_total)})
            if not issues:
                issues.append({'级别': '通过', '项': '—',
                               '说明': '未发现明显问题（已核对：评分项名称是否出现在'
                                       '招标文件、每项分值与该评分标准文本里的'
                                       '「最多得X分」是否一致、总分合计是否相符；'
                                       '分档细节仍需人工确认）'})
            rep = {'项目': pname, '生成时间': now(), '规则来源': rpath,
                   '招标文件数': len(txts), '你设置合计': round(total, 2),
                   '招标文件合计': tender_total,
                   '问题数': len([i for i in issues
                                if i['级别'] in ('疑似不一致', '未找到',
                                                '合计不符')]),
                   '问题': issues, '明细': items}
            pf = os.path.join(pd, '评分标准_校验.json')
            try:
                with open(pf, 'w', encoding='utf-8') as f:
                    json.dump(rep, f, ensure_ascii=False, indent=1)
            except Exception:
                traceback.print_exc()
            rep.update({'ok': True, '任务': task, '校验文件': pf})
            return self._send(200, rep)

        if task == 'autoscore':
            return self._err(400, '「自动打分」已停用，请使用「自检」或「开始解读」')

        if task == 'gen_points':
            return self._gen_points(params)

        if task == 'audit_citations':
            return self._audit_citations(params)

        if task == 'ocr_prewarm':
            return self._ocr_prewarm(params)

        if task == 'clean_pages':
            return self._clean_pages(params)

        if task == 'archive':
            return self._archive(params)

        if task == 'export':
            return self._export(params)

        if task == 'check_images':
            return self._check_images(params)

        if task == 'ocr_plan':
            return self._ocr_plan(params)

        if task == 'project_reset':
            return self._project_reset(params)

        if task == 'expert_reset':
            return self._expert_reset(params)

        if task == 'queue_stop':
            return self._queue_stop(params)

        if task == 'queue_wake':
            return self._queue_wake(params)

        if task == 'queue_reset':
            return self._queue_reset(params)

        if task == 'queue_resume':
            return self._queue_resume(params)

        if task == 'submit_stop':
            return self._submit_stop(params)

        if task == 'expert_submit':
            return self._expert_submit(params)

        if task == 'expert_status':
            return self._expert_status(params)

        if task == 'expert_import':
            return self._expert_import(params)

        if task == 'expert_progress':
            return self._expert_progress(params)

        if task == 'expert_extract':
            return self._expert_extract(params)

        if task == 'expert_load':
            return self._expert_load(params)

        if task == 'expert_save':
            return self._expert_save(params)

        if task == 'expert_compare':
            return self._expert_compare(params)

        if task == 'project_check':
            return self._project_check(params)

        if task == 'project_archive':
            return self._project_archive(params)

        if task == 'project_reopen':
            return self._project_reopen(params)

        if task == 'session_assign':
            return self._session_assign(params)

        if task == 'ocr_files':
            return self._ocr_files(params)

        if task == 'rescue_run':
            acted = rescue_once(do_wake=bool(params.get('唤醒', True)))
            return self._send(200, {'ok': True, '本次处置': acted,
                                    '自动兜底': '开' if AUTO_RESCUE else '关'})

        if task == 'autoscore_check':
            return self._autoscore_check(params)

        if task == 'fix_pending':
            return self._fix_pending(params)

        if task == 'stopcheck':
            return self._stopcheck(params)

        if task == 'ocr_pages':
            return self._ocr_pages(params)

        if task == 'page_crop':
            return self._page_crop(params)

        if task == 'ocr_search':
            return self._ocr_search(params)

        if task == 'report_issue':
            return self._report_issue(params)

        if task == 'verify_evidence':
            return self._verify_evidence(params)

        if task == 'claim':
            return self._claim(params)

        if task == 'health':
            return self._health(params)

        if task == 'audit_evidence':
            return self._audit_evidence(params)

        if task == 'finalize':
            return self._finalize(params)

        if task == 'read_bid':
            pname = (params.get('项目') or '').strip()
            if not pname:
                return self._err(400, '缺少「项目」参数')
            files = project_bid_files(pname)
            if not files:
                return self._err(400, '项目「%s」的投标文件目录下没有文件，'
                                      '请先上传投标文件' % pname)
            bid = params.get('投标文件') or files[0]
            # M6：路径白名单——只允许本项目投标文件目录下的文件，
            # 防止在任意路径旁写 .txt/.pages/解读.json
            _bid = os.path.abspath(str(bid))
            _allowed = [f for f in files if os.path.abspath(f) == _bid]
            if not _allowed:
                _base = os.path.basename(str(bid))
                _allowed = [f for f in files if os.path.basename(f) == _base]
                if len(_allowed) != 1:
                    return self._err(400, '「%s」不是项目「%s」的唯一投标文件'
                                     % (bid, pname))
            bid = _allowed[0]
            tid = task_new()
            threading.Thread(target=_read_bid_job,
                             args=(tid, bid, pname, params.get('OCR上限') or 60),
                             daemon=True).start()
            return self._send(200, {'ok': True, '任务': 'read_bid', 'id': tid,
                                    '投标文件': os.path.basename(bid)})

        return self._err(400, '未知任务：%s（可用：selfcheck, parse_rules, '
                              'check_rules, autoscore, read_bid, list）' % task)

    # ---------- 自检 ----------
    def _selfcheck(self, params):
        pname = (params.get('项目') or '').strip()
        if not pname:
            return self._err(400, '缺少「项目」参数')
        want = (params.get('投标单位') or '').strip()
        pd = project_dir_for(pname)
        if not pd or not os.path.isdir(pd):
            return self._err(404, '项目不存在：%s' % pname)
        bdir = os.path.join(pd, '投标文件')
        units = {}
        if os.path.isdir(bdir):
            for d in sorted(os.listdir(bdir)):
                dp = os.path.join(bdir, d)
                if not os.path.isdir(dp) or d.endswith('.pages'):
                    continue
                fs = [os.path.join(dp, x) for x in os.listdir(dp)
                      if not x.endswith(('.txt', '.json'))
                      and os.path.isfile(os.path.join(dp, x))]
                if fs:
                    units[d] = os.path.join(dp, fs[0])
        if want:
            units = {k: v for k, v in units.items() if k == want}
        if not units:
            return self._err(404, '没有可自检的投标文件')
        CHECKS = [
            ('业绩表', ['业绩一览表', '类似工程业绩', '类似项目业绩']),
            ('获奖表', ['获奖一览表', '获奖情况']),
            ('工法材料', ['工法']),
            ('项目管理机构人员配备表', ['项目管理机构人员配备表', '项目管理机构人员']),
            ('主要人员简历表', ['简历表']),
            ('注册安全工程师', ['注册安全工程师']),
            ('一级/注册造价工程师', ['注册造价工程师', '一级注册造价']),
            ('一级建造师', ['一级建造师', '一级注册建造师']),
        ]
        out = []
        for unit, bid in units.items():
            txt = bid + '.txt'
            res = {'投标单位': unit, '投标文件': os.path.basename(bid),
                   '检查': [], '提示': []}
            if not os.path.isfile(txt):
                res['提示'].append('正文尚未抽取，请先点「开始解读」提交任务')
                out.append(res)
                continue
            with open(txt, encoding='utf-8') as f:
                body = f.read()
            lines = body.split('\n')
            cjk = len(re.findall(r'[\u4e00-\u9fff]', body))
            ratio = round(100.0 * cjk / max(1, len(body)), 2)
            res['页数'] = len(lines)
            res['中文字符'] = cjk
            res['中文占比'] = ratio
            res['文字层'] = ('良好' if ratio >= 30 else
                             ('部分可用' if ratio >= 1 else '不可用'))
            pdir = bid + '.pages'
            n_img = 0
            if os.path.isdir(pdir):
                n_img = len([x for x in os.listdir(pdir)
                             if x.lower().endswith(('.jpg', '.png', '.jpeg'))])
            res['扫描页图片数'] = n_img
            try:
                import ocr_text as _ot3
                _aud = _ot3.audit_images(pdir)
                res['页数'] = _aud['页数']
                res['多图页数'] = len(_aud['多图页'])
                res['扩展名'] = _aud['扩展名']
                if _aud['警告']:
                    res['扫描页警告'] = _aud['警告']
            except Exception as _e:
                res['扫描页警告'] = ['审计失败：%r' % _e]
            for label, kws in CHECKS:
                pages = sorted({i + 1 for i, l in enumerate(lines)
                                for k in kws if k in l})
                state = '找到' if pages else '未找到'
                note = ''
                if label == '主要人员简历表' and not pages and n_img > 30:
                    state = '可能为扫描件'
                    note = '正文未见「简历表」字样，但有 %d 张扫描页，需 OCR 核实' % n_img
                res['检查'].append({'项目': label, '状态': state,
                                    '找到': bool(pages),
                                    '命中页数': len(pages),
                                    '前几页': pages[:6], '说明': note})
            if ('联合体' in unit) or ('主）' in unit and '（成）' in unit):
                pages = sorted({i + 1 for i, l in enumerate(lines)
                                if '联合体协议' in l or '联合体投标协议' in l})
                res['检查'].append({'项目': '联合体协议（联合体投标必查）',
                                    '状态': '找到' if pages else '未找到',
                                    '找到': bool(pages), '命中页数': len(pages),
                                    '前几页': pages[:6], '说明': ''})
            n_resume = len({i + 1 for i, l in enumerate(lines) if '简历表' in l})
            res['简历表页数'] = n_resume
            m = re.search(r'总计\s*(\d+)', body)
            res['配备表总人数'] = int(m.group(1)) if m else None
            if n_resume == 0 and n_img > 30:
                res['提示'].append('「简历表」正文未见文字，但有 %d 张扫描页，'
                                   '人员职称需 OCR 或人工核实' % n_img)
            elif n_resume == 0:
                res['提示'].append('未见「简历表」，人员职称可能无法核实')
            if res['配备表总人数'] and res['配备表总人数'] != 13:
                res['提示'].append('配备表总人数为 %s，招标要求 13 岗'
                                   % res['配备表总人数'])
            if res['文字层'] == '不可用':
                res['提示'].append('文字层几乎无中文，需依赖 OCR 识读')
            out.append(res)
        return self._send(200, {'ok': True, '任务': 'selfcheck', '项目': pname,
                                '单位数': len(out), '结果': out})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


def _lan_ips():
    """列出本机可用于局域网访问的 IPv4 地址"""
    ips = []
    try:
        import socket as _sk
        host = _sk.gethostname()
        for info in _sk.getaddrinfo(host, None, _sk.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
        # 兜底：连一个外部地址看出口网卡 IP（不实际发包）
        try:
            s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
        except Exception:
            pass
    except Exception:
        pass
    return ips


def main():
    port = 8767
    host = '127.0.0.1'
    for i, a in enumerate(sys.argv):
        if a == '--port' and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == '--host' and i + 1 < len(sys.argv):
            host = sys.argv[i + 1].strip()
    try:
        srv = Server((host, port), H)
    except OSError as e:
        print('端口 %d 无法绑定到 %s：%s' % (port, host, e))
        print('换端口：python server.py --port 8770')
        print('仅本机：python server.py            （默认 127.0.0.1）')
        print('局域网：python server.py --host 0.0.0.0')
        return 1
    try:
        _left = submit_inflight_load()
        if _left:
            print('  注意   上次有 %d 个提交未完成（预处理被打断）：%s'
                  % (len(_left), '、'.join(str(x.get('投标单位')) for x in _left)))
    except Exception:
        pass
    # P0-3：生成访问口令——/api/* 需带 X-API-Token；前端首次访问需输入
    import secrets as _sec
    global API_TOKEN
    API_TOKEN = _sec.token_urlsafe(16)
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, 'api_token.txt'), 'w', encoding='utf-8') as _f:
            _f.write(API_TOKEN)
    except Exception:
        pass
    print('评分复核服务已启动')
    try:
        import version as _ver
        _vi = _ver.info(HERE)
        print('  版本   v%s（构建 %s）' % (_vi['版本'], _vi['构建']))
    except Exception:
        pass
    # 唤醒目标：没有绑定文件时，等于本进程启动时的 DSH_SESSION_ID
    try:
        import wake as _wake
        _sid, _src, _title = _wake.resolve_session()
        print('  唤醒   %s（来源：%s%s）'
              % (_sid or '无', _src, ('，' + _title) if _title else ''))
    except Exception as e:
        print('  唤醒   目标解析失败：%r' % (e,))
    print('  绑定   %s:%d' % (host, port))
    print('  口令   %s（前端首次访问需输入；见 work\\api_token.txt）' % API_TOKEN)
    print('  本机   http://127.0.0.1:%d/' % port)
    if host in ('0.0.0.0', ''):
        ips = _lan_ips()
        if ips:
            for ip in ips:
                print('  局域网 http://%s:%d/' % (ip, port))
        else:
            print('  局域网 http://<本机IP>:%d/（未能自动获取 IP）' % port)
        print('  注意   已对局域网开放：无登录校验，能访问即可操作，请只在可信网络使用')
    else:
        print('  说明   仅本机可访问；要让局域网其他电脑打开，用 --host 0.0.0.0 启动')
    print('  数据   %s' % RULES)
    if AUTO_RESCUE:
        print('  兜底   已启动看门狗：每 %d 秒巡检一次；唤醒 %d 分钟没人认领就重发（最多 %d 次）'
              % (RESCUE_TICK, WAKE_GRACE_MIN, WAKE_MAX_TRIES))
    try:
        start_rescue()
    except Exception:
        traceback.print_exc()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
    return 0


if __name__ == '__main__':
    sys.exit(main())
