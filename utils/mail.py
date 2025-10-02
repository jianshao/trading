import smtplib
from email.mime.text import MIMEText
from email.header import Header
from data.config import *
from utils.logger_manager import LoggerManager

def send_email(subject: str, body: str):
    """
    发送邮件通知
    :param subject: 邮件主题
    :param body: 邮件内容
    :return: None
    """
    # 构造邮件
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header("交易系统", "utf-8")  # 显示发件人昵称
    msg["To"] = Header("投资人", "utf-8")     # 显示收件人昵称
    msg["Subject"] = Header(subject, "utf-8")

    # 发送邮件
    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, auth_code)
            server.sendmail(sender_email, [receiver_email], msg.as_string())
    except Exception as e:
        LoggerManager.Error("app", strategy="mail", event="send_email", content=f"Email sent to {receiver_email} with subject: {subject}")