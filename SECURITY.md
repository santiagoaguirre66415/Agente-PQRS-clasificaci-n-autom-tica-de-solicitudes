# Política de seguridad

## Cómo reportar una vulnerabilidad
**No abra un issue público ni publique los detalles.** Use el reporte privado de GitHub:
pestaña **Security** de este repositorio → **Report a vulnerability** (el mantenedor debe tenerlo activado en *Settings → Code security*).

Incluya, si puede:
- la versión o el commit afectado;
- los pasos exactos para reproducirlo, con **datos ficticios** (nunca datos personales reales);
- qué impacto cree que tiene;
- si ya lo ha compartido con alguien más.

Se acusará recibo lo antes posible y se acordará con usted el momento de divulgarlo una vez corregido.

## Alcance
Este repositorio contiene una aplicación que **maneja datos personales** (nombres, correos y el texto de las solicitudes de la ciudadanía).
Interesa especialmente cualquier problema de: control de acceso y roles, autenticación y sesiones, exposición de datos personales,
inyección (SQL, cabeceras de correo, HTML), envío de correo a terceros, el formulario público, el webhook de entrega y las herramientas de respaldo y retención.

Fuera de alcance: ataques de denegación de servicio por volumen, ingeniería social, y la configuración de su propia infraestructura.

## Pruebas responsables
- Pruebe solo en una **copia local** o de pruebas, con datos ficticios. No contra sistemas de terceros ni de producción de otra persona.
- No acceda a datos que no sean suyos ni los conserve.

## Qué controles incluye (visión general)
Cuentas individuales con roles y sesiones con caducidad, contraseñas con scrypt, registro de auditoría, límites de frecuencia, validación estricta de entradas,
verificación del correo antes de enviar información de un caso, protección propia contra envíos automáticos en el formulario público, cabeceras de seguridad,
dependencias fijadas con hashes y revisadas con `pip-audit`, y herramientas de operación (respaldo, comprobación previa a producción y monitoreo).
Cómo desplegarlo de forma segura está en [DESPLIEGUE.md](DESPLIEGUE.md); el tratamiento de datos personales, en [PRIVACIDAD.md](PRIVACIDAD.md).

## Versiones con soporte
Solo la rama `main`. Es un proyecto en evolución: **antes de usarlo con datos reales** haga el recorrido de `DESPLIEGUE.md` (sección 6) y pida una revisión de seguridad independiente.

Esto es una política de divulgación, no una garantía ni una certificación de seguridad.
