import os
import smtplib
from email.message import EmailMessage

email = os.environ["GMX_EMAIL"]
password = os.environ["GMX_PASSWORD"]

msg = EmailMessage()
msg["From"] = email
msg["To"] = email
msg["Subject"] = "Prueba de aviso-cultura-getafe"
msg.set_content(
    "Este es un correo de prueba del proyecto aviso-cultura-getafe.\n\n"
    "Si lo has recibido, el envío desde GitHub Actions mediante GMX funciona."
)

with smtplib.SMTP("mail.gmx.es", 587, timeout=30) as smtp:
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(email, password)
    smtp.send_message(msg)

print("Correo de prueba enviado a la propia cuenta GMX.")
