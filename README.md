# Agente PQRS — clasificación automática de solicitudes

[![CI](https://github.com/santiagoaguirre66415/Agente-PQRS-clasificaci-n-autom-tica-de-solicitudes/actions/workflows/ci.yml/badge.svg)](https://github.com/santiagoaguirre66415/Agente-PQRS-clasificaci-n-autom-tica-de-solicitudes/actions/workflows/ci.yml)
![Licencia MIT](https://img.shields.io/badge/licencia-MIT-green)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)

Una empresa de servicios públicos recibe cientos de peticiones, quejas, reclamos y sugerencias (PQRS).
Alguien tiene que leer cada una, decidir de qué trata, cuán urgente es y a qué área enviarla.
Este proyecto automatiza ese primer filtro y deja a una persona la decisión final.

## Qué hace
- **Recibe** una solicitud (API REST o formulario web).
- **Clasifica**: tipo (petición, queja, reclamo, sugerencia), categoría (energía, agua, facturación, atención), **prioridad** y **área responsable**.
- **Resume** el caso y **propone un borrador de respuesta** para que el funcionario lo revise.
- **Guarda** todo en base de datos y ofrece un **panel** con filtros, estados y estadísticas.

## Sin costos por defecto
La clasificación funciona **con reglas locales**: no hace llamadas de red, no usa claves de API y no tiene costo.
Opcionalmente puedes activar una **API de IA gratuita** (Groq u OpenRouter con modelos `:free`) para refinar el resultado.
Está **apagada por defecto** y tiene salvaguardas contra costos y fallos (ver «IA gratuita opcional»). No se usa la API de Claude.

## Decisiones de diseño
- **El sistema propone, la persona decide:** la respuesta es un borrador y el estado lo cambia un funcionario.
- **Reglas primero:** siempre se calcula el resultado con reglas; la IA, si está activa, solo lo refina.
- **La IA nunca baja la prioridad** calculada por las reglas, así un texto manipulado no puede esconder una urgencia.
- **Salida validada:** la respuesta de la IA se valida contra catálogos fijos y se le quitan los enlaces; si no cumple, se descarta.
- **Tolerancia a fallos:** si el proveedor falla o rechaza, se usan las reglas y la solicitud se registra igual.

## Stack
Python · FastAPI · SQLAlchemy · SQLite o PostgreSQL · HTML/CSS/JS sin frameworks · pytest.
IA opcional: cualquier API gratuita compatible con OpenAI, vía `httpx` (sin SDK de ningún proveedor).

## Cómo ejecutarlo
```bash
pip install --require-hashes -r requirements.txt
python crear_usuario.py           # crea tu primer administrador (la clave se pide sin mostrarla)
python seed.py                    # datos de ejemplo (ficticios)
uvicorn app.main:app --reload     # abre http://localhost:8000 e inicia sesión
```
El panel y la lectura de solicitudes exigen iniciar sesión. Hay tres roles: `admin` (todo, incluida la gestión de usuarios y la auditoría),
`funcionario` (leer y cambiar estados) y `consulta` (solo leer). Cada consulta y cambio queda en el registro de auditoría.
La documentación interactiva de la API (`/docs`) está apagada por defecto; actívala con `PQRS_DOCS=1`.

Documentos: [SECURITY.md](SECURITY.md) (cómo reportar una vulnerabilidad) · [PRIVACIDAD.md](PRIVACIDAD.md) · [DESPLIEGUE.md](DESPLIEGUE.md) (HTTPS, secretos, proxy, respaldos, monitoreo) · [DESIGN.md](DESIGN.md).

Para PostgreSQL, define `DATABASE_URL=postgresql+psycopg://usuario:clave@host:5432/pqrs` (e instala `psycopg[binary]`).

## IA gratuita opcional
Solo si quieres que una IA refine la clasificación. **Necesitas tu propia clave gratuita**, que se crea en la cuenta de tu proveedor.

| Proveedor | Cómo | Notas |
|---|---|---|
| **Groq** | Crea una cuenta en `console.groq.com` y una clave de API. No añadas método de pago | `PQRS_IA_PROVIDER=groq` · elige un modelo gratuito vigente en la consola y ponlo en `PQRS_IA_MODEL` |
| **OpenRouter** | Cuenta en `openrouter.ai`, clave en `/keys` | `PQRS_IA_PROVIDER=openrouter` · el modelo **debe terminar en `:free`** o la IA se desactiva sola |
| Otro compatible con OpenAI | — | `PQRS_IA_PROVIDER=custom`, `PQRS_IA_URL=https://…/v1` |

```powershell
$env:PQRS_IA_PROVIDER = "groq"
$env:PQRS_IA_KEY      = "tu-clave-gratuita"
$env:PQRS_IA_MODEL    = "nombre-del-modelo-gratuito"
```
Salvaguardas: solo HTTPS; tope local de `PQRS_IA_MAX_MIN` (10) por minuto y `PQRS_IA_MAX_DIA` (40) por día, por debajo de los límites gratuitos habituales;
si el proveedor rechaza o falla, pausa 10 minutos; timeout de 10 s sin reintentos; se quitan correos y números largos antes de enviar;
nunca se envían el nombre ni el correo. Los límites gratuitos cambian: confírmalos en la consola de tu proveedor.
**Lee antes los términos de datos del proveedor** (los planes gratuitos pueden usar los textos para mejorar sus modelos); ver `PRIVACIDAD.md`.

## Página pública para ciudadanos
`/solicitar` es el formulario para la ciudadanía (sin sesión) y `/privacidad` el aviso de privacidad, **generado con los datos de su empresa**
(`PQRS_EMPRESA`, `PQRS_CONTACTO_DATOS`, `PQRS_RETENCION`, y `PQRS_PROVEEDOR_CORREO` si envía correos). **Si falta alguno, el aviso lo dice y el formulario se cierra**
(no se reciben solicitudes sin un aviso completo). Un abogado debe revisar el texto antes de publicarlo.
- **Autorización de datos:** el formulario exige marcarla y guarda cuándo se dio.
- **Anti-bots propio y gratuito, sin servicios de terceros:** el navegador resuelve un cálculo breve (prueba de trabajo, unos segundos) mientras la persona escribe;
  cada reto sirve una sola vez, caduca en 1 hora y se rechaza si se resolvió demasiado rápido; además hay un campo trampa oculto y límites por IP.
  No es un CAPTCHA infalible, pero encarece mucho el abuso masivo y el correo a direcciones ajenas. El personal con sesión no lo necesita.
- Compatible con lectores de pantalla, teclado y móvil; los mensajes de error explican qué hacer.

## Cómo se responde una PQRS
1. El funcionario abre la solicitud, lee lo que escribió la persona y **revisa o edita el borrador**.
2. Elige cómo responder:
   - **Enviar por correo:** la app envía el texto al correo de la persona (pide confirmación antes de enviar).
   - **Respondí por otro medio:** registra la respuesta cuando se contestó por teléfono, en oficina, etc.
3. Se guarda la **respuesta oficial** (el texto exacto, quién la dio, cuándo y por qué canal) y la solicitud pasa sola a **Resuelta**.
   No se puede marcar como resuelta sin responder, ni responder dos veces; si el correo falla, la solicitud sigue sin responder y se puede reintentar.
4. Cada solicitud tiene una **fecha límite** (por defecto 15 días hábiles, de lunes a viernes, sin descontar festivos; ajústala con `PQRS_PLAZO_DIAS` según el plazo legal que te aplique).

El envío por correo es opcional: se activa con las variables `PQRS_SMTP_*` (ver `.env.example`). Sin ellas, el botón aparece desactivado y se puede responder por otro medio.

**Verificación del correo.** Para no enviar datos de un caso a una dirección que escribió un desconocido, al registrar una solicitud con correo la app envía
un enlace de confirmación (sin datos del caso; requiere `PQRS_URL_PUBLICA` con HTTPS). La persona abre el enlace y pulsa «Confirmar mi correo».
**Solo se responde por correo a direcciones verificadas**; si no lo está, se responde por otro medio. El panel muestra «(verificado)» o «(sin verificar)».
«Enviado» significa que el servidor de correo aceptó el mensaje, no que llegó. Si tu proveedor de correo (o un traductor gratuito como n8n o Make) avisa de entregas y rebotes
al webhook `POST /webhooks/correo`, el panel muestra «Entrega confirmada» o «El correo rebotó» (ver `DESPLIEGUE.md`).

## Herramientas gratuitas (carpeta `herramientas/`)
| Herramienta | Para qué |
|---|---|
| `servidor_correo_local.py` | Servidor SMTP de pruebas con STARTTLS y autenticación reales: recibe los correos de la app y los guarda como `.eml`, sin enviar nada a internet. Requiere `pip install aiosmtpd cryptography` |
| `verificar_dns.py tuempresa.com [selector]` | Comprueba SPF, DKIM y DMARC con DNS público gratuito. Solo envía el nombre del dominio |
| `prueba_proveedor.py --a tu@correo.com [--confirmar]` | Prueba **tu proveedor real**: conexión y TLS sin enviar; con `--confirmar`, envía solo a tu dirección y da la lista de comprobación manual |
| `enviar_evento_webhook.py` | Envía avisos de entrega/rebote firmados (también repetidos o viejos) para probar el webhook y tu traductor |
| `retencion.py` | Conservación y supresión: anonimiza lo antiguo, atiende el derecho de supresión, limpia sesiones y auditoría. Simula por defecto |
| `simular_abuso.py` | Mide cuánto cuesta superar la prueba anti-bots y prueba los topes contra tu instancia **local** |
| `prueba_humo.py URL` | Recorrido de punta a punta por **HTTP real** (27 comprobaciones): páginas, cabeceras, rutas cerradas, formulario con anti-bots, sesión, respuesta |
| `respaldo.py` | Copia en caliente de SQLite con integridad, cifrado opcional, huella SHA-256, rotación, **restauración comprobada** y copia externa (`--copiar-a`) restaurada desde la copia |
| `staging.py` | **Ensayo general local y aislado**: base propia, correo de pruebas con TLS real, humo, verificación → respuesta, webhook, respaldo y restauración; borra todo al terminar |
| `cazar_intermitentes.py` | Ejecuta la suite N veces (con carga opcional) y guarda cada fallo con su traza, para explicar pruebas que fallan a veces |
| `preflight.py` | Revisa la configuración real antes de producción (HTTPS, secretos, usuarios de demostración, respaldos, dependencias) |
| `monitoreo.py` | Métricas y alertas (solicitudes vencidas, fuerza bruta, correos fallidos, respaldo viejo…); `GET /salud` para un monitor de disponibilidad |

La carpeta `deploy/` trae ayudas de operación que **simulan por defecto**: `programar_tareas_windows.ps1` (respaldo, monitoreo y retención programados), `restringir_permisos_windows.ps1` y `crontab.ejemplo` (Linux).

Antes de usarlo con datos reales, haga el recorrido de `DESPLIEGUE.md` (sección 6): `preflight.py`, `prueba_humo.py` y un ensayo con `staging.py`.

## API
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/solicitar` · `/privacidad` · `/api/reto` | Públicos: formulario, aviso de privacidad y reto anti-bots |
| POST | `/solicitudes` | Público, con límite de frecuencia y prueba anti-bots. Registra y clasifica; responde solo `id` y `estado` |
| POST | `/webhooks/correo` | Avisos de entrega o rebote del proveedor de correo, con firma HMAC. Apagado sin `PQRS_WEBHOOK_SECRETO` |
| POST | `/auth/login` · `/auth/logout` · GET `/auth/yo` | Sesión (token `Bearer`, caduca) |
| GET | `/solicitudes?prioridad=alta&estado=nuevo&limite=50` | Cualquier rol. Lista con filtros y paginación |
| GET | `/solicitudes/{id}` | Cualquier rol. Detalle |
| PATCH | `/solicitudes/{id}` | `funcionario` o `admin`. Cambia el estado (no permite «resuelto» sin respuesta) |
| POST | `/solicitudes/{id}/responder` | `funcionario` o `admin`. Guarda la respuesta oficial y, con `canal: "correo"`, la envía |
| GET | `/config` | Cualquier rol. Indica si el correo está configurado; no expone secretos |
| GET | `/estadisticas` | Cualquier rol |
| GET/POST/PATCH | `/usuarios` | Solo `admin` |
| GET | `/auditoria` | Solo `admin` |

Las rutas protegidas exigen la cabecera `Authorization: Bearer <token>`.

## Pruebas
```bash
python -m pytest -q
```
271 pruebas: clasificador, validaciones, roles, sesiones, auditoría, límites, cabeceras, respuestas, anti-bots, webhook (incluidas entradas hostiles), verificador DNS, retención y la IA opcional.
La IA y el correo se prueban con servidores simulados (sin red, sin cuota). Además, 3 pruebas usan un **servidor SMTP local real** (STARTTLS, autenticación y certificado de verdad,
solo en 127.0.0.1) y se omiten si no tienes `aiosmtpd` y `cryptography`.

## Mejoras futuras
- Lectura automática del buzón de correo y envío de la respuesta aprobada.
- Automatización con n8n para notificar al área responsable.
- Evaluación del clasificador contra un conjunto de solicitudes etiquetadas.

> Todos los datos de ejemplo son ficticios.
