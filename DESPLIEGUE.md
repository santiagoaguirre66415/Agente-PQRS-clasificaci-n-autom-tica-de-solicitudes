# Despliegue seguro

## 1. Variables de entorno
| Variable | Para qué | Valor recomendado |
|---|---|---|
| `DATABASE_URL` | Base de datos | PostgreSQL con usuario de mínimo privilegio |
| `PQRS_ADMIN_USER`, `PQRS_ADMIN_PASSWORD` | Crear el primer admin **solo la primera vez** (si ya hay usuarios se ignoran). Quítalas después | Clave larga y única; cámbiala tras el primer ingreso |
| `PQRS_IA_PROVIDER`, `PQRS_IA_KEY`, `PQRS_IA_MODEL` | IA gratuita **opcional** (Groq, OpenRouter `:free`...). Sin ellas no hay llamadas de red | **Sin definir** hasta revisar los términos de datos del proveedor y tener autorización de privacidad. La clave va en un gestor de secretos |
| `PQRS_SMTP_HOST`, `PQRS_SMTP_PORT`, `PQRS_SMTP_USER`, `PQRS_SMTP_FROM` | Envío de respuestas y de verificaciones por correo (opcional). Solo STARTTLS (puerto 587) | Una cuenta **dedicada** de la empresa. Configura SPF, DKIM y DMARC del dominio (sección 4) |
| `PQRS_SMTP_PASSWORD_FILE` (preferida) o `PQRS_SMTP_PASSWORD` | Contraseña SMTP | **Archivo** con solo la contraseña y permisos de lectura solo para el usuario del servicio (o Docker/Kubernetes secrets): no queda en el entorno del proceso. Rótala periódicamente |
| `PQRS_URL_PUBLICA` | URL pública con HTTPS, usada para armar el enlace de verificación del correo | `https://pqrs.tuempresa.com`. Sin ella no se envían verificaciones y solo se podrá responder por otro medio |
| `PQRS_VERIF_MAX_DIA` | Tope diario de correos de verificación | `200` (ajústalo a tu volumen) |
| `PQRS_MAIL_LIMIT`, `PQRS_PLAZO_DIAS` | Correos por minuto por funcionario / días hábiles de plazo | `10` / el plazo legal que aplique (confírmalo) |
| `PQRS_IA_MAX_MIN`, `PQRS_IA_MAX_DIA` | Topes locales de llamadas a la IA | `10` / `40` (por debajo de los límites gratuitos) |
| `PQRS_TRUST_PROXY` | `1` si hay **exactamente un** proxy de confianza delante | `1` con Caddy/nginx |
| `PQRS_HTTPS` | `1` activa HSTS | `1` cuando el dominio ya sirva HTTPS |
| `PQRS_RATE_LIMIT`, `PQRS_LOGIN_LIMIT` | Envíos por minuto / fallos de login por 5 min | `10` / `5` |
| `PQRS_SESION_HORAS` | Duración de la sesión | `8` o menos |
| `PQRS_SCRYPT_N` | Costo de scrypt | no definir (usa 2^17) |

**Gestión de secretos:** no escribas claves en el código, en `.env` versionados ni en la línea de comandos compartida.
Usa el gestor de secretos de tu plataforma (variables cifradas del servicio, Docker/Kubernetes secrets, Vault).
Más usuarios: `python crear_usuario.py` (pide la clave sin mostrarla) o `POST /usuarios` como admin.

