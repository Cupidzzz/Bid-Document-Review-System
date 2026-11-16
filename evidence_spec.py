#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""项目证据清单（按项目从评分标准原文抽取，不写死）

思路
----
每个项目要求的证明材料不同，固定列清单不可用。改为：

1. 读本项目的 评分标准.json；
2. 对每个评分项的「评分标准」原文，抽取「须提供 / 须同时提供 / 须附 / 应提供」
   等表述，匹配到标准材料类型；
3. 生成 项目\\证据清单.json（可人工增删改）；
4. 复核完成后用 check_result() 校验结果文件的明细表是否覆盖了这些材料。

对外接口
--------
build_spec(rules)              -> {'项': {序号: {...}}, '生成时间':...}
save_spec(project_dir, spec)
load_spec(project_dir)
check_result(result_dict, spec) -> {'通过': bool, '问题': [...], '提示': [...]}
"""
import datetime
import json
import os
import re

# 材料类型字典：canonical -> (识别关键词, 明细表头里应出现的字样)
MATERIALS = {
    '中标通知书': (('中标通知书', '中标结果', '中标候选', '成交通知'), '中标'),
    '合同': (('施工合同', '合同关键页', '合同协议书', '分包合同'), '合同'),
    '验收': (('质量验收', '竣工验收', '验收记录', '验收资料', '交工验收'), '验收'),
    '建设单位证明': (('建设单位证明',), '建设单位证明'),
    '获奖证书': (('获奖证书', '奖项证书', '证书扫描件'), '证书'),
    '授奖文件': (('授奖单位文件', '公告截图', '官网'), '授奖'),
    '协会登记': (('中国社会组织政务服务平台', '民政', '登记备案'), '登记'),
    '工法证书': (('工法', '获奖证书原件'), '工法'),
    '成果鉴定': (('科学技术成果鉴定', '成果鉴定证书', '科学技术成果评价'), '鉴定'),
    '社保': (('社保', '参保证明', '参保'), '社保'),
    '职称证书': (('职称证书', '技术职称', '职称'), '职称'),
    '注册证书': (('注册证书', '执业资格', '注册造价', '注册建造师',
                  '安全生产考核', '注册安全工程师', '注册消防'), '注册'),
    '岗位证': (('上岗证', '培训证', '岗位证'), '岗位'),
    '平台记录': (('信用管理平台', '平台', '查询截图', '网上查询'), '平台'),
    '名单公示': (('名单公示', '公布截图', '名单'), '名单'),
    '营业执照': (('营业执照', '法人资格'), '营业执照'),
    '业绩索引': (('项目名称及项目编号',), '编号'),
    '承诺书': (('承诺书', '更优承诺', '服务承诺'), '承诺'),
}

REQ_PAT = re.compile(
    r'[^。；\n]{0,40}?(须提供|须同时提供|须附|应提供|需提供|须出具|须提交)'
    r'[^。；\n]{0,80}')


def _sentences(text):
    return [s.strip() for s in re.split(r'[。\n]', text or '') if s.strip()]


# 评分项类别 → 判定关键词 / 该类别下应扫描的句子关键词
CATEGORIES = [
    ('业绩', ('业绩',), ('业绩',)),
    ('获奖', ('获奖', '奖项'), ('获奖', '奖项')),
    ('工法', ('研发', '工法'), ('工法', '科学技术成果', '研发')),
    ('人员', ('团队', '负责人', '经理', '总监', '人员', '规模'),
     ('人员', '社保', '职称', '注册', '上岗证', '培训证', '岗位证', '执业资格')),
]


def category_of(name):
    for cat, name_kws, _s in CATEGORIES:
        if any(k in (name or '') for k in name_kws):
            return cat
    return '通用'


def extract_materials(rule_text, category='通用'):
    """从一段评分标准原文里抽取材料类型。

    每个评分项的原文都附带通用注（注1~4），若整段扫描会把业绩的
    「合同+验收」要求污染到人员项。因此按**类别关键词筛句**后，
    只在相关句子里识别材料类型。
    """
    scan_kws = None
    for cat, _n, s in CATEGORIES:
        if cat == category:
            scan_kws = s
            break
    found = []
    for s in _sentences(rule_text):
        if scan_kws is not None and not any(k in s for k in scan_kws):
            continue
        for mat, (kws, _hdr) in MATERIALS.items():
            if any(k in s for k in kws) and mat not in found:
                found.append(mat)
    return found


def build_spec(rules):
    """rules: 评分标准 dict（含 分组/项）"""
    items = {}
    if isinstance(rules, dict) and '分组' in rules:
        for g in rules['分组']:
            for it in g.get('项') or []:
                seq = str(it.get('序号') or '').strip()
                name = it.get('名称') or ''
                key = seq or ('名:' + name)
                text = it.get('评分标准') or ''
                cat = category_of(name)
                mats = extract_materials(text, cat)
                items[key] = {'序号': seq, '名称': name, '分值': it.get('分值'),
                              '类别': cat,
                              '候选': mats,          # 程序抽取，可能有噪声
                              '必核': [],            # 人工确认后填写；每项是一组
                                                     # 如 [['合同'], ['验收','建设单位证明']]
                              '已确认': False,
                              '原文': text[:300]}
    return {'生成时间': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            '说明': '本清单由程序从本项目评分标准原文按类别抽取，'
                    '可人工增删；复核结果的明细表须覆盖所列材料类型。',
            '项': items}


def save_spec(project_dir, spec):
    fp = os.path.join(project_dir, '证据清单.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    return fp


def load_spec(project_dir):
    fp = os.path.join(project_dir, '证据清单.json')
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _missing(cell):
    """「漏填」：空白或占位符，说明没填，不算已核"""
    s = (cell or '').strip()
    if not s:
        return True
    return s in ('—', '-', '/', '－', '–')


def _documented_missing(cell):
    """「已记录缺失」：明确写了无/未提供/未找到，属事实陈述，不算漏填"""
    s = (cell or '').strip()
    if not s:
        return False
    # M21：「无」后必须是标点或结尾才算"缺失"；"无锡/无损检测"这类词不误判
    if re.match(r'^无[、。；，,.]?$', s):
        return True
    if s.startswith(('未见', '未提供', '未找到', '未附', '缺')):
        return True
    return any(k in s for k in ('未找到', '未提供', '未附', '未见'))


def _page_ref(text):
    return bool(re.search(r'p\d{1,4}', text or ''))


def _fallback_has(it, mat):
    """表头缺列时的回退判断：明细任意单元格或依据文字里，
    出现该材料关键词且附近有页码。"""
    kws = MATERIALS.get(mat)
    if not kws:
        return False
    words = tuple(kws[0]) + (kws[1],)
    chunks = []
    mx = it.get('明细') or {}
    for row in (mx.get('行') or []):
        for c in row:
            chunks.append(str(c))
    chunks.append(str(it.get('依据') or ''))
    chunks.append(str(it.get('待核原因') or ''))
    chunks.append(str(it.get('待核类型') or ''))
    for c in chunks:
        for w in words:
            for m in re.finditer(re.escape(w), c):
                # 关键词前后各 45 字内出现页码即视为覆盖
                seg = c[max(0, m.start() - 45):m.start() + 45]
                if _page_ref(seg):
                    return True
    return False


_EXCLUDE_HDR = ('金额', '时间', '名称', '性质', '数量', '工期', '单价', '总价',
                '序号', '页码')


def _hdr_index(headers, mat):
    """在表头里找材料类型对应的列索引"""
    kws = MATERIALS.get(mat)
    if not kws:
        return None
    hits = kws[0] + (kws[1],)
    # M22：精确匹配优先（表头 == 关键词），避免「合同」命中「合同金额」
    for i, h in enumerate(headers or []):
        if str(h) in hits:
            return i
    # 子串命中但含排除词（金额/时间/名称…）时跳过
    for i, h in enumerate(headers or []):
        hs = str(h)
        if any(k in hs for k in hits) and not any(x in hs for x in _EXCLUDE_HDR):
            return i
    return None


def check_result(result, spec):
    """按「必核」组校验，分级返回。

    groups 例：[['合同'], ['验收','建设单位证明']]
    组内任选其一，组间为且。

    分级：
      阻断（block）——无明细表、缺整列、或该列一行都没填 → 不许结单
      警告（warn） ——列存在，但个别单元格漏填（空白/—/） → 允许结单
      提示（hint） ——单元格已注明「无/未提供/未找到」 → 事实性缺失
    """
    block, warn, hint = [], [], []
    by_seq, by_name = {}, {}
    for x in (result.get('评分') or []):
        by_seq[str(x.get('序号') or '').strip()] = x
        by_name[(x.get('名称') or '').strip()] = x

    unconfirmed = 0
    for key, req in (spec.get('项') or {}).items():
        groups = req.get('必核') or []
        if not groups:
            unconfirmed += 1
            continue
        it = by_seq.get(str(req.get('序号') or '').strip()) or \
            by_name.get((req.get('名称') or '').strip())
        name = req.get('名称') or ''
        tag = '项%s %s' % (req.get('序号') or key, name)
        if not it:
            # M20：整项缺失——分值>0 的升级为阻断（漏评不能结单），否则仍 warn
            _v = req.get('分值')
            try:
                _vf = float(_v)
            except Exception:
                _vf = 0
            if _vf and _vf > 0:
                block.append('%s：整项缺失（分值 %s），可能漏评，不能结单'
                             % (tag, _v))
            else:
                warn.append('%s：未在结果中出现' % tag)
            continue
        mx = it.get('明细')
        if not isinstance(mx, dict) or not mx.get('表头'):
            block.append('%s：无明细表，无法核对材料齐备性' % tag)
            continue
        headers = mx.get('表头') or []
        rows = mx.get('行') or []
        if not rows:
            # G2（2026-11-11）：只有表头、没有数据行 = 明细表空，齐备性无从核对，
            # 必须阻断，不能静默放行。
            block.append('%s：明细表只有表头、没有数据行' % tag)
            continue
        for grp in groups:
            label = '或'.join(grp)
            idx = None
            for mat in grp:
                idx = _hdr_index(headers, mat)
                if idx is not None:
                    break
            if idx is None:
                # 仅「紧凑表」（列数≤4，材料写成文字）允许回退；
                # 宽表缺列一律视为结构性缺陷，阻断结单。
                if len(headers) <= 4 and any(_fallback_has(it, m) for m in grp):
                    continue
                block.append('%s：明细表缺「%s」列' % (tag, label))
                continue
            empty, documented = [], []
            for r in rows:
                cell = str(r[idx]) if idx < len(r) else ''
                rowid = r[0] if r else '?'
                if _missing(cell):
                    empty.append(rowid)
                elif _documented_missing(cell):
                    documented.append(rowid)
            if documented:
                hint.append('%s：「%s」有 %d 行已注明缺失（%s）'
                            % (tag, label, len(documented),
                               '、'.join(str(x) for x in documented[:4])))
            if empty:
                if len(empty) == len(rows):
                    block.append('%s：「%s」整列未填（%d 行）'
                                 % (tag, label, len(rows)))
                else:
                    warn.append('%s：「%s」有 %d 行漏填（%s）'
                                % (tag, label, len(empty),
                                   '、'.join(str(x) for x in empty[:6])))

    if unconfirmed:
        hint.append('有 %d 个评分项的证据清单尚未确认必核材料，未纳入校验'
                    % unconfirmed)
    return {'通过': not block,
            '阻断': block, '警告': warn, '提示': hint,
            '检查项数': len(spec.get('项') or {}),
            '未确认项数': unconfirmed,
            '结论': ('不可结单（%d 项结构性缺陷）' % len(block)) if block
                    else ('可结单（%d 条警告、%d 条提示）'
                          % (len(warn), len(hint)))}


def confirm_spec(project_dir, mapping):
    """确认必核材料。mapping: {序号或'名:名称': [[材料组], ...]}"""
    spec = load_spec(project_dir)
    if not spec:
        raise RuntimeError('证据清单不存在，请先生成')
    done = 0
    for key, req in (spec.get('项') or {}).items():
        k2 = key
        if k2 not in mapping:
            k2 = '名:' + (req.get('名称') or '')
        if k2 in mapping:
            req['必核'] = mapping[k2]
            req['已确认'] = True
            done += 1
    spec['确认时间'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_spec(project_dir, spec)
    return done


if __name__ == '__main__':
    import sys
    pd = sys.argv[1]
    rules = json.load(open(os.path.join(pd, '评分标准.json'), encoding='utf-8'))
    spec = build_spec(rules)
    print(save_spec(pd, spec))
    for seq, v in sorted(spec['项'].items(), key=lambda x: str(x[0])):
        cand = v.get('候选') or []
        if cand:
            print('  项%-3s %-26s %s' % (seq, (v['名称'] or '')[:24],
                                         '、'.join(cand)))
