import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def _url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///pqrs.db")


engine = create_engine(_url(), connect_args={"check_same_thread": False} if _url().startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def sumar_dias_habiles(desde: datetime, dias: int) -> datetime:
    """Suma días hábiles (lunes a viernes). No descuenta festivos: confirme el plazo legal que aplica a su caso."""
    fecha = desde
    while dias > 0:
        fecha += timedelta(days=1)
        if fecha.weekday() < 5:
            dias -= 1
    return fecha


def _plazo() -> datetime:
    return sumar_dias_habiles(datetime.now(timezone.utc).replace(tzinfo=None), int(os.environ.get("PQRS_PLAZO_DIAS", "15")))


class Solicitud(Base):
    __tablename__ = "solicitudes"
    __table_args__ = (
        # La propia base de datos impide valores fuera de catálogo y fechas incoherentes
        CheckConstraint("tipo IN ('peticion','queja','reclamo','sugerencia')", name="ck_tipo"),
        CheckConstraint("categoria IN ('energia','agua','facturacion','atencion','otros')", name="ck_categoria"),
        CheckConstraint("prioridad IN ('alta','media','baja')", name="ck_prioridad"),
        CheckConstraint("estado IN ('nuevo','en_proceso','resuelto')", name="ck_estado"),
        CheckConstraint("metodo IN ('ia','reglas')", name="ck_metodo"),
        # resuelto_en existe si y solo si la solicitud está resuelta
        CheckConstraint(
            "(estado = 'resuelto' AND resuelto_en IS NOT NULL) OR (estado <> 'resuelto' AND resuelto_en IS NULL)",
            name="ck_resuelto_coherente",
        ),
        CheckConstraint("resuelto_en IS NULL OR resuelto_en >= creado", name="ck_fechas_orden"),
        # Una solicitud solo puede estar resuelta si fue respondida, y la respuesta va completa o no va
        CheckConstraint("estado <> 'resuelto' OR respondido_en IS NOT NULL", name="ck_resuelta_requiere_respuesta"),
        CheckConstraint(
            "(respondido_en IS NULL AND respuesta_final IS NULL AND respondido_por IS NULL AND canal_respuesta IS NULL) OR "
            "(respondido_en IS NOT NULL AND respuesta_final IS NOT NULL AND respondido_por IS NOT NULL AND canal_respuesta IS NOT NULL)",
            name="ck_respuesta_completa",
        ),
        CheckConstraint("canal_respuesta IS NULL OR canal_respuesta IN ('correo','otro')", name="ck_canal"),
        CheckConstraint("entrega IS NULL OR entrega IN ('entregado','rebotado')", name="ck_entrega"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ciudadano: Mapped[str] = mapped_column(String(120))
    correo: Mapped[str] = mapped_column(String(160), default="")
    texto: Mapped[str] = mapped_column(Text)
    # Resultado del agente
    tipo: Mapped[str] = mapped_column(String(20), default="peticion")  # peticion | queja | reclamo | sugerencia
    categoria: Mapped[str] = mapped_column(String(40), default="otros")
    prioridad: Mapped[str] = mapped_column(String(10), default="media")  # alta | media | baja
    area: Mapped[str] = mapped_column(String(60), default="Atención al cliente")
    resumen: Mapped[str] = mapped_column(String(300), default="")
    respuesta_sugerida: Mapped[str] = mapped_column(Text, default="")
    metodo: Mapped[str] = mapped_column(String(10), default="reglas")  # reglas | ia
    estado: Mapped[str] = mapped_column(String(15), default="nuevo")  # nuevo | en_proceso | resuelto
    creado: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    resuelto_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Respuesta oficial: lo que realmente se contestó, quién, cuándo y por qué canal
    respuesta_final: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    respondido_por: Mapped[str | None] = mapped_column(String(40), nullable=True, default=None)
    respondido_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    canal_respuesta: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)  # correo | otro
    fecha_limite: Mapped[datetime] = mapped_column(DateTime, default=_plazo)
    # Verificación del correo: solo se envía información del caso a una dirección cuyo dueño la confirmó
    correo_verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    verif_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, default=None)  # SHA-256 del token
    verif_expira: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Autorización de tratamiento de datos dada en el formulario público (cuándo la aceptó la persona)
    autorizacion_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Entrega del correo de respuesta, según el webhook del proveedor de correo
    mensaje_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True, default=None)
    entrega: Mapped[str | None] = mapped_column(String(12), nullable=True, default=None)  # entregado | rebotado
    entrega_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = ("admin", "funcionario", "consulta")


class Usuario(Base):
    __tablename__ = "usuarios"
    __table_args__ = (CheckConstraint("rol IN ('admin','funcionario','consulta')", name="ck_rol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario: Mapped[str] = mapped_column(String(40), unique=True)
    clave_hash: Mapped[str] = mapped_column(String(255))  # scrypt con sal; nunca la clave en claro
    rol: Mapped[str] = mapped_column(String(15), default="consulta")
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado: Mapped[datetime] = mapped_column(DateTime, default=_ahora)


class Sesion(Base):
    """Sesión con expiración y revocación. Se guarda solo el hash SHA-256 del token."""
    __tablename__ = "sesiones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    creada: Mapped[datetime] = mapped_column(DateTime, default=_ahora)
    expira: Mapped[datetime] = mapped_column(DateTime)
    revocada: Mapped[bool] = mapped_column(Boolean, default=False)


class Auditoria(Base):
    """Registro de acciones. Solo se inserta: la API no ofrece modificar ni borrar."""
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime, default=_ahora, index=True)
    usuario: Mapped[str] = mapped_column(String(40), default="-")
    rol: Mapped[str] = mapped_column(String(15), default="-")
    accion: Mapped[str] = mapped_column(String(40))
    objeto: Mapped[str] = mapped_column(String(60), default="")
    detalle: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(45), default="")


def get_db():
    with SessionLocal() as db:
        yield db


def init_db() -> None:
    Base.metadata.create_all(engine)
