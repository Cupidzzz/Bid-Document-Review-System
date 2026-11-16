# -*- coding: utf-8 -*-
"""停掉监听指定端口的进程（由 stop_server.cmd 调用，也可单独用）。

用法：
    python _stop_port.py 8767
"""
import ctypes
import ctypes.wintypes as wt
import sys

AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3
PROCESS_TERMINATE = 0x0001


class ROW(ctypes.Structure):
    _fields_ = [('st', wt.DWORD), ('la', wt.DWORD), ('lp', wt.DWORD),
                ('ra', wt.DWORD), ('rp', wt.DWORD), ('pid', wt.DWORD)]


class TBL(ctypes.Structure):
    _fields_ = [('n', wt.DWORD), ('t', ROW * 1)]


def pids_listening(port):
    ip = ctypes.WinDLL('iphlpapi')
    sz = wt.DWORD(0)
    ip.GetExtendedTcpTable(None, ctypes.byref(sz), False, AF_INET,
                           TCP_TABLE_OWNER_PID_LISTENER, 0)
    if not sz.value:
        return []
    buf = ctypes.create_string_buffer(sz.value)
    if ip.GetExtendedTcpTable(buf, ctypes.byref(sz), False, AF_INET,
                              TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    t = ctypes.cast(buf, ctypes.POINTER(TBL)).contents
    rows = ctypes.cast(t.t, ctypes.POINTER(ROW))
    out = set()
    for i in range(t.n):
        r = rows[i]
        p = ((r.lp & 0xFF) << 8) | ((r.lp >> 8) & 0xFF)
        if p == port and r.pid:
            out.add(r.pid)
    return sorted(out)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
    pids = pids_listening(port)
    if not pids:
        print('  没有进程在监听 %d' % port)
        return 0
    k = ctypes.WinDLL('kernel32')
    rc = 0
    for p in pids:
        h = k.OpenProcess(PROCESS_TERMINATE, False, p)
        if not h:
            print('  无法打开 pid %d（可能已在退出）' % p)
            rc = 1
            continue
        ok = k.TerminateProcess(h, -1)
        k.CloseHandle(h)
        print(('  已停止 pid %d' % p) if ok else ('  停止失败 pid %d' % p))
        if not ok:
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
