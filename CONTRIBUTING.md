# Contributing to HotMic

感谢你的关注！欢迎提交 Issue 和 Pull Request。

## 开发环境

```bash
git clone https://github.com/xiaohongsimon/hotmic.git
cd hotmic

# 安装开发依赖
pip install -e ".[dev]"

# 安装 Qwen3-ASR
python3 -m venv ~/.hotmic/venv
~/.hotmic/venv/bin/pip install mlx-qwen3-asr

# 构建 whisper.cpp（备用引擎）
python3 scripts/setup.py
```

## 代码规范

- 使用 [ruff](https://docs.astral.sh/ruff/) 进行代码检查
- 提交前运行 `ruff check src/ scripts/`
- Python 3.10+ 兼容

## 提交 PR

1. Fork 本仓库
2. 创建分支：`git checkout -b feat/your-feature`
3. 提交更改
4. 推送并创建 PR

## 报告 Bug

请使用 [Issue 模板](https://github.com/xiaohongsimon/hotmic/issues/new/choose) 提交 bug 报告，附上：
- 操作系统版本
- Python 版本
- `daemon health` 输出
- `~/.config/hotmic/daemon.log` 最后 20 行
