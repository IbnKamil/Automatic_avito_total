from __future__ import annotations

import base64
import logging
import mimetypes
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from avito_monitor.config import AppConfig

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    pass


class EmailSender:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def is_configured(self) -> bool:
        return bool(self.config.smtp_to and self.config.smtp_username and self.config.smtp_password)

    def _connect(self) -> smtplib.SMTP:
        if self.config.smtp_port == 465 and not self.config.smtp_use_tls:
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=60,
                context=context,
            )
        server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=60)
        if self.config.smtp_use_tls:
            server.starttls()
        return server

    def _login_hint(self) -> str:
        if "gmail.com" in self.config.smtp_host:
            return (
                "Для Gmail нужен пароль приложения, а не обычный пароль аккаунта. "
                "Создайте его здесь: https://myaccount.google.com/apppasswords "
                "(сначала включите двухэтапную аутентификацию). "
                "В .env укажите полный email в SMTP_USERNAME и 16-символьный пароль без пробелов."
            )
        return (
            "Проверьте SMTP_USERNAME, SMTP_PASSWORD и что в почтовом сервисе "
            "разрешена отправка через SMTP."
        )

    def send_report(
        self,
        subject: str,
        html_body: str,
        embedded_charts: list[dict[str, str]],
        attachment_path: Path | None = None,
    ) -> None:
        if not self.is_configured():
            raise EmailConfigurationError(
                "Email не настроен. Укажите SMTP_USERNAME, SMTP_PASSWORD и SMTP_TO в .env"
            )

        message = MIMEMultipart("related")
        message["Subject"] = subject
        message["From"] = self.config.smtp_from or self.config.smtp_username
        message["To"] = self.config.smtp_to

        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(html_body, "html", "utf-8"))
        message.attach(alternative)

        for chart in embedded_charts:
            image = MIMEImage(base64.b64decode(chart["content"]), _subtype="png")
            image.add_header("Content-ID", f"<{chart['cid']}>")
            image.add_header(
                "Content-Disposition", "inline", filename=chart["filename"]
            )
            message.attach(image)

        if attachment_path and attachment_path.exists():
            mime_type, _ = mimetypes.guess_type(attachment_path.name)
            _, subtype = (mime_type or "text/html").split("/", 1)
            with attachment_path.open("rb") as handle:
                attachment = MIMEText(handle.read().decode("utf-8"), _subtype=subtype)
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=attachment_path.name,
            )
            message.attach(attachment)

        try:
            with self._connect() as server:
                server.login(self.config.smtp_username, self.config.smtp_password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailConfigurationError(
                f"Ошибка входа в почту ({exc.smtp_code}): логин или пароль не приняты. "
                f"{self._login_hint()}"
            ) from exc
        except smtplib.SMTPException as exc:
            raise EmailConfigurationError(f"Не удалось отправить email: {exc}") from exc

    def send_test_email(self) -> None:
        html = """
        <html><body>
          <h2>Тест Avito Monitor</h2>
          <p>Если вы видите это письмо, SMTP настроен правильно.</p>
        </body></html>
        """
        self.send_report(
            subject="Тест Avito Monitor — SMTP работает",
            html_body=html,
            embedded_charts=[],
        )
        logger.info("Тестовое письмо отправлено на %s", self.config.smtp_to)
