#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdf_images.py —— 导出 PDF 页面内嵌图片（用于读取证书扫描页）

只处理两类最常见的扫描图：
  * /DCTDecode           JPEG 原始字节，直接落盘 .jpg
  * /FlateDecode         zlib 压缩的原始像素，按宽高/色彩空间包成 .png
其它滤镜（JPXDecode/CCITTFaxDecode/JBIG2）本环境无法解码，跳过并计数。

按页导出，文件名 p<页码>_i<序号>.<ext>，同时返回清单供前端与 agent 使用。
"""
import io
import os
import json
import re
import struct
import time
import sys
import zlib

import pdf_text

HERE = os.path.dirname(os.path.abspath(__file__))
VEND = os.path.join(HERE, 'vendor')
if os.path.isdir(VEND) and VEND not in sys.path:
    sys.path.insert(0, VEND)



def _png(w, h, raw, colors):
    """把 8bit 原始像素包成 PNG（colors: 0=灰,2=RGB）。raw 需按行加 filter 字节。"""
    stride = w * (3 if colors == 2 else 1)
    rows = bytearray()
    for y in range(h):
        rows.append(0)                       # filter type 0
        rows += raw[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(rows), 6)

    def chunk(typ, data):
        c = struct.pack('>I', len(data)) + typ + data
        return c + struct.pack('>I', zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack('>IIBBBBB', w, h, 8, colors, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
            chunk(b'IDAT', comp) + chunk(b'IEND', b''))


def _dict_of(pdf, num):
    return pdf.d(num)


def _num(d, key, path=None):
    m = re.search(rb'/' + key + rb'\s+(\d+)', d)
    return int(m.group(1)) if m else None


def _read_dict(buf, pos):
    """buf[pos:] 以 << 开头；返回配对完整的字典字节（处理嵌套）"""
    if not buf.startswith(b'<<', pos):
        return None
    depth = 0
    i = pos
    n = len(buf)
    while i < n - 1:
        two = buf[i:i + 2]
        if two == b'<<':
            depth += 1
            i += 2
        elif two == b'>>':
            depth -= 1
            i += 2
            if depth == 0:
                return buf[pos:i]
        else:
            i += 1
    return None


def _value_after(buf, key):
    """取 key 之后的值：字典（平衡配对）或间接引用对象号"""
    m = re.search(key + rb'\s*', buf)
    if not m:
        return None, None
    pos = m.end()
    if buf.startswith(b'<<', pos):
        return 'dict', _read_dict(buf, pos)
    m2 = re.match(rb'(\d+)\s+0\s+R', buf[pos:])
    if m2:
        return 'ref', int(m2.group(1))
    return None, None


def _page_resources(pdf, page):
    """取页面的 /Resources 字典字节（支持间接引用、嵌套字典、父节点继承）"""
    d = pdf.d(page)
    kind, val = _value_after(d, rb'/Resources')
    if kind == 'dict':
        return val
    if kind == 'ref':
        return pdf.d(val)
    seen = set()
    m = re.search(rb'/Parent\s+(\d+)\s+0\s+R', d)
    while m:
        num = int(m.group(1))
        if num in seen:
            break
        seen.add(num)
        pd = pdf.d(num)
        k2, v2 = _value_after(pd, rb'/Resources')
        if k2 == 'dict':
            return v2
        if k2 == 'ref':
            return pdf.d(v2)
        m = re.search(rb'/Parent\s+(\d+)\s+0\s+R', pd)
    return d


def _balanced_dict(buf, start):
    """从 buf[start] 处的 '<<' 起，按层配平取出整个字典字节；失败返回 b''"""
    if buf[start:start + 2] != b'<<':
        return b''
    i, level = start, 0
    while i < len(buf) - 1:
        two = buf[i:i + 2]
        if two == b'<<':
            level += 1
            i += 2
            continue
        if two == b'>>':
            level -= 1
            i += 2
            if level == 0:
                return buf[start:i]
            continue
        i += 1
    return buf[start:]


def _block_after(pdf, buf, key, hop=0):
    """取 /key 后面的字典字节：支持内联 << >> 与间接引用（跟随上限 3 跳）"""
    if not buf:
        return b''
    m = re.search(re.escape(key) + rb'\s*', buf)
    if not m:
        return b''
    i = m.end()
    if buf[i:i + 2] == b'<<':
        return _balanced_dict(buf, i)
    m2 = re.match(rb'(\d+)\s+0\s+R', buf[i:])
    if m2 and hop < 3:
        return _block_after(pdf, pdf.d(int(m2.group(1))) or b'', key, hop + 1)
    return b''


MAX_FORM_DEPTH = 12      # Form XObject 递归上限（实测链条最深 4 层）


def _collect_images(pdf, res_bytes, depth=0, seen=None, out=None):
    """从资源字典里收集图片对象；对 Form XObject 递归（限深 12）

    关键点：图片常藏在多层 Form 里（页 /Contents 只写 `/Xf6 Do`），
    而 /Resources 可能是嵌套字典，必须按 `<<`/`>>` 配平解析，不能用非贪婪正则。
    """
    if out is None:
        out = []
    if seen is None:
        seen = set()
    if depth > MAX_FORM_DEPTH or not res_bytes:
        return out
    xo = _block_after(pdf, res_bytes, b'/XObject')
    if not xo:
        return out
    for r in re.finditer(rb'/([A-Za-z0-9#._+\-]+)\s+(\d+)\s+0\s+R', xo):
        name, num = r.group(1).decode('latin1'), int(r.group(2))
        if num in seen:
            continue
        seen.add(num)
        sub = pdf.d(num) or b''
        if not sub:
            continue
        if re.search(rb'/Subtype\s*/Image', sub):
            out.append((name, num))
        elif re.search(rb'/Subtype\s*/Form', sub):
            inner = _block_after(pdf, sub, b'/Resources')
            if inner:
                _collect_images(pdf, inner, depth + 1, seen, out)
    return out


def _page_xobjects(pdf, page):
    """返回 [(资源名, 对象号)]，仅图片类型（含多层 Form 内嵌图）"""
    res = _page_resources(pdf, page)
    return _collect_images(pdf, res)


def _filters(d):
    """返回滤镜名列表（兼容单值与数组）"""
    m = re.search(rb'/Filter\s*\[(.*?)\]', d, re.S)
    if m:
        return [x.decode('latin1') for x in
                re.findall(rb'/(\w+)', m.group(1))]
    m = re.search(rb'/Filter\s*/(\w+)', d)
    return [m.group(1).decode('latin1')] if m else []


def _sniff(data):
    """按字节头识别图片格式"""
    if data[:2] == b'\xff\xd8':
        return 'jpeg'
    if data[:4] in (b'\x00\x00\x00\x0c', b'\xff\x4f\xff\x51') \
            or data[:12].find(b'jP  ') >= 0:
        return 'jp2'
    return None


def _unwrap(raw, filters):
    """按滤镜链还原，返回 (kind, bytes)；kind: jpeg|jp2|pixels

    注意：pdf_text 解析时可能已做过 FlateDecode（pdf.s() 返回解压后字节），
    因此先按字节头判断，必要时才再解压一次。
    """
    kind = _sniff(raw)
    if kind:
        return kind, raw
    data = raw
    if 'FlateDecode' in filters:
        try:
            import zlib
            data = zlib.decompress(data)
        except Exception:
            try:
                data = zlib.decompressobj().decompress(data)
            except Exception:
                data = raw
        kind = _sniff(data)
        if kind:
            return kind, data
    return 'pixels', data


def _jp2_to_jpg(data):
    """JPEG2000 → JPEG（先 Pillow，失败再用 cv2）"""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(data))
        im.load()
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        out = _io.BytesIO()
        im.save(out, format='JPEG', quality=88)
        return out.getvalue()
    except Exception:
        pass
    try:
        import cv2
        import numpy as _np
        arr = _np.frombuffer(data, dtype=_np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            ok, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            if ok:
                return buf.tobytes()
    except Exception:
        pass
    return None


def _colorspace_info(pdf, d):
    """返回 (色彩空间名, 分量数)。兼容具名 / [ICCBased n 0 R] / 间接引用"""
    m = re.search(rb'/ColorSpace\s*\[\s*/ICCBased\s+(\d+)\s+0\s+R', d)
    if m:
        try:
            icc = pdf.d(int(m.group(1)))
            n = re.search(rb'/N\s+(\d+)', icc)
            if n:
                return 'ICCBased', int(n.group(1))
        except Exception:
            pass
        return 'ICCBased', 0
    m = re.search(rb'/ColorSpace\s*/(\w+)', d)
    if m:
        name = m.group(1).decode('latin1')
        return name, 3 if name == 'DeviceRGB' else (1 if name in
                                                    ('DeviceGray', 'CalGray') else 0)
    m = re.search(rb'/ColorSpace\s+(\d+)\s+0\s+R', d)
    if m:
        try:
            ref = pdf.d(int(m.group(1)))
            mm = re.search(rb'/ICCBased\s+(\d+)\s+0\s+R', ref)
            if mm:
                icc = pdf.d(int(mm.group(1)))
                n = re.search(rb'/N\s+(\d+)', icc)
                if n:
                    return 'ICCBased', int(n.group(1))
            mm = re.search(rb'/(DeviceRGB|DeviceGray|DeviceCMYK)', ref)
            if mm:
                nm = mm.group(1).decode('latin1')
                return nm, {'DeviceRGB': 3, 'DeviceGray': 1,
                            'DeviceCMYK': 4}.get(nm, 0)
        except Exception:
            pass
    return '', 0


def _decode_image(pdf, num):
    """返回 (扩展名, bytes) 或 (None, 原因)"""
    d = pdf.d(num)
    raw = pdf.s(num)
    if not raw:
        return None, 'empty'
    filters = _filters(d)
    w, h = _num(d, b'Width'), _num(d, b'Height')
    bpc = _num(d, b'BitsPerComponent') or 8
    cname, ncomp = _colorspace_info(pdf, d)
    if b'/DecodeParms' in d and b'Predictor' in d:
        pm = re.search(rb'/Predictor\s+(\d+)', d)
        if pm and int(pm.group(1)) > 1:
            return None, 'predictor'

    kind, data = _unwrap(raw, filters)

    if kind == 'jpeg':
        return 'jpg', data
    if kind == 'jp2':
        conv = _jp2_to_jpg(data)
        if conv:
            return 'jpg', conv
        return None, 'jp2-unsupported'

    # 原始像素（FlateDecode 解压后）：按分量数包成 PNG
    if kind == 'pixels':
        if not (w and h):
            return None, 'no-size'
        n = ncomp
        if not n and bpc == 8:
            # 用字节数反推通道数（1=灰度, 3=RGB, 4=CMYK）
            total = w * h
            if len(data) == total * 3:
                n = 3
            elif len(data) == total:
                n = 1
            elif len(data) == total * 4:
                n = 4
        if bpc != 8:
            return None, 'bpc%d' % bpc
        if n == 3:
            return 'png', _png(w, h, data, 2)
        if n == 1:
            return 'png', _png(w, h, data, 0)
        if n == 4:
            # CMYK → 转 RGB 再存
            try:
                out = bytearray(w * h * 3)
                for i in range(w * h):
                    c, m2, y, k = data[i * 4:i * 4 + 4]
                    out[i * 3] = 255 - min(255, c + k)
                    out[i * 3 + 1] = 255 - min(255, m2 + k)
                    out[i * 3 + 2] = 255 - min(255, y + k)
                return 'png', _png(w, h, bytes(out), 2)
            except Exception:
                return None, 'cmyk-fail'
        return None, 'cs:%s/n%d/len%d' % (cname or '?', n, len(data))

    if 'DCTDecode' in filters:
        return 'jpg', data
    return None, 'filter:%s/bpc%d/%s' % ('+'.join(filters) or 'none', bpc,
                                         cname or '?')


# 碎片页判定：一页内嵌图数量超过该值，说明文字被逐字栅格化，改走整页渲染
FRAGMENT_MAX_IMAGES = 20


def render_page_jpeg(path, page_no, dpi=200, quality=88):
    """用 pypdfium2 把第 page_no 页（1 起）渲染成 JPEG bytes；不可用返回 None"""
    # PIL 只在 vendor 里（主环境没有），to_pil() 需要它
    _vend = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor')
    if os.path.isdir(_vend) and _vend not in sys.path:
        sys.path.insert(0, _vend)
    try:
        import pypdfium2 as pdfium
    except Exception:
        return None
    doc = None
    try:
        doc = pdfium.PdfDocument(path)
        if page_no < 1 or page_no > len(doc):
            return None
        page = doc[page_no - 1]
        bitmap = page.render(scale=dpi / 72.0)
        pil = bitmap.to_pil()
        if pil.mode != 'RGB':
            pil = pil.convert('RGB')
        buf = io.BytesIO()
        pil.save(buf, 'JPEG', quality=quality)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def render_available():
    try:
        import pypdfium2  # noqa: F401
        return True
    except Exception:
        return False


# 判定「整页证书/扫描图」的字节下限：正文内小图标通常 < 20KB
CERT_MIN_BYTES = 40000

# 字体乱码判定：有些 PDF 用子集字体把整段中文重映射成生僻码位
# （例：华农那份商务标全部变成 䇴ḷ㍒…），文字层字数很多但一个可读字都没有。
# 判据：正常字符（ASCII / 常用汉字 / 常用标点 / 全角符号）之外的占比。
GARBLED_MAX = 0.5


def _is_common_char(cp):
    # 控制字符（\u0011 之类）在字体乱码页里非常常见，必须算作"不正常"，
    # 否则像华农 p4 那样 90% 是控制符的页会被判成正常页而漏导出。
    if cp < 0x20 and cp not in (9, 10, 13):
        return False
    if cp == 0x7F:
        return False
    return (cp < 0x80
            or 0x4E00 <= cp <= 0x9FFF        # 常用汉字
            or 0x3000 <= cp <= 0x303F        # 中文标点
            or 0xFF00 <= cp <= 0xFFEF        # 全角字符
            or 0x2000 <= cp <= 0x206F        # 通用标点
            or cp in (0x00B7, 0x2022, 0x3007))


def garbled_ratio(text):
    """返回「非常用字符」占比（0~1）。空文本返回 0。"""
    if not text:
        return 0.0
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if not _is_common_char(ord(c)))
    return bad / float(len(chars))


def export(path, outdir, only_blank_pages=True, min_bytes=8000, limit=None,
           on_progress=None):
    """
    导出图片。only_blank_pages=True 时只导出"无文字页"——即证书扫描页，
    避免把正文页里的小图标也导出来。
    """
    pdf = pdf_text.PDF(path)
    pages = pdf.pages()
    os.makedirs(outdir, exist_ok=True)
    # M11：清理旧图片文件（PDF 换版后旧页图残留会与新版混淆）。
    # 保留 .ocr_cache 与 .export_stats.json——OCR 缓存按内容指纹匹配，不因重导失效。
    for _fn in os.listdir(outdir):
        if _fn.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp',
                                 '.tif', '.tiff')):
            try:
                os.remove(os.path.join(outdir, _fn))
            except Exception:
                pass
    saved = []
    skipped = {}
    _total = len(pages)
    from collections import Counter
    stat_filt = Counter()     # 滤镜链 → 次数
    stat_fail = Counter()     # 解码失败原因 → 次数
    n_obj = 0
    n_ok = 0
    n_garbled = 0
    garbled_pages = []      # S9（v1.8.0）：被判为字体乱码的页码，供报告列出抽查

    for idx, pg in enumerate(pages):
        if limit and len(saved) >= limit:
            break
        if on_progress:
            try:
                on_progress(idx + 1, _total, len(saved))
            except Exception:
                pass
        objs = _page_xobjects(pdf, pg)
        # 碎片页：内嵌图过多说明文字被逐字栅格化，逐图提取无意义 → 整页渲染
        if len(objs) > FRAGMENT_MAX_IMAGES:
            data = render_page_jpeg(path, idx + 1)
            if data:
                fn = 'p%04d_i0.jpg' % (idx + 1)
                with open(os.path.join(outdir, fn), 'wb') as f:
                    f.write(data)
                saved.append({'页': idx + 1, '文件': fn, '字节': len(data),
                              '对象': None, '资源名': '整页渲染'})
                n_obj += len(objs)
                n_ok += 1
                continue
        decoded = [(name, _decode_image(pdf, num)) for name, num in objs]
        if only_blank_pages:
            txt = pdf.page_text(pg).strip()
            garbled = garbled_ratio(txt)
            if garbled > GARBLED_MAX:
                # 文字层是字体乱码：等同"无文字页"，必须出图，否则这页读不了
                n_garbled += 1
                garbled_pages.append(idx + 1)
                big = any(d and d[1] and len(d[1]) >= CERT_MIN_BYTES
                          for _n, d in decoded)
                if not big:
                    data = render_page_jpeg(path, idx + 1)
                    if data:
                        fn = 'p%04d_i0.jpg' % (idx + 1)
                        with open(os.path.join(outdir, fn), 'wb') as f:
                            f.write(data)
                        saved.append({'页': idx + 1, '文件': fn,
                                      '字节': len(data), '对象': None,
                                      '资源名': '整页渲染（乱码页）'})
                        n_obj += len(objs)
                        n_ok += 1
                        continue
            elif len(txt) > 30:
                # 文字虽多，但若含整页扫描图（证书页常见「一行标题+大图」），仍要导出
                big = any(d and d[1] and len(d[1]) >= CERT_MIN_BYTES
                          for _n, d in decoded)
                if not big:
                    continue
        for k, (name, d) in enumerate(decoded):
            ext, data = d
            n_obj += 1
            if not ext:
                stat_fail[str(data)[:40]] += 1
                skipped[data] = skipped.get(data, 0) + 1
                continue
            n_ok += 1
            if len(data) < min_bytes:
                skipped['too-small'] = skipped.get('too-small', 0) + 1
                continue
            fn = 'p%04d_i%d.%s' % (idx + 1, k, ext)
            with open(os.path.join(outdir, fn), 'wb') as f:
                f.write(data)
            saved.append({'页': idx + 1, '文件': fn, '字节': len(data),
                          '对象': None, '资源名': name})
    # 滤镜类型统计（按每页对象再算一次，成本很低）
    try:
        for pg in pages:
            for _nm, num in _page_xobjects(pdf, pg):
                stat_filt['+'.join(_filters(pdf.d(num))) or 'none'] += 1
    except Exception:
        pass
    stats = {'页面数': len(pages), '图片对象': n_obj, '解码成功': n_ok,
             '整页渲染可用': render_available(),
             '乱码页数': n_garbled, '乱码页': garbled_pages,
             '解码失败': dict(stat_fail), '滤镜类型': dict(stat_filt),
             '导出图片': len(saved), '时间': time.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        with open(os.path.join(outdir, '.export_stats.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return {'图片数': len(saved), '图片': saved, '跳过': skipped,
            '输出目录': outdir, '总页数': len(pages), '统计': stats}


def page_summary(path):
    """返回每页是否有文字，供前端列出扫描页。"""
    pdf = pdf_text.PDF(path)
    pages = pdf.pages()
    out = []
    for idx, pg in enumerate(pages):
        t = pdf.page_text(pg).strip()
        has_img = bool(_page_xobjects(pdf, pg))
        out.append({'页': idx + 1, '有文字': len(t) > 30,
                    '字符数': len(t), '有图片': has_img,
                    '乱码': garbled_ratio(t) > GARBLED_MAX})
    return out


if __name__ == '__main__':
    import sys
    import json
    p = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(p), 'pages')
    r = export(p, out)
    print('导出 %d 张图片 -> %s' % (r['图片数'], r['输出目录']))
    if r['跳过']:
        print('跳过统计:', r['跳过'])
    for x in r['图片'][:10]:
        print('  p%-5d %-18s %8.1f KB' % (x['页'], x['文件'], x['字节'] / 1024.0))
