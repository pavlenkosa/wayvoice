from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_import_path() -> None:
    """Make the ``wayvoice`` package importable from this script.

    The runner is executed by the engine runtime interpreter, which knows nothing about
    the application sources; this module lives inside the package directory, so the import
    root is its parent - the same layout in a checkout and in the installed package.
    """
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_ensure_import_path()

from wayvoice import fw_worker, languages  # noqa: E402  (needs the path bootstrap above)


def _run_once(args: argparse.Namespace) -> int:
    """Load the model, transcribe one file and print the text.

    The original one-shot mode, and still the fallback whenever the warm worker is
    unavailable or disabled.
    """
    config = fw_worker.WorkerConfig(
        model=args.model,
        device=args.device,
        beam_size=args.beam_size,
        vad=args.vad,
    )
    cache = fw_worker.ModelCache()
    reply = fw_worker.handle_request(
        {
            "cmd": "transcribe",
            "audio": args.audio,
            # Normalized here as well as by the caller: the runner can be
            # started by hand, and "auto" has to survive as "auto" so that
            # fw_worker can turn it into the None the model expects.
            "language": languages.normalize(args.language),
            "request_id": "one-shot",
        },
        cache,
        config,
    )
    if not reply.get("ok"):
        print(str(reply.get("error") or "Faster-Whisper failed"), file=sys.stderr)
        return 1
    print(str(reply.get("text") or ""))
    return 0


def _serve(args: argparse.Namespace) -> int:
    """Run the warm worker that keeps the model in memory."""
    socket_path = Path(args.socket) if args.socket else fw_worker.default_socket_path()
    config = fw_worker.WorkerConfig(
        model=args.model,
        device=args.device,
        beam_size=args.beam_size,
        vad=args.vad,
    )
    fw_worker.run_server(socket_path, config, idle_timeout=float(args.idle_timeout))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", help="WAV file to transcribe (one-shot mode)")
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default=languages.AUTO)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad", action="store_true")
    parser.add_argument("--serve", action="store_true", help="run as a warm worker instead of once")
    parser.add_argument("--socket", default="", help="unix socket of the warm worker")
    parser.add_argument("--idle-timeout", type=float, default=fw_worker.DEFAULT_IDLE_TIMEOUT)
    args = parser.parse_args()

    if args.serve:
        return _serve(args)
    if not args.audio:
        parser.error("--audio is required unless --serve is used")
    return _run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