## 2. HTTPS con Caddy (certificados automáticos)
```
pqrs.tuempresa.com {
    reverse_proxy 127.0.0.1:8000
}
```
Arranca la app solo en la interfaz local para que nadie la alcance sin pasar por el proxy:
```bash
PQRS_TRUST_PROXY=1 PQRS_HTTPS=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Con un solo proxy delante, Caddy añade al final de `X-Forwarded-For` la IP real del cliente, que es la que la app usa para sus límites.
Si hay más de un proxy, **no** actives `PQRS_TRUST_PROXY` sin adaptar `ip_cliente()`.

## 3. Comprobaciones tras publicar
- [ ] `curl -I https://pqrs.tuempresa.com` muestra `strict-transport-security`, `content-security-policy` y `x-frame-options`.
- [ ] `http://` redirige a `https://`.
- [ ] `/docs` y `/openapi.json` devuelven 404.
- [ ] `GET /solicitudes` sin sesión devuelve 401.
- [ ] Inicia sesión con un usuario `consulta` y confirma que no puede cambiar estados.
- [ ] La tabla `auditoria` registra tus pruebas.
- [ ] El archivo de base de datos y las copias de seguridad no son accesibles desde internet.
- [ ] `pip install --require-hashes -r requirements.txt` y `python -m pip_audit -r requirements.txt` en el servidor.

## 4. Correo: SPF, DKIM, DMARC y rebotes
Sin estos registros en el DNS de tu dominio, los correos pueden caer en spam o ser suplantados. **Esto se configura en el panel de tu proveedor de DNS y de correo, no en la app.**

| Registro | Para qué sirve | Cómo conseguirlo |
|---|---|---|
| **SPF** (TXT en el dominio) | Dice qué servidores pueden enviar correo en nombre del dominio | Tu proveedor de correo te da el valor (por ejemplo `v=spf1 include:_spf.google.com ~all` en Google Workspace) |
| **DKIM** (TXT en `selector._domainkey`) | Firma digital de cada mensaje | Se genera en el panel de administración de tu proveedor de correo y se publica en el DNS |
| **DMARC** (TXT en `_dmarc`) | Qué hacer con los mensajes que fallan SPF/DKIM y a dónde reportar | Empieza en modo observación: `v=DMARC1; p=none; rua=mailto:reportes@tuempresa.com` y endurece a `quarantine` y luego `reject` cuando los reportes salgan limpios |

Comprueba que quedaron publicados (PowerShell; cambia el dominio y el selector):
```powershell
Resolve-DnsName tuempresa.com -Type TXT | Where-Object Strings -match "v=spf1"
Resolve-DnsName selector._domainkey.tuempresa.com -Type TXT
Resolve-DnsName _dmarc.tuempresa.com -Type TXT
```
Después envía un correo de prueba a una cuenta tuya y revisa en «Mostrar original» (Gmail) que SPF, DKIM y DMARC digan **PASS**.

Para comprobarlo sin escribir comandos DNS a mano, la app incluye una herramienta gratuita (solo envía el nombre del dominio a Google Public DNS):
```powershell
python herramientas/verificar_dns.py tuempresa.com selector
```
Muestra `[OK]`, `[AVISO]` o `[FALTA]` por cada registro y termina con código 1 si falta algo imprescindible.

**Rebotes y entrega.** «Enviado» en el panel significa que el servidor de correo aceptó el mensaje. Si la dirección no existe o la bandeja rechaza el mensaje,
el aviso llega a la bandeja de la cuenta emisora (`PQRS_SMTP_FROM`). Revísala a diario.
Para que el panel lo muestre solo, defina `PQRS_WEBHOOK_SECRETO` y haga que su proveedor de correo envíe los avisos a `POST https://pqrs.tuempresa.com/webhooks/correo`.
Cada proveedor usa su propio formato, así que normalmente hace falta un **traductor** (por ejemplo un flujo gratuito de n8n o Make) que reciba el aviso del proveedor
y llame a este endpoint con el contrato de la app. **No verifiqué el formato de ningún proveedor concreto ni los límites de sus planes gratuitos: confírmelos antes.**

