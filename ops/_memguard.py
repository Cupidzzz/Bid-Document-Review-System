# -*- coding: utf-8 -*-
"""启动前的内存闸门（极轻量：只用 ctypes，不导入 numpy/openvino）。

为什么需要它：Windows 的 0xc0000142（应用程序无法正常启动）多半是**提交内存见底**，
进程在加载 DLL 阶段就挂了，连 Python 都跑不起来，看不到任何有用报错。
这里先起一个 6 MB 的小进程探一下水位，不够就给出明确提示并拒绝启动，
而不是让调用方看到那个神秘错误框。

用法：
    python _memguard.py            # 只检查：够就静默退出 0，不够打印原因并退出 3
    python _memguard.py --verbose  # 总是打印当前水位
    python _memguard.py --need 1200
退出码：0 可用 / 3 内存不足 / 4 取不到数据（视为可用，不拦）
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

DEFAULT_NEED_MB = 700


class MEMSTATUS(ctypes.Structure):
    # 注意：MEMORYSTATUSEX 实际长度 64 字节（6×ULONGLONG 之后还有对齐/pad），
    # 传 56 会被 GlobalMemoryStatusEx 判为 ERROR_INVALID_PARAMETER(87)。
    _fields_ = [('len', wt.DWORD), ('mem', wt.DWORD),
                ('total', ctypes.c_ulonglong), ('avail', ctypes.c_ulonglong),
                ('commit_total', ctypes.c_ulonglong),
                ('commit_avail', ctypes.c_ulonglong),
                ('page_total', ctypes.c_ulonglong),
                ('page_avail', ctypes.c_ulonglong),
                ('_pad', ctypes.c_ulonglong)]


def mem():
    ms = MEMSTATUS()
    ms.len = ctypes.sizeof(MEMSTATUS)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMSTATUS)]
    k32.GlobalMemoryStatusEx.restype = wt.BOOL
    if not k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        if '--debug' in sys.argv:
            print('[memguard] 调用失败 err=%d' % ctypes.get_last_error())
        return None
    return {'物理总量MB': round(ms.total / 1048576),
            '物理可用MB': round(ms.avail / 1048576),
            '提交上限MB': round(ms.commit_total / 1048576),
            '提交可用MB': round(ms.commit_avail / 1048576)}


def main(argv):
    verbose = '--verbose' in argv or '-v' in argv
    need = DEFAULT_NEED_MB
    for i, a in enumerate(argv):
        if a == '--need' and i + 1 < len(argv):
            try:
                need = int(argv[i + 1])
            except Exception:
                pass
    m = mem()
    if m is None:
        if verbose:
            print('[memguard] 取不到内存数据，放行')
        return 4
    if verbose:
        print('[memguard] 物理 %(物理可用MB)d/%(物理总量MB)d MB'
              '　提交 %(提交可用MB)d/%(提交上限MB)d MB' % m)
    if m['提交可用MB'] < need:
        print('[memguard] 提交内存只剩 %d MB（需要 ≥ %d MB），拒绝启动新的重进程。'
              % (m['提交可用MB'], need))
        print('           这不是代码错误，是 8 GB 机器的内存被占满：')
        print('           · 关掉 WPS / 多余浏览器标签（每个 Edge 标签 150~300 MB）')
        print('           · 不要同时跑两个项目的复核')
        print('           · 必要时重启服务端（stop_server.cmd → start_server.cmd）')
        print('           现状：物理可用 %(物理可用MB)d MB，提交可用 %(提交可用MB)d MB' % m)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
