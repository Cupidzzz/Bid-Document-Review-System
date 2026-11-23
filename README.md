# 投标文件复核系统

用 AI 替代"人工逐页翻标书找证据"的苦活，让每一分都能点回标书原文那一页的投标文件复核工具。

## 技术栈

- 后端：Python 3.9（标准库 HTTP 服务）
- 平台：**仅 Windows**（启动脚本为 `.cmd`）
- 前端：原生 HTML + CSS + JavaScript（单文件，无框架）
- OCR：PP-OCRv4 模型 + OpenVINO 推理引擎
- 判分：独立 AI 智能体（逐项判分、每分留依据与出处）
- 报告：python-docx（Word）、reportlab（PDF）

## 特点

- **本地部署**：标书全程不出内网；
- **可追溯**：每项得分都带「册名+页码+文字层/OCR」出处，可回查原文；
- **不瞎判**：找不到证据标"需人工"，绝不静默给 0 分；
- **扫描件友好**：无文字层的扫描页自动渲染 + OCR。

## 架构

本仓库是**服务端 + 前端 + OCR 预处理**，真正的**判分由一个外部 AI 智能体**完成：

```
UI（ui/main.html）
  ↓ HTTP
server.py（队列、预处理、OCR 预热、汇总）
  ↓ 唤醒（wake.py 向 agent 会话注入消息）
agent（外部 AI 智能体，逐项判分）
  ↓ POST /api/run（claim / stopcheck / finalize）
server.py（回填得分、汇总、导出报告）
```

- **服务端只做**：任务队列、正文抽取、扫描页导出、OCR、结果汇总、导出报告；
- **判分由 agent 做**：agent 通过 `POST /api/run` 认领任务（claim）、查停止（stopcheck）、
  判分后结单（finalize）；判分规则在私有的《复核方法.md》里；
- **唤醒**：`wake.py` 通过环境变量 `DSH_SESSION_ID`（或 `work/wake_target.json`）指定
  要唤醒的 agent 会话；当前实现面向 DeepSeek Harness（DSH）平台。

> ⚠️ **本仓库不能独立跑出分数**：它需要一个"能读文件 + 调 HTTP 接口"的 AI 智能体当
> 判分执行器，并配置好唤醒目标（`DSH_SESSION_ID`）。没有 agent，只能跑起 UI、预处理
> 和 OCR，跑不出评分。

## 唤醒机制与部署边界

`wake.py` 用本机 DSH 凭据库里的签名密钥自动签发会话 cookie，实现「前端点开始复核 →
自动唤醒 agent」，让客户零输入、零命令。

> ⚠️ **仅限单机自用**：此唤醒链路依赖本机凭据，等价于「能登录本机 DSH 的人即可驱动
> agent」。多用户 / 公网 / 需要互相隔离的环境**不要部署这条唤醒链路**；若 DSH 日后提供
> 官方唤醒 API，应优先改用。

## 目录

- `server.py` 服务端主程序（路由、队列、预处理、预热、汇总）
- `ui/main.html` 前端单页
- `start_server.cmd` / `stop_server.cmd` 启停脚本
- `ops/` 启动依赖的运维工具

## 运行

```cmd
start_server.cmd
```

然后浏览器打开 http://127.0.0.1:8767/ —— 首次打开会弹「访问口令」框，口令在
服务启动窗口的「口令」行（或 `workpi_token.txt`）；每重启一次口令会重新生成。

## 安装（搬到新机器）

1. 安装 Python 3.9；
2. 把 `vendor/`、`vendor_ov/` 两个目录**整个复制**过去（已打包好，含 OCR 引擎与模型）；
   若要重新下载依赖，运行 `python download_ocr.py <输出目录>`（下载会校验 SHA256）；
3. 依赖版本见 `requirements.txt`（锁定版本，保证可复现）；
4. 运行 `start_server.cmd`，浏览器打开 http://127.0.0.1:8767/ 输访问口令即可。

## 目录结构

```
server.py           服务端主程序（路由 / 队列 / 预处理 / OCR / 汇总）
ui/main.html        前端单页
ocr_text.py         OCR（PP-OCRv4 + onnxruntime / openvino 双后端）
download_ocr.py     依赖下载（带 SHA256 校验）
wake.py             唤醒 agent 会话
vendor/             打包的第三方依赖（onnxruntime 引擎等）
vendor_ov/          打包的 OpenVINO 引擎（可选，不装则回退 onnxruntime）
requirements.txt    锁定依赖版本
start_server.cmd    启动（Windows）
stop_server.cmd     停止
```

## 已知限制

- **仅 Windows**：启动脚本是 `.cmd`，未做跨平台适配；
- **判分依赖外部 agent**：本仓库不含判分执行器，需配置 DSH 会话（见「架构」）；
- **单机自用**：访问口令鉴权 + 唤醒链路都按「单机」设计，多用户 / 公网环境需另行加固。
- **大模型内容安全拦截**：判分 agent 要读投标文件全文，测绘 / 地图 / 地理信息 / 海域海岛 /
  地下管线 / 人脸识别等涉密敏感内容可能触发大模型（如 DeepSeek）的 `refusal`
  （拒绝回答，报 `Content Exists Risk`），导致该单位复核跑不动；属外部模型平台的内容
  安全策略，非本系统缺陷，遇此类单位建议换模型或改人工复核。