Contrato: cuerpo JSON `{"mensaje_id": "<Message-ID del correo>", "evento": "entregado" | "rebotado", "fecha": <segundos desde 1970>}` y cabecera
`X-Firma: sha256=<HMAC-SHA256 del cuerpo con el secreto, en hexadecimal>`. Eventos con más de 5 minutos se rechazan. Ejemplo (PowerShell):
```powershell
$secreto = "el-mismo-que-PQRS_WEBHOOK_SECRETO"
$cuerpo  = '{"mensaje_id":"<id@ejemplo.test>","evento":"rebotado","fecha":' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + '}'
$hmac    = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (,[Text.Encoding]::UTF8.GetBytes($secreto))
$firma   = "sha256=" + (($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($cuerpo)) | ForEach-Object { $_.ToString("x2") }) -join "")
Invoke-RestMethod -Method Post -Uri https://pqrs.tuempresa.com/webhooks/correo -Body $cuerpo -ContentType "application/json" -Headers @{ "X-Firma" = $firma }
```

## 4b. Probar el correo sin enviar nada a internet
```powershell
pip install aiosmtpd cryptography
python herramientas/servidor_correo_local.py        # terminal 1: servidor SMTP de pruebas con TLS real
```
En otra terminal, arranque la app con las variables que imprime ese script (`PQRS_SMTP_HOST=localhost`, `PQRS_SMTP_PORT=2525`, `PQRS_SMTP_CA_FILE=herramientas/.local/cert.pem`, etc.).
Los correos aparecen como `.eml` en `herramientas/.local/buzon/`. Úselo solo para pruebas.

## 4c. Formulario público
Publique `https://pqrs.tuempresa.com/solicitar` y enlácelo desde su web. Antes defina `PQRS_EMPRESA`, `PQRS_CONTACTO_DATOS`, `PQRS_RETENCION` y, si envía correos, `PQRS_PROVEEDOR_CORREO`:
con ellos se genera `/privacidad`, y **sin ellos el formulario se cierra** (503) para no recibir solicitudes con una autorización sobre un aviso incompleto. Haga que un abogado revise el texto resultante.
`PQRS_PUBLICO_MAX_HORA` (300) es un tope total por hora contra el abuso distribuido; ajústelo a su volumen real. Si ejecuta **más de un proceso**, defina la misma `PQRS_SECRETO` en todos, o los retos emitidos por uno serán rechazados por otro.

## 4d. Conservación y supresión (programar)
La política de `PRIVACIDAD.md` solo se cumple si alguien ejecuta estas tareas. `retencion.py` **simula** salvo que se agregue `--aplicar`: pruebe primero sin él.
```powershell
python herramientas/retencion.py antiguas  --dias 730 --aplicar     # anonimiza solicitudes resueltas hace más de 2 años
python herramientas/retencion.py sesiones  --dias 30  --aplicar
python herramientas/retencion.py auditoria --dias 365 --aplicar
python herramientas/retencion.py suprimir  --correo persona@mail.com --aplicar    # solicitud de supresión de un titular
```
Programe las tres primeras (Programador de tareas de Windows o `cron`, por ejemplo cada semana) con los plazos que defina su política, y registre quién revisa que se ejecuten.
**Haga una copia de seguridad antes de aplicar por primera vez**: la anonimización es irreversible. Las copias de seguridad antiguas conservan los datos originales: aplique la misma retención a ellas.

## 5. Copias de seguridad (herramienta incluida, gratuita)
```powershell
python herramientas/respaldo.py generar-clave --archivo D:\claves\respaldo.key          # una sola vez; guárdela APARTE de los respaldos
python herramientas/respaldo.py crear --destino D:\respaldos --clave-archivo D:\claves\respaldo.key --conservar 14
python herramientas/respaldo.py verificar D:\respaldos\pqrs-20261126-030000.zip.enc --clave-archivo D:\claves\respaldo.key
python herramientas/respaldo.py restaurar D:\respaldos\pqrs-....zip.enc --a pqrs.db --clave-archivo D:\claves\respaldo.key --forzar   # con la app APAGADA
```
- **Copia externa (recomendado):** `crear ... --copiar-a E:\externo` copia el respaldo a **otro disco, otro equipo o una carpeta sincronizada**, comprueba que la copia es idéntica (SHA-256)
  y **restaura DESDE LA COPIA** para demostrar que sirve. Si la copia queda en el mismo disco que el original, la herramienta lo avisa: no protege contra una falla del disco ni un robo del equipo.
