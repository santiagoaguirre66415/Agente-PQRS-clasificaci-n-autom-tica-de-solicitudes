"""Aviso de privacidad generado con los datos reales de la empresa (variables de entorno), no con texto fijo.

Si falta algún dato obligatorio, la página lo dice con un aviso visible y el formulario público NO acepta solicitudes
(una casilla de «autorizo» sobre un aviso incompleto no vale como autorización). PQRS_PERMITIR_AVISO_INCOMPLETO=1 lo permite solo para pruebas.
No es asesoría jurídica: debe revisarlo un abogado (Ley 1581 de 2012 y Decreto 1377 de 2013).
"""
import html
import os

from fastapi import HTTPException

from . import agente, correo

OBLIGATORIOS = {
    "PQRS_EMPRESA": "razón social y NIT de la empresa",
    "PQRS_CONTACTO_DATOS": "correo de contacto para temas de datos",
    "PQRS_RETENCION": "plazo de conservación de los datos",
}


def _valor(nombre: str) -> str:
    return os.environ.get(nombre, "").strip()


def faltantes() -> list[str]:
    falta = [nombre for nombre in OBLIGATORIOS if not _valor(nombre)]
    if correo.configurado() and not _valor("PQRS_PROVEEDOR_CORREO"):
        falta.append("PQRS_PROVEEDOR_CORREO")  # si se envían correos, hay que nombrar al proveedor
    return falta


def completo() -> bool:
    return not faltantes()


def exigir_completo() -> None:
    """Puerta del formulario público: sin aviso completo, no se reciben solicitudes."""
    if not completo() and os.environ.get("PQRS_PERMITIR_AVISO_INCOMPLETO") != "1":
        raise HTTPException(503, "Este formulario todavía no está disponible. Estamos terminando de configurar nuestro aviso de privacidad.")


def _dato(nombre: str, descripcion: str) -> str:
    v = _valor(nombre)
    return html.escape(v) if v else f'<mark title="Falta configurar {nombre}">[{html.escape(descripcion)}]</mark>'


def pagina() -> str:
    falta = faltantes()
    aviso = ""
    if falta:
        aviso = ('<p class="nota"><b>Aviso incompleto.</b> Faltan datos de la empresa: ' + html.escape(", ".join(falta)) +
                 '. Hasta completarlos, el formulario público no recibe solicitudes. Revise el texto con un abogado antes de publicarlo.</p>')
    ia = ""
    if agente.ia_activa():
        ia = ("<li>Con su autorización, el texto de su solicitud (sin correo ni números largos) se procesa en un servicio de inteligencia artificial "
              "ubicado fuera de Colombia para clasificarla.</li>")
    mail = ""
    if correo.configurado():
        mail = f"<li>El proveedor de correo de la empresa ({_dato('PQRS_PROVEEDOR_CORREO', 'proveedor de correo')}) transporta nuestros mensajes.</li>"
    direccion = f", con domicilio en {_dato('PQRS_DIRECCION', 'dirección')}" if _valor("PQRS_DIRECCION") else ""
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aviso de privacidad</title>
<style>
  html{{font-size:clamp(16px,.5vw + 8px,21px)}}
  body{{margin:0;font:1rem/1.7 system-ui,Segoe UI,sans-serif;color:#13262B;background:#fff}}
  main{{max-width:40rem;margin:0 auto;padding:32px 20px 64px}} h1{{font-size:1.75rem;line-height:1.2}} h2{{font-size:1.25rem;margin-top:28px}}
  p,li{{color:#2E4046}} a{{color:#0F5E63;font-weight:600}} mark{{background:#FFF1B8;padding:0 4px;border-radius:3px}}
  .nota{{background:#FDECEA;border-left:4px solid #C8381F;padding:10px 14px;border-radius:4px;font-size:.9375rem;color:#7a1712}}
</style></head><body><main>
<h1>Aviso de privacidad</h1>{aviso}
<h2>Quién trata sus datos</h2>
<p>{_dato('PQRS_EMPRESA', 'razón social y NIT')}{direccion}. Contacto para temas de datos: {_dato('PQRS_CONTACTO_DATOS', 'correo de contacto')}.</p>
<h2>Qué datos pedimos y para qué</h2>
<ul>
<li><b>Su nombre y el texto de su solicitud:</b> para atenderla y responderle.</li>
<li><b>Su correo (opcional):</b> para confirmar que es suyo y enviarle la respuesta. Solo le escribiremos si usted lo confirma.</li>
<li><b>Su dirección IP:</b> por seguridad, para limitar abusos del formulario.</li>
<li><b>La fecha y hora en que usted autorizó este tratamiento.</b></li>
</ul>
<p>No pedimos datos sensibles. Le pedimos que no los escriba en su solicitud.</p>
<h2>Con quién los compartimos</h2>
<p>No los vendemos ni los usamos para publicidad.</p>
<ul>{mail}{ia}</ul>
<h2>Cuánto tiempo los conservamos</h2>
<p>{_dato('PQRS_RETENCION', 'plazo de conservación')}. Después los anonimizamos: conservamos solo datos estadísticos sin su nombre, correo ni texto.</p>
<h2>Sus derechos</h2>
<p>Puede conocer, actualizar, rectificar y pedir la supresión de sus datos, y revocar esta autorización, escribiendo a {_dato('PQRS_CONTACTO_DATOS', 'correo de contacto')}.</p>
<p><a href="/solicitar">Volver al formulario</a></p>
</main></body></html>"""
