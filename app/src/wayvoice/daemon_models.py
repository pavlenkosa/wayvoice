"""Daemon-owned maintenance: never delete weights beneath a live dictation."""
from . import model_store
from .runtime import worker_client, locations


def delete_model(daemon, model_id):
    with daemon._lock:
        if daemon._shutdown.is_set() or daemon.busy or daemon.recorder.recording or daemon.preparation.running or daemon._model_maintenance:
            return {"ok": False, "error_key": "store.maintenance_busy", "model_id": model_id}
        daemon._model_maintenance = model_id
    try:
        # Validate the target before stopping any process; local/shared-cache
        # refusal remains authoritative in model_store.
        model_store._check_deletable(model_id, None)
        # File deletion and warm-worker replacement share the same ownership lock.
        with worker_client._worker_guard(daemon._shutdown):
            worker_client._retry_unresolved_workers()
            reply = worker_client._worker_ping()
            if reply is not None:
                config = reply.get("config") or {}
                held_model = str(config.get("model") or reply.get("model") or "")
                if not held_model:
                    return {"ok": False, "error_key": "store.worker_stop_failed", "model_id": model_id}
                if held_model == model_id:
                    identity = worker_client._worker_identity(int(reply.get("pid") or 0))
                    if identity is None:
                        return {"ok": False, "error_key": "store.worker_stop_failed", "model_id": model_id}
                    worker_client.stop_worker(timeout=0.5, expected_identity=identity)
                    if worker_client._worker_identity(identity[0]) == identity and worker_client._pid_alive(identity[0]):
                        return {"ok": False, "error_key": "store.worker_stop_failed", "model_id": model_id}
            else:
                # A hung worker is not evidence that no worker holds these files.
                try:
                    pid = int(locations.worker_pid_path().read_text().split()[0])
                except (OSError, ValueError, IndexError):
                    pid = 0
                if pid > 1 and worker_client._pid_alive(pid):
                    return {"ok": False, "error_key": "store.worker_stop_failed", "model_id": model_id}
            if daemon._shutdown.is_set():
                return {"ok": False, "error_key": "store.maintenance_busy", "model_id": model_id}
            return model_store.delete(model_id)
    except model_store.RefusedError as exc:
        return {"ok": False, "error_key": exc.key, "detail": exc.detail, "model_id": model_id}
    except Exception as exc:
        return {"ok": False, "error_key": "store.delete_failed", "detail": str(exc), "model_id": model_id}
    finally:
        with daemon._lock:
            daemon._model_maintenance = None
