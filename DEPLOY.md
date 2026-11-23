# 部署指南（从零开始）

> 本指南面向**从 GitHub 克隆仓库、在一台全新 Windows 机器上跑起来**的场景。
> 仓库只含代码与依赖清单，**OCR 引擎与模型需按本指南补齐**（模型随依赖包自带，引擎可选）。
>
> **如果你本机已经跑通过**（有 `vendor\`、`vendor_ov\` 两个目录），直接把它们整个复制到新机器，
> 跳到第五节「启动」即可，无需重新下载。

---

## 一、前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（启动脚本是 `.cmd`，仅 Windows） |
| Python | 3.9（必须，锁定的依赖是 cp39 wheel） |
| 内存 | 串行 OCR ≥ 8 GB；若要并行多项目 ≥ 16 GB |
| 网络 | 能访问 `pypi.tuna.tsinghua.edu.cn`（清华镜像） |

---

## 二、获取代码

```cmd
git clone https://github.com/Cupidzzz/Bid-Document-Review-System.git
cd Bid-Document-Review-System
```

> 克隆下来的仓库**没有** `vendor\`、`vendor_ov\` 两个目录（太大，未进 git），下面三步补齐。

---

## 三、安装依赖（含 OCR 模型）

### 方式 A（推荐）：用脚本从清华镜像下载并离线安装

```cmd
python download_ocr.py vendor
python -m pip install --no-deps --target vendor vendor\*.whl
```

`download_ocr.py` 会解析清华镜像、选最新 `cp39 win_amd64` wheel，下载后做 SHA256 校验。

### 方式 B：pip 配镜像直接装

```cmd
python -m pip install --target vendor -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> **为什么不用默认 pip 源**：项目实测直连 PyPI（`files.pythonhosted.org`）会 TLS 握手超时，
> 必须走清华镜像。

### 关于模型（重要，别去单独下模型）

**OCR 模型（PP-OCRv4 的 det/rec/cls）是 `rapidocr-onnxruntime` 包自带的**，
装完依赖就自动出现在 `vendor\rapidocr_onnxruntime\models\` 下，**不需要单独下载**。

---

## 四、OpenVINO 引擎（可选，但强烈建议）

OpenVINO 是加速推理后端，实测比 onnxruntime 快 **5~20 倍**。不装也能跑（自动回退 onnxruntime），
只是慢。

```cmd
python download_ov.py vendor_ov
python -m pip install --no-deps --target vendor_ov vendor_ov\*.whl
```

- 装好后服务端**自动**用 OpenVINO（检测到 `vendor_ov\` 目录即启用）；
- 想强制指定后端，设环境变量 `OCR_ENGINE=onnxruntime` 或 `OCR_ENGINE=openvino`。

---

## 五、启动

```cmd
start_server.cmd
```

然后浏览器打开 <http://127.0.0.1:8767/>。

首次打开会弹「访问口令」框，口令在**服务启动窗口的「口令」行**（或 `work\api_token.txt`）。
每重启一次口令会重新生成。

---

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| `python.exe 无法启动 0xc0000142` | 内存不足。关掉占内存的进程，或加内存到 16 GB |
| OCR 慢（6~8 秒/页） | 没装 OpenVINO，正在用 onnxruntime。按第四节补 `vendor_ov\` |
| pip 直连超时 | 用清华镜像（`-i https://pypi.tuna.tsinghua.edu.cn/simple`） |
| 手写/印章/模糊影印识别错字 | PP-OCRv4 对印刷体好、对手写弱，属模型固有边界，结果会带「疑手写·需回原图」标记 |
| 判分跑不起来 | 本仓库**不含判分执行器**，需另行配置外部 AI 智能体会话（见 README「架构」） |

---

## 七、部署后自检

```cmd
python -c "import sys; sys.path.insert(0,'vendor'); import ocr_text; print('引擎:', ocr_text._ENGINE_NAME)"
```

- 输出 `引擎: openvino` → 已启用加速后端；
- 输出 `引擎: onnxruntime` → 未装 OpenVINO，能用但慢（按第四节补）。
