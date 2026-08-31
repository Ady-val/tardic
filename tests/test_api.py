"""Tests de la API (agente C).

Todos usan una BD Postgres real (ver docstring de `conftest.py` sobre por
qué no SQLite) y por eso llevan `@pytest.mark.db`. Ninguno usa audio real:
el wav es sintético (fixture `synth_wav_bytes`, generado con ffmpeg).
"""
from __future__ import annotations

import uuid

import pytest

from tardic.api.deps import get_settings_dep
from tardic.models import (
    ProcessingJob,
    Recording,
    RecordingStatus,
    Segment,
    Speaker,
    Stage,
    Transcript,
)
from tardic.storage import Storage

pytestmark = pytest.mark.db


def _upload(client, headers, content: bytes, filename: str = "audio.wav", diarize: bool | None = None):
    files = {"file": (filename, content, "audio/wav")}
    data = {} if diarize is None else {"diarize": "true" if diarize else "false"}
    return client.post("/v1/recordings", headers=headers, files=files, data=data)


# --------------------------------------------------------------------------
# Subida
# --------------------------------------------------------------------------
def test_upload_happy_path_returns_201_queued_and_writes_file(client, auth_headers, synth_wav_bytes, test_settings):
    resp = _upload(client, auth_headers, synth_wav_bytes)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert uuid.UUID(body["id"])
    assert body["size_bytes"] == len(synth_wav_bytes)
    assert body["duration_seconds"] and body["duration_seconds"] > 0

    storage = Storage(test_settings.data_dir)
    recording_dir = storage.recording_dir(uuid.UUID(body["id"]))
    files_on_disk = list(recording_dir.glob("original.*"))
    assert len(files_on_disk) == 1
    assert files_on_disk[0].stat().st_size == len(synth_wav_bytes)

    assert "X-Request-ID" in resp.headers


def test_upload_without_api_key_is_401(client, synth_wav_bytes):
    resp = _upload(client, {}, synth_wav_bytes)
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_upload_with_wrong_api_key_is_401(client, synth_wav_bytes):
    resp = _upload(client, {"X-API-Key": "definitivamente-no-es-la-key"}, synth_wav_bytes)
    assert resp.status_code == 401


def test_upload_non_audio_file_is_400_and_disk_clean(client, auth_headers, non_audio_bytes, test_settings):
    resp = _upload(client, auth_headers, non_audio_bytes, filename="nota.txt")
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_audio"

    audio_root = test_settings.data_dir / "audio"
    leftover = list(audio_root.glob("*")) if audio_root.exists() else []
    assert leftover == []


def test_upload_too_large_is_413_and_disk_clean(client, app, test_settings, auth_headers, synth_wav_bytes):
    tiny_settings = test_settings.model_copy(update={"max_upload_bytes": 100})
    app.dependency_overrides[get_settings_dep] = lambda: tiny_settings

    resp = _upload(client, auth_headers, synth_wav_bytes)

    assert resp.status_code == 413, resp.text
    assert resp.json()["code"] == "upload_too_large"

    audio_root = tiny_settings.data_dir / "audio"
    leftover = list(audio_root.glob("*")) if audio_root.exists() else []
    assert leftover == []


