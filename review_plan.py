#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复核用 OCR 计划器（先定位 → 按分值优先 → 范围内识别）

核心思路
--------
1. 文字层定位免费：一览表 / 章节标题 / 评标索引表，秒级；
2. 扫描页的判分价值差别巨大（获奖 12分/21张=0.57 分/张，工法 2分/104张=0.02 分/张）；
3. 因此按「证据章节」分段识别，段内一次扫完，段间按 分值/图片数 排序。

对外接口
--------
plan(pdf_path, txt_path, rules) -> dict
run(pdf_path, plan, on_section=None) -> dict
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ocr_text

# 章节锚点关键词（按优先级；越靠前越具体）
ANCHORS = [
    ('人员', ('项目管理机构人员配备表', '项目管理机构人员基础人员配备表',
              '人员配备表', '主要人员简历表')),
    ('业绩', ('企业类似业绩一览表', '类似业绩一览表', '企业类似业绩及获奖一览表',
              '业绩一览表')),
    ('获奖', ('企业获奖一览表', '获奖一览表')),
    ('工法', ('工法奖项一览表', '工法一览表')),
]

# 评分项 → 证据章节
# 预计耗时基准：每张扫描页 2.1 秒
# （实测只识别「必需图片」约 3.5 秒/张，占导出图片约 60%，
#   折算到每张导出图片 ≈ 2.1 秒；与 server.OCR_PER_IMG 保持一致）
SEC_PER_IMG = 2.1


ITEM_SECTION = [
    ('业绩', ('业绩',)),
    ('获奖', ('获奖',)),
    ('工法', ('研发', '工法')),
    ('人员', ('团队', '负责人', '人员')),
]


def find_anchors(txt_lines, min_page=10):
    """在文字层找各章节锚点页。

    判据：
      1. 表标题出现在**页首 140 字**内（确为表格标题）；
      2. 同页出现「序号」（确为表格页）；
      3. 跳过前 min_page 页（目录 / 评标索引表）；
      4. 同类有多个候选时，取**页文本最长**的那个——资格审查用的小表
         （如 2 行的「类似工程业绩一览表」）会被评分用的大表（12 行）取代。
    「企业类似业绩及获奖一览表」同时含业绩与获奖，仅作业绩锚点。
    """
    cand = {}
    for pg, line in enumerate(txt_lines, 1):
        if pg < min_page:
            continue
        s = (line or '').strip()
        if not s or '序号' not in s:
            continue
        head = s[:140]
        for cat, kws in ANCHORS:
            for kw in kws:
                if kw not in head:
                    continue
                if cat == '获奖' and '业绩' in head:
                    continue
                old = cand.get(cat)
                if old is None or len(s) > old[1]:
                    cand[cat] = (pg, len(s))
                break
    return {cat: pg for cat, (pg, _) in cand.items()}


def build_sections(txt_lines, per_page):
    """按锚点切分章节，返回 [(类别, 起始页, 结束页, 图片数)]"""
    anchors = find_anchors(txt_lines)
    total = len(txt_lines)
    last_img = max(per_page) if per_page else total

    # 「施工组织设计」起点：只作为最后一个证据章节的右边界。
    # 判据：在全部锚点之后，且标题出现在页首 40 字内（
    # 排除正文里"按…编制施工组织设计"这类引用）。
    ssz_start = None
    if anchors:
        after = max(anchors.values())
        for pg in range(after + 1, len(txt_lines) + 1):
            s = (txt_lines[pg - 1] or '').strip()
            if not s:
                continue
            head = s[:40]
            if ('施工组织设计' in head) or head.startswith('格式八') \
                    or ('第一章' in head and '施工部署' in s[:80]):
                ssz_start = pg
                break

    ordered = sorted([(pg, cat) for cat, pg in anchors.items()])
    secs = []
    used_end = 0
    for i, (pg, cat) in enumerate(ordered):
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else (last_img + 1)
        # M28：起页不低于上一段止页+1，避免同页被两个锚点重复计入图片数
        start = max(pg, used_end + 1)
        end = max(start, nxt - 1)
        if ssz_start and i + 1 == len(ordered):
            end = min(end, ssz_start - 1)
        end = min(end, last_img)
        used_end = end
        n = sum(len(per_page.get(p, [])) for p in range(start, end + 1))
        secs.append({'类别': cat, '起': start, '止': end, '图片数': n})
    return secs, anchors


def map_items_to_sections(items, secs):
    """评分项 → 章节"""
    by_cat = {s['类别']: s for s in secs}
    out = []
    for seq, name, val in items:
        cat = None
        for c, kws in ITEM_SECTION:
            if any(k in (name or '') for k in kws):
                cat = c
                break
        s = by_cat.get(cat)
        out.append({'序号': seq, '名称': name, '分值': val,
                    '章节': cat, '范围': (s['起'], s['止']) if s else None})
    return out


