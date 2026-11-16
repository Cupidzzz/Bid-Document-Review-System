#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""probe_pdf.py —— 诊断一份 PDF 到底为什么读不出文字"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_text


def main(path):
    pdf = pdf_text.PDF(path)
    pages = pdf.pages()
    n = len(pages)
    print('文件: %s' % os.path.basename(path))
    print('大小: %.1f MB   页数: %d' % (os.path.getsize(path) / 1048576.0, n))
    print('对象数: %d   流对象数: %d' % (len(pdf.objs), len(pdf.streams)))

    # 对象流（/ObjStm）：若大量对象压在压缩流里，按原始字节正则找对象会漏
    objstm = sum(1 for k in pdf.streams if b'/ObjStm' in pdf.d(k))
    xrefstm = len(re.findall(rb'/Type\s*/XRef', pdf.data))
    print('压缩对象流 /ObjStm: %d   XRef流: %d' % (objstm, xrefstm))

    # 图像统计
    img = 0
    pages_with_img = 0
    txt_ops = 0
    pages_with_text = 0
    no_tounicode = 0
    fonts_total = 0
    content_bytes = 0
    per_chars = []
    samples = []

    for i, pg in enumerate(pages[:n]):
        d = pdf.d(pg)
        # 图像
        m = re.search(rb'/XObject\s*<<(.*?)>>', d, re.S) or \
            re.search(rb'/XObject\s+(\d+)\s+0\s+R', d)
        has_img = False
        if m:
            scope = m.group(1) if m.lastindex else pdf.d(int(m.group(1)))
            for r in re.finditer(rb'(\d+)\s+0\s+R', scope):
                if re.search(rb'/Subtype\s*/Image', pdf.d(int(r.group(1)))):
                    img += 1
                    has_img = True
        if has_img:
            pages_with_img += 1

        # 内容流
        bodies = []
        cm = re.search(rb'/Contents\s+(\d+)\s+0\s+R', d)
        if cm:
            bodies.append(pdf.s(int(cm.group(1))))
        else:
            cm = re.search(rb'/Contents\s*\[(.*?)\]', d, re.S)
            if cm:
                for r in re.finditer(rb'(\d+)\s+0\s+R', cm.group(1)):
                    bodies.append(pdf.s(int(r.group(1))))
        body = b'\n'.join(bodies)
        content_bytes += len(body)

        ops = len(re.findall(rb'(?:Tj|TJ)\b', body))
        txt_ops += ops
        if ops:
            pages_with_text += 1

        # 字体 / ToUnicode
        for fm in re.finditer(rb'/Font\s*<<(.*?)>>', d, re.S):
            for r in re.finditer(rb'/([A-Za-z0-9#._-]+)\s+(\d+)\s+0\s+R', fm.group(1)):
                fonts_total += 1
                fo = pdf.d(int(r.group(2)))
                if b'/ToUnicode' not in fo:
                    no_tounicode += 1

        t = pdf.page_text(pg)
        per_chars.append(len(t))
        if len(t) > 5 and len(samples) < 3:
            samples.append((i + 1, t[:80]))

    print()
    print('=== 文本层判定 ===')
    print('内容流总字节      : %d' % content_bytes)
    print('文本绘制操作 Tj/TJ: %d  （有文本操作的页: %d / %d）'
          % (txt_ops, pages_with_text, n))
    print('字体资源总数      : %d  （无 ToUnicode 的: %d）'
          % (fonts_total, no_tounicode))
    print('图像 XObject      : %d  （含图像的页: %d / %d）'
          % (img, pages_with_img, n))
    print('每页平均字符      : %.1f' % (sum(per_chars) / max(1, len(per_chars))))
    print('取到字符的页数    : %d / %d' % (sum(1 for c in per_chars if c > 0), n))

    print()
    print('=== 判定 ===')
    if txt_ops == 0 and pages_with_img >= n * 0.5:
        print('结论: 扫描件。页面是图片，内容流里没有任何文本绘制指令。')
        print('      纯文本提取无法解决，需要 OCR 或视觉识读。')
    elif txt_ops > 0 and sum(per_chars) < txt_ops * 0.2:
        print('结论: 有文本操作但几乎取不到字 —— 多为字体缺 ToUnicode，')
        print('      或文本藏在压缩对象流里。属提取器问题，可修。')
    else:
        print('结论: 文本层存在，需进一步核对提取结果。')

    if samples:
        print()
        print('=== 取到的文字样本 ===')
        for pno, s in samples:
            print('  p%d: %r' % (pno, s))


if __name__ == '__main__':
    main(sys.argv[1])
