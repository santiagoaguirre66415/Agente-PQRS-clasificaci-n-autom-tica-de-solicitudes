# Tratamiento de datos personales — Agente PQRS

> Borrador técnico para el responsable del tratamiento. **Debe revisarlo un abogado** antes de publicarse (Ley 1581 de 2012,
> Decreto 1377 de 2013 y normas de la SIC). Aquí se documenta lo que hace el software; no es asesoría jurídica.

## Datos que se tratan
| Dato | Origen | Necesario para |
|---|---|---|
| Nombre | Formulario | Identificar y responder la solicitud |
| Correo (opcional) | Formulario | Enviar la respuesta |
| Texto de la solicitud | Formulario | Atender la PQRS; puede contener datos personales adicionales que la persona decida escribir |
| Usuario, rol y registro de acciones del personal | Sistema | Control de acceso y trazabilidad |
| Dirección IP | Servidor | Seguridad (límites de frecuencia) y auditoría |
| Fecha y hora de la autorización de tratamiento de datos | Formulario público (casilla obligatoria) | Constancia de que la persona autorizó. El aviso que ve está en `/privacidad` (plantilla por completar) |
| Aviso de entrega o rebote del correo | Proveedor de correo (webhook, opcional) | Saber si la respuesta llegó; solo se guarda «entregado» o «rebotado» y la fecha |

No se piden datos sensibles. Se recomienda indicar en el formulario que **no escriba** datos de salud, cédula ni cuentas bancarias.

## Finalidades
1. Recibir, clasificar, asignar y responder peticiones, quejas, reclamos y sugerencias.
2. Medir tiempos y volumen de atención (estadísticas agregadas).
3. Garantizar la seguridad del sistema y la trazabilidad de las acciones del personal.

No se usan para publicidad ni se venden.

## Encargados y terceros
| Tercero | Qué recibe | Condiciones |
|---|---|---|
| **Proveedor de IA gratuita** (p. ej. Groq u OpenRouter) — **solo si** se definen `PQRS_IA_PROVIDER`, `PQRS_IA_KEY` y `PQRS_IA_MODEL`. Por defecto está **apagado** y no sale ningún dato | El **texto** de la solicitud (máx. 1500 caracteres), tras quitar correos y números largos. **No** se envían nombre ni correo | Implica **transferencia internacional**. Los planes gratuitos suelen ofrecer menos garantías de privacidad que los de pago, y algunos pueden usar los textos para mejorar sus modelos: **no verifiqué los términos vigentes de ningún proveedor**. Antes de activarlo con datos reales hay que leer sus términos y su política de datos, confirmar si conservan o usan los textos, e informarlo a los titulares. Si no hay certeza, déjalo apagado: el sistema funciona por reglas sin enviar nada fuera |
| **Proveedor de correo (SMTP)** — solo si se configura `PQRS_SMTP_*` | El correo de la persona, el enlace de verificación (sin datos del caso) y el texto de la respuesta, al enviarla. La respuesta solo se envía a correos que la persona confirmó | Contrato o términos del proveedor de correo, ubicación de sus servidores y su política de retención. La auditoría guarda el destinatario **enmascarado** (`j***@mail.com`) |
| Proveedor de alojamiento | Todos los datos almacenados | Contrato de encargado del tratamiento, ubicación del servidor y cifrado en reposo |

## Limitaciones conocidas
Con la IA apagada (valor por defecto) los datos nunca salen del servidor.
Si se activa, la anonimización previa al envío usa expresiones regulares: **no elimina nombres propios ni direcciones** escritos dentro del texto libre.

## Conservación propuesta
| Dato | Plazo propuesto | Estado |
|---|---|---|
| Solicitudes resueltas | 2 años desde la resolución, luego anonimizar (borrar nombre, correo y texto) | Herramienta lista (`herramientas/retencion.py`); **falta programarla** (ver `DESPLIEGUE.md`, 4d) |
| Auditoría | 1 año | Herramienta lista (`herramientas/retencion.py`); **falta programarla** (ver `DESPLIEGUE.md`, 4d) |
| Sesiones vencidas | 30 días | Herramienta lista (`herramientas/retencion.py`); **falta programarla** (ver `DESPLIEGUE.md`, 4d) |

## Derechos de los titulares
Conocer, actualizar, rectificar, suprimir y revocar la autorización. Canal sugerido: el mismo correo de atención de la empresa.
Para atender una supresión: `python herramientas/retencion.py suprimir --correo persona@mail.com --aplicar` (o `--id N`) anonimiza todas sus solicitudes. La herramienta simula si no se agrega `--aplicar`.
Esto no alcanza las copias de seguridad antiguas: aplique la misma retención a ellas.

## Seguridad aplicada
Cuentas individuales con roles, contraseñas con scrypt, sesiones con caducidad, registro de auditoría de cada consulta y cambio,
HTTPS obligatorio en producción, límites de frecuencia y cabeceras de seguridad. Para reportar un problema de seguridad, vea `SECURITY.md`.

## Aviso corto sugerido para el formulario
> Usaremos su nombre y su solicitud para atenderla y responderle. La clasificación se hace de forma automática en nuestros servidores.
> *(Solo si se activa la IA opcional, añadir: «El texto de su solicitud, sin correo ni números largos, será procesado por un proveedor
> externo ubicado fuera de Colombia».)*
> No escriba datos sensibles. Puede pedir la corrección o supresión de sus datos en [correo de contacto].
