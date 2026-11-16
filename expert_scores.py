#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""expert_scores.py —— 专家打分表（PDF）提取与录入

设计原则：PDF 版式差异极大，**不做全自动解析**，而是
  ① 逐页取文本（有文字层直读，无文字页 OCR）
  ② 按评分项名称匹配行，抓取该行数字作为「候选值」
  ③ 候选值只作预填建议，最终以人工确认为准

文件约定：
  项目/<项目>/评委打分/*.pdf            原始打分表
  项目/<项目>/评委打分/_提取.json        提取结果（每次重新生成）
  项目/<项目>/评委打分/_录入.json        人工确认后的分数（参与对比）
"""
import glob
import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')


# ---------------------------------------------------------------- 读取 PDF
def _page_texts(pdf_path):
    """返回 [(页码, 文本)]；无文字页用导出的图 OCR 补"""
    import pdf_text
    out = []
    try:
        doc = pdf_text.PDF(pdf_path)
        pages = doc.pages()
    except Exception as e:
        return [], ['解析 PDF 失败：%r' % e]

    warns = []
    pdir = pdf_path + '.pages'
    imgs_by_page = {}
    try:
        import pdf_images
        pdf_images.export(pdf_path, pdir, only_blank_pages=True)
        import ocr_text
        info = ocr_text.collect_page_images(pdir)
        imgs_by_page = info.get('页') or {}
    except Exception as e:
        warns.append('导出/准备 OCR 失败：%r' % e)

    for i, obj in enumerate(pages, 1):
        t = ''
        try:
            t = (doc.page_text(obj) or '').strip()
        except Exception:
            t = ''
        if len(t) <= 2 and imgs_by_page.get(i):
            try:
                import ocr_text
                t = '\n'.join(ocr_text.ocr_image(p)
                              for p in imgs_by_page[i]).strip()
            except Exception:
                pass
        out.append((i, t))
    return out, warns


# ---------------------------------------------------------------- 候选值
def _norm(s):
    return re.sub(r'[\s（）()、,，.·．:：;；/／\-—_]', '', s or '')


def _guess(text, item_names, unit_names):
    """在一段文本里找评分项名称与该行数字"""
    rows = []
    lines = [x for x in re.split(r'[\n]', text) if x.strip()]
    for ln in lines:
        nline = _norm(ln)
        hit_item = None
        for name in item_names:
            nn = _norm(name)
            if nn and nn in nline:
                hit_item = name
                break
        if not hit_item:
            # 允许只匹配前 6 个字（评分项名称常被表头截断）
            for name in item_names:
                nn = _norm(name)[:6]
                if len(nn) >= 4 and nn in nline:
                    hit_item = name
                    break
        if not hit_item:
            continue
        nums = []
        for m in NUM_RE.finditer(ln):
            v = m.group(0)
            try:
                f = float(v)
            except ValueError:
                continue
            if 0 <= f <= 100:
                nums.append(f)
        if not nums:
            continue
        hit_unit = None
        for u in unit_names:
            if _norm(u) and _norm(u) in nline:
                hit_unit = u
                break
        rows.append({'行': ln.strip()[:160], '评分项': hit_item,
                     '投标人': hit_unit, '候选分': nums})
    return rows



# ---------------------------------------------------------------- 版面重建
HEAD_ITEM = ('评分项', '评审项目', '评分要素', '评审因素', '评分内容', '项目',
             '评分点', '指标')
HEAD_UNIT = ('投标人', '投标单位', '单位名称', '投标人名称', '单位')
HEAD_SCORE = ('分数', '得分', '评分', '分值', '满分', '评标', '评委')
HEAD_EXPERT = ('专家', '评委', '评审人')


def _rows_from_boxes(lines, ytol_ratio=0.6):
    """把 OCR 行按 y 聚成表格行；行内按 x 排序"""
    if not lines:
        return []
    hs = sorted(l['h'] for l in lines)
    med_h = hs[len(hs) // 2] or 12.0
    tol = max(6.0, med_h * ytol_ratio)
    lines = sorted(lines, key=lambda l: (l['cy'], l['cx']))
    rows = []
    for l in lines:
        placed = False
        for r in rows:
            if abs(l['cy'] - r['cy']) <= tol:
                r['cells'].append(l)
                r['cy'] = (r['cy'] * len(r['cells']) + l['cy']) / (len(r['cells']) + 1)
                placed = True
                break
        if not placed:
            rows.append({'cy': l['cy'], 'cells': [l]})
    for r in rows:
        r['cells'].sort(key=lambda c: c['cx'])
        r['text'] = ' '.join(c['text'] for c in r['cells'])
    rows.sort(key=lambda r: r['cy'])
    return rows


def _header_kind(row):
    t = row['text']
    score = sum(1 for k in HEAD_SCORE if k in t)
    item = sum(1 for k in HEAD_ITEM if k in t)
    unit = sum(1 for k in HEAD_UNIT if k in t)
    exp = sum(1 for k in HEAD_EXPERT if k in t)
    return {'score': score, 'item': item, 'unit': unit, 'expert': exp,
            'total': score + item + unit + exp}


def _role_of(t):
    """判断表头单元格的角色。

    顺序很重要：越具体的词越先判，避免「评分项」被「评分」吃掉、
    「张专家」被当成专家名列。
    """
    tt = (t or '').strip().replace(' ', '').replace('\u3000', '')
    if not tt:
        return 'other'
    # ① 权重列（分值/满分）——不是得分
    if tt in ('分值', '满分', '权重', '标准分', '分值上限'):
        return 'weight'
    # ② 评分项列（具体词优先，先于「评分」判断）
    for k in ('评分项', '评审项目', '评审因素', '评审内容', '评分要素',
              '评分内容', '评分点', '项目名称', '评审要点', '指标'):
        if k in tt:
            return 'item'
    # ③ 序号列
    if tt in ('序号', '序', '编号', '项次', 'No', 'NO'):
        return 'idx'
    # ④ 投标人列
    for k in ('投标人', '投标单位', '单位名称'):
        if k in tt:
            return 'unit'
    # ⑤ 专家名列：恰好就是这几个词
    if tt in ('专家', '评委', '评审人', '专家名称', '评委名称', '专家组'):
        return 'expert'
    # ⑥ 「人名 + 专家/评委」→ 分数列，列标题就是专家名
    if ('专家' in tt or '评委' in tt) and len(tt) <= 8:
        return 'score'
    # ⑦ 得分列
    for k in ('得分', '分数', '评分', '评标得分', '得分情况', '评定'):
        if k in tt:
            return 'score'
    return 'other'


def table_from_page(lines):
    """单页版面 → {表头, 列, 行}"""
    rows = _rows_from_boxes(lines)
    if not rows:
        return None

    best, best_i = None, -1
    for i, r in enumerate(rows[:8]):
        k = _header_kind(r)
        if len(r['cells']) >= 2 and (best is None or k['total'] > best['total']):
            best, best_i = k, i

    if best is None or best['total'] < 2:
        header = None
        cols = _cols_by_numbers(rows)
    else:
        header = rows[best_i]
        cols = []
        for c in header['cells']:
            role = _role_of(c['text'])
            cols.append({'x': c['cx'], 'x0': c['x0'], 'x1': c['x1'],
                         '标题': c['text'].strip(), '角色': role})
        # 若一个分数列都没有，但存在多个 other 列（表头就是专家名），
        # 把 other 列当分数列（排除序号/权重/项目/单位）
        if not any(c['角色'] == 'score' for c in cols):
            for c in cols:
                if c['角色'] == 'other':
                    c['角色'] = 'score'
        # 序号列若被当成 other 且不是最左列，保留 other 不参与取分

    data_rows = rows[(best_i + 1):] if best_i >= 0 else rows
    return {'表头': header['text'] if header else None, '列': cols,
            '行': [{'cy': r['cy'], 'cells': r['cells'], 'text': r['text']}
                  for r in data_rows]}


def _cols_by_numbers(rows):
    """无表头兜底：把「数字最多的一列」当分数列，最左文字列当项目名"""
    xs = []
    for r in rows:
        for c in r['cells']:
            if NUM_RE.fullmatch(c['text'].strip()):
                xs.append(c['cx'])
    if not xs:
        return []
    xs.sort()
    groups = []
    for x in xs:
        if groups and x - groups[-1][-1] <= 40:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [{'x': sum(g) / len(g), 'x0': min(g), 'x1': max(g),
             '标题': '（推测分数列）', '角色': 'score'} for g in groups]


def _assign(row, cols):
    """把一行的 cell 分配到各列"""
    out = {i: [] for i in range(len(cols))}
    for c in row['cells']:
        best, bd = None, None
        for i, col in enumerate(cols):
            # 落在列区间内优先
            if col['x0'] - 12 <= c['cx'] <= col['x1'] + 12:
                d = abs(c['cx'] - col['x'])
            else:
                d = abs(c['cx'] - col['x']) + 40
            if bd is None or d < bd:
                best, bd = i, d
        if best is not None:
            out[best].append(c['text'])
    return {i: ' '.join(v).strip() for i, v in out.items()}


def scores_from_pdf(pdf_path, item_names=None):
    """自动识别一页/多页打分表 → 候选记录

    返回 {'页': [{'页':n, '表头':…, '候选':[{评分项,投标人,专家,分数,原文}]}],
          '告警': [...]}
    """
    import pdf_text
    import pdf_images
    import ocr_text

    item_names = item_names or []
    pdir = pdf_path + '.pages'
    warns = []
    try:
        pdf_images.export(pdf_path, pdir, only_blank_pages=True)
        info = ocr_text.collect_page_images(pdir)
        per = info.get('页') or {}
    except Exception as e:
        return {'页': [], '告警': ['导出页面图失败：%r' % e]}

    doc = pdf_text.PDF(pdf_path)
    objs = doc.pages()
    pages_out = []
    for i, obj in enumerate(objs, 1):
        # 优先文字层坐标：pdf_text 暂不提供，统一走 OCR（打分表多为扫描件）
        cand = []
        header = None
        for k, img in enumerate(per.get(i, [])):
            lines = ocr_text.ocr_boxes(img)
            if not lines:
                continue
            t = table_from_page(lines)
            if not t:
                continue
            header = t['表头'] or header
            cols = t['列']
            if not cols:
                continue
            for r in t['行']:
                got = _assign(r, cols)
                vals = {}
                item = None
                unit = None
                for idx, col in enumerate(cols):
                    v = got.get(idx, '')
                    if not v:
                        continue
                    if col['角色'] == 'item' and not item:
                        item = v
                    elif col['角色'] == 'unit' and not unit:
                        unit = v
                    elif col['角色'] == 'score':
                        m = NUM_RE.search(v)
                        if m:
                            try:
                                vals[col['标题'] or ('第%d列' % (idx + 1))] = float(m.group(0))
                            except ValueError:
                                pass
                if not vals:
                    continue
                cand.append({'评分项': item, '投标人': unit,
                             '分数': vals, '原文': r['text'][:200], '图': k})
        pages_out.append({'页': i, '表头': header, '候选': cand})

    if not any(p['候选'] for p in pages_out):
        warns.append('未识别出表格结构：该 PDF 可能是纯扫描件或版式特殊，'
                     '请用「录入表」手工填写')
    return {'页': pages_out, '告警': warns}


# ---------------------------------------------------------------- 图片/PDF 统一入口
def table_from_image(img_path):
    """对单张图做版面重建，返回与 scores_from_pdf 中一页相同的结构"""
    import ocr_text
    lines = ocr_text.ocr_boxes(img_path)
    if not lines:
        return {'页': 1, '表头': None, '候选': [], '图': os.path.basename(img_path)}
    t = table_from_page(lines)
    if not t or not t['列']:
        return {'页': 1, '表头': None, '候选': [],
                '图': os.path.basename(img_path)}
    cand = []
    for r in t['行']:
        got = _assign(r, t['列'])
        vals, item, unit = {}, None, None
        for idx, col in enumerate(t['列']):
            v = got.get(idx, '')
            if not v:
                continue
            if col['角色'] == 'item' and not item:
                item = v
            elif col['角色'] == 'unit' and not unit:
                unit = v
            elif col['角色'] == 'score':
                m = NUM_RE.search(v)
                if m:
                    try:
                        vals[col['标题'] or ('第%d列' % (idx + 1))] = float(m.group(0))
                    except ValueError:
                        pass
        if vals or item:
            cand.append({'评分项': item, '投标人': unit, '分数': vals,
                         '原文': r['text'][:200], '图': os.path.basename(img_path)})
    return {'页': 1, '表头': t['表头'], '候选': cand,
            '图': os.path.basename(img_path)}


def _match_item(text, item_names):
    """把表里的项目名映射回评分项名称。

    优先级（从强到弱）：
      ① 项名完整出现在表内文本里 → 取**最长**的匹配（避免「项目负责人」
         抢走「项目负责人（职称）」这类更具体的项）
      ② 表内文本完整出现在某个项名里（表里用了简写）
      ③ 取项名前 6 字匹配
    """
    if not text:
        return None
    nt = _norm(text)
    if not nt:
        return None
    # ① 最长项名优先
    cands = []
    for name in item_names:
        nn = _norm(name)
        if nn and nn in nt:
            cands.append((len(nn), name))
    if cands:
        cands.sort(reverse=True)
        return cands[0][1]
    # ② 表内文本是项名的一部分
    cands = []
    for name in item_names:
        nn = _norm(name)
        if nt and len(nt) >= 4 and nt in nn:
            cands.append((len(nn), name))
    if cands:
        cands.sort()      # 取最短的项名（最贴合）
        return cands[0][1]
    # ③ 前 6 字
    for name in item_names:
        nn = _norm(name)[:6]
        if len(nn) >= 4 and nn in nt:
            return name
    return None




def resolve_item(label, items, item_names):
    """把表内行标签解析成 (项号, 项名)。

    优先级：
      ① 带排除式的行（含「除外」「除外」/「除…外」）→ 找名称同样带排除式且含「成员」的项
         （例：「项目组成员职称（项目负责人除外）」必须归到「除项目负责人外的项目组成员」）
      ② 完整项名匹配（最长优先）
      ③ 简写表兜底
    """
    t = _norm(label)
    if not t:
        return None, None
    # ① 排除式：仅在标签同时含人员/成员/职称/负责人等词时才归到"成员"项，
    #    否则「类似业绩（不包括分包）」会被误记进人员项（M24）。
    if ('除外' in t or '不包括' in t) \
            and any(k in t for k in ('成员', '人员', '职称', '负责人', '资格')):
        for it in items:
            nn = _norm(it.get('名称'))
            if '成员' in nn and ('除' in nn or '外' in nn):
                return str(it.get('序号')), it.get('名称')
    # ② 完整项名（最长优先）
    nm = _match_item(label, item_names)
    if nm:
        for it in items:
            if it['名称'] == nm:
                return str(it.get('序号')), nm
    # ③ 简写
    seq = _alias_seq(label)
    if seq:
        for it in items:
            if str(it.get('序号')) == str(seq):
                return str(seq), it.get('名称')
    return None, None

# ---------------------------------------------------------------- 文字层打分表
NUM2_RE = re.compile(r'\d+\.\d{1,2}')
NUM_ANY_RE = re.compile(r'\d+(?:\.\d+)?')

# 短名 → 评分项序号（表里常用简写）
ALIAS = [
    (('淡水壳菜',), '1'),
    (('长距离供水工程勘察设计', '隧洞衬砌', '勘察设计工作'), '2'),
    (('获奖',), '3'),
    (('企业信用', '信用'), '4'),
    # 项6 必须排在项5 之前：「项目组成员职称（项目负责人除外）」里也含「项目负责人」
    (('项目组成员', '除项目负责人外'), '6'),
    (('项目负责人',), '5'),
    (('专家组成员', '专家组'), '7'),
    (('服务承诺', '承诺'), '8'),
]


def _alias_seq(text):
    """用简写表映射到评分项序号；'项目组成员' 必须在 '项目负责人' 之后判断"""
    t = _norm(text)
    if not t:
        return None
    for keys, seq in ALIAS:
        for k in keys:
            if _norm(k) in t:
                return seq
    return None


def _score_units_in_text(text, units):
    """用单位名在文本中的出现位置，判断单位顺序（用于列序校准）"""
    pos = []
    for u in units:
        p = text.find(u)
        pos.append((p, u))
    pos = [x for x in pos if x[0] >= 0]
    pos.sort()
    if len(pos) == len(units):
        return [u for _, u in pos]
    return units


def parse_text_sheet(text, units, items):
    """解析「单位做列」的文字层打分表

    返回 {'行': [{序号, 项目原文, 分数:{单位:分}}], '合计': {单位:分},
          '校验': '通过'/'不符'/'无合计行', '评委': str, '单位顺序': [...]}
    """
    n = len(units)
    if n == 0:
        return {'行': [], '合计': {}, '校验': '无投标单位', '评委': None,
                '单位顺序': []}
    order = _score_units_in_text(text, units)

    # 找评委名
    expert = None
    m = re.search(r'评委姓名[：: ]*([\u4e00-\u9fa5]{2,4})', text)
    if m:
        expert = re.sub(r'(第|页|共|签名)$', '', m.group(1)) or m.group(1)

    # 定位数字串：优先两位小数格式
    nums = [(mm.start(), mm.end(), mm.group(0)) for mm in NUM2_RE.finditer(text)]
    if len(nums) < n:
        nums = [(mm.start(), mm.end(), mm.group(0))
                for mm in NUM_ANY_RE.finditer(text)]
    if len(nums) < n:
        return {'行': [], '合计': {}, '校验': '未找到数字', '评委': expert,
                '单位顺序': order}

    # 按 N 个一组切行；组前的文字即该行标签
    rows, total_row = [], {}
    prev_end = 0
    i = 0
    skipped = 0
    while i + n <= len(nums):
        grp = nums[i:i + n]
        # K2（2026-11-11）：组内数字必须紧密相邻，否则可能是跨行的数字/混入序号页码。
        # 换行是硬证据——同一行的 n 个数字之间不会有换行；跨度异常同理。
        span_text = text[grp[0][0]:grp[-1][1]]
        if '\n' in span_text or len(span_text) > 10 * n + 30:
            skipped += 1
            i += 1          # 滑动 1 个数字重试，而不是整组错位后继续
            continue
        label = text[prev_end:grp[0][0]].strip()
        vals = []
        ok = True
        for _, _, v in grp:
            try:
                vals.append(float(v))
            except ValueError:
                ok = False
                break
        if not ok:
            i += 1
            continue
        # 标签清洗：去掉表头、序号、上一行数字残留
        clean = re.sub(r'^[\d\s、.．)）:：]+', '', label)
        clean = clean.replace('序号', '').replace('评审因素', '')
        clean = clean.replace('投标单位名称及评审意见', '')
        for _u in order:
            clean = clean.replace(_u, '')
        clean = clean.strip()

        if '合计' in label[-6:] or clean.endswith('合计') or '合计' in clean:
            total_row = dict(zip(order, vals))
        elif clean:
            rows.append({'项目原文': clean[-60:], '分数': dict(zip(order, vals))})
        prev_end = grp[-1][1]
        i += n

    # 交叉验证：各项之和 的字符串是否出现在「合计」段的原文里
    # （连续数字流无法可靠切分，如 28.035.040.013.0 可有多种切法）
    chk = '无合计行'
    if total_row and rows:
        tail = text[text.rfind('合计'):] if '合计' in text else ''
        chk = '通过'
        for u in order:
            ssum = round(sum(r['分数'].get(u, 0) for r in rows), 2)
            cands = ['%.2f' % ssum, '%.1f' % ssum, '%g' % ssum]
            if not any(c in tail for c in cands):
                chk = '不符（%s 逐项和应为 %s，合计段未见该值）' % (u[:12],
                                                              cands[1])
                break

    return {'行': rows, '合计': total_row, '校验': chk, '评委': expert,
            '单位顺序': order, '跳过组': skipped}


def sheet_from_pdf_text(pdf_path, units, items, item_names):
    """优先用文字层解析；失败返回 None 由 OCR 兜底"""
    import pdf_text
    try:
        doc = pdf_text.PDF(pdf_path)
        txt = '\n'.join((doc.page_text(o) or '') for o in doc.pages())
    except Exception:
        return None
    if len(txt.strip()) < 30:
        return None
    r = parse_text_sheet(txt, units, items)
    if not r['行']:
        return None
    # 行标签 → 评分项序号
    out = []
    for row in r['行']:
        label = row['项目原文']
        seq, matched = resolve_item(label, items, item_names)
        matched = matched or label
        if not seq:
            continue
        out.append({'序号': str(seq), '项目原文': label,
                    '分数': row['分数'], '匹配': matched})
    return {'评委': r['评委'], '行': out, '合计': r['合计'],
            '校验': r['校验'], '单位顺序': r['单位顺序'],
            '原文长度': len(txt)}


# ---------------------------------------------------------------- 主流程
def extract(project_dir, on_progress=None):
    """提取 评委打分/ 下的 PDF 与图片，产出结构化候选并落盘"""
    d = os.path.join(project_dir, '评委打分')
    os.makedirs(d, exist_ok=True)
    files = sorted([p for p in glob.glob(os.path.join(d, '*'))
                    if p.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))])

    def _tick(pct, msg):
        if on_progress:
            try:
                on_progress(msg, pct)
            except Exception:
                pass

    _tick(8, '读取评分标准与投标单位')

    item_names, units, items = [], [], []
    rf = os.path.join(project_dir, '评分标准.json')
    if os.path.isfile(rf):
        try:
            rules = json.load(io.open(rf, encoding='utf-8'))
            for g in rules.get('分组') or []:
                for it in g.get('项') or []:
                    if it.get('名称'):
                        item_names.append(it['名称'])
                        items.append({'序号': it.get('序号'),
                                      '名称': it['名称'],
                                      '分值': it.get('分值')})
        except Exception:
            pass
    bdir = os.path.join(project_dir, '投标文件')
    if os.path.isdir(bdir):
        for x in os.listdir(bdir):
            if os.path.isdir(os.path.join(bdir, x)) and not x.endswith('.pages'):
                units.append(x)

    docs, warns, pairs = [], [], []
    experts_seen = []

    def _absorb(fname, pages, extra_warn=None):
        """从 pages 里收集候选，转成 pairs(投标人|序号 → {专家:分})"""
        for pg in pages:
            for c in pg.get('候选') or []:
                _sq, item = resolve_item(c.get('评分项'), items, item_names)
                if not item:
                    continue
                seq = None
                for it in items:
                    if it['名称'] == item:
                        seq = str(it.get('序号'))
                        break
                if seq is None:
                    continue
                unit = c.get('投标人') or (units[0] if len(units) == 1 else None)
                if not unit:
                    # 尝试从原文里找单位名
                    for u in units:
                        if _norm(u)[:6] and _norm(u)[:6] in _norm(c.get('原文')):
                            unit = u
                            break
                if not unit:
                    continue
                for ename, val in (c.get('分数') or {}).items():
                    if not isinstance(val, (int, float)):
                        continue
                    if ename not in experts_seen:
                        experts_seen.append(ename)
                    pairs.append({'投标人': unit, '序号': seq, '评分项': item,
                                  '专家': ename, '分数': val,
                                  '来源': fname, '页': pg.get('页'),
                                  '原文': (c.get('原文') or '')[:160]})

    nfile = max(1, len(files))
    for fi, fp in enumerate(files):
        name = os.path.basename(fp)
        low = name.lower()
        base_pct = 12 + int(fi / nfile * 68)
        _tick(base_pct, '解析 %s（%d/%d）' % (name, fi + 1, nfile))
        # 专家名：优先取表内「评委姓名」，其次文件名前缀
        stem = os.path.splitext(name)[0]

        if low.endswith('.pdf'):
            # ① 文字层解析（单位做列，最快最准）
            try:
                r = sheet_from_pdf_text(fp, units, items, item_names)
            except Exception as e:
                r = None
                warns.append('%s：文字层解析异常 %r' % (name, e))
            if r:
                expert = r.get('评委') or stem[:6]
                if expert not in experts_seen:
                    experts_seen.append(expert)
                got = []
                for row in r['行']:
                    for u, v in (row['分数'] or {}).items():
                        if not isinstance(v, (int, float)):
                            continue
                        pairs.append({'投标人': u, '序号': row['序号'],
                                      '评分项': row.get('匹配') or row['项目原文'],
                                      '专家': expert, '分数': v,
                                      '来源': name, '页': 1,
                                      '原文': row['项目原文']})
                        got.append(v)
                docs.append({'文件': name, '类型': 'PDF·文字层',
                             '专家': expert, '行数': len(r['行']),
                             '校验': r.get('校验'),
                             '候选行': [{'项目原文': x['项目原文'],
                                        '序号': x['序号'],
                                        '分数': x['分数']}
                                       for x in r['行']]})
                if r.get('校验') not in (None, '通过', '无合计行'):
                    warns.append('%s：合计校验%s' % (name, r.get('校验')))
                continue

            # ② OCR 版面重建兜底
            try:
                rr = scores_from_pdf(fp, item_names)
                warns.extend(['%s：%s' % (name, x) for x in rr.get('告警') or []])
                pages = rr.get('页') or []
                docs.append({'文件': name, '类型': 'PDF·OCR',
                             '页数': len(pages),
                             '候选行': [c for p in pages for c in (p.get('候选') or [])]})
                _absorb(name, pages)
            except Exception as e:
                warns.append('%s：OCR 解析失败 %r' % (name, e))
        else:
            try:
                p = table_from_image(fp)
                docs.append({'文件': name, '类型': '图片', '页数': 1,
                             '有文字页': 1 if p.get('候选') else 0,
                             '候选行': p.get('候选') or []})
                _absorb(name, [p])
            except Exception as e:
                warns.append('%s：识别失败 %r' % (name, e))

    # 去重：同一(投标人,序号,专家)保留置信度最高的（此处按先到先得）
    seen, uniq = set(), []
    for p in pairs:
        k = (p['投标人'], p['序号'], p['专家'])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)

    if not uniq:
        warns.append('未能自动识别出分数：可能是版式特殊或纯扫描件，请用「录入表」手工填写')

    _tick(84, '整理识别结果')
    res = {'生成时间': _now(), '评分项': item_names, '投标单位': units,
           '专家': experts_seen,
           '文件': docs, '候选': uniq, '告警': warns,
           '说明': '候选仅作预填建议，须在「录入表」确认并保存后才参与对比'}
    io.open(os.path.join(d, '_提取.json'), 'w', encoding='utf-8').write(
        json.dumps(res, ensure_ascii=False, indent=1))
    return res


def entries_path(project_dir):
    return os.path.join(project_dir, '评委打分', '_录入.json')


def load_entries(project_dir):
    p = entries_path(project_dir)
    if os.path.isfile(p):
        try:
            return json.load(io.open(p, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_entries(project_dir, data):
    d = os.path.join(project_dir, '评委打分')
    os.makedirs(d, exist_ok=True)
    data['更新时间'] = _now()
    io.open(os.path.join(d, '_录入.json'), 'w', encoding='utf-8').write(
        json.dumps(data, ensure_ascii=False, indent=1))
    return data


def _now():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- 对比
def compare(project_dir, warn=0.5, alert=1.5):
    """把专家分与复核结果对比。

    商务部分均为**客观分**，故判定为等值比较：
        复核分 == 专家分 → 一致
        否则             → 不一致
    （warn / alert 参数保留兼容，不再用于分级）
    """
    ent = load_entries(project_dir)
    experts = ent.get('专家') or []
    scores = ent.get('分数') or {}
    rdir = os.path.join(project_dir, '复核报告')

    out = []
    for fn in sorted(glob.glob(os.path.join(rdir, '*_复核结果.json'))):
        try:
            res = json.load(io.open(fn, encoding='utf-8'))
        except Exception:
            continue
        unit = res.get('投标人') or ''
        rows, sum_review, sum_exp = [], 0.0, 0.0
        pair_review = 0.0
        n_same = n_diff = n_none = 0
        for it in res.get('评分') or []:
            seq = str(it.get('序号') or '')
            review = it.get('得分')
            per = (scores.get('%s|%s' % (unit, seq)) or {})
            vals = [per.get(e) for e in experts]
            vals = [v for v in vals if isinstance(v, (int, float))]
            avg = round(sum(vals) / len(vals), 2) if vals else None
            diff = None
            concl = '无专家分'
            if isinstance(review, (int, float)):
                sum_review += review        # 系统总分始终取全部项
            if isinstance(review, (int, float)) and avg is not None:
                diff = round(review - avg, 2)
                concl = '一致' if abs(diff) < 0.005 else '不一致'
                sum_exp += avg
                pair_review += review       # 仅比对项参与差异
                if concl == '一致':
                    n_same += 1
                else:
                    n_diff += 1
            else:
                n_none += 1
            rows.append({'序号': seq, '名称': it.get('名称'),
                         '分值': it.get('分值'), '系统评分': review,
                         '专家分': {e: per.get(e) for e in experts},
                         '专家打分': avg, '差异': diff, '结论': concl})
        _has_pair = (n_same + n_diff) > 0   # L13：有比对项（即使专家分合计为 0）
        total_diff = (round(pair_review - sum_exp, 2) if _has_pair else None)
        concl_total = '无专家分'
        if total_diff is not None:
            if n_none == 0:
                concl_total = '一致' if abs(total_diff) < 0.005 else '不一致'
            else:
                concl_total = ('已比对部分一致' if abs(total_diff) < 0.005
                               else '已比对部分不一致')
        # G9：数字自洽校验——compare 之前绕过 S1a 的护栏，这里补上，
        # 手改过单项得分的结果文件不再能比出"一致"。
        _rows2 = res.get('评分') or []
        _sum_sc = round(sum((x.get('得分')
                             if isinstance(x.get('得分'), (int, float)) else 0)
                            for x in _rows2), 2)
        _sum_fu = round(sum((x.get('分值')
                             if isinstance(x.get('分值'), (int, float)) else 0)
                            for x in _rows2), 2)
        _sub = res.get('小计')
        _full = res.get('满分')
        _bad = []
        if isinstance(_sub, (int, float)) and abs(_sum_sc - _sub) > 0.01:
            _bad.append('小计 %.2f ≠ Σ得分 %.2f' % (_sub, _sum_sc))
        if isinstance(_full, (int, float)) and abs(_sum_fu - _full) > 0.01:
            _bad.append('满分 %.2f ≠ Σ分值 %.2f' % (_full, _sum_fu))

        out.append({
            '投标人': unit,
            # G9：'系统' 改为比对项口径（与 '专家' 同口径），差异=系统-专家 才自洽；
            # 全项系统分单列，避免再出现"系统58 专家42 差异0.00"的自相矛盾。
            '总分': {'系统': round(pair_review, 2),
                     '全项系统分': round(sum_review, 2),
                     '专家': round(sum_exp, 2) if _has_pair else None,
                     '差异': total_diff,
                     '覆盖项数': (n_same + n_diff),
                     '结论': concl_total},
            '结果自洽': not _bad,
            '自洽问题': _bad,
            '明细': rows,
            '一致项数': n_same, '不一致项数': n_diff, '无专家分项数': n_none,
        })
    return {'专家': experts, '单位': out,
            '录入时间': ent.get('更新时间'),
            '说明': '商务客观分：系统复核分与专家打分等值比较'}


def import_and_compare(project_dir, on_progress=None):
    """导入即完成：解析打分表 → 自动写入录入 → 比对"""
    def _tick(pct, msg):
        if on_progress:
            try:
                on_progress(msg, pct)
            except Exception:
                pass

    _tick(5, '开始导入')
    r = extract(project_dir, on_progress=on_progress)
    cand = r.get('候选') or []
    experts = r.get('专家') or []
    if cand:
        # G8：合并已有录入，只填空缺键，不覆盖人工确认过的分数；
        # 已有录入（可能含人工确认）先备份一份。
        ent = load_entries(project_dir) or {'专家': experts, '分数': {}}
        if ent.get('分数'):
            try:
                import shutil as _sh
                _p = entries_path(project_dir)
                if os.path.isfile(_p):
                    _sh.copy2(_p, _p + '.bak')
            except Exception:
                pass
        merged_experts = list(ent.get('专家') or [])
        for e in experts:
            if e not in merged_experts:
                merged_experts.append(e)
        ent['专家'] = merged_experts
        ent.setdefault('分数', {})
        for c in cand:
            k = '%s|%s' % (c['投标人'], c['序号'])
            cell = ent['分数'].setdefault(k, {})
            if c['专家'] not in cell:
                cell[c['专家']] = c['分数']
        save_entries(project_dir, ent)
    _tick(90, '保存录入结果')
    cmp = compare(project_dir)
    _tick(97, '生成比对结论')
    return {'提取': {'文件': r.get('文件') or [],
                    '专家': experts,
                    '候选条数': len(cand),
                    '告警': r.get('告警') or []},
            '比对': cmp}
