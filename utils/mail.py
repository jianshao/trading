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
    msg = MIMEText(body, "html", "utf-8")
    msg["From"] = Header(f"profits <{sender_email}>", "ascii")
    msg["To"] = Header(f"owner <{receiver_email}>", "ascii")
    msg["Subject"] = Header(subject, "utf-8")

    # 发送邮件
    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
            server.login(sender_email, auth_code)
            server.sendmail(sender_email, [receiver_email], msg.as_string())

    except Exception as e:
        # print(f"{subject} 发送失败: {e}")
        LoggerManager.Error("app", strategy="mail", event="send_email", content=f"Email sent to {receiver_email} with subject: {subject} failed. Error: {e}")
