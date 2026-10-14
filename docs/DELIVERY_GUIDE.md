# 源码包阅读与运行指南

交付日期：2026-10-14。ZIP 根目录为 `AlphaLab/`，仅包含当前 Alpha Lab 项目，不包含早先的游戏或 OASIS。

## 内容与阅读顺序

1. `README.md`：功能、规则、能力边界和基本操作。
2. `docs/DESIGN.md`：架构图、模块职责、数据流、并发、账本、Agent、缓存与选型。
3. `docs/INTERVIEW.md`：面试追问、回答思路、源码练习与简历表述边界。
4. `docs/VALIDATION.md`：历次工程验证记录，区分模拟测试与实际行情验证。
5. `docs/PACKAGE_VERIFICATION.md`：此次干净解压环境的启动与测试结果。
6. `docs/previews/`：干净实例截图，没有本机个人实验历史。
7. `app/`、`static/`、`tests/`：全部后端、前端与测试源码。
8. `tools/package_release.py`、依赖文件、启动脚本、空白环境示例与 `MANIFEST.json`：复现与打包辅助。

不包含 `.env`、真实密钥、DPAPI 密文数据库、本机比赛与证据资料、`.venv`、`node_modules`、日志和旧 `test-results`。首次运行会创建新的数据目录、默认配置与账户，不会自动出现本机六位 Agent 已完成的 15 轮历史比赛。

## 运行条件

- 交付在 Windows、Python 3.12 上验证。密钥保险箱使用当前 Windows 用户的 DPAPI；没有实现 Linux/云端密钥管理适配。
- 获取真实行情与首次安装依赖需要网络；行情源可能失败或休市，页面保留原始报价时间。
- 看行情不需要模型密钥。运行 Agent 需要自己的服务连接与额度。
- 前端无需构建。浏览器测试额外需要 Node.js 与 Chrome。
- 只运行一个 Uvicorn worker，不能直接加多个 worker 宣称完成扩容。

## 启动

解压后在 `AlphaLab` 根目录打开 PowerShell：

```powershell
./run.ps1
```

默认打开 `http://127.0.0.1:8010/`，接口文档是 `/docs`。如果 8010 已被本机原项目占用，使用新解压目录并手动选择 8020：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.lock.txt
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8020 --workers 1
```

随后打开 `http://127.0.0.1:8020/`。直接依赖在 `requirements.txt`；精确版本快照在 `requirements.lock.txt`，对应此次 Windows/Python 3.12 环境，不代表其他系统的兼容性保证。

不要让两个实例共享同一运行数据目录。若 PowerShell 禁止执行脚本，可使用上面的手动命令，无需改变系统全局执行策略。

## 配置模型与比赛

先确认首页检查时间和原始报价时间；再进入“模型连接”填写自己的服务地址、凭据和模型 ID。通过“Agent 配置”编辑策略、温度、Token 上限、工具轮数和权限。

新建历史实验，选择学习与验证区间，逐日推进或运行全区间；完成后核对策略检查点、成交与验证表现，再迁移到重新初始化的实时账户。在“实时模拟”中开启 Agent 决策。仅查看首页行情不会启动模型交易。

推荐使用界面配置连接。`.env.example` 是空白示例，可复制成 `.env` 设置进程超时、并发和预算。包里没有可直接使用的私人模型密钥。实际模型调用使用自己的供应商额度。

## 安全隔离的测试入口

后端测试：

```powershell
./.venv/Scripts/python.exe -m pytest -q
```

浏览器流程先在第一个终端启动 8011 隔离服务：

```powershell
./.venv/Scripts/python.exe tests/browser_server.py
```

另一个终端运行：

```powershell
npm ci
npm run test:browser
```

默认浏览器脚本只操作明确标记的 8011 模拟环境，依次检查历史学习/验证/迁移、配置与证据管理、实时行情。测试使用临时数据库、合成行情与模拟模型，不继承私人密钥，不调用真实模型；样本收益不是实际交易成绩。结束后在第一个终端按 Ctrl+C，临时数据库随服务退出清理。

也可单独执行 `npm run test:browser:workflow`、`npm run test:browser:config`、`npm run test:browser:market`。

`browser-visual.cjs` 是展示服务只读布局验收，要求已有已完成回测，干净实例不满足这个前置条件。`browser-research-live.cjs` 会在展示服务归档资料，不属于默认隔离测试。旧 `browser.cjs` 保留源码，但不作为新版默认入口。

## 面试演示与源码练习

**10 秒开场：** 实时首页 → 点一只股票展示报价时间与详情 → 打开 Agent 配置，说明模型、策略与工具权限独立配置。

**2 分钟展开：** 事先准备历史实验 → 展示学习/验证边界与冻结检查点 → 展示工具记录与一笔委托实际执行结果 → 迁移经验并检查来源 → 解释事务、幂等、预算和单进程限制。

真实实验应说明配置与数据来源；使用 8011 演示时明确说是模拟测试。不要现场临时启动漫长付费回测，也不要将测试曲线当作真实模型收益。

读源码建议从 `main.py` 的推进接口进入 `engine.py`，追踪工作副本、并发选手、工具执行与提交；接着阅读 `broker.py` 的资金冻结测试、`market_monitor.py` 的并发与故障测试，以及配置/证据权限测试。结合 `INTERVIEW.md` 自己修改一个可验证行为。

## 常见问题

| 问题 | 处理 |
| --- | --- |
| 端口占用 | 换端口或关闭自己之前启动的实例 |
| 股票价格不变 | 查看是否休市、原始报价时间和错误提示，不生成假波动 |
| 没有旧比赛 | 包不携带本机数据库，需新建实验 |
| 模型请求失败 | 核对地址、凭据、模型 ID 和工具调用兼容性 |
| 预算不足 | 调整预算或减少 Agent、工具轮数与实验天数 |
| 历史工具被拒绝 | 当前网页与部分当前财务工具受到历史时间边界限制 |
| 无法读取报告 | 可能反爬、登录限制或扫描 PDF，不会生成假原文 |
| 找不到 Chrome | 安装 Chrome；后端 pytest 不需要浏览器 |

## 完整性与后续分享

`MANIFEST.json` 包含每个交付文件的大小与 SHA-256，可核对快照。运行环境、Node 依赖与数据库应在解压后重新生成。

这是本地源码交付，没有创建或发布 GitHub 仓库。后续可上传源码和文档；生成的数据与运行文件由 `.gitignore` 排除。
