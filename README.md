# 抖音下载器 · douyin-downloader

供 Codex 和 DeepSeek Harness 使用的抖音公开视频下载 Skill。支持 Windows、macOS，接收分享文案、短链接、标准作品链接和 CSV；标题与媒体地址在任务队列中解析。

需要 **Python 3.10+**。下载与网页服务只使用 Python 标准库，核心功能无需 Node.js、浏览器 Cookie 或登录令牌。

## 安装与发现

GitHub：[jevons1314-creator/douyin-downloader](https://github.com/jevons1314-creator/douyin-downloader)。

解压后复制整个 `douyin-downloader` 文件夹，保持 `SKILL.md` 与脚本目录的相对结构。推荐只安装一份到两个 agent 都支持的共享目录：

| 系统 | Codex 与 DeepSeek Harness 共用目录 |
| --- | --- |
| macOS | `~/.agents/skills/douyin-downloader/` |
| Windows | `%USERPROFILE%\.agents\skills\douyin-downloader\` |

也可以在项目的 `.agents/skills/douyin-downloader/` 安装。DeepSeek 的专用用户目录为 `~/.dsh/skills/`（Windows 为 `%USERPROFILE%\.dsh\skills\`，如设置了 `DSH_HOME`，以该位置为准）。已有 Codex 版本也可能从 `~/.codex/skills` 读取；无需为两个 agent 重复复制同名 Skill。

发现规则：[Codex 官方文档](https://learn.chatgpt.com/docs/build-skills)、[DeepSeek Harness 官方文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)。Codex 可用 `$douyin-downloader` 显式调用；DeepSeek Harness 可用 `/douyin-downloader`，然后附上分享文案及保存目录。若列表没有刷新，重新打开对应 agent。

DeepSeek 的预设需要启用 Skill 发现与加载能力；标准模式提供这些能力。桌面 agent 若提供工作区运行时查询，可直接使用它返回的 Python 绝对路径。

同一台机器的两个 agent 通过 `127.0.0.1:9180` 共用一个任务中心；不同电脑的任务和文件各自保存在本机。远程或沙箱中的 agent 必须能访问实际运行服务的本机文件系统与网络。

## 直接使用

以下命令从 Skill 根目录运行；从其他目录运行时，为脚本传入带引号的绝对路径。

macOS：

```sh
python3 --version
python3 scripts/start_dashboard.py
python3 scripts/download_douyin.py --share-text '粘贴一条或多条抖音分享文案' --output-dir '/绝对路径/视频目录'
```

Windows PowerShell：

```powershell
py -3 --version
py -3 scripts/start_dashboard.py
py -3 scripts/download_douyin.py --share-text '粘贴一条或多条抖音分享文案' --output-dir 'D:\Videos\抖音下载'
```

确认选用的解释器版本至少为 3.10。`--share-text` 与网页“新建下载”走同一队列，自动编号和命名；无需先等链接解析。打开 <http://127.0.0.1:9180/> 可查看任务并暂停、继续或重试。关闭网页或等待中的 CLI 不会取消任务。

`--url` 用于带作品 ID 的标准链接，可配合 `--sequence`、`--title`、`--expected-title`；`--manifest` 接受 CSV。具体来源校验见 [SKILL.md](SKILL.md)。

## 下载行为

- macOS 自动检测 PATH 中的 aria2c，以及系统或用户 Applications 下的 Motrix（含重命名的应用）；可用时四连接下载，否则用标准库。
- Windows 使用标准库下载，以保存可核对的连续断点；“打开本地”使用资源管理器，macOS 使用 Finder。
- 暂停与续传保留 `.part`。aria2 还需 `.aria2`；续传前核对来源标题、前 64 KiB 和总大小。已传完、仅校验未完成的任务可继续完成校验。
- 解析启动错开至少 31 秒。服务限流最多自动重试 5 次；临时网络错误最多重试 3 次；永久错误显示具体原因。下载并发在网页统一设置为 1–4。
- 状态与设置在运行中的 Skill 安装目录 `state/` 内。更新源码前备份并保留它；重启服务前先暂停任务。电脑重启后重新运行启动命令。

解析依赖第三方 RCuts，指定的公开视频链接会发送给它。配置通过 HTTPS 获取，当前返回的解析 API 仍为 HTTP；请勿传私密链接或凭据。下载完成须进一步检查轨道、时长与完整解码，才能证明媒体可播放。

## 验证与 GitHub

标准库测试（Windows 将 `python3` 改为 `py -3`）：

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m unittest discover -s dashboard -p 'test_dashboard.py' -q
```

可选：使用 Node.js 24+ 验证官方消费者确实发现 Skill；此检查不会调用模型或要求账户凭据：

```sh
npm install --prefix .qa-agent-deps --ignore-scripts --no-audit --no-fund @openai/codex@0.163.0 @deepseek-ai/dsh-skill-filesystem@0.2.0-rc.2
node tests/check_agent_discovery.mjs .qa-agent-deps/node_modules
```

GitHub Actions 会在 Windows/macOS 上运行 Python 测试和 agent 发现检查。本次实际执行结果与未验证项见 [TESTING.md](TESTING.md)；提交 CI 文件并不等于已经在 Windows 实机通过。

发布包排除了 `state/`、媒体、日志、断点、`.venv/`、开发依赖和缓存；`.gitignore` 继续保护本机运行数据。当前仓库未附 LICENSE。