- **«crear» copia en caliente** (segura con la app encendida), comprueba la integridad, comprime, cifra (si da la clave) y guarda la huella SHA-256. Se niega a guardar una copia dañada.
- **«verificar» es la prueba de que sirve:** restaura en una carpeta temporal y cuenta las filas. **Hágalo con regularidad**: una copia que nunca se restauró no es una copia.
- **Sin clave, el respaldo queda SIN CIFRAR** y la herramienta lo avisa: contiene datos personales.
- Solo para SQLite. Con PostgreSQL use `pg_dump` y cifre el resultado.
- Defina `PQRS_RESPALDO_DIR` (la misma carpeta) para que `preflight.py` y `monitoreo.py` comprueben que hay copias recientes.
- **Los respaldos antiguos conservan datos que la retención ya anonimizó**: aplique a los respaldos la misma política de conservación (la herramienta rota y borra los más viejos con `--conservar`).

## 6. Comprobar antes de abrir al público, y vigilar después
```powershell
python herramientas/preflight.py                                  # configuración: HTTPS, secretos, usuarios de demostración, respaldos, dependencias
python herramientas/prueba_humo.py https://pqrs.tuempresa.com     # recorrido de punta a punta por HTTP real (usa una cuenta de PRUEBA)
python herramientas/monitoreo.py --correo-a admin@tuempresa.com   # métricas y alertas; código 1 si hay alertas
```
- **`preflight.py`**: ejecútelo EN EL SERVIDOR con las mismas variables con que arrancará la app. Marca como FALTA lo imprescindible (por ejemplo `PQRS_HTTPS`, un usuario `admin.demo`,
  `PQRS_PERMITIR_AVISO_INCOMPLETO`, un secreto de webhook corto, respaldos viejos). En Windows no puede verificar permisos de archivo: use `icacls` y compruebe que solo la cuenta del servicio lea la base y los secretos.
- **`prueba_humo.py`**: usa `PQRS_HUMO_USUARIO` y `PQRS_HUMO_CLAVE` (cuenta dedicada; desactívela después) y crea una solicitud «PRUEBA DE HUMO (ignorar)»; con `--sin-escritura` solo lee.
  Ya pasó 27 de 27 comprobaciones contra el servidor local. Pásela contra el entorno de pruebas antes de producción.
- **`monitoreo.py`**: alerta por solicitudes **vencidas**, intentos de acceso fallidos o bloqueos, envíos de correo fallidos, rebotes, solicitudes sin atender, respaldo ausente o viejo y retención sin ejecutar.
  Solo cuenta: no muestra datos personales. `GET /salud` (público, sin detalles) sirve para un monitor de disponibilidad gratuito (UptimeRobot, Healthchecks.io…).

**Programarlo** (usted lo ejecuta; la app no crea tareas por su cuenta). La carpeta `deploy/` trae ayudas que **solo simulan** hasta que se les pasa `-Aplicar`:
```powershell
# Windows: respaldo diario 03:00, monitoreo cada 15 min, retención semanal
.\deploy\programar_tareas_windows.ps1 -Proyecto C:\pqrs -DirRespaldos D:\respaldos -ClaveRespaldo D:\claves\respaldo.key -CopiaExterna E:\externo -CorreoAlertas admin@tuempresa.com
.\deploy\programar_tareas_windows.ps1 -Proyecto C:\pqrs -DirRespaldos D:\respaldos -ClaveRespaldo D:\claves\respaldo.key -CopiaExterna E:\externo -Aplicar
```
En Linux, `deploy/crontab.ejemplo` tiene las mismas tareas. Las tareas deben ejecutarse con una cuenta de servicio que tenga las variables de entorno de la app y acceso a la base.
Compruebe después que corrieron (`schtasks /Query /TN "PQRS respaldo" /V /FO LIST`) y que `monitoreo.py` no marca el respaldo como viejo.

