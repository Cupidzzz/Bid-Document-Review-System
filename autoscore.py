#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
autoscore.py —— 依据 rules.json 里人工录入的评分标准文本自动打分

设计原则
--------
1. **不认评分项名称**：不判断"这是业绩还是获奖"，只认从评分标准文本里挖出的条件。
2. **条件来于文本**：时间窗口、金额门槛、类型关键词、单项分值、数量、封顶，
   全部从 评分标准 字符串中提取。改门槛/改分/改窗口只需改 rules.json。
3. **可溯**：每条分都输出用到的表/页、命中行、筛选条件，供人工核。
4. **不瞎给分**：挖不到条件、或找不到可筛的数据时，返回 None 并注明原因。

支持的评分标准形态（正则，覆盖常见中文表述）
    "自2020年6月1日至今"           -> 时间窗口起
    "金额不低于550万元" / "≥550万"  -> 金额门槛
    "建筑幕墙工程" / "建筑装修装饰工程" -> 类型关键词
    "每提供1个得1.6分" / "每个得0.4分" -> 单项分
    "最高得8分" / "最多得12分"      -> 封顶
    "国家级…每项得2分,最多得12分"   -> 分级档位（多档并存）
"""
import json
import os
import re

CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
          '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

DATE_RE = re.compile(r'(\d{4})\s*[年\-\./]\s*(\d{1,2})\s*[月\-\./]\s*(\d{1,2})')
MONEY_RE = re.compile(r'(?:不低于|不少于|大于或等于|≥|大于等于|超过)\s*'
                      r'([\d,]+(?:\.\d+)?)\s*万元?')
UNIT_RE = re.compile(r'每\s*(?:提供|提交|取得|获得|完成)?[^，。；：\n]{0,14}?'
                     r'得\s*([\d.]+)\s*分')
PER_RE = re.compile(r'每\s*(?:个|项|人|份|条|处)\s*得\s*([\d.]+)\s*分')
PER_RE2 = re.compile(r'每个\s*得\s*([\d.]+)\s*分')
CAP_RE = re.compile(r'(?:最多|最高)\s*(?:得)?\s*([\d.]+)\s*分')
LEVEL_RE = re.compile(r'(国家级|省级|市级)[^。；]{0,40}?'
                      r'每\s*(?:项|个)\s*得\s*([\d.]+)\s*分[^。；]{0,20}?'
                      r'(?:最多|最高)\s*(?:得)?\s*([\d.]+)\s*分')
# 类型关键词：只取明确的工程类别，避免把"由承担…"这类状语抓进来
KW_RE = re.compile(r'(建筑幕墙工程|建筑装修装饰工程|装修装饰工程|幕墙工程|'
                   r'房屋建筑工程|市政公用工程|机电安装工程|'
                   r'中国建筑工程装饰奖|鲁班奖|国家优质工程[金银]?质?奖?|'
                   r'省级工法|国家级工法|工法)')


def _d(s):
    return tuple(int(x) for x in s)


def mine(text):
    """从一段评分标准里挖出结构化条件。挖不到的留空。"""
    t = (text or '').replace('\n', ' ')
    out = {'原文': text}
    m = DATE_RE.search(t)
    if m:
        out['时间窗口起'] = '%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)),
                                            int(m.group(3)))
    m = MONEY_RE.search(t)
    if m:
        out['金额门槛万元'] = float(m.group(1).replace(',', ''))
    per = UNIT_RE.search(t) or PER_RE.search(t) or PER_RE2.search(t)
    if per:
        out['单项分'] = float(per.group(1))
    m = CAP_RE.search(t)
    if m:
        out['封顶'] = float(m.group(1))
    levels = []
    for lm in LEVEL_RE.finditer(t):
        levels.append({'级别': lm.group(1), '单项分': float(lm.group(2)),
                       '封顶': float(lm.group(3))})
    if levels:
        out['分级档位'] = levels
    kws = []
    for km in KW_RE.finditer(t):
        k = km.group(0)
        if k not in kws and not k.startswith(('每提供', '最高得')):
            kws.append(k)
    out['类型关键词'] = kws
    # 职称等级要求（高级/中级/副高级/正高级）与岗位名、证书要求
    lv = []
    for pat in (r'正高级', r'副高级', r'高级工程师', r'高级职称', r'中级工程师',
                r'中级职称', r'助理级', r'助理工程师'):
        if pat in t:
            lv.append(pat)
    if lv:
        out['职称要求'] = lv
    post = None
    for pat in ('项目负责人', '技术负责人', '质量负责人', '安全负责人', '造价负责人',
                '建筑工程师', '电气工程师', '给排水工程师', '消防工程师',
                '项目副经理', '深化设计师', '施工员', '质量员', '资料员', '材料员'):
        if pat in t:
            post = pat
            break
    if post:
        out['岗位'] = post
    certs = []
    for pat in ('注册安全工程师', '一级注册造价工程师', '二级注册造价工程师',
                '注册造价工程师', '一级建造师', '注册消防工程师',
                '安全生产考核合格证', '一级注册建造师'):
        if pat in t:
            certs.append(pat)
    if certs:
        out['证书要求'] = certs
    return out


ROW_RE = re.compile(
    r'([\d,]+(?:\.\d+)?)\s*(万元|万|元)?\s*'                 # 金额（可带单位）
    r'(\d{4})\s*[-\./年]\s*(\d{1,2})\s*[-\./月]\s*(\d{1,2})')  # 日期
    # 一行里同时出现"金额 + 日期"，是业绩/获奖类表格行的强特征

# 只有金额（带单位）的行，用于"金额与日期被拆到相邻两行"的情形（v1.8.0 / S6）
MONEY_LINE_RE = re.compile(r'([\d,]+(?:\.\d+)?)\s*(万元|万|元)')


def _to_wan(val, unit):
    v = float(str(val).replace(',', ''))
    return v if str(unit).startswith('万') else round(v / 10000.0, 4)


def _date_ok(y, m, d):
    """日期合理性：OCR/跨行匹配很容易把编号凑成日期，先挡掉明显不可能的。"""
    try:
        y, m, d = int(y), int(m), int(d)
    except Exception:
        return None
    if y < 1990 or y > 2100 or not (1 <= m <= 12) or not (1 <= d <= 31):
        return None
    return '%04d-%02d-%02d' % (y, m, d)


def _amount_ok(v):
    """金额合理性（万元）：0 < v <= 100 亿；超出的多是编号/页码误识别。"""
    try:
        v = float(v)
    except Exception:
        return None
    if v <= 0 or v > 1000000:
        return None
    return v


def find_rows(page_lines, cross=True):
    """
    在按页切分的正文里找出「金额 + 日期」的数据行，返回命中列表。

    1) 同行命中：一行里同时有金额与日期（表格行的强特征），最可靠；
    2) 跨行命中（v1.8.0）：抽取出的 .txt 常把业绩表拆成
           "某某工程  2023-11-13" 与 "合同金额 5792 万元" 两行，
       此时在日期行 ±2 行内找"只有金额"的行并合并成一条；
       跨行命中会标 `来源: 跨行`，方便人工抽查。
    日期与金额都会做合理性过滤，避免把编号/页码凑成假行。
    """
    hits = []
    used = set()
    for i, line in enumerate(page_lines):
        for m in ROW_RE.finditer(line):
            d = _date_ok(m.group(3), m.group(4), m.group(5))
            a = _amount_ok(_to_wan(m.group(1), m.group(2) or '万'))
            if not d or a is None:
                continue
            hits.append({'页': i + 1, '金额': a, '日期': d,
                         '文': line[max(0, m.start() - 80):m.end() + 80],
                         '来源': '同行'})
            used.add(i)
    if not cross:
        return hits

    for i, line in enumerate(page_lines):
        if i in used:
            continue
        dm = DATE_RE.search(line)
        if not dm:
            continue
        d = _date_ok(dm.group(1), dm.group(2), dm.group(3))
        if not d:
            continue
        if MONEY_LINE_RE.search(line):
            continue          # 同行有金额：上面的 ROW_RE 已能处理，不重复计
        best = None
        for off in (1, -1, 2, -2):
            j = i + off
            if j < 0 or j >= len(page_lines) or j in used:
                continue
            other = page_lines[j]
            mm = MONEY_LINE_RE.search(other)
            if not mm:
                continue
            if DATE_RE.search(other):
                continue      # 那一行自己也带日期，交给它自己处理
            best = (j, mm)
            break
        if not best:
            continue
        j, mm = best
        a = _amount_ok(_to_wan(mm.group(1), mm.group(2)))
        if a is None:
            continue
        lo, hi = min(i, j), max(i, j)
        hits.append({'页': lo + 1, '金额': a, '日期': d,
                     '文': ' '.join(page_lines[lo:hi + 1])[:600],
                     '来源': '跨行'})
        used.add(i)
        used.add(j)
    hits.sort(key=lambda h: (h['页'], h['日期']))
    return hits


def _filter(hits, conds):
    """按挖出的条件筛行；返回 (命中, 淘汰原因统计)"""
    ok, why = [], {}
    kw = conds.get('类型关键词') or []
    cond_kw = [k for k in kw if k in ('建筑幕墙工程', '建筑装修装饰工程')] or kw[:1]
    floor = conds.get('金额门槛万元')
    since = conds.get('时间窗口起')
    for h in hits:
        if floor is not None and h['金额'] < floor:
            why['金额不足'] = why.get('金额不足', 0) + 1
            continue
        if since and h['日期'] < since:
            why['早于时间窗口'] = why.get('早于时间窗口', 0) + 1
            continue
        if cond_kw and not any(k in h['文'] for k in cond_kw):
            why['类型不符'] = why.get('类型不符', 0) + 1
            continue
        ok.append(h)
    return ok, why


def score_item(text, page_lines):
    """
    对一个评分项打分。返回 dict，含分值与完整追溯链。
    """
    c = mine(text)
    # M38：无法判定时分值=None（不是 0.0），避免"算不出"被输出成"0 分"
    res = {'条件': c, '分值': None, '命中': 0, '依据': '', '置信度': '低'}
    hits = find_rows(page_lines)
    if not hits:
        res['依据'] = '未在正文中找到同时含「金额+日期」的数据行，无法自动判定'
        return res
    ok, why = _filter(hits, c)
    per = c.get('单项分')
    cap = c.get('封顶')
    levels = c.get('分级档位')
    if levels:
        # 分级档位：按级分别计数并封顶
        total = 0.0
        parts = []
        for lv in levels:
            sub = [h for h in ok if lv['级别'][:2] in h['文']]
            s = min(len(sub), int(lv['封顶'] / lv['单项分'])) * lv['单项分'] \
                if lv['单项分'] else 0
            s = min(s, lv['封顶'])
            total += s
            parts.append('%s %d 项 × %s = %s（封顶 %s）'
                         % (lv['级别'], len(sub), lv['单项分'], round(s, 2), lv['封顶']))
        if cap:
            total = min(total, cap)
        res.update({'分值': round(total, 2), '命中': len(ok),
                    '依据': '；'.join(parts) + ('；总封顶 %s' % cap if cap else ''),
                    '置信度': '中'})
        return res
    if per is None:
        res['依据'] = '未从评分标准中挖出单项分值（如"每提供1个得X分"），无法计算'
        res['条件'] = c
        return res
    n = len(ok)
    val = n * per
    capped = False
    if cap and val > cap:
        val, capped = cap, True
    res.update({'分值': round(val, 2), '命中': n,
                '依据': '命中 %d 行 × %s 分 = %s%s；淘汰：%s'
                        % (n, per, round(n * per, 2),
                           ('，封顶 %s' % cap) if capped else '',
                           '、'.join('%s %d' % (k, v) for k, v in why.items()) or '无'),
                '置信度': '中' if n else '低'})
    return res


def run(rules, bid_txt, out_path=None):
    """rules 可以是规则文件路径，也可以是已载入的规则字典（项目级）"""
    if isinstance(rules, (str, bytes, os.PathLike)):
        _src = rules   # L12：先存路径，否则 json.load 后 rules 变 dict、str(rules) 是一串字典
        with open(rules, encoding='utf-8') as f:
            rules = json.load(f)
        rule_src = os.path.basename(str(_src))
    else:
        rule_src = (rules.get('项目') or {}).get('名称') or '（项目规则）'
    with open(bid_txt, encoding='utf-8') as f:
        lines = f.read().split('\n')
    results = []
    for g in rules.get('分组') or []:
        for it in g.get('项') or []:
            if it.get('主观项'):
                results.append({'分组': g['名称'], '序号': it.get('序号'),
                                '名称': it.get('名称'), '分值': it.get('分值'),
                                '自动得分': None, '说明': '主观项，不自动打分'})
                continue
            r = score_item(it.get('评分标准') or '', lines)
            results.append({'分组': g['名称'], '序号': it.get('序号'),
                            '名称': it.get('名称'), '分值': it.get('分值'),
                            '自动得分': r['分值'], '命中行数': r['命中'],
                            '依据': r['依据'], '置信度': r['置信度'],
                            '挖出条件': r['条件']})
    doc = {'规则来源': rule_src,
           '投标正文': os.path.basename(bid_txt),
           '结果': results}
    if out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return doc


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('用法: python autoscore.py <rules.json> <bid.txt> [out.json]')
        sys.exit(2)
    d = run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    tot = 0
    for r in d['结果']:
        v = r['自动得分']
        tot += v or 0
        print('%-4s %-22s 分值%-4s 自动%-6s %s'
              % (r['序号'], r['名称'][:22], r['分值'],
                 '待定' if v is None else v, (r.get('依据') or r.get('说明') or '')[:60]))
    print('自动合计: %.2f' % tot)
