"""Prueba de humo por HTTP REAL contra una instancia en ejecución (local, de pruebas o de producción).

    python herramientas/prueba_humo.py https://pqrs.tuempresa.com [--sin-escritura]

Recorre lo esencial de punta a punta con peticiones de verdad (no simuladas): páginas públicas, cabeceras de seguridad, documentación cerrada,
rutas protegidas sin sesión, formulario con prueba anti-bots, inicio de sesión, consulta y respuesta de una solicitud, y webhook cerrado.
Imprime [OK]/[FALLA] por cada comprobación y termina con código 1 si algo falla.

Variables (solo para las comprobaciones con sesión): PQRS_HUMO_USUARIO y PQRS_HUMO_CLAVE: una cuenta de PRUEBA con rol funcionario o admin.
Use una cuenta dedicada y bórrela o desactívela después. Nunca ponga la clave como argumento (quedaría en el historial).

CREA UNA SOLICITUD DE PRUEBA llamada «PRUEBA DE HUMO (ignorar)» y la responde con el canal «otro». Para no escribir nada use --sin-escritura.
En producción, prefiera correrla contra un entorno de pruebas; si la corre contra producción, anonimice esa solicitud después
(python herramientas/retencion.py suprimir --id N --aplicar).
"""
import argparse
import hashlib
import os
import sys
import time

import httpx


