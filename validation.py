# -*- coding: utf-8 -*-
"""validation.py —— 纯校验/规整函数（无副作用、不依赖 server 其他部分，便于单测）。

从 server.py 拆出（P2-1 第一步）：as_text / check_pending_reason /
total_consistency_errors / check_result_shape。
"""
import json
import re


def as_text(v):
    """把字段值规整成文本：数组用「、」连接，None → ''。

    历史结果里 `出处` 有写成数组的（["p66（OCR）", "p203（OCR）"]），
    直接 .replace() 会抛 AttributeError，把整个请求打断。
    """
    if v is None:
        return ''
    if isinstance(v, (list, tuple)):
        parts = [as_text(x) for x in v]
        return '、'.join(x for x in parts if x)
    if isinstance(v, dict):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return str(v)


def check_pending_reason(txt):
    """待核原因结构校验（S3）：必须三行 要点/出处/疑问，出处要有页码，疑问要够长。

    返回 (是否通过, 说明)。
    """
    t = str(txt or '').strip()
    if not t:
        return False, '没写'
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    have = {}
    for key in ('要点', '出处', '疑问'):
        hit = None
        for x in lines:
            if x.startswith(key + '：') or x.startswith(key + ':'):
                hit = x[len(key) + 1:].strip()
                break
        have[key] = hit
    miss = [k for k in ('要点', '出处', '疑问') if have[k] is None]
    if miss:
        # 兼容旧数据：历史结果里是自由文本（没有三行前缀），只要写得够具体就放行；
        # 真正要拦的是"待核"这种敷衍的两三个字。
        if len(t) >= 12:
            return True, ''
        return False, ('缺 %s 行（应写成 要点：/出处：/疑问： 三行，'
                       '或写满 12 字以上的具体说明）' % '/'.join(miss))
    if not re.search(r'[pP]\s*\d+', have['出处'] or ''):
        return False, '「出处」里没有页码（应含 p123 这样的页号）'
    if len(have['疑问'] or '') < 12:
        return False, '「疑问」太短（至少 12 字，要写清要人判断什么）'
    return True, ''


def total_consistency_errors(d):
    """S1a 数字自洽（v1.7.8）：返回错误说明列表；
    空列表 = 小计==Σ得分 且 满分==Σ分值（自洽）。
    """
    _rows = (d or {}).get('评分') or []

    def _num(v):
        try:
            return float(v)
        except Exception:
            return None

    _sum_sc = round(sum(_num(x.get('得分')) or 0 for x in _rows), 2)
    _sum_fu = round(sum(_num(x.get('分值')) or 0 for x in _rows), 2)
    _sub = _num(d.get('小计'))
    _full = _num(d.get('满分'))
    bad = []
    if _sub is not None and abs(_sum_sc - _sub) > 0.01:
        bad.append('小计 %.2f ≠ 各评分项得分之和 %.2f（差 %+.2f）'
                   % (_sub, _sum_sc, _sum_sc - _sub))
    if _full is not None and abs(_sum_fu - _full) > 0.01:
        bad.append('满分 %.2f ≠ 各评分项分值之和 %.2f（差 %+.2f）'
                   % (_full, _sum_fu, _sum_fu - _full))
    return bad


def check_result_shape(d):
    """检查复核结果 JSON 的必备字段；返回缺失/类型不符的字段名列表"""
    if not isinstance(d, dict):
        return ['根节点不是对象']
    bad = []
    if not d.get('投标人'):
        bad.append('投标人')
    items = d.get('评分')
    if not isinstance(items, list) or not items:
        bad.append('评分（应为非空数组）')
    elif not all(isinstance(x, dict) for x in items):
        bad.append('评分（元素应为对象）')
    else:
        need = ('序号', '名称', '分值', '得分')
        for i, x in enumerate(items):
            miss = [k for k in need if k not in x]
            if miss:
                bad.append('评分[%d]缺少 %s' % (i, '/'.join(miss)))
                break
            if not isinstance(x.get('得分'), (int, float)):
                bad.append('评分[%d].得分 不是数字' % i)
                break
    if not isinstance(d.get('小计'), (int, float)):
        bad.append('小计（应为数字）')
    if not isinstance(d.get('满分'), (int, float)):
        bad.append('满分（应为数字）')
    return bad
