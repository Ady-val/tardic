"""Contrato del motor de transcripción.

RNF-05 (sustituibilidad) en la práctica: el worker habla con `SttEngine`, nunca
con faster-whisper directamente. Cambiar a whisper.cpp —el sustituto que el
benchmark dejó documentado— o a un servicio externo es escribir otra clase que
cumpla este protocolo, sin tocar API, worker ni base de datos.

Esto también es lo que permite que los tests corran en segundos: `FakeEngine`
implementa el mismo protocolo y no descarga 1.6 GB de modelo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SttSegment:
    """Un fragmento hablado. Los tiempos son SIEMPRE relativos al audio
    completo, aunque internamente se haya procesado por trozos."""

    start: float
    end: float
    text: str
    confidence: float | None = None


@dataclass
class SttResult:
    segments: list[SttSegment]
    language: str | None
    model: str
    processing_time_seconds: float
    audio_duration_seconds: float
    # Trazabilidad del doc 03 §14: qué hizo cada trozo. Útil para entender
    # dónde se fue el tiempo sin volver a correr nada.
    chunks: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        """El texto plano, SIEMPRE en orden cronológico.

        Se ordena explícitamente en vez de confiar en el orden de emisión del
        motor: la API devuelve los segmentos ordenados por la base de datos, y
        si el TXT saliera en otro orden el mismo recording se contradiría a sí
        mismo según por dónde se consulte.
        """
        return "\n".join(s.text for s in sorted(self.segments, key=lambda s: (s.start, s.end)))


@dataclass(frozen=True)
class ChunkProgress:
    """Lo que el motor reporta al terminar cada trozo."""

    chunks_done: int
    chunks_total: int
    seconds_done: float
    seconds_total: float
    elapsed_seconds: float
    # Segundos de audio REALMENTE transcritos en esta corrida. Difiere de
    # `seconds_done` al reanudar: los trozos que vienen de checkpoint cuentan
    # como avance, pero no consumieron tiempo ahora. Sin separarlos, el ritmo
    # sale absurdamente rápido y el ETA anuncia 0 s para trabajo que todavía
    # va a tardar quince minutos.
    seconds_computed: float | None = None

    @property
    def percent(self) -> int:
        if self.seconds_total <= 0:
            return 0
        return min(100, int(100 * self.seconds_done / self.seconds_total))

    @property
    def eta_seconds(self) -> int | None:
        """Se estima con el ritmo medido en ESTA corrida, no con un RTF fijo:
        el VPS y la laptop rinden distinto y el número tiene que ser honesto.

        Si todavía no se ha calculado nada aquí (todo vino de checkpoint), no
        hay ritmo que medir y se devuelve None — que el cliente muestre "sin
        estimación" es preferible a prometerle un cero falso.
        """
        computed = self.seconds_computed if self.seconds_computed is not None else self.seconds_done
        remaining = self.seconds_total - self.seconds_done
        if remaining <= 0:
            return 0
        if computed <= 0 or self.elapsed_seconds <= 0:
            return None
        rate = self.elapsed_seconds / computed
        return max(0, int(remaining * rate))


class ProgressCallback(Protocol):
    def __call__(self, progress: ChunkProgress) -> None: ...


@runtime_checkable
class SttEngine(Protocol):
    """Motor de transcripción.

    Implementaciones deben:
      · aceptar un WAV PCM 16 kHz mono (el preprocesado ya lo garantiza);
      · procesar por trozos y llamar `on_progress` al terminar cada uno;
      · ser reanudables: si `checkpoint_dir` trae trozos ya hechos, se
        reutilizan en vez de recalcularlos (RF-10);
      · devolver timestamps en la línea de tiempo del audio completo.
    """

    def transcribe(
        self,
        audio_path: Path,
        *,
        checkpoint_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SttResult: ...
