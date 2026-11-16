#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汇总多格式导出：Markdown → PDF / DOCX
   - PDF：reportlab Platypus（A4 横向，微软雅黑，表头重复）
   - DOCX：stdlib zipfile 直接写 WordprocessingML，无第三方依赖
"""
import io
import os
import re
import sys
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, 'vendor')


# ============ Markdown → 块模型 ============
def md_to_blocks(md):
    blocks = []
    lines = md.split('\n')
    i, n = 0, len(lines)
    HEAD = (('#### ', 4), ('### ', 3), ('## ', 2), ('# ', 1))
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        if s.startswith('|'):
            tbl = []
            while i < n and lines[i].strip().startswith('|'):
                tbl.append(lines[i].strip())
                i += 1
            rows = []
            for t in tbl:
                if re.match(r'^\|[\s\-:|]+\|$', t):
                    continue
                cells = [c.strip() for c in t.strip('|').split('|')]
                rows.append(cells)
            if rows:
                blocks.append({'t': 'table', 'hdr': rows[0], 'rows': rows[1:]})
            continue
        hit = None
        for mark, lvl in HEAD:
            if s.startswith(mark):
                hit = lvl
                blocks.append({'t': 'h', 'lvl': lvl, 'text': s[len(mark):].strip()})
                break
        if hit:
            i += 1
            continue
        if s.startswith('> '):
            blocks.append({'t': 'quote', 'text': s[2:].strip()})
            i += 1
            continue
        if s == '---':
            blocks.append({'t': 'hr'})
            i += 1
            continue
        if s.startswith('- '):
            blocks.append({'t': 'li', 'text': s[2:].strip()})
            i += 1
            continue
        para = [s]
        i += 1
        while i < n:
            t = lines[i].strip()
            if not t or t.startswith(('|', '#', '>', '- ', '---')):
                break
            para.append(t)
            i += 1
        blocks.append({'t': 'p', 'text': ' '.join(para)})
    return blocks


def _plain(t):
    """去掉行内 markdown 标记"""
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t or '')
    t = re.sub(r'\*(.+?)\*', r'\1', t)
    t = t.replace('`', '')
    return t


def _runs(t):
    """把 **粗体** 拆成 [(文本, 是否粗体)]"""
    out = []
    pos = 0
    for m in re.finditer(r'\*\*(.+?)\*\*', t or ''):
        if m.start() > pos:
            out.append((t[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(t or ''):
        out.append((t[pos:], False))
    return out or [('', False)]


# ============ DOCX ============
CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>'''

DOCRELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/>
<w:sz w:val="20"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="H1"><w:name w:val="heading 1"/>
<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="H2"><w:name w:val="heading 2"/>
<w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="H3"><w:name w:val="heading 3"/>
<w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="H4"><w:name w:val="heading 4"/>
<w:pPr><w:spacing w:before="120" w:after="60"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:pPr><w:ind w:left="240"/></w:pPr>
<w:rPr><w:i/><w:color w:val="44546A"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="Grid"><w:name w:val="Table Grid"/>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:color="BFBFBF"/>
<w:left w:val="single" w:sz="4" w:color="BFBFBF"/>
<w:bottom w:val="single" w:sz="4" w:color="BFBFBF"/>
<w:right w:val="single" w:sz="4" w:color="BFBFBF"/>
<w:insideH w:val="single" w:sz="4" w:color="BFBFBF"/>
<w:insideV w:val="single" w:sz="4" w:color="BFBFBF"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>'''


_XML_BAD = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')   # L15：XML 非法控制字符


def _w_run(text, bold=False, mono=False):
    rpr = '<w:rPr><w:b/></w:rPr>' if bold else ''
    s = _XML_BAD.sub('', text or '')   # L15：过滤掉，否则 Word 报"文件已损坏"
    return ('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>'
            % (rpr, escape(s)))


def _w_para(text, style=None, bold=False, align=None):
    ppr = ''
    inner = ''
    if style or align:
        bits = ''
        if style:
            bits += '<w:pStyle w:val="%s"/>' % style
        if align:
            bits += '<w:jc w:val="%s"/>' % align
        ppr = '<w:pPr>%s</w:pPr>' % bits
    for seg, b in _runs(text):
        inner += _w_run(seg, bold or b)
    return '<w:p>%s%s</w:p>' % (ppr, inner or _w_run(''))


def _w_table(hdr, rows):
    ncol = max([len(hdr)] + [len(r) for r in rows] or [1])
    w = 9400 // max(1, ncol)

    def cell(txt, bold=False, fill=None):
        shd = ('<w:shd w:val="clear" w:fill="%s"/>' % fill) if fill else ''
        return ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr>%s</w:tc>'
                % (w, shd, _w_para(txt, bold=bold)))

    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/>'
           '<w:tblW w:w="5000" w:type="pct"/>'
           '<w:tblLayout w:type="autofit"/></w:tblPr>']
    out.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>')
    cells = list(hdr) + [''] * (ncol - len(hdr))
    for c in cells:
        out.append(cell(c, bold=True, fill='DCE6F1'))
    out.append('</w:tr>')
    for r in rows:
        cells = list(r) + [''] * (ncol - len(r))
        out.append('<w:tr>')
        for c in cells:
            out.append(cell(c))
        out.append('</w:tr>')
    out.append('</w:tbl>')
    out.append('<w:p/>')
    return ''.join(out)


def blocks_to_docx(blocks, out_path, title=''):
    body = []
    for b in blocks:
        t = b['t']
        if t == 'h':
            body.append(_w_para(_plain(b['text']), style='H%d' % min(b['lvl'], 4)))
        elif t == 'table':
            body.append(_w_table([_plain(c) for c in b['hdr']],
                                 [[_plain(c) for c in r] for r in b['rows']]))
        elif t == 'quote':
            body.append(_w_para(_plain(b['text']), style='Quote'))
        elif t == 'li':
            body.append(_w_para('• ' + _plain(b['text'])))
        elif t == 'hr':
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" '
                        'w:color="BFBFBF"/></w:pBdr></w:pPr></w:p>')
        else:
            body.append(_w_para(_plain(b['text'])))

    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s'
           '<w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
           'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
           '</w:body></w:document>' % ''.join(body))

    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>%s</dc:title></cp:coreProperties>' % escape(title or ''))

    # G11（2026-11-11）：先写临时文件，成功后再原子替换，失败不留半成品、不截断旧件
    tmp = out_path + '.tmp'
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', CT)
            z.writestr('_rels/.rels', RELS)
            z.writestr('word/_rels/document.xml.rels', DOCRELS)
            z.writestr('word/styles.xml', STYLES)
            z.writestr('word/document.xml', doc)
            z.writestr('docProps/core.xml', core)
        os.replace(tmp, out_path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    return out_path


# ============ PDF ============
def blocks_to_pdf(blocks, out_path, title=''):
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle, HRFlowable)

    # 字体
    cands = [(r'C:\Windows\Fonts\msyh.ttc', 'CN', 0),
             (r'C:\Windows\Fonts\msyhbd.ttc', 'CNB', 0),
             (r'C:\Windows\Fonts\simsun.ttc', 'CN', 0),
             (r'C:\Windows\Fonts\simhei.ttf', 'CN', 0)]
    reg = {}
    for path, name, idx in cands:
        if not os.path.isfile(path) or name in reg:
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            reg[name] = path
        except Exception:
            pass
    if 'CN' not in reg:
        raise RuntimeError('未找到可用中文字体')
    try:
        if 'CNB' in reg:
            pdfmetrics.registerFontFamily('CN', normal='CN', bold='CNB',
                                          italic='CN', boldItalic='CNB')
        else:
            pdfmetrics.registerFontFamily('CN', normal='CN', bold='CN',
                                          italic='CN', boldItalic='CN')
    except Exception:
        pass

    ACC = colors.HexColor('#1554b0')
    INK = colors.HexColor('#1f2733')
    GREY = colors.HexColor('#5A6675')
    LINE = colors.HexColor('#D6DEE8')
    HEADBG = colors.HexColor('#EAF1FA')

    base = ParagraphStyle('base', fontName='CN', fontSize=8.5, leading=12.5,
                          textColor=INK, wordWrap='CJK')
    st = {
        1: ParagraphStyle('h1', parent=base, fontName='CN', fontSize=17, leading=23,
                          textColor=ACC, spaceBefore=2, spaceAfter=8),
        2: ParagraphStyle('h2', parent=base, fontName='CN', fontSize=13, leading=19,
                          textColor=ACC, spaceBefore=12, spaceAfter=6),
        3: ParagraphStyle('h3', parent=base, fontName='CN', fontSize=11, leading=16,
                          textColor=INK, spaceBefore=10, spaceAfter=4),
        4: ParagraphStyle('h4', parent=base, fontName='CN', fontSize=9.5, leading=14,
                          textColor=GREY, spaceBefore=8, spaceAfter=3),
    }
    quote = ParagraphStyle('q', parent=base, fontSize=8, textColor=GREY,
                           leftIndent=6, spaceBefore=2, spaceAfter=4)
    cellp = ParagraphStyle('c', parent=base, fontSize=7.5, leading=10.5)
    cellb = ParagraphStyle('cb', parent=base, fontSize=7.5, leading=10.5,
                           textColor=ACC)

    def md2rl(t):
        t = escape(t or '')
        t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
        t = re.sub(r'\*(.+?)\*', r'<i>\1</i>', t)
        return t or '&nbsp;'

    story = []
    PW, PH = landscape(A4)
    avail = PW - 24 * mm

    for b in blocks:
        t = b['t']
        if t == 'h':
            story.append(Paragraph(md2rl(b['text']), st[min(b['lvl'], 4)]))
        elif t == 'table':
            hdr = [Paragraph('<b>%s</b>' % md2rl(c), cellb) for c in b['hdr']]
            rows = [[Paragraph(md2rl(c), cellp) for c in r] for r in b['rows']]
            ncol = len(hdr)
            data = [hdr] + rows
            # 自动列宽：按最长内容权重
            weights = []
            for ci in range(ncol):
                lens = [len(_plain(b['hdr'][ci]))] + [len(_plain(r[ci]))
                                                      if ci < len(r) else 0 for r in b['rows']]
                w = max(4, min(40, max(lens) if lens else 4))
                weights.append(w)
            tot = float(sum(weights))
            widths = [max(18 * mm, avail * w / tot) for w in weights]
            scale = avail / sum(widths)
            widths = [w * scale for w in widths]
            tb = Table(data, colWidths=widths, repeatRows=1)
            tb.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HEADBG),
                ('GRID', (0, 0), (-1, -1), 0.4, LINE),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 2.5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
            ]))
            story.append(tb)
            story.append(Spacer(1, 4))
        elif t == 'quote':
            story.append(Paragraph(md2rl(b['text']), quote))
        elif t == 'li':
            story.append(Paragraph('• ' + md2rl(b['text']), base))
        elif t == 'hr':
            story.append(Spacer(1, 3))
            story.append(HRFlowable(width='100%', thickness=0.5, color=LINE))
            story.append(Spacer(1, 5))
        else:
            story.append(Paragraph(md2rl(b['text']), base))
            story.append(Spacer(1, 3))

    # G11（2026-11-11）：先写临时文件，成功后再原子替换
    tmp = out_path + '.tmp'
    doc = BaseDocTemplate(tmp, pagesize=(PW, PH),
                          leftMargin=12 * mm, rightMargin=12 * mm,
                          topMargin=12 * mm, bottomMargin=12 * mm,
                          title=title or '', author='DSH 复核')
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='n')

    def on_page(cv, d):
        cv.saveState()
        cv.setFont('CN', 7)
        cv.setFillColor(GREY)
        cv.drawString(doc.leftMargin, 7 * mm, (title or '')[:70])
        cv.drawRightString(PW - doc.rightMargin, 7 * mm, '第 %d 页' % d.page)
        cv.setStrokeColor(LINE)
        cv.line(doc.leftMargin, 10.5 * mm, PW - doc.rightMargin, 10.5 * mm)
        cv.restoreState()

    doc.addPageTemplates([PageTemplate(id='p', frames=[frame], onPage=on_page)])
    try:
        doc.build(story)
        os.replace(tmp, out_path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    return out_path


def convert(md_text, out_path, fmt, title=''):
    blocks = md_to_blocks(md_text)
    if fmt == 'docx':
        return blocks_to_docx(blocks, out_path, title)
    if fmt == 'pdf':
        return blocks_to_pdf(blocks, out_path, title)
    raise ValueError('不支持的格式：%s' % fmt)


if __name__ == '__main__':
    md_path = sys.argv[1]
    out = sys.argv[2]
    fmt = sys.argv[3] if len(sys.argv) > 3 else ('docx' if out.endswith('.docx') else 'pdf')
    md = io.open(md_path, encoding='utf-8').read()
    convert(md, out, fmt, os.path.basename(md_path))
    print('已生成:', out, os.path.getsize(out), 'bytes')