@pytest.mark.parametrize(
    "malicious_name",
    ["../../../../pwned.wav", "..\\..\\..\\windows\\system32\\pwned.wav"],
)
def test_malicious_filename_never_escapes_data_dir(
    client, auth_headers, synth_wav_bytes, test_settings, malicious_name
):
    resp = _upload(client, auth_headers, synth_wav_bytes, filename=malicious_name)
    assert resp.status_code == 201, resp.text

    # No debe haber aparecido ningún archivo fuera de data_dir: se busca en
    # dos niveles arriba de data_dir, que es a donde ".. /.. /.. /.." apuntaría.
    suspicious_root = test_settings.data_dir.parent.parent
    assert not list(suspicious_root.rglob("pwned.wav"))

    # Y el archivo real vive exactamente donde storage.py dice que debe vivir.
    storage = Storage(test_settings.data_dir)
    recording_id = uuid.UUID(resp.json()["id"])
    saved = list(storage.recording_dir(recording_id).glob("original.*"))
    assert len(saved) == 1
    assert saved[0].name == "original.wav"  # la extensión sí se conserva, el nombre no


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def test_get_status_unknown_id_is_404(client, auth_headers):
    resp = client.get(f"/v1/recordings/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_get_status_malformed_id_is_422(client, auth_headers):
    resp = client.get("/v1/recordings/no-soy-un-uuid", headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_status_reflects_each_recording_status(client, auth_headers, db_session, test_settings):
    storage = Storage(test_settings.data_dir)

    # QUEUED: recién subida, sin job tocado por nadie más.
    recording = Recording(
        filename="reunion.wav", storage_path="audio/x/original.wav",
        status=RecordingStatus.QUEUED, size_bytes=10,
    )
    db_session.add(recording)
    db_session.flush()
    job = ProcessingJob(recording_id=recording.id, job_type="transcribe")
    db_session.add(job)
    db_session.commit()

    resp = client.get(f"/v1/recordings/{recording.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["transcript_url"] is None
    assert body["transcript_txt_url"] is None
    assert body["progress"] == {"chunks_done": 0, "chunks_total": 0, "percent": 0, "eta_seconds": None}

    # PROCESSING: el worker (simulado a mano, porque todavía no existe) va
    # dejando avance real en el job.
    recording.status = RecordingStatus.PROCESSING
    job.status = job.status  # sin cambios de tipo, solo referencia clara
    job.stage = Stage.TRANSCRIBE
    job.progress = {"chunks_done": 2, "chunks_total": 5, "percent": 40, "eta_seconds": 90}
    db_session.commit()

    resp = client.get(f"/v1/recordings/{recording.id}", headers=auth_headers)
    body = resp.json()
    assert body["status"] == "PROCESSING"
    assert body["stage"] == "TRANSCRIBE"
    assert body["progress"]["percent"] == 40
    assert body["transcript_url"] is None

    # FAILED: mensaje limpio para el usuario, sin rutas ni trazas (eso lo
    # garantiza quien escriba `processing_error`, no esta prueba).
    recording.status = RecordingStatus.FAILED
    recording.processing_error = "el modelo no pudo procesar el audio"
    db_session.commit()

    resp = client.get(f"/v1/recordings/{recording.id}", headers=auth_headers)
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"] == "el modelo no pudo procesar el audio"

    # COMPLETED: hay transcript en BD y transcript.txt en disco (lo deja el
    # worker); ahora sí deben aparecer las dos URLs.
    recording.status = RecordingStatus.COMPLETED
    recording.processing_error = None
    transcript = Transcript(recording_id=recording.id, text="hola mundo", model="fake-model")
    db_session.add(transcript)
    db_session.flush()
    db_session.add(Segment(transcript_id=transcript.id, start_time=0.0, end_time=1.0, text="hola mundo"))
    db_session.commit()
    storage.ensure_dir(recording.id)
    # write_bytes (no write_text): en Windows, write_text traduce "\n" a
    # "\r\n" y el assert de la descarga quedaría atado a la plataforma.
    storage.transcript_txt_path(recording.id).write_bytes(b"hola mundo\n")

    resp = client.get(f"/v1/recordings/{recording.id}", headers=auth_headers)
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["transcript_url"].endswith(f"/v1/recordings/{recording.id}/transcript")
    assert body["transcript_txt_url"].endswith(f"/v1/recordings/{recording.id}/transcript.txt")


# --------------------------------------------------------------------------
# Transcript
# --------------------------------------------------------------------------
def _make_completed_recording(db_session, storage: Storage) -> Recording:
    recording = Recording(
        filename="clase de piano.wav", storage_path="audio/x/original.wav",
        status=RecordingStatus.COMPLETED, size_bytes=10, duration_seconds=3.5,
        language="es",
    )
    db_session.add(recording)
    db_session.flush()
    transcript = Transcript(
        recording_id=recording.id, text="hola,\nmundo", model="fake-model",
        language="es", processing_time_seconds=1.2,
    )
    db_session.add(transcript)
    db_session.flush()
    speaker = Speaker(recording_id=recording.id, label="SPEAKER_00")
    db_session.add(speaker)
    db_session.flush()
    db_session.add(Segment(
        transcript_id=transcript.id, start_time=0.0, end_time=1.5, text="hola,",
        speaker_id=speaker.id, confidence=0.9,
    ))
    db_session.add(Segment(
        transcript_id=transcript.id, start_time=1.5, end_time=3.5, text="mundo",
        speaker_id=speaker.id, confidence=0.8,
    ))
    db_session.commit()
    storage.ensure_dir(recording.id)
    storage.transcript_txt_path(recording.id).write_bytes(b"hola,\nmundo\n")
    return recording


def test_transcript_not_ready_is_409(client, auth_headers, db_session):
    recording = Recording(
        filename="a.wav", storage_path="audio/x/original.wav", status=RecordingStatus.QUEUED,
    )
    db_session.add(recording)
    db_session.commit()

    resp_json = client.get(f"/v1/recordings/{recording.id}/transcript", headers=auth_headers)
    resp_txt = client.get(f"/v1/recordings/{recording.id}/transcript.txt", headers=auth_headers)

    assert resp_json.status_code == 409
    assert resp_json.json()["code"] == "not_ready"
    assert "QUEUED" in resp_json.json()["detail"]
    assert resp_txt.status_code == 409


def test_transcript_json_when_completed(client, auth_headers, db_session, test_settings):
    storage = Storage(test_settings.data_dir)
    recording = _make_completed_recording(db_session, storage)

    resp = client.get(f"/v1/recordings/{recording.id}/transcript", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recording_id"] == str(recording.id)
    assert body["text"] == "hola,\nmundo"
    assert body["model"] == "fake-model"
    assert len(body["segments"]) == 2
    assert body["segments"][0]["speaker"] == "SPEAKER_00"


def test_transcript_txt_download_when_completed(client, auth_headers, db_session, test_settings):
    storage = Storage(test_settings.data_dir)
    recording = _make_completed_recording(db_session, storage)

    resp = client.get(f"/v1/recordings/{recording.id}/transcript.txt", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".txt" in resp.headers["content-disposition"]
    assert resp.text == "hola,\nmundo\n"


# --------------------------------------------------------------------------
# Listado y borrado
# --------------------------------------------------------------------------
def test_list_recordings_paginates(client, auth_headers, db_session):
    for i in range(3):
        db_session.add(Recording(
            filename=f"rec-{i}.wav", storage_path=f"audio/{i}/original.wav",
            status=RecordingStatus.QUEUED,
        ))
    db_session.commit()

    first_page = client.get("/v1/recordings?limit=2&offset=0", headers=auth_headers).json()
    assert first_page["total"] == 3
    assert len(first_page["items"]) == 2
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0

    second_page = client.get("/v1/recordings?limit=2&offset=2", headers=auth_headers).json()
    assert len(second_page["items"]) == 1


def test_delete_recording_removes_row_and_files(client, auth_headers, synth_wav_bytes, test_settings, db_session):
    upload = _upload(client, auth_headers, synth_wav_bytes)
    recording_id = upload.json()["id"]

    storage = Storage(test_settings.data_dir)
    recording_dir = storage.recording_dir(uuid.UUID(recording_id))
    assert recording_dir.exists()

    resp = client.delete(f"/v1/recordings/{recording_id}", headers=auth_headers)
    assert resp.status_code == 204

    assert not recording_dir.exists()
    assert db_session.get(Recording, uuid.UUID(recording_id)) is None
    assert client.get(f"/v1/recordings/{recording_id}", headers=auth_headers).status_code == 404


def test_delete_unknown_id_is_404(client, auth_headers):
    resp = client.delete(f"/v1/recordings/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
def test_health_without_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert "version" in body
