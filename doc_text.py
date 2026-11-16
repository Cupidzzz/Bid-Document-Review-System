#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
doc_text.py —— .doc（Word 97-2003 二进制 / OLE 复合文档）文本近似抽取

说明
----
环境无第三方库、无 COM，无法解析 OLE 结构中的 WordDocument 流。
这里采用**近似**办法：整文件按 UTF-16LE 解码，抓取连续的中文/数字/标点片段。

局限
----
* 可能混入样式名、域代码等无关片段；
* 表格会失去结构（只能得到文字流）；
* 顺序可能与原文不完全一致。

因此返回值里带 文字层质量 与 提示，调用方应据此提示用户「建议另存为 .docx」。
"""
import re

CJK = re.compile(r'[\u4e00-\u9fff]')
RUN = re.compile(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef'
                 r'A-Za-z0-9，。、；：（）《》“”‘’%．.\-—/\\+\s]{6,}')


def extract(path):
    with open(path, 'rb') as f:
        data = f.read()
    best = ''
    for enc in ('utf-16-le', 'gbk', 'utf-8'):
        try:
            s = data.decode(enc, 'ignore')
        except Exception:
            continue
        runs = [r.strip() for r in RUN.findall(s)]
        runs = [r for r in runs if len(CJK.findall(r)) >= 3]
        txt = '\n'.join(runs)
        if len(CJK.findall(txt)) > len(CJK.findall(best)):
            best = txt
    cjk = len(CJK.findall(best))
    ratio = round(100.0 * cjk / max(1, len(best)), 2)
    if cjk == 0:
        q = '不可用'
    elif ratio >= 30:
        q = '近似可用'
    else:
        q = '质量差'
    hint = None
    if q != '近似可用':
        hint = ('这是 .doc 旧格式，程序只能近似抽取（中文字符 %d，占比 %.2f%%）。'
                '建议用 Word/WPS 另存为 .docx 后重新上传，抽取会准确得多。'
                % (cjk, ratio))
    return {'段落数': best.count('\n') + 1, '表格数': 0,
            '总字符': len(best), '中文字符': cjk, '中文占比': ratio,
            '文字层质量': q, '提示': hint, '正文': best, '表格': None}


if __name__ == '__main__':
    import sys
    r = extract(sys.argv[1])
    print('中文字符 %d  占比 %.2f%%  质量 %s'
          % (r['中文字符'], r['中文占比'], r['文字层质量']))
    if r.get('提示'):
        print('提示：', r['提示'])
    print(r['正文'][:300])
