"""Guía interactiva para probar su PROVEEDOR DE CORREO REAL (el que usted configuró en PQRS_SMTP_*).

Por defecto solo comprueba la conexión (STARTTLS + inicio de sesión) y NO envía ningún correo. Para enviar de verdad:

    python herramientas/prueba_proveedor.py --a tucorreo@tudominio.com --confirmar [--rebote inexistente@tudominio.com]

Solo escriba direcciones SUYAS: el script envía 2 mensajes de prueba (uno de verificación y uno de respuesta) a la dirección --a,
y, si indica --rebote, un tercero a una dirección que NO existe en su dominio para comprobar que los rebotes llegan.
Imprime una lista de lo que debe revisar a mano (bandeja, spam, SPF/DKIM/DMARC en «Mostrar original», rebote). Nunca imprime la contraseña.
Requiere las mismas variables PQRS_SMTP_* (y PQRS_SMTP_PASSWORD_FILE si la guarda en un archivo) que la app.
"""
import argparse
import os
import re
import smtplib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import correo  # noqa: E402

_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def probar_conexion() -> dict:
    """Conecta, negocia TLS y se autentica SIN enviar nada. Devuelve datos para el informe."""
    t0 = time.perf_counter()
    with correo.SMTP(os.environ["PQRS_SMTP_HOST"], int(os.environ.get("PQRS_SMTP_PORT", "587")),
                     timeout=float(os.environ.get("PQRS_SMTP_TIMEOUT", "15"))) as s:
        s.starttls(context=correo._contexto_tls())
        version = getattr(getattr(s, "sock", None), "version", lambda: "desconocida")()
        s.login(os.environ["PQRS_SMTP_USER"], correo._clave())
        return {"tls": version, "segundos": round(time.perf_counter() - t0, 2)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", help="SU dirección de correo, donde recibirá los mensajes de prueba")
    ap.add_argument("--rebote", help="dirección inexistente de SU dominio, para probar los rebotes")
    ap.add_argument("--confirmar", action="store_true", help="enviar de verdad (sin esto solo se prueba la conexión)")
    a = ap.parse_args(argv[1:])
    for nombre, valor in (("--a", a.a), ("--rebote", a.rebote)):
        if valor and not _CORREO.match(valor):
            print(f"{nombre}: dirección no válida")
            return 2

    print("1) Configuración")
    if not correo.configurado():
        print("   FALTA: defina PQRS_SMTP_HOST, PQRS_SMTP_USER, PQRS_SMTP_FROM y la contraseña (PQRS_SMTP_PASSWORD o PQRS_SMTP_PASSWORD_FILE).")
        return 2
    print(f"   servidor={os.environ['PQRS_SMTP_HOST']}:{os.environ.get('PQRS_SMTP_PORT', '587')}  usuario={os.environ['PQRS_SMTP_USER']}  "
          f"remitente={os.environ['PQRS_SMTP_FROM']}  contraseña=(oculta)")
    base = os.environ.get("PQRS_URL_PUBLICA", "")
    print(f"   PQRS_URL_PUBLICA={'OK ' + base if base.startswith('https://') else 'FALTA (debe empezar con https://; sin ella no se envían verificaciones)'}")

    print("2) Conexión, TLS y autenticación (no envía nada)")
    try:
        r = probar_conexion()
    except (smtplib.SMTPException, OSError) as e:
        print(f"   ERROR: {type(e).__name__}. Revise servidor, puerto 587, usuario y contraseña (en Gmail: «contraseña de aplicación»).")
        return 1
    print(f"   OK  TLS={r['tls']}  tiempo={r['segundos']} s")

    if not a.confirmar:
        print("\nNo se envió ningún correo. Para enviar los mensajes de prueba agregue --a su@correo.com --confirmar")
        return 0
    if not a.a:
        print("Falta --a con su dirección.")
        return 2

    print("3) Envío de mensajes de prueba")
    try:
        correo.enviar_verificacion(a.a, f"{base or 'https://pqrs.ejemplo.test'}/verificar/ENLACE-DE-PRUEBA-NO-VALIDO")
        print(f"   enviado: verificación de prueba -> {a.a}")
        mid = correo.enviar(a.a, "PRUEBA: respuesta a su solicitud n.º 0",
                            "Este es un mensaje de PRUEBA del sistema de PQRS. Si lo ve en su bandeja principal, el envío funciona.")
        print(f"   enviado: respuesta de prueba -> {a.a}   Message-ID={mid}")
        if a.rebote:
            mid = correo.enviar(a.rebote, "PRUEBA de rebote", "Mensaje a una dirección que no existe, para comprobar los rebotes.")
            print(f"   enviado: prueba de rebote -> {a.rebote}   Message-ID={mid}")
    except correo.ErrorCorreo as e:
        print(f"   ERROR al enviar ({e}). Nada más se envió.")
        return 1

    print("""
4) Revise A MANO (el script no puede verlo)
   [ ] Los mensajes llegaron a la bandeja principal, no a spam.
   [ ] En Gmail: ⋮ > «Mostrar original»: SPF, DKIM y DMARC dicen PASS (si no, revise herramientas/verificar_dns.py y su DNS).
   [ ] El nombre del remitente se ve bien («Atención al ciudadano», etc.).
   [ ] Si usó --rebote: en pocos minutos llega un aviso de «no entregado» a la bandeja de la cuenta emisora (PQRS_SMTP_FROM).
   [ ] Su proveedor muestra los Message-ID de arriba en sus registros (útil para enlazar avisos de entrega con el webhook).
   [ ] Si configuró el webhook: llegó el aviso de entrega/rebote a /webhooks/correo (herramientas/enviar_evento_webhook.py lo simula).""")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
