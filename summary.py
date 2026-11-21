# -*- coding: utf-8 -*-
"""summary.py —— 汇总/结果处理纯函数（权重折算、分组配对、待核原因生成）。

从 server.py 拆出（P2-1 第五步）：pair_group / weighted_detail / auto_pending_reason。
"""
import re

from validation import as_text


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
