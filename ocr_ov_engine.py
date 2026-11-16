# -*- coding: utf-8 -*-
"""把 RapidOCR 的推理后端换成 OpenVINO（不需要 rapidocr_openvino 包）。

做法：`rapidocr_onnxruntime` 的三个子模块在构造时用 `OrtInferSession`，
把这个名字换成 `OVInferSession` 即可，模型、预处理、后处理、识别字典全都不变。
识别字典从 ONNX 元数据读（用 onnxruntime 只读元数据、不做推理），
所以输出与 ONNX Runtime 同一口径，可以直接逐字比对。

设备：`OCR_OV_DEVICE=CPU`（默认）| `GPU`
本机实测（毫秒/次，best of 3，i5-1035G4 / Iris Plus）：
    det 1×3×736×1056   ORT 162.0 → OV-CPU 56.0 → OV-GPU 107.0
    rec 6×3×48×320     ORT 143.0 → OV-CPU 42.3 → OV-GPU  58.0
    cls 6×3×48×192     ORT   5.0 → OV-CPU  3.0 → OV-GPU  20.7
核显反而更慢，所以默认走 CPU。

用法：
    import ocr_ov_engine; ocr_ov_engine.patch()
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
"""
import json
import os

import threading

import numpy as np

_META_CACHE = {}
_META_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'work', 'ov_meta_cache.json')


def _load_meta_disk():
    """磁盘缓存：第一次用 onnxruntime 读出 custom metadata 后落盘，
    之后再也不导入 onnxruntime（它本身要占几百 MB 提交内存）。"""
    try:
        with open(_META_CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta_disk(all_meta):
    try:
        os.makedirs(os.path.dirname(_META_CACHE_FILE), exist_ok=True)
        with open(_META_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_meta, f, ensure_ascii=False)
    except Exception:
        pass


def _read_metadata(model_path):
    """读模型自带的 custom metadata（含识别字典 character）。

    顺序：内存缓存 → 磁盘缓存 → onnxruntime 读一次并落盘。
    这样常规启动完全不加载 onnxruntime（省下数百 MB 提交内存，
    也是 0xc0000142「python.exe 无法启动」的诱因之一）。
    """
    if model_path in _META_CACHE:
        return _META_CACHE[model_path]
    disk = _load_meta_disk()
    hit = disk.get(os.path.basename(model_path))
    if isinstance(hit, dict) and hit:
        _META_CACHE[model_path] = hit
        return hit
    meta = {}
    try:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 4
        sess = ort.InferenceSession(model_path, sess_options=so,
                                    providers=['CPUExecutionProvider'])
        meta = dict(sess.get_modelmeta().custom_metadata_map or {})
        del sess
        if meta:
            disk[os.path.basename(model_path)] = meta
            _save_meta_disk(disk)
    except Exception:
        meta = {}
    _META_CACHE[model_path] = meta
    return meta


class OVInferSession:
    """接口与 OrtInferSession 对齐：__call__ 返回 [输出数组]"""

    def __init__(self, config):
        import openvino as ov
        self.model_path = config.get('model_path')
        # 量化模型目录（可选）：里面有同名 .xml（IR）时优先用它，例如
        #   OCR_OV_MODELS=vendor\models_ov_int8
        # 由 _quantize_ov.py 生成，用于 int8 权重压缩后的对比测试。
        self.use_path = self.model_path
        mdir = os.environ.get('OCR_OV_MODELS')
        if mdir:
            stem = os.path.splitext(os.path.basename(self.model_path))[0]
            cand = os.path.join(mdir, stem + '.xml')
            if os.path.isfile(cand):
                self.use_path = cand
        self.device = (os.environ.get('OCR_OV_DEVICE') or 'CPU').upper()
        self.core = ov.Core()
        model = self.core.read_model(self.use_path)
        rw = {}
        n = int(config.get('intra_op_num_threads') or -1)
        if n and n > 0:
            rw['INFERENCE_NUM_THREADS'] = n
        # 内存相关（2026-11-02）：默认 NUM_STREAMS=1、线程数上限 4。
        # 目的：在 8 GB 机器上压住每个推理会话的提交内存，避免多进程叠加到
        # 提交上限后新起的 python.exe 报 0xc0000142（DLL 初始化失败）。
        # 需要更多吞吐时可设 OCR_OV_STREAMS=2 观察提交量。
        try:
            streams = int(os.environ.get('OCR_OV_STREAMS') or 1)
        except Exception:
            streams = 1
        try:
            threads = int(os.environ.get('OCR_OV_THREADS') or 0)
        except Exception:
            threads = 0
        if threads <= 0:
            threads = min(4, os.cpu_count() or 4)
        if 'INFERENCE_NUM_THREADS' not in rw:
            rw['INFERENCE_NUM_THREADS'] = threads
        if streams > 0:
            rw['NUM_STREAMS'] = streams
        self.compiled = self.core.compile_model(model, self.device, rw)
        self._local = threading.local()

    def _request(self):
        """每线程一个 InferRequest：OpenVINO 的 request 不能跨线程并发使用，
        并发会抛 "Infer Request is busy"（2026-10-30 实测 188 张图挂了 111 张）。"""
        req = getattr(self._local, 'req', None)
        if req is None:
            req = self.compiled.create_infer_request()
            self._local.req = req
        return req

    def __call__(self, x):
        arr = np.ascontiguousarray(np.asarray(x, dtype=np.float32))
        out = self._request().infer({0: arr})
        return [out[self.compiled.output(0)]]

    def get_input_names(self):
        return [i.get_any_name() for i in self.compiled.inputs]

    def get_output_names(self):
        return [o.get_any_name() for o in self.compiled.outputs]

    def have_key(self, key='character'):
        return key in _read_metadata(self.model_path)

    def get_character_list(self, key='character'):
        raw = _read_metadata(self.model_path).get(key) or ''
        return raw.splitlines()


def patch():
    """把三个子模块里的 OrtInferSession 换成 OpenVINO 版（幂等）"""
    import rapidocr_onnxruntime.ch_ppocr_det.text_detect as td
    import rapidocr_onnxruntime.ch_ppocr_cls.text_cls as tc
    import rapidocr_onnxruntime.ch_ppocr_rec.text_recognize as tr
    for mod in (td, tc, tr):
        mod.OrtInferSession = OVInferSession
    return True
