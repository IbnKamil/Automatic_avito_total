from __future__ import annotations

import mimetypes
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from avito_monitor.config import AppConfig


class EmailSender:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def is_configured(self) -> bool:
        return bool(self.config.smtp_to and self.config.smtp_username and self.config.smtp_password)

    def send_report(
        self,
        subject: str,
        html_body: str,
        embedded_charts: list[dict[str, str]],
        attachment_path: Path | None = None,
    ) -> None:
        if not self.is_configured():
            raise RuntimeError(
                "Email не настроен. Укажите SMTP_USERNAME, SMTP_PASSWORD и SMTP_TO в .env"
            )

        message = MIMEMultipart("related")
        message["Subject"] = subject
        message["From"] = self.config.smtp_from or self.config.smtp_username
        message["To"] = self.config.smtp_to

        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(html_body, "html", "utf-8"))
        message.attach(alternative)

        import base64

        for chart in embedded_charts:
            image = MIMEImage(base64.b64decode(chart["content"]), _subtype="png")
            image.add_header("Content-ID", f"<{chart['cid']}>")
            image.add_header(
                "Content-Disposition", "inline", filename=chart["filename"]
            )
            message.attach(image)

        if attachment_path and attachment_path.exists():
            mime_type, _ = mimetypes.guess_type(attachment_path.name)
            maintype, subtype = (mime_type or "text/html").split("/", 1)
            with attachment_path.open("rb") as handle:
                attachment = MIMEText(handle.read().decode("utf-8"), _subtype=subtype)
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=attachment_path.name,
            )
            message.attach(attachment)

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=60) as server:
            if self.config.smtp_use_tls:
                server.starttls()
            server.login(self.config.smtp_username, self.config.smtp_password)
            server.send_message(message)
