"""Núcleo de procesamiento de audio y transcripción.

El worker (agente B) habla con estos módulos a través del Protocol
`SttEngine` de `core.stt`, nunca con faster-whisper directamente.
"""
