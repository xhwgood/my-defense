# my-defense
我的防线自动化脚本

邮件告警需要在项目根目录的 `.env` 文件中设置 163 邮箱客户端授权码：

```dotenv
EMAIL_AUTH_CODE=你的163邮箱客户端授权码
```

也可以在启动脚本前设置系统环境变量 `EMAIL_AUTH_CODE`；系统环境变量优先于 `.env`。`.env` 已加入 Git 忽略规则，不会被提交。