def plan(pdf_path, txt_path, rules):
    if isinstance(rules, str):
        with open(rules, encoding='utf-8') as f:
            rules = json.load(f)
    items = []
    if isinstance(rules, dict) and '分组' in rules:
        for g in rules['分组']:
            for it in g.get('项') or []:
                items.append((str(it.get('序号')), it.get('名称') or '',
                              it.get('分值') or 0))
    else:
        for x in rules:
            items.append((str(x['序号']), x['名称'], x['分值']))

    with open(txt_path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    pdir = pdf_path + '.pages'
    info = ocr_text.collect_page_images(pdir)
    per_page = (info or {}).get('页') or {}   # M28：目录缺失不再 KeyError

    secs, anchors = build_sections(lines, per_page)
    mapped = map_items_to_sections(items, secs)

    # 每个章节覆盖的分值
    for s in secs:
        s['覆盖项'] = [m['序号'] for m in mapped if m['章节'] == s['类别']]
        s['分值'] = round(sum(float(m['分值']) for m in mapped
                              if m['章节'] == s['类别']), 2)
        s['优先级'] = round(s['分值'] / max(1, s['图片数']), 4)
        s['起止'] = 'p%d-%d' % (s['起'], s['止'])

    order = sorted([s for s in secs if s['图片数']],
                   key=lambda x: -x['优先级'])
    need = sum(s['图片数'] for s in secs)
    return {'章节': secs, '顺序': [s['类别'] for s in order],
            '项目': mapped, '锚点': anchors,
            '总页数': len(lines), '总图片': (info or {}).get('图片数') or 0,
            '必需图片': need, '非必需图片': max(0, info['图片数'] - need)}


def estimate(p, sec_per_img=None):
    """预计识别耗时（分钟），与单位表的「预计识别」同口径。

    统一口径：预计分钟 = 扫描页数 × SEC_PER_IMG / 60
    """
    if sec_per_img is None:
        sec_per_img = SEC_PER_IMG
    first = p['顺序'][0] if p['顺序'] else None
    first_imgs = next((s['图片数'] for s in p['章节'] if s['类别'] == first), 0)
    total = p.get('总图片') or 0
    need = p.get('必需图片') or 0
    return {'全量': round(total * sec_per_img / 60, 1),
            '仅必需': round(need * sec_per_img / 60, 1),
            '首段出结论': round(first_imgs * sec_per_img / 60, 1),
            '段落数': len([s for s in p['章节'] if s['图片数']]),
            '基准秒每张': sec_per_img,
            '总图片': total,
            '必需图片': need}


def run(pdf_path, p, on_section=None, seconds_per=None):
    pdir = pdf_path + '.pages'
    info = ocr_text.collect_page_images(pdir)
    per_page = (info or {}).get('页') or {}   # M28：目录缺失不再 KeyError
    by_cat = {s['类别']: s for s in p['章节']}

    results = []
    t_all = time.time()
    for cat in p['顺序']:
        s = by_cat[cat]
        imgs = []
        for pg in range(s['起'], s['止'] + 1):
            imgs.extend(per_page.get(pg, []))
        if not imgs:
            continue
        t0 = time.time()
        r = ocr_text.ocr_many(imgs, workers=3)
        el = time.time() - t0
        got = r.get('结果') or {}
        rec = {'章节': cat, '范围': s['起止'], '图片数': len(imgs),
               '耗时秒': round(el, 1),
               '秒每张': round(el / max(1, len(imgs)), 2),
               '覆盖项': s['覆盖项'], '分值': s['分值'],
               '文本': '\n'.join(got.get(x, '') for x in imgs)}
        results.append(rec)
        if on_section:
            try:
                on_section(rec)
            except Exception:
                pass
    return {'结果': results, '总耗时秒': round(time.time() - t_all, 1)}


if __name__ == '__main__':
    pdf = sys.argv[1]
    rules = sys.argv[2] if len(sys.argv) > 2 else None
    p = plan(pdf, pdf + '.txt', rules)
    print('总页数 %d，总图片 %d，必需 %d，非必需 %d'
          % (p['总页数'], p['总图片'], p['必需图片'], p['非必需图片']))
    print('锚点:', p['锚点'])
    print()
    print('%-16s %-12s %-8s %-8s %-8s %-10s %s'
          % ('证据章节', '页范围', '图片数', '覆盖项', '分值', '分/张', '识别顺序'))
    rank = {c: i + 1 for i, c in enumerate(p['顺序'])}
    for s in sorted(p['章节'], key=lambda x: rank.get(x['类别'], 99)):
        if not s['图片数']:
            continue
        print('%-16s %-12s %-8d %-8s %-8s %-10s 第%d位'
              % (s['类别'], s['起止'], s['图片数'],
                 ','.join(s['覆盖项'])[:8], s['分值'], s['优先级'],
                 rank.get(s['类别'], 0)))
    print()
    print('估算（2.7 秒/张）:', estimate(p))
