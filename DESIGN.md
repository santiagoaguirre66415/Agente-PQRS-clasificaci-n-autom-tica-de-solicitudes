# Sistema de diseño — Mesa de PQRS

Interfaz de trabajo diario para funcionarios de atención al cliente: se usa durante horas, así que prioriza legibilidad y rapidez sobre decoración.

## Color
| Token | Valor | Rol |
|---|---|---|
| `--tinta` | #13262B | Texto principal y barra superior |
| `--suave` | #55686D | Texto secundario (contraste 5.0:1 sobre el fondo de la cola) |
| `--fondo` / `--superficie` | #E9EEED / #FFFFFF | Fondo de la cola / paneles |
| `--accion` | #0F5E63 | Botones principales y borde del borrador (7.5:1 con texto blanco) |
| `--borde-control` | #758789 | Bordes de campos y botones (3.8:1, cumple 3:1 para controles) |
| `--alta` / `--media` / `--baja` | #C8381F / #9A6500 / #4F8A60 | Prioridad. Siempre acompañada de texto, nunca solo color |
| Categorías | ámbar, azul, violeta, rosa, gris | Punto de color + nombre de la categoría |

## Tipografía
Familia única: Schibsted Grotesk. Base 16 px, nada por debajo de 14 px.

| Token | Tamaño | Uso |
|---|---|---|
| `--t-xs` | 14 px | Hora, etiquetas de la cola, ayudas |
| `--t-sm` | 16 px | Texto general |
| `--t-md` | 17 px | Borrador de respuesta |
| `--t-lg` | 18 px | Nombre en la cola, títulos de sección, cita original |
| `--t-xl` / `--t-2xl` | 24 / 30 px | Reservados / título del detalle |

## Espacio y forma
- Objetivo táctil mínimo: 44 px (`--objetivo`) en botones, pestañas, filtros y opciones de estado.
- Elementos de la cola con 16 px de relleno vertical; detalle con 36–48 px de margen y ancho máximo de 46 rem (≈ 70 caracteres).
- Radio de 6 px en controles y 10 px en el bloque del borrador. Sin sombras: la jerarquía sale del borde y el color.

## Jerarquía del detalle
1. Qué escribió la persona (cita).
2. **Borrador de respuesta** (bloque con borde de acento): es lo que el funcionario más usa.
3. Clasificación.
4. Estado.

## Accesibilidad
Foco visible, navegación por teclado, `aria-live` en el detalle, `prefers-reduced-motion` respetado y adaptación a móvil.
