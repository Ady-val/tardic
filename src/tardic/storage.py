"""Rutas en disco. Único lugar del sistema que las construye.

Regla que no se negocia: **el nombre que sube el usuario nunca toca el disco**.
Un `filename` de "../../etc/passwd" o "C:\\Windows\\x" es un archivo válido para
un cliente HTTP y un agujero para el servidor. La ruta se deriva del UUID que
genera el servidor; el nombre original solo se guarda en la base de datos para
mostrárselo de vuelta al usuario.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

# Extensiones que sabemos convertir con ffmpeg. La validación real es del
# contenido, no de la extensión; esto solo elige cómo nombrar el archivo.
KNOWN_SUFFIXES = {
    ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".aac",
    ".mp4", ".mov", ".webm", ".mkv", ".amr", ".wma",
}

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


def safe_suffix(filename: str | None) -> str:
    """Extensión utilizable a partir del nombre del cliente, o `.bin`.

    Solo se conserva para que ffmpeg tenga una pista del contenedor; cualquier
    cosa rara se descarta sin discutir.
    """
    if not filename:
        return ".bin"
    suffix = Path(filename).suffix.lower()
    if suffix in KNOWN_SUFFIXES and _SAFE_SUFFIX.match(suffix):
        return suffix
    return ".bin"


def safe_display_name(filename: str | None, *, max_length: int = 255) -> str:
    """Nombre que SÍ se guarda en la base y se le muestra de vuelta al usuario.

    No construye rutas (de eso se encarga el UUID), pero igual hay que limpiarlo
    antes de que toque la base: PostgreSQL rechaza el byte nulo en campos de
    texto, y un `filename` con `\\x00` hacía que el POST reventara con un 500
    después de haber escrito el audio en disco. También se quitan los controles
    (CR/LF incluidos) porque este nombre termina en una cabecera
    `Content-Disposition` al descargar.
    """
    if not filename:
        return "archivo"
    # Solo el nombre: si el cliente mandó una ruta, el resto sobra.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(c for c in base if c.isprintable() and c not in "\x00")
    cleaned = cleaned.strip().strip(".")
    return cleaned[:max_length] or "archivo"


class Storage:
    """Layout bajo `data_dir`:

        audio/<recording_id>/original.<ext>   lo que subió el usuario, intacto
        audio/<recording_id>/audio.wav        16 kHz mono, lo que come el motor
        audio/<recording_id>/chunks/          checkpoints por trozo (RF-10)
        audio/<recording_id>/transcript.txt   entregable de descarga
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    # --- directorios ---
    def recording_dir(self, recording_id: uuid.UUID) -> Path:
        return self.data_dir / "audio" / str(recording_id)

    def chunks_dir(self, recording_id: uuid.UUID) -> Path:
        return self.recording_dir(recording_id) / "chunks"

    # --- archivos ---
    def original_path(self, recording_id: uuid.UUID, suffix: str = ".bin") -> Path:
        return self.recording_dir(recording_id) / f"original{safe_suffix('x' + suffix)}"

    def wav_path(self, recording_id: uuid.UUID) -> Path:
        return self.recording_dir(recording_id) / "audio.wav"

    def transcript_txt_path(self, recording_id: uuid.UUID) -> Path:
        return self.recording_dir(recording_id) / "transcript.txt"

    # --- operaciones ---
    def ensure_dir(self, recording_id: uuid.UUID) -> Path:
        d = self.recording_dir(recording_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def relative(self, path: Path) -> str:
        """Ruta guardada en la BD: relativa, para que mover `data_dir` (o
        montar el volumen en otro lado) no invalide las filas existentes."""
        return str(Path(path).relative_to(self.data_dir).as_posix())

    def absolute(self, relative_path: str) -> Path:
        """Resuelve una ruta de la BD y verifica que no se salga de `data_dir`.

        Defensa en profundidad: aunque hoy solo escribimos rutas derivadas de
        UUIDs, una fila alterada no debe poder leer fuera del volumen.
        """
        candidate = (self.data_dir / relative_path).resolve()
        root = self.data_dir.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"ruta fuera del almacenamiento: {relative_path!r}")
        return candidate

    def delete_recording(self, recording_id: uuid.UUID) -> None:
        import shutil

        shutil.rmtree(self.recording_dir(recording_id), ignore_errors=True)

    def disk_free_bytes(self) -> int:
        import shutil

        return shutil.disk_usage(self.data_dir).free
