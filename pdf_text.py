#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdf_text.py —— 纯标准库 PDF 文本提取（模块2：只需文字与中文占比）

环境无 fitz/pypdf/pdfminer，pip 无网络，故自行实现：
对象解析 → FlateDecode → ToUnicode CMap → 内容流解释。

两个已踩过的坑
--------------
1. WPS 导出的文字是**字面串 `(...)`**，不是十六进制 `<...>`。两种都要支持，
   否则一个片段都取不到。
2. 字体未必有 ToUnicode。没有 CMap 时按 2 字节 CID 直译，虽不可读，
   但仍能统计"取到多少字符"，用于判断文字层是否存在。
"""
import re
import zlib


class PDF(object):
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = f.read()
        self.objs = {}
        self.streams = {}
        self.failed_streams = []   # G6：解压失败的对象号
        if b'/Encrypt' in self.data:
            # G6：加密 PDF，标准库无法解密；明确报错，而不是抽成"空文字层"被当成扫描件
            raise ValueError('该 PDF 已加密（含 /Encrypt），无法提取文字层')
        self._parse()
        self._inflate()
        self._expand_objstm()

    def _parse(self):
        """
        单次扫描建立对象索引。

        不能用行首锚点 `(?m)^`：二进制流之后常紧跟 `N 0 obj` 而不带换行，
        行首匹配会漏掉这类对象（表现为内容流 0 字节、字体全无 ToUnicode）。
        故不锚定行首，改为校验"obj 之后是否像 PDF 对象开头"，以排除
        二进制数据里的巧合匹配。
        """
        good = re.compile(rb'\s*(?:<<|<[0-9A-Fa-f]|\[|\(|/|[-+]?\d|true|false|null)')
        for m in re.finditer(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b', self.data):
            if not good.match(self.data, m.end()):
                continue
            num = int(m.group(1))
            end = self.data.find(b'endobj', m.end())
            if end < 0:
                continue
            body = self.data[m.end():end]
            # 增量更新修复（2026-11-12）：PDF 增量更新（incremental update）会在
            # 文件末尾追加同对象号的新定义，覆盖文件头的旧定义。若不处理，
            # 会取到文件头的旧空 /Pages（Count 0），表现为 pages()=0、
            # 正文 0 字节，被误判成"这份文件没有材料"。
            # 结构对象（页树/目录）取最后一次定义；普通对象保持首次
            # （避免二进制巧合匹配覆盖真实对象）。
            _is_struct = (re.search(rb'/Type\s*/Pages', body) is not None
                          or re.search(rb'/Type\s*/Catalog', body) is not None)
            if num in self.objs:
                if _is_struct:
                    self.objs[num] = body
                continue
            self.objs[num] = body

    def _expand_objstm(self):
        """
        展开压缩对象流（/Type/ObjStm）。

        PDF 1.5+ 会把页面字典、资源、字体等压进 ObjStm，只按原始字节找
        `N 0 obj` 会漏掉这些对象，表现为：页数能数出来，但内容流为 0 字节、
        字体全部"无 ToUnicode"、图像数为 0。

        ObjStm 布局：解压后开头是 N 组「对象号 偏移」，之后是按偏移排列的对象体，
        偏移基准为 /First。规范不允许流对象放进 ObjStm，故这里只注册字典对象。
        """
        for num in list(self.streams):
            d = self.objs.get(num, b'')
            if b'/ObjStm' not in d:
                continue
            s = self.streams.get(num) or b''
            if not s:
                continue
            mn = re.search(rb'/N\s+(\d+)', d)
            mf = re.search(rb'/First\s+(\d+)', d)
            if not (mn and mf):
                continue
            n, first = int(mn.group(1)), int(mf.group(1))
            if first > len(s):
                continue
            nums = [int(x) for x in re.findall(rb'\d+', s[:first])]
            pairs = []
            for i in range(0, min(len(nums), 2 * n) - 1, 2):
                pairs.append((nums[i], nums[i + 1]))
            for idx, (onum, off) in enumerate(pairs):
                start = first + off
                end = (first + pairs[idx + 1][1]) if idx + 1 < len(pairs) else len(s)
                if 0 <= start < end <= len(s) and onum not in self.objs:
                    self.objs[onum] = s[start:end]

    def _inflate(self):
        for num, raw in list(self.objs.items()):
            i = raw.find(b'stream')
            if i < 0:
                continue
            head = raw[:i]
            j = i + 6
            while j < len(raw) and raw[j] in (13, 10):
                j += 1
            k = raw.rfind(b'endstream')
            body = raw[j:k if k > 0 else len(raw)]
            # G4（2026-11-11）：图像流（/Subtype /Image）不做 FlateDecode——
            # 解压后的原始像素每页 20-60MB，常驻 self.streams 是
            # 「python.exe 0xc0000142」的直接成因。保留压缩字节（通常小 10-20 倍），
            # pdf_images._unwrap 会按需自行 zlib.decompress（已有容错逻辑）。
            is_image = (b'/Subtype' in head and b'/Image' in head)
            if b'FlateDecode' in head and not is_image:
                try:
                    body = zlib.decompress(body)
                except Exception:
                    try:
                        body = zlib.decompressobj().decompress(body)
                    except Exception:
                        body = b''
                        self.failed_streams.append(num)   # G6：记录解压失败的对象号
            self.streams[num] = body
            self.objs[num] = head

    def _build_obj_index(self):
        """一次扫描建立 对象号 -> 结束位置 的索引（避免逐对象全文搜索）"""
        idx = {}
        for m in re.finditer(rb'(?<![0-9])(\d{1,10})\s+0\s+obj\b', self.data):
            num = int(m.group(1))
            if num not in idx:          # 保留首次出现，与原逻辑一致
                idx[num] = m.end()
        self._objidx = idx

    def _ensure(self, n):
        """
        按需定位对象。

        不可依赖"逐行扫描所有 obj"：二进制流之后常紧跟 `N 0 obj` 而不带换行，
        行首锚点会漏掉这些对象，表现为内容流为 0 字节、字体全部无 ToUnicode。
        故引用到缺失对象时，用对象号在原始字节里定向查找（走一次性索引）。
        """
        if n in self.objs or n is None or n <= 0:
            return
        if not hasattr(self, '_objidx'):
            self._build_obj_index()
        start = self._objidx.get(int(n))
        if start is None:
            return
        end = self.data.find(b'endobj', start)
        if end < 0:
            return
        raw = self.data[start:end]
        i = raw.find(b'stream')
        if i < 0:
            self.objs[n] = raw
            return
        head = raw[:i]
        j = i + 6
        while j < len(raw) and raw[j] in (13, 10):
            j += 1
        k = raw.rfind(b'endstream')
        body = raw[j:k if k > 0 else len(raw)]
        if b'FlateDecode' in head:
            try:
                body = zlib.decompress(body)
            except Exception:
                try:
                    body = zlib.decompressobj().decompress(body)
                except Exception:
                    body = b''
        self.streams[n] = body
        self.objs[n] = head

    def d(self, n):
        self._ensure(n)
        return self.objs.get(n, b'')

    def s(self, n):
        self._ensure(n)
        return self.streams.get(n, b'')

    def _find_pages_ref(self):
        """找 /Pages 引用。优先在已解析对象里找 /Type/Catalog（可靠），
        回退到原始字节配平（Catalog 在未展开的流对象里时）。

        原始字节配平有个坑：`/Names<</Dests...>>` 这类嵌套 << 会让
        rfind(b'<<') 找到内层字典而非 Catalog 字典本体，导致找不到 /Pages。
        而 _parse/_expand_objstm 已经把 Catalog 对象正确解析进 self.objs，
        直接查字典比配平更可靠。
        """
        # ① 优先：已解析对象里的 Catalog
        for _n, _d in self.objs.items():
            if re.search(rb'/Type\s*/Catalog\b', _d):
                mm = re.search(rb'/Pages\s+(\d+)\s+0\s+R', _d)
                if mm:
                    return int(mm.group(1))
        # ② 回退：原始字节配平
        for m in re.finditer(rb'/Type\s*/Catalog\b', self.data):
            start = self.data.rfind(b'<<', 0, m.start())
            if start < 0:
                continue
            depth, i = 0, start
            while i < len(self.data):
                if self.data[i:i + 2] == b'<<':
                    depth += 1
                    i += 2
                elif self.data[i:i + 2] == b'>>':
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    i += 1
            seg = self.data[start:i]
            mm = re.search(rb'/Pages\s+(\d+)\s+0\s+R', seg)
            if mm:
                return int(mm.group(1))
        return None

    def pages(self):
        root = self._find_pages_ref()
        if root is None:
            # G5：找不到 /Pages 就明确报错，不再按对象号升序推页序——
            # 对象号升序 ≠ 实际页序，页图/OCR/报告页码会整体错位，比报错更危险。
            raise ValueError('无法定位 PDF 的 /Pages（Catalog 结构异常、加密或文件损坏）')
        out = []
        self._walk(root, out, set())
        return out

    def _walk(self, n, out, seen):
        if n in seen:
            return
        seen.add(n)
        d = self.d(n)
        if re.search(rb'/Type\s*/Page\b', d):
            out.append(n)
            return
        m = re.search(rb'/Kids\s*\[(.*?)\]', d, re.S)
        if m:
            for k in re.finditer(rb'(\d+)\s+0\s+R', m.group(1)):
                self._walk(int(k.group(1)), out, seen)

    def cmaps(self, page):
        d = self.d(page)
        out = {}
        m = re.search(rb'/Font\s*<<(.*?)>>', d, re.S)
        if not m:
            return out
        for fm in re.finditer(rb'/([A-Za-z0-9#._-]+)\s+(\d+)\s+0\s+R', m.group(1)):
            fo = self.d(int(fm.group(2)))
            tm = re.search(rb'/ToUnicode\s+(\d+)\s+0\s+R', fo)
            out[fm.group(1)] = _cmap(self.s(int(tm.group(1)))) if tm else {}
        return out

    def fonts(self, page):
        """返回 {字体名: {'cmap': {...}, 'w': 1|2}}

        w=2 为 Type0（CJK，双字节），w=1 为简单字体（单字节）。
        """
        d = self.d(page)
        out = {}
        m = re.search(rb'/Font\s*<<(.*?)>>', d, re.S)
        if not m:
            return out
        for fm in re.finditer(rb'/([A-Za-z0-9#._-]+)\s+(\d+)\s+0\s+R', m.group(1)):
            name = fm.group(1)
            fo = self.d(int(fm.group(2)))
            tm = re.search(rb'/ToUnicode\s+(\d+)\s+0\s+R', fo)
            cmap = _cmap(self.s(int(tm.group(1)))) if tm else {}
            sub = re.search(rb'/Subtype\s*/Type0', fo)
            out[name] = {'cmap': cmap, 'w': 2 if sub else 1}
        return out

    def page_text(self, page):
        d = self.d(page)
        cm = self.fonts(page)
        bodies = []
        m = re.search(rb'/Contents\s+(\d+)\s+0\s+R', d)
        if m:
            bodies.append(self.s(int(m.group(1))))
        else:
            m = re.search(rb'/Contents\s*\[(.*?)\]', d, re.S)
            if m:
                for r in re.finditer(rb'(\d+)\s+0\s+R', m.group(1)):
                    bodies.append(self.s(int(r.group(1))))
        return _interpret(b'\n'.join(bodies), cm)


def _cmap(data):
    out = {}
    for blk in re.findall(rb'beginbfchar(.*?)endbfchar', data, re.S):
        for m in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
            out[int(m.group(1), 16)] = _u(m.group(2))
    for blk in re.findall(rb'beginbfrange(.*?)endbfrange', data, re.S):
        for m in re.finditer(
                rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
            lo, hi, base = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
            for i in range(min(hi - lo + 1, 4096)):
                out[lo + i] = chr(base + i)
    return out


def _u(hb):
    try:
        return bytes.fromhex(hb.decode()).decode('utf-16-be')
    except Exception:
        return ''


TOKEN = re.compile(rb'<<|>>|\[|\]|<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\()])*\)'
                   rb'|/[^\s/<>\[\]()]+|[-+]?\d*\.?\d+|[A-Za-z\'"*]+')


def _interpret(content, cmaps):
    parts = []
    stack = []
    font = None
    fw = 2 if not cmaps else (
        2 if isinstance(next(iter(cmaps.values())), dict) else 0)
    for m in TOKEN.finditer(content):
        t = m.group(0)
        if t == b'Tf':
            if len(stack) >= 2 and isinstance(stack[-2], bytes) \
                    and stack[-2].startswith(b'/'):
                font = stack[-2][1:]
            stack = []
        elif t in (b'Tj', b'TJ'):
            ent = cmaps.get(font) or {}
            if isinstance(ent, dict) and 'cmap' in ent:
                cm, fw = ent['cmap'], ent['w']
            else:
                cm, fw = ent, 2
            for s in stack:
                if isinstance(s, bytes) and (s.startswith(b'<') or s.startswith(b'(')):
                    txt = _str(s, cm, fw)
                    if txt:
                        parts.append(txt)
            stack = []
        elif t in (b'BT', b'ET', b'Tm', b'Td', b'TD', b'T*', b'cm', b'q', b'Q'):
            stack = []
        else:
            stack.append(t)
            if len(stack) > 32:
                stack = stack[-32:]
    return ''.join(parts)


def _str(raw, cmap, w=2):
    if raw.startswith(b'<') and raw.endswith(b'>'):
        h = re.sub(rb'\s+', b'', raw[1:-1])
        if len(h) % 2:
            h += b'0'
        try:
            bs = bytes.fromhex(h.decode())
        except Exception:
            return ''
    elif raw.startswith(b'(') and raw.endswith(b')'):
        bs = _unesc(raw[1:-1])
    else:
        return ''
    out = []
    if w == 1:
        # 单字节简单字体：有 ToUnicode 用映射，否则按 ASCII/WinAnsi 直译
        for b in bs:
            if cmap and b in cmap:
                out.append(cmap[b])
            elif 0x20 <= b <= 0x7E:
                out.append(chr(b))
            elif b >= 0xA0:
                out.append(bytes([b]).decode('cp1252', 'ignore'))
        return ''.join(out)
    for i in range(0, len(bs) - 1, 2):
        code = (bs[i] << 8) | bs[i + 1]
        out.append(cmap.get(code, '') if cmap else chr(code))
    return ''.join(out)


def _unesc(b):
    m = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
         0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c != 0x5C:
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(b):
            break
        c = b[i]
        if c in m:
            out.append(m[c])
            i += 1
        elif 0x30 <= c <= 0x37:
            o = b''
            while i < len(b) and 0x30 <= b[i] <= 0x37 and len(o) < 3:
                o += bytes([b[i]])
                i += 1
            out.append(int(o, 8) & 0xFF)
        elif c in (10, 13):
            i += 1
        else:
            out.append(c)
            i += 1
    return bytes(out)


CJK = re.compile(r'[\u4e00-\u9fff]')


def extract(path):
    """返回 {'页数','每页字符','总字符','中文字符','中文占比','文字层质量','正文'}"""
    pdf = PDF(path)
    pages = pdf.pages()
    per = []
    texts = []
    for pg in pages:
        t = pdf.page_text(pg)
        per.append(len(t))
        texts.append(t)
    body = '\n'.join(texts)
    cjk = len(CJK.findall(body))
    ratio = round(100.0 * cjk / max(1, len(body)), 2)
    fail_n = len(pdf.failed_streams)
    if ratio >= 30:
        q = '良好'
    elif ratio >= 1:
        q = '部分可用'
    elif fail_n:
        q = '读取失败'   # G6：有解压失败且没抽出字，是"读不出"而非"无文字层"
    else:
        q = '不可用'
    return {'页数': len(pages), '每页字符': per, '总字符': len(body),
            '中文字符': cjk, '中文占比': ratio, '文字层质量': q,
            '解压失败数': fail_n, '正文': body}


if __name__ == '__main__':
    import sys
    r = extract(sys.argv[1])
    print('页数 %d  总字符 %d  中文字符 %d  占比 %.2f%%  质量 %s'
          % (r['页数'], r['总字符'], r['中文字符'], r['中文占比'], r['文字层质量']))
    print('每页字符:', r['每页字符'][:20])
    print('正文前 300 字:')
    print(r['正文'][:300])