**Disponibilidad:** registre `GET /salud` en un monitor externo gratuito (UptimeRobot, Healthchecks.io…) con alerta a su correo o teléfono; así se entera si la app o el servidor caen.

**Permisos del servidor.** La base, la clave de respaldo y los secretos solo deben leerlos la cuenta de servicio y los administradores:
```powershell
.\deploy\restringir_permisos_windows.ps1 -Ruta C:\pqrs\pqrs.db, D:\claves\respaldo.key -Cuenta "MIEMPRESA\pqrs-servicio"            # simula
.\deploy\restringir_permisos_windows.ps1 -Ruta C:\pqrs\pqrs.db, D:\claves\respaldo.key -Cuenta "MIEMPRESA\pqrs-servicio" -Aplicar   # aplica
```
En Linux: `chmod 600` y `chown` a la cuenta de servicio (ver `deploy/crontab.ejemplo`). Pruebe primero con un archivo de prueba y confirme que la app sigue arrancando.

**Cuentas de administración.** Cree sus propias cuentas con `python crear_usuario.py` (una por persona, rol mínimo necesario, **al menos dos administradores** para no quedar bloqueado si uno pierde el acceso).
Después de crearlas, **elimine o desactive las cuentas de demostración** (`admin.demo` y similares, que crea `seed.py`) y cambie cualquier clave que se haya compartido. `preflight.py` marca como FALTA si todavía existen.

## 7. Staging y ensayo general
`python herramientas/staging.py` levanta un **staging local aislado** (base propia, servidor de correo de pruebas con TLS real, la app en otro puerto, datos ficticios) y recorre de punta a punta:
prueba de humo por HTTP real, correo de verificación → confirmación → respuesta por correo, aviso de rebote por webhook, respaldo cifrado + copia externa + restauración desde la copia
(comparando contra la app viva), monitoreo y simulación de retención. Al terminar apaga todo y borra la carpeta (con `--mantener` queda corriendo para explorarlo). Repítalo antes de cada despliegue.
**No sustituye un staging real**: usa un servidor de correo falso, sin DNS ni HTTPS reales. Cree además un entorno de staging separado de producción en su infraestructura, con **su propia base, sus propios secretos y un dominio distinto**,
para probar correos, webhooks, migraciones y restauraciones sin arriesgar datos reales. Nunca copie datos reales de producción a staging sin anonimizarlos (`retencion.py`).

## 7b. SQLite o PostgreSQL
SQLite (por defecto) es suficiente para un volumen bajo con un solo servidor: es lo que traen las pruebas y los ensayos. Si espera varios procesos, mucha concurrencia o necesita copias gestionadas, use PostgreSQL con `DATABASE_URL`.
**Estado honesto:** el código admite PostgreSQL, pero las pruebas y el ensayo de staging corren sobre SQLite; **pruebe en un PostgreSQL de staging antes de producción** (migración, restricciones, respaldo con `pg_dump` y restauración).
Mientras use SQLite, `respaldo.py` cubre la copia; con PostgreSQL esa herramienta no aplica.

## 8. Copias de seguridad: lo que esta herramienta NO hace
No copia por sí sola a otro lugar físico (use `--copiar-a`), ni a la nube, ni cifra el disco: **un respaldo en el mismo disco se pierde con el disco**. Mueva las copias fuera del servidor (otro disco, otra máquina)
o un incendio o un ransomware se llevarán la base y sus copias juntas.

## 9. Recordatorio final
Copias cifradas, con acceso restringido, fuera del servidor y con una restauración de prueba periódica. Aplique a los respaldos la misma retención de `PRIVACIDAD.md`.
Antes de abrir al público: `preflight.py` sin FALTAs, `prueba_humo.py` en verde contra el entorno de pruebas, respaldo y restauración probados (incluida una copia en otro disco), tareas programadas, permisos del servidor revisados y una **revisión de seguridad hecha por alguien que no haya escrito el código**.
