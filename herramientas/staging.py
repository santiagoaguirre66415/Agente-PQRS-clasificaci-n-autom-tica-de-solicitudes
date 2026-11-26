"""Staging LOCAL y aislado: ensayo general completo sin tocar datos reales ni enviar nada a internet.

    python herramientas/staging.py [--puerto 8100] [--mantener]

Crea una carpeta temporal propia (base de datos, certificado, buzón de correo, respaldos), arranca un servidor SMTP de pruebas y la app en OTRO puerto,
carga datos ficticios y recorre: salud, prueba de humo por HTTP real, correo de verificación → confirmación → respuesta por correo (con TLS real),
aviso de rebote por webhook, respaldo cifrado + copia externa + restauración desde la copia (comparando contra la app viva), monitoreo y simulación de retención.
Al terminar apaga todo y borra la carpeta, salvo que use --mantener (queda corriendo hasta Ctrl+C para que la explore).

Es el «ensayo general» que conviene repetir antes de cada despliegue. NO sustituye a un staging real con su proveedor de correo, su DNS y su HTTPS:
esos solo se pueden probar en la infraestructura real. Requiere: pip install aiosmtpd cryptography
"""
import argparse
import email
import email.header
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "herramientas"))


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Resultados:
    def __init__(self):
        self.filas: list[tuple[bool, str]] = []

    def paso(self, ok: bool, texto: str, detalle: str = "") -> bool:
        self.filas.append((bool(ok), texto + (f"  ({detalle})" if detalle and not ok else "")))
        print(f"[{'OK' if ok else 'FALLA'}] {self.filas[-1][1]}", flush=True)
        return bool(ok)


def esperar(url: str, segundos: float = 40) -> bool:
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def correos(buzon: Path, esperados: int, espera: float = 10.0) -> list:
    limite = time.time() + espera
    while time.time() < limite:
        archivos = sorted(buzon.glob("*.eml"))
        if len(archivos) >= esperados:
            return [email.message_from_bytes(a.read_bytes()) for a in archivos]
        time.sleep(0.1)
    return [email.message_from_bytes(a.read_bytes()) for a in sorted(buzon.glob("*.eml"))]


