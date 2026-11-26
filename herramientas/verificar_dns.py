"""Comprueba SPF, DKIM y DMARC de un dominio con DNS sobre HTTPS público y gratuito (Google Public DNS).

Uso:   python herramientas/verificar_dns.py tuempresa.com [selector-dkim]
Salida: [OK] / [AVISO] / [FALTA] por cada registro. Código de salida 1 si falta algo imprescindible.
Privacidad: solo se envía el NOMBRE DEL DOMINIO a dns.google. No se envía ningún dato de la aplicación ni de las solicitudes.
"""
import re
import sys

import httpx

DOH = "https://dns.google/resolve"
_DOMINIO = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SELECTOR = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,60}[a-z0-9])?$")
OK, AVISO, FALTA = "OK", "AVISO", "FALTA"


def consultar_txt(nombre: str, cliente: httpx.Client | None = None) -> list[str]:
    cliente = cliente or httpx.Client(timeout=10)
    r = cliente.get(DOH, params={"name": nombre, "type": "TXT"}, headers={"accept": "application/dns-json"})
    r.raise_for_status()
    textos = []
    for resp in r.json().get("Answer") or []:
        if resp.get("type") == 16:  # TXT; una cadena larga llega partida en trozos entre comillas
            trozos = re.findall(r'"((?:[^"\\]|\\.)*)"', resp.get("data", ""))
            textos.append("".join(trozos) if trozos else resp.get("data", ""))
    return textos


def evaluar_spf(registros: list[str]) -> list[tuple[str, str]]:
    spf = [r for r in registros if r.lower().startswith("v=spf1")]
    if not spf:
        return [(FALTA, "SPF: no hay registro TXT que empiece con v=spf1. Sin él, cualquiera puede suplantar su dominio.")]
    if len(spf) > 1:
        return [(FALTA, "SPF: hay más de un registro v=spf1; es inválido y los receptores lo ignoran. Deje solo uno.")]
    r = spf[0].lower()
    if "redirect=" in r:
        return [(OK, "SPF: presente; delega la política con redirect= a otro dominio (revise también ese dominio).")]
    if r.endswith("-all"):
        return [(OK, "SPF: presente y estricto (-all).")]
    if r.endswith("~all"):
        return [(OK, "SPF: presente con fallo suave (~all). Es lo habitual al empezar; endurezca a -all cuando todo funcione.")]
    if r.endswith("?all"):
        return [(AVISO, "SPF: termina en ?all (neutral): no protege. Use ~all o -all.")]
    if r.endswith("+all"):
        return [(FALTA, "SPF: termina en +all: autoriza a CUALQUIER servidor. Es peor que no tenerlo.")]
    return [(AVISO, "SPF: no termina en una regla all (-all o ~all); el resultado es impreciso.")]


def evaluar_dmarc(registros: list[str]) -> list[tuple[str, str]]:
    dmarc = [r for r in registros if r.lower().startswith("v=dmarc1")]
    if not dmarc:
        return [(FALTA, "DMARC: no hay registro en _dmarc. Cree uno, empezando con p=none para observar.")]
    etiquetas = {k.strip().lower(): v.strip() for k, _, v in (p.partition("=") for p in dmarc[0].split(";")) if k.strip()}
    res = []
    politica = etiquetas.get("p", "").lower()
    if politica in ("quarantine", "reject"):
        res.append((OK, f"DMARC: presente con política {politica}."))
    elif politica == "none":
        res.append((AVISO, "DMARC: política none (solo observación). Revise los reportes y pase a quarantine y luego a reject."))
    else:
        res.append((FALTA, "DMARC: falta la política p= o no es válida (none, quarantine o reject)."))
    if "rua" not in etiquetas:
        res.append((AVISO, "DMARC: sin rua=mailto:...; no recibirá reportes para saber quién envía en nombre de su dominio."))
    return res


def evaluar_dkim(registros: list[str] | None, selector: str | None) -> list[tuple[str, str]]:
    if not selector:
        return [(AVISO, "DKIM: no se indicó el selector, así que no se puede comprobar. Búsquelo en su proveedor de correo "
                        "(p. ej. google, selector1, default) y vuelva a ejecutar con él.")]
    dkim = [r for r in (registros or []) if "v=dkim1" in r.lower() or re.search(r"\bp=", r)]
    if not dkim:
        return [(FALTA, f"DKIM: no hay registro en {selector}._domainkey. Los mensajes no llevarán firma verificable.")]
    if re.search(r"\bp=\s*(;|$)", dkim[0]):
        return [(FALTA, f"DKIM: el selector {selector} tiene la clave pública vacía (p=): está revocado.")]
    return [(OK, f"DKIM: clave publicada para el selector {selector}.")]


def verificar(dominio: str, selector: str | None = None, cliente: httpx.Client | None = None) -> list[tuple[str, str]]:
    dominio = dominio.strip().lower().rstrip(".")
    if not _DOMINIO.match(dominio):
        raise ValueError("Escriba solo el dominio, por ejemplo: tuempresa.com")
    if selector and not _SELECTOR.match(selector.lower()):
        raise ValueError("El selector DKIM solo puede tener letras, números, punto, guion y guion bajo")
    resultado = evaluar_spf(consultar_txt(dominio, cliente))
    resultado += evaluar_dkim(consultar_txt(f"{selector.lower()}._domainkey.{dominio}", cliente) if selector else None, selector)
    resultado += evaluar_dmarc(consultar_txt(f"_dmarc.{dominio}", cliente))
    return resultado


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 2
    try:
        resultado = verificar(argv[1], argv[2] if len(argv) == 3 else None)
    except ValueError as e:
        print(e)
        return 2
    except httpx.HTTPError as e:
        print(f"No se pudo consultar el DNS público ({type(e).__name__}). Revise su conexión.")
        return 2
    for nivel, texto in resultado:
        print(f"[{nivel}] {texto}")
    return 1 if any(n == FALTA for n, _ in resultado) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
