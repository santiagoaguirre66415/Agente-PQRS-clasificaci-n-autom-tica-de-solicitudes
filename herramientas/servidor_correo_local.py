"""Servidor SMTP local de pruebas (gratuito). Recibe los correos de la app con STARTTLS y autenticación reales
y los guarda como archivos .eml, para probar el envío de punta a punta SIN enviar nada a internet.

Requiere (solo para pruebas, no es dependencia de la app):  pip install aiosmtpd cryptography
Uso:    python herramientas/servidor_correo_local.py
Luego, en OTRA terminal, arranque la app con:
    $env:PQRS_SMTP_HOST="localhost"; $env:PQRS_SMTP_PORT="2525"
    $env:PQRS_SMTP_USER="prueba";    $env:PQRS_SMTP_PASSWORD="prueba-local-123"
    $env:PQRS_SMTP_FROM="pqrs@ejemplo.test"
    $env:PQRS_SMTP_CA_FILE="herramientas/.local/cert.pem"      # confía SOLO en este certificado; la verificación TLS sigue activa
    $env:PQRS_URL_PUBLICA="https://pqrs.ejemplo.test"
Los correos aparecen en herramientas/.local/buzon/*.eml (ábralos con cualquier cliente de correo o un editor de texto).
"""
import datetime
import ipaddress
import ssl
import sys
import time
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

USUARIO, CLAVE = "prueba", "prueba-local-123"


def crear_certificado(carpeta: Path) -> tuple[Path, Path]:
    """Certificado autofirmado para localhost (válido 30 días). La app lo usará como única autoridad de confianza."""
    carpeta.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = carpeta / "cert.pem", carpeta / "key.pem"
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(clave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(ahora - datetime.timedelta(minutes=5))
            .not_valid_after(ahora + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(clave, hashes.SHA256()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                             serialization.NoEncryption()))
    return cert_path, key_path


class Buzon:
    def __init__(self, carpeta: Path):
        self.carpeta, self.n = carpeta, 0
        carpeta.mkdir(parents=True, exist_ok=True)

    async def handle_DATA(self, server, session, envelope):
        self.n += 1
        archivo = self.carpeta / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.n:03d}.eml"
        archivo.write_bytes(envelope.content)
        print(f"[correo recibido] {archivo.name}  de={envelope.mail_from}  para={envelope.rcpt_tos}", flush=True)
        return "250 Message accepted for delivery"


def autenticar(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == USUARIO.encode() and auth_data.password == CLAVE.encode()
    return AuthResult(success=ok)


def iniciar(carpeta: Path, puerto: int) -> tuple[Controller, Path]:
    """Arranca el servidor en un hilo. Devuelve el controlador (llamar .stop()) y la ruta del certificado."""
    cert, key = crear_certificado(carpeta)
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexto.load_cert_chain(cert, key)
    controlador = Controller(Buzon(carpeta / "buzon"), hostname="127.0.0.1", port=puerto, tls_context=contexto,
                             require_starttls=True, authenticator=autenticar, auth_required=True, auth_require_tls=True)
    controlador.start()
    return controlador, cert


if __name__ == "__main__":
    carpeta = Path(__file__).parent / ".local"
    controlador, cert = iniciar(carpeta, 2525)
    print(f"Servidor SMTP de pruebas en localhost:2525 (usuario '{USUARIO}', clave '{CLAVE}').\n"
          f"Certificado de confianza: {cert}\nBuzón: {carpeta / 'buzon'}\nCtrl+C para detenerlo.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        controlador.stop()
        sys.exit(0)