def ensayo(carpeta: Path, puerto: int, mantener: bool) -> int:
    import enviar_evento_webhook as webhook
    import prueba_humo as humo
    import respaldo
    import servidor_correo_local as sink

    r = Resultados()
    bd = carpeta / "staging.db"
    clave_admin, secreto_wh = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
    puerto_smtp = puerto_libre()
    controlador, cert = sink.iniciar(carpeta / "correo", puerto_smtp)
    entorno = {k: v for k, v in os.environ.items() if not k.startswith("PQRS_")}       # parte de un entorno limpio
    entorno.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "DATABASE_URL": f"sqlite:///{bd.as_posix()}", "PQRS_ADMIN_USER": "staging.admin", "PQRS_ADMIN_PASSWORD": clave_admin,
        "PQRS_SMTP_HOST": "127.0.0.1", "PQRS_SMTP_PORT": str(puerto_smtp), "PQRS_SMTP_USER": sink.USUARIO, "PQRS_SMTP_PASSWORD": sink.CLAVE,
        "PQRS_SMTP_FROM": "Atención <pqrs@staging.test>", "PQRS_SMTP_CA_FILE": str(cert), "PQRS_URL_PUBLICA": "https://pqrs.staging.test",
        "PQRS_EMPRESA": "Empresa de staging (ficticia)", "PQRS_CONTACTO_DATOS": "datos@staging.test", "PQRS_RETENCION": "2 años (staging)",
        "PQRS_PROVEEDOR_CORREO": "Servidor de pruebas local", "PQRS_WEBHOOK_SECRETO": secreto_wh, "PQRS_SECRETO": secrets.token_urlsafe(32),
        "PQRS_RESPALDO_DIR": str(carpeta / "respaldos"),
    })
    base = f"http://127.0.0.1:{puerto}"
    proceso = None
    try:
        subprocess.run([sys.executable, "seed.py"], cwd=RAIZ, env=entorno, check=True, capture_output=True)
        proceso = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(puerto)],
                                   cwd=RAIZ, env=entorno, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not r.paso(esperar(base + "/salud"), "La app de staging arranca y responde en /salud", "no arrancó en 40 s"):
            return 1
        c = httpx.Client(base_url=base, timeout=30)

        # 1) prueba de humo por HTTP real
        c.post("/auth/login", json={"usuario": "staging.admin", "clave": clave_admin})   # (calienta el hash de contraseña)
        informe = humo.ejecutar(c, True, "staging.admin", clave_admin, espera_reto=2.2, https=False)
        r.paso(informe.fallas == 0, f"Prueba de humo por HTTP real: {len(informe.filas) - informe.fallas}/{len(informe.filas)} comprobaciones",
               "; ".join(t for ok, t in informe.filas if not ok)[:160])

        # 2) correo de verificación -> confirmación -> respuesta por correo (TLS real)
        token = c.post("/auth/login", json={"usuario": "staging.admin", "clave": clave_admin}).json()["token"]
        auth = {"Authorization": "Bearer " + token}
        sid = c.post("/solicitudes", headers=auth, json={"ciudadano": "Ciudadana de Staging", "correo": "ciudadana@staging.test",
                                                         "texto": f"Quiero saber el estado de mi reclamo por la factura {secrets.token_hex(3)}"}).json()["id"]
        buzon = carpeta / "correo" / "buzon"
        previos = len(list(buzon.glob("*.eml")))
        recibidos = correos(buzon, previos + 1)
        verif = next((m for m in reversed(recibidos) if "Confirme" in str(email.header.make_header(email.header.decode_header(m["Subject"])))), None)
        ok = r.paso(verif is not None and verif["To"] == "ciudadana@staging.test", "Llegó el correo de verificación (SMTP con TLS real)")
        if ok:
            enlace = re.search(r"/verificar/([A-Za-z0-9_-]+)", verif.get_payload(decode=True).decode()).group(1)
            r.paso("Correo confirmado" in c.post(f"/verificar/{enlace}").text, "La persona confirma su correo con el enlace")
            resp = c.post(f"/solicitudes/{sid}/responder", headers=auth, json={"respuesta": "Estimada ciudadana, revisamos su reclamo y corregimos el cobro.", "canal": "correo"})
            r.paso(resp.status_code == 200, "Respuesta oficial enviada por correo", f"HTTP {resp.status_code} {resp.text[:80]}")
            enviado = correos(buzon, previos + 2)[-1]
            r.paso("corregimos el cobro" in enviado.get_payload(decode=True).decode(), "El correo de respuesta llegó con el texto aprobado")

            # 3) webhook de entrega: rebote
            with sqlite3_conn(bd) as db:
                mid = db.execute("SELECT mensaje_id FROM solicitudes WHERE id = ?", (sid,)).fetchone()[0]
            wh = webhook.enviar(c, base, mid, "rebotado", secreto_wh)
            det = c.get(f"/solicitudes/{sid}", headers=auth).json()
            r.paso(wh.status_code == 200 and det["entrega"] == "rebotado", "El aviso de rebote del webhook queda registrado en la solicitud")

        # 4) respaldo cifrado + copia externa + restauración desde la copia, comparando con la app viva
        clave_resp = carpeta / "respaldo.key"
        respaldo.generar_clave(clave_resp)
        viva = c.get("/estadisticas", headers=auth).json()["total"]
        arch = respaldo.crear(bd, carpeta / "respaldos", str(clave_resp))
        copia, mismo = respaldo.copiar_externo(arch, carpeta / "externo", str(clave_resp))
        info = respaldo.verificar(copia, str(clave_resp))
        r.paso(info["integridad"] == "ok" and info["filas"]["solicitudes"] == viva,
               f"Respaldo cifrado + copia externa restaurada desde la copia: {info['filas']['solicitudes']} solicitudes = {viva} en la app viva")
        destino = carpeta / "restaurada.db"
        respaldo.restaurar(copia, destino, str(clave_resp))
        with sqlite3_conn(destino) as db:
            r.paso(db.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0] == viva, "Restauración real a una base nueva con las mismas filas")
        if mismo:
            print("      (aviso: la copia externa del ensayo está en el mismo disco; en producción debe estar en otro)")

        # 5) monitoreo y retención (simulación)
        # UTF-8 explícito en ambos lados: en Windows el hijo escribía UTF-8 y se leía como cp1252 («SIMULACIÃ“N»)
        mon = subprocess.run([sys.executable, "herramientas/monitoreo.py", "--json"], cwd=RAIZ, env=entorno, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        r.paso(mon.returncode in (0, 1) and '"metricas"' in mon.stdout, "El monitoreo lee la base de staging y produce métricas")
        ret = subprocess.run([sys.executable, "herramientas/retencion.py", "antiguas", "--dias", "730"], cwd=RAIZ, env=entorno, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        r.paso(ret.returncode == 0 and "SIMULACIÓN" in ret.stdout, "La retención simula sin modificar nada por defecto")

        faltan = sum(1 for ok, _ in r.filas if not ok)
        print(f"\nEnsayo general: {len(r.filas) - faltan} de {len(r.filas)} pasos correctos.")
        if mantener:
            print(f"\nStaging corriendo en {base}  (usuario staging.admin, clave {clave_admin})\nBuzón de correo: {carpeta / 'correo' / 'buzon'}\nCtrl+C para apagar y borrar.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return 1 if faltan else 0
    finally:
        if proceso:
            proceso.terminate()
            try:
                proceso.wait(10)
            except subprocess.TimeoutExpired:
                proceso.kill()
        controlador.stop()


def sqlite3_conn(ruta: Path):
    import sqlite3
    return closing(sqlite3.connect(ruta))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--puerto", type=int, default=8100)
    ap.add_argument("--mantener", action="store_true")
    a = ap.parse_args(argv[1:])
    try:
        import aiosmtpd  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError:
        print("Instale las herramientas de prueba:  pip install aiosmtpd cryptography")
        return 2
    carpeta = Path(tempfile.mkdtemp(prefix="pqrs_staging_"))
    try:
        return ensayo(carpeta, a.puerto, a.mantener)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)   # nada del ensayo queda en el disco


if __name__ == "__main__":
    sys.exit(main(sys.argv))
