# HotMic

[![Status: Archived](https://img.shields.io/badge/status-archived-lightgrey.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/Platform-macOS%20(Apple%20Silicon)-blue.svg)]()
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

> ## 📦 项目归档说明（不再更新）
>
> 在持续打磨过程中逐渐意识到一个现实：**产品级语音输入**是一场需要海量真实场景语料标注、长周期模型加训、以及大量工程细节打磨的重活。个人开发者仅凭开源 ASR 模型（哪怕是 Qwen3-ASR 这样顶级的开源模型）加规则化后处理，在识别稳定性、标点节奏、短语切分、口音与场景适配等细节上，与有专门数据管线和标注团队的闭源产品（如智谱 [AutoGLM 语音输入法](https://chatglm.cn/) 等）仍存在难以逾越的差距 —— 这部分差距并非算法层能补齐。
>
> 因此，日常使用已经切换到 AutoGLM 输入法；有类似需求的朋友推荐直接使用它。
>
> **但这段折腾本身的收获并不小**，尤其是在 agentic coding（AI 协同开发）这个语境下：
>
> - 和 AI 反复打磨过 supervisor 架构、UDS vs TCP、`flock` 单实例锁、macOS 主线程 / NSPanel 约束、PortAudio 状态机怪癖等工程细节 —— 这些都是独立开发时容易绕过、但要面向真实用户就绕不开的坑
> - 全程 spec-first + feature branch 的协作流程，让 AI 从"写代码片段"升级到"独立推进整条 feature"
> - 最终的架构形态（menubar supervisor + 原生 NSPanel 浮窗 + 单实例锁）保留在 `feat/native-overlay`、`feat/single-instance-lock` 分支，供参考
>
> 作为一份 **agentic coding 的真实踩坑记录** 归档在此。代码以 MIT 许可开源，欢迎 fork；主仓库不再发版、不再接受 PR，issue 也不再响应。
>
> 下文保留原有设计文档与使用说明，仅供存档与参考。

---

macOS 本地语音输入工具。按住热键说话，松开自动粘贴到任意窗口。

基于 [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) 流式识别，Apple Silicon GPU 加速，中文识别效果优秀。

> 基于 [enesbasbug/voice-to-claude](https://github.com/enesbasbug/voice-to-claude) 重构，新增统一 Qwen3-ASR 流式转录、实时浮窗、自动粘贴等核心功能。

[English](docs/README_EN.md)

## 特性

- **任意窗口可用** — 不绑定特定应用，编辑器、浏览器、终端、聊天工具都能用
- **实时流式转录** — 说话时浮窗实时显示识别文字
- **中文识别优秀** — Qwen3-ASR 对中文普通话识别准确率高，中英混合也支持
- **完全本地运行** — 所有处理在本机完成，音频不出设备，保护隐私
- **自动粘贴** — 转录完成后自动粘贴到你之前操作的窗口
- **按住说话** — 按住 Ctrl+Alt（左右均可）录音，松开即转录
- **亚秒级延迟** — 模型常驻内存，0.5 秒间隔增量推送音频

## 安装

### 方式一：下载 DMG（推荐）

1. **[下载 HotMic.dmg](https://github.com/xiaohongsimon/hotmic/releases/latest/download/HotMic.dmg)**
2. 双击打开，将 HotMic 拖到 Applications
3. 双击 HotMic.app（首次自动安装依赖，约 5 分钟）
4. 菜单栏出现 🎙 图标，按住 **Ctrl+Alt** 说话

### 方式二：命令行一键安装

```bash
curl -sSL https://raw.githubusercontent.com/xiaohongsimon/hotmic/main/install.sh | bash
```

### 首次使用需授权

- **系统设置 → 隐私与安全 → 麦克风** → 允许终端应用
- **系统设置 → 隐私与安全 → 辅助功能** → 允许终端应用

**按住 Ctrl+Alt** → 说话 → 松开 → 文字自动粘贴到当前窗口。

<details>
<summary>手动安装（高级用户）</summary>

```bash
brew install cmake && xcode-select --install

git clone https://github.com/xiaohongsimon/hotmic.git
cd hotmic

# Qwen3-ASR
python3 -m venv ~/.hotmic/venv
~/.hotmic/venv/bin/pip install mlx-qwen3-asr

# whisper.cpp 备用 + 项目依赖
python3 scripts/setup.py
```

</details>

### 作为 Claude Code 插件使用

```bash
/hotmic:setup    # 首次安装
/hotmic:start    # 启动
```

## 架构

```
┌──────────────────────────────────────────────────┐
│  守护进程（后台运行）                                │
│                                                   │
│  [热键监听] → [麦克风录音]                          │
│      ↓              ↓                             │
│  [浮窗显示] ← [流式转录客户端]                      │
│                      ↓ TCP (localhost)             │
│  [自动粘贴] ← ─ ─ ─ ─┘                            │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────┴────────────────────────────┐
│  Qwen3-ASR Worker（独立进程）                       │
│  - 模型加载一次，常驻 MLX 内存                       │
│  - 原生流式 API，增量返回文字                        │
│  - 1.7B 参数，float16，Metal GPU 加速              │
└───────────────────────────────────────────────────┘
```

### 工作流程

1. **按住热键** → 记录当前前台应用，启动麦克风，初始化流式会话
2. **每 0.5 秒** → 增量音频发送到 Worker，浮窗实时更新文字
3. **松开热键** → `finish_streaming()` 输出最终准确文本
4. **自动粘贴** → 通过 osascript 激活之前的应用，Cmd+V 粘贴

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `asr_backend` | `qwen3` | 识别引擎：`qwen3` 或 `whisper` |
| `qwen3_model` | `Qwen/Qwen3-ASR-1.7B` | Qwen3-ASR 模型 |
| `feed_interval` | `0.5` | 音频推送间隔（秒） |
| `language` | `zh` | 语言代码 |
| `hotkey` | `ctrl+alt` | 热键组合（左右均可） |
| `overlay_enabled` | `true` | 实时浮窗显示 |

配置文件：`~/.config/hotmic/config.json`

## 健康检查

```bash
python3 scripts/exec.py daemon health
```

自动诊断守护进程、ASR Worker、麦克风状态，发现问题自动修复。

## 故障排除

| 问题 | 解决方案 |
|------|---------|
| 无声音输入 | 系统设置 → 隐私与安全 → 麦克风 → 允许终端 |
| 热键无反应 | 系统设置 → 隐私与安全 → 辅助功能 → 允许终端 |
| 蓝牙耳机切换后不工作 | 运行 `daemon health` 或重启守护进程 |
| 自动粘贴无效 | 授予辅助功能权限 |
| 构建失败 | `brew install cmake && xcode-select --install` |

日志：`tail -50 ~/.config/hotmic/daemon.log`

## 为什么选 HotMic

| | HotMic | Typeless | macOS 听写 |
|---|---|---|---|
| 处理方式 | 本地（Apple Silicon） | 云端 | 本地 |
| 隐私 | 音频不出设备 | 音频发送到云端 | 本地 |
| 中文支持 | Qwen3-ASR，专门优化 | 通用 | 一般 |
| 实时显示 | 浮窗流式更新 | 有 | 有 |
| 自动粘贴 | 粘贴到任意窗口 | 有 | 仅限输入框 |
| 价格 | 免费开源 | $12-30/月 | 免费 |
| 可定制 | 完全开源 | 闭源 | 不可定制 |

## 隐私

所有处理在本地完成。音频不发送到任何服务器。无遥测，无数据收集。

## 致谢

- [hotmic](https://github.com/enesbasbug/hotmic) by [@enesbasbug](https://github.com/enesbasbug) — 原始项目
- [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) by Alibaba — 语音识别模型，中文表现优秀
- [mlx-qwen3-asr](https://github.com/nicholasgasior/mlx-qwen3-asr) — Apple Silicon MLX 适配
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) — 备用转录引擎

## License

MIT
