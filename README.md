# my-defense
我的防线自动化脚本

邮件告警需要在项目根目录的 `.env` 文件中设置 163 邮箱客户端授权码：

```dotenv
EMAIL_AUTH_CODE=你的163邮箱客户端授权码
```

也可以在启动脚本前设置系统环境变量 `EMAIL_AUTH_CODE`；系统环境变量优先于 `.env`。`.env` 已加入 Git 忽略规则，不会被提交。

自动战斗过程中，同一场景连续检测到 10 次会发送一封告警邮件；场景变化后重新计数。
未知画面持续超过 180 秒时也会发送告警邮件；邮件发送成功后脚本停止。

## 卡牌选择策略

卡牌不再只按固定优先级挑选，而是在原有优先级（`card_constants.CARD_NAME_LIST`）上叠加局内上下文评分：已选同流派张数、同名卡升级。权重默认在 `card_policy.py` 的 `DEFAULT_WEIGHTS`，可通过项目根目录的 `card_policy.json` 覆盖调参。

每完成 `FIT_EVERY_RUNS` 局（当前为 3，常量在 `card_policy.py` 顶部，可自行改大）且有通关/失败结果后，脚本会用最近的对局数据做一次批量拟合，更新「流派协同 / 动量 / 同名升级」三个权重，结果保存到 `logs/card_policy_learned.json`，重启后继续累加。控制台会打印 `[拟合]` 一行，显示用了多少局、权重变化。`base` 与动量阈值仍由人工配置。

每次选牌的候选/得分写入 `logs/card_decisions.jsonl`，每局结束写入 `run_end`，每局样本写入 `logs/card_policy_samples.jsonl`（供批量拟合），拟合结果写入 `learn` 记录。删掉 `logs/card_policy_learned.json` 与 `logs/card_policy_samples.jsonl` 可完全重置学习。
