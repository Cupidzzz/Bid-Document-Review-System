#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""docx_text.py —— 纯标准库提取 .docx 文字与表格（zipfile + XML）"""
import re
import zipfile
from xml.etree import ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
CJK = re.compile(r'[\u4e00-\u9fff]')


def extract(path):
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read('word/document.xml')
        except KeyError:
            raise ValueError('不是有效的 docx（缺 word/document.xml）')
    root = ET.fromstring(xml)
    body = root.find(W + 'body')
    paras, tables = [], []
    if body is not None:
        for el in body:
            if el.tag == W + 'p':
                t = ''.join(n.text or '' for n in el.iter(W + 't'))
                if t.strip():
                    paras.append(t.strip())
            elif el.tag == W + 'tbl':
                rows = []
                for tr in el.findall(W + 'tr'):
                    cells = []
                    for tc in tr.findall(W + 'tc'):
                        cells.append(''.join(
                            n.text or '' for n in tc.iter(W + 't')).strip())
                    rows.append(cells)
                if rows:
                    tables.append(rows)
    lines = list(paras)
    for tb in tables:
        lines.append('')
        for r in tb:
            lines.append(' | '.join(r))
    text = '\n'.join(lines)
    return {'段落数': len(paras), '表格数': len(tables),
            '总字符': len(text), '中文字符': len(CJK.findall(text)),
            '中文占比': round(100.0 * len(CJK.findall(text)) / max(1, len(text)), 2),
            '文字层质量': '良好' if len(CJK.findall(text)) else '无中文',
            '正文': text, '表格': tables}


def find_table(path, keywords, any_one=True):
    """在 docx 里找表头含关键词的表，返回 [(表序, 表头, 行)]"""
    r = extract(path)
    out = []
    for i, tb in enumerate(r['表格']):
        head = ' '.join(tb[0]) if tb else ''
        hit = (any(k in head for k in keywords) if any_one
               else all(k in head for k in keywords))
        if hit:
            out.append({'表序': i, '表头': tb[0], '行数': len(tb), '表': tb})
    return out


if __name__ == '__main__':
    import sys
    r = extract(sys.argv[1])
    print('段落 %d  表格 %d  总字符 %d  中文 %d (%.2f%%)  %s'
          % (r['段落数'], r['表格数'], r['总字符'], r['中文字符'],
             r['中文占比'], r['文字层质量']))
    if len(sys.argv) > 2:
        for x in find_table(sys.argv[1], sys.argv[2].split(',')):
            print('--- 表%d 行数%d 表头: %s' % (x['表序'], x['行数'], x['表头']))
            for row in x['表'][:12]:
                print('    ', ' | '.join(c[:28] for c in row))
