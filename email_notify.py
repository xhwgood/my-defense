# -*- coding: utf-8 -*-
"""
email_notify.py —— 通过 163 邮箱 SMTP 发送告警邮件。

未知画面持续超过阈值时，auto_battle.py 调用 send_alert_email() 通知人工处理。

依赖：Python 标准库 smtplib / ssl / email（无需额外安装）。
"""

import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465  # 网易邮箱 SMTP 使用 SSL 直连 465 端口

SENDER = "18989792827@163.com"
RECEIVER = "734824565@qq.com"

_SEND_FROM_NAME = "自动战斗脚本"


def _load_auth_code():
    auth_code = os.environ.get("EMAIL_AUTH_CODE")
    if auth_code:
        return auth_code

    env_file = Path(__file__).with_name(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "EMAIL_AUTH_CODE":
                return value.strip().strip("\"'")
    return ""


AUTH_CODE = _load_auth_code()


def send_alert_email(subject, body, receiver=RECEIVER, timeout=30):
    """发送一封纯文本告警邮件，返回是否成功；失败时打印原因（不抛异常）。"""
    if not AUTH_CODE:
        print("[!] 发送告警邮件失败：未设置 EMAIL_AUTH_CODE 环境变量，请在项目根目录 .env 中配置")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header(_SEND_FROM_NAME, "utf-8")), SENDER))
    msg["To"] = formataddr((str(Header("告警接收人", "utf-8")), receiver))
    msg["Subject"] = Header(subject, "utf-8")

    try:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout, context=ctx)
        server.login(SENDER, AUTH_CODE)
        server.sendmail(SENDER, [receiver], msg.as_string())
        server.quit()
        print("[+] 告警邮件已发送")
        return True
    except Exception as e:
        print(f"[!] 发送告警邮件失败：{type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    # 独立运行本模块即可测试邮件是否可正常发送：
    #   python email_notify.py
    ok = send_alert_email("【测试】未知画面邮件通知", "")
    print("测试结果：" + ("成功" if ok else "失败"))
