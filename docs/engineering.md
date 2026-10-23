# 工程化基线（2026-10-23 起）

> 目标：让“改东西”从“凭感觉 + 线上观察”变成“先跑测试，再上线”。
> 当前是第一步，不追求一次重构完。

## 1. 现在有什么

- `tests/`：24 个 `unittest` 用例，跑在**真实 NoneBot 容器**里（不是 mock 假环境）。
- `scripts/test.sh`：一键执行
  1. Python 语法编译
  2. Node 语法检查（bridge + 所有 persona 工具）
  3. 单元测试（本机无 Docker 时自动 ssh `your-server`，在 `qqbot-nonebot` 容器跑）
  4. `git diff --check`
- 覆盖的模块：
  - `segments`：空消息判定、@ 名字、引用抑制、媒体锚点
  - `bus`：昵称/群名片两段格式、主人标识、私聊
  - `interact`：目标/参数校验、立即戳/赞、间隔计划器
  - `sticker`：STICKER/SEND 协议、标签打分
  - `doc2text`：CSV/HTML 提取

运行：

```bash
./scripts/test.sh
# 或指定部署机：
DEPLOY_HOST=myhost ./scripts/test.sh
```

测试不会真的戳/赞/发文件：`interact` 用例把 `sender.pick_bot` 换成假 bot。

## 2. 改代码时的约定

1. 先跑 `./scripts/test.sh` 确认基线是绿的；
2. 改纯逻辑（解析、格式、校验、评分）必须加/改用例；
3. 涉及网络（NapCat、B站、QZone、MiMo）的改动，至少补“参数/错误分支”的单元测试，再上服务器验证；
4. 改完再跑一次 `./scripts/test.sh`，通过才部署。

## 3. 已知欠账（不是一次能改完的）

- **配置边界**：路径、端口、QQ、群号仍有硬编码；应逐步抽成 `FISHTANK_*` 环境变量/配置文件。
- **无 CI**：目前靠本地/服务器脚本；公开仓库前需要 GitHub Actions（至少语法 + 纯逻辑测试）。
- **无 LICENSE**：公开前必须选许可证（代码建议 MIT/Apache-2.0）。
- **无 mock adapter**：别人不接真实 QQ 跑不起来；公开版需要 mock OneBot/DSH。
- **大模块**：`triggers.py` / `browser.py` 仍然偏大；只在有测试保护后再拆。
- **集成测试**：浏览器、QZone、B站、媒体理解依赖真实登录/外部服务，暂不做自动 CI。

## 4. 建议顺序

| 阶段 | 内容 | 目的 |
|---|---|---|
| P1（已完成） | 24 个单测 + `scripts/test.sh` | 先有回归网 |
| P2 | 配置抽离（env/config）+ 公开导出 allowlist + secret scan | 能安全开源、能多环境部署 |
| P3 | GitHub Actions + LICENSE + mock adapter + 架构文档 | 公开仓库可维护 |
| P4 | 按模块拆 `triggers`/`browser`，补集成 smoke | 降低长期复杂度 |

> 原则：**先有测试，再重构**；不要为了“看起来工程化”先大拆目录。