def _ceros(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        return n + 8 - b.bit_length()
    return n


def resolver(reto: str, bits: int) -> str:
    n = 0
    while _ceros(hashlib.sha256(f"{reto}:{n}".encode()).digest()) < bits:
        n += 1
    return str(n)


class Informe:
    def __init__(self):
        self.filas: list[tuple[bool, str]] = []

    def check(self, ok: bool, texto: str, detalle: str = "") -> bool:
        self.filas.append((bool(ok), texto + (f"  ({detalle})" if detalle and not ok else "")))
        return bool(ok)

    @property
    def fallas(self) -> int:
        return sum(1 for ok, _ in self.filas if not ok)


def ejecutar(c: httpx.Client, escribir: bool = True, usuario: str = "", clave: str = "", espera_reto: float = 2.2, https: bool | None = None) -> Informe:
    """`c` es un httpx.Client ya apuntado a la instancia (en pruebas puede ser el TestClient de la app)."""
    i = Informe()
    base = str(c.base_url)
    https = base.startswith("https://") if https is None else https

    # --- páginas públicas y cabeceras ---
    r = c.get("/solicitar")
    i.check(r.status_code == 200, "GET /solicitar responde 200", f"HTTP {r.status_code}")
    h = r.headers
    i.check("script-src 'self'" in h.get("content-security-policy", ""), "Política de contenido (CSP) presente y sin scripts en línea")
    i.check(h.get("x-frame-options", "").upper() == "DENY", "Anti-clickjacking (X-Frame-Options: DENY)")
    i.check(h.get("x-content-type-options", "").lower() == "nosniff", "X-Content-Type-Options: nosniff")
    if https:
        i.check("max-age" in h.get("strict-transport-security", ""), "HSTS activo (Strict-Transport-Security)", "defina PQRS_HTTPS=1 detrás de HTTPS")
    p = c.get("/privacidad")
    i.check(p.status_code == 200 and "Aviso incompleto" not in p.text, "Aviso de privacidad completo (sin «Aviso incompleto»)",
            "complete PQRS_EMPRESA, PQRS_CONTACTO_DATOS, PQRS_RETENCION")

    # --- superficie que debe estar CERRADA ---
    for ruta in ("/docs", "/openapi.json"):
        i.check(c.get(ruta).status_code == 404, f"{ruta} está cerrada")
    for ruta in ("/solicitudes", "/solicitudes/1", "/estadisticas", "/usuarios", "/auditoria", "/config"):
        i.check(c.get(ruta).status_code == 401, f"GET {ruta} exige sesión", f"HTTP {c.get(ruta).status_code}")
    i.check(c.patch("/solicitudes/1", json={"estado": "en_proceso"}).status_code == 401, "PATCH /solicitudes/1 exige sesión")
    w = c.post("/webhooks/correo", content=b"{}", headers={"X-Firma": "sha256=00"})
    i.check(w.status_code in (401, 404), "Webhook rechaza peticiones sin firma válida", f"HTTP {w.status_code}")
    i.check(c.post("/solicitudes", json={"ciudadano": "Bot", "texto": "Intento sin prueba anti-bots ni autorización", "acepto": True}).status_code in (400, 422, 503),
            "El formulario rechaza envíos sin prueba anti-bots")

    # --- sin escritura: aquí termina ---
    if not escribir:
        return i

    # --- formulario público real (prueba de trabajo incluida) ---
    reto = c.get("/api/reto")
    if not i.check(reto.status_code == 200, "GET /api/reto entrega un reto", f"HTTP {reto.status_code}: el formulario puede estar cerrado por aviso incompleto"):
        return i
    datos = reto.json()
    time.sleep(espera_reto)  # respeta la trampa de tiempo mínima
    # El texto lleva un sufijo único: la app rechaza duplicados exactos en 24 h, y esta prueba debe poder repetirse el mismo día
    unico = f"{int(time.time())}-{os.urandom(2).hex()}"
    sol = c.post("/solicitudes", json={"ciudadano": "PRUEBA DE HUMO (ignorar)", "texto": f"Esta es una solicitud de prueba automática, ignórela por favor ({unico})",
                                       "acepto": True, "reto": datos["reto"], "prueba": resolver(datos["reto"], datos["bits"])})
    if not i.check(sol.status_code == 201 and sol.json().get("id"), "El formulario público registra una solicitud", f"HTTP {sol.status_code} {sol.text[:80]}"):
        return i
    sid = sol.json()["id"]
    i.check(set(sol.json()) == {"id", "estado", "mensaje"}, "La respuesta pública no expone datos internos")
    reuso = c.post("/solicitudes", json={"ciudadano": "PRUEBA DE HUMO (ignorar)", "texto": "Segundo intento con el mismo reto, debe rechazarse", "acepto": True,
                                         "reto": datos["reto"], "prueba": resolver(datos["reto"], datos["bits"])})
    i.check(reuso.status_code == 400, "Un reto anti-bots no se puede reutilizar", f"HTTP {reuso.status_code}")

    # --- con sesión ---
    if not (usuario and clave):
        i.check(False, "Comprobaciones con sesión", "defina PQRS_HUMO_USUARIO y PQRS_HUMO_CLAVE (cuenta de prueba)")
        return i
    lg = c.post("/auth/login", json={"usuario": usuario, "clave": clave})
    if not i.check(lg.status_code == 200, "Inicio de sesión con la cuenta de prueba", f"HTTP {lg.status_code}"):
        return i
    auth = {"Authorization": "Bearer " + lg.json()["token"]}
    d = c.get(f"/solicitudes/{sid}", headers=auth)
    i.check(d.status_code == 200 and d.json()["ciudadano"].startswith("PRUEBA DE HUMO"), "El personal ve la solicitud recién creada")
    i.check(d.json().get("autorizacion_en") is not None, "Quedó constancia de la autorización de datos")
    mal = c.patch(f"/solicitudes/{sid}", headers=auth, json={"estado": "resuelto"})
    i.check(mal.status_code == 409, "No se puede resolver sin responder", f"HTTP {mal.status_code}")
    resp = c.post(f"/solicitudes/{sid}/responder", headers=auth, json={"respuesta": "Respuesta de prueba automática, ignórela por favor.", "canal": "otro"})
    i.check(resp.status_code == 200 and resp.json()["estado"] == "resuelto", "Responder deja la solicitud como resuelta", f"HTTP {resp.status_code}")
    i.check(c.post(f"/solicitudes/{sid}/responder", headers=auth, json={"respuesta": "Segunda respuesta de prueba", "canal": "otro"}).status_code == 409,
            "No se puede responder dos veces")
    out = c.post("/auth/logout", headers=auth)
    i.check(out.status_code == 200 and c.get("/estadisticas", headers=auth).status_code == 401, "Cerrar sesión invalida el token")
    return i


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--sin-escritura", action="store_true", help="solo lecturas: no crea ninguna solicitud")
    a = ap.parse_args(argv[1:])
    if not a.url.startswith(("http://", "https://")):
        print("La URL debe empezar con http:// o https://")
        return 2
    try:
        with httpx.Client(base_url=a.url.rstrip("/"), timeout=30, follow_redirects=False) as c:
            informe = ejecutar(c, not a.sin_escritura, os.environ.get("PQRS_HUMO_USUARIO", ""), os.environ.get("PQRS_HUMO_CLAVE", ""))
    except httpx.HTTPError as e:
        print(f"No se pudo conectar con {a.url} ({type(e).__name__}).")
        return 2
    for ok, texto in informe.filas:
        print(f"[{'OK' if ok else 'FALLA'}] {texto}")
    print(f"\n{len(informe.filas) - informe.fallas} de {len(informe.filas)} comprobaciones correctas.")
    return 1 if informe.fallas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
