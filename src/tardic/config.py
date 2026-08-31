"""Configuración del sistema. Todo por variables de entorno (doc 03 §15:
secretos fuera del repositorio).

Una sola fuente de verdad: API y worker importan de aquí, nadie lee os.environ
por su cuenta.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TARDIC_", env_file=".env", extra="ignore")

    # --- API ---
    api_key: str = Field(min_length=16, description="valor esperado en el header X-API-Key")
    max_upload_bytes: int = 1_073_741_824  # 1 GiB: 4 h de m4a caben de sobra
    # No hay lista blanca de MIME types a propósito: el content-type lo declara
    # el cliente y puede mentir. La única validación que vale es ffprobe sobre
    # el archivo ya escrito — si tiene pista de audio, sirve; si no, se rechaza.

    # --- Base de datos ---
    # La contraseña se define UNA sola vez, en `db_password`: es la misma que
    # recibe el contenedor de Postgres. Antes había que escribirla también
    # dentro de `database_url`, y desincronizarlas era facilísimo — el arranque
    # moría con "password authentication failed", que no dice nada sobre tener
    # que sincronizar dos variables. `database_url` queda solo como escape para
    # apuntar a una base externa; si está vacío, se arma con las piezas.
    db_user: str = "tardic"
    # Sin valor por defecto a propósito: una contraseña "por defecto" es una
    # contraseña conocida. Se exige más abajo, salvo que se pase una
    # `database_url` completa (que ya la lleva dentro).
    db_password: str = ""
    db_name: str = "tardic"
    db_host: str = "db"  # el nombre del servicio en docker-compose
    db_port: int = 5432
    database_url: str | None = None

    # --- Almacenamiento ---
    data_dir: Path = Path("/data")

    # --- Motor STT ---
    stt_model: str = "large-v3-turbo"
    stt_compute_type: str = "int8"
    stt_language: str | None = "es"
    stt_threads: int = 0  # 0 = núcleos físicos disponibles
    chunk_minutes: float = 15.0
    vad: bool = True

    # --- Worker ---
    poll_interval_seconds: float = 5.0
    max_attempts: int = 3
    job_timeout_seconds: int = 8 * 3600  # una sesión de 4 h en un VPS lento
    # Vigencia del lease sobre un job tomado. NO es lo mismo que
    # `job_timeout_seconds`: aquel es el tope de duración de un trabajo, este es
    # "cuánto tiempo sin dar señales de vida antes de dar por muerto al worker
    # que lo tiene". El worker renueva `locked_at` en cada trozo (15 min por
    # defecto), así que media hora deja margen de sobra para un trozo lento y
    # aun así recupera rápido un job huérfano de otra máquina. Antes se usaba
    # `job_timeout_seconds` (8 h) para esto y un job huérfano tardaba 8 HORAS
    # en volver a la cola.
    lease_timeout_seconds: int = 1800
    # Al terminar bien se borran el WAV derivado (~345 MB por 3 h de audio) y
    # los checkpoints por trozo: ya no sirven para nada y llenan el volumen.
    # En True se conservan, solo para depurar.
    keep_intermediate_files: bool = False

    @field_validator("api_key")
    @classmethod
    def _no_placeholder(cls, v: str) -> str:
        """Rechaza claves que no son claves.

        La lista negra por sí sola no basta: el propio placeholder de
        `.env.example` la pasaba, así que un despliegue apurado arrancaba con
        una "clave secreta" publicada en el repo. Por eso además se exige que
        no contenga texto en español ni guiones de frase — una clave de verdad
        sale de `openssl rand -hex 32`.
        """
        low = v.lower()
        if low in {"changeme", "cambiame", "secret", "tardic", "apikey", "test"}:
            raise ValueError("TARDIC_API_KEY no puede quedarse en el valor de ejemplo")
        placeholders = ("reemplaza", "cambia", "ejemplo", "example", "placeholder", "tu-key")
        if any(w in low for w in placeholders):
            raise ValueError(
                "TARDIC_API_KEY sigue siendo el valor de ejemplo. "
                "Genera una real con: openssl rand -hex 32"
            )
        # Debe verse aleatoria: sin espacios y sin caracteres no imprimibles.
        if any(c.isspace() for c in v):
            raise ValueError("TARDIC_API_KEY no puede contener espacios")
        # ASCII obligatorio: la comparación en el header se hace sobre bytes y
        # una clave con acentos solo invita a problemas de codificación.
        if not v.isascii():
            raise ValueError("TARDIC_API_KEY debe ser ASCII (usa: openssl rand -hex 32)")
        return v

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def sqlalchemy_url(self) -> str:
        """La cadena de conexión que usa la aplicación.

        Si alguien define `TARDIC_DATABASE_URL` explícitamente, manda esa (base
        externa, un pooler, lo que sea). Si no, se compone con las piezas, que
        es el camino normal y evita tener la contraseña escrita dos veces.
        """
        if self.database_url:
            return self.database_url
        if not self.db_password:
            raise ValueError(
                "falta TARDIC_DB_PASSWORD (o define TARDIC_DATABASE_URL completa)"
            )
        password = quote_plus(self.db_password)
        user = quote_plus(self.db_user)
        return f"postgresql+psycopg://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
