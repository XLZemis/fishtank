# Contributing

- 先跑测试：`python3 -m compileall -q perception persona/tools scripts && python3 -m unittest discover -s tests -v`（或 `./scripts/test.sh`）。
- 纯逻辑改动必须补测试；涉及外部服务（NapCat/浏览器/模型）的改动请把参数和错误分支单独测。
- 不要提交 `.env`、token、cookie、QQ 账号、聊天数据库或任何真实用户数据。
- 提交前跑 `git diff --check`；保持变更聚焦，不顺手大改无关模块。
