import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# El paquete vive en src/, no instalado siempre en modo editable en todos los
# entornos donde corre `alembic` (p. ej. un contenedor que solo instaló
# dependencias) — se agrega a mano para que `import tardic` funcione igual.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tardic.models import Base  # noqa: E402 — después del sys.path.insert a propósito

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compara contra esto: las 5 entidades de models.py, nada más.
target_metadata = Base.metadata

# Secretos fuera del código (doc 03 §15, AGENTS.md regla 3): la URL real sale
# de la configuración, nunca de `alembic.ini` (que solo trae un ejemplo para
# desarrollo local). Se usa `Settings.sqlalchemy_url` para que las migraciones
# se conecten EXACTAMENTE igual que la app: componiendo la contraseña desde
# `TARDIC_DB_PASSWORD`, sin obligar a repetirla dentro de una URL.
try:
    from tardic.config import get_settings

    config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_url)
except Exception:
    # Sin configuración válida (p. ej. `alembic revision --autogenerate` en una
    # máquina sin .env) se cae al valor del ini o a TARDIC_DATABASE_URL.
    _env_url = os.environ.get("TARDIC_DATABASE_URL")
    if _env_url:
        config.set_main_option("sqlalchemy.url", _env_url)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
