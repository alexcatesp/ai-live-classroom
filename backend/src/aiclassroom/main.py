"""Backend entry point.

Started by the Tauri shell as a child process. It binds to a free loopback port and
announces the port and the session token on stdout as a single line, which the
shell reads before opening the window. Nothing is written to a fixed port or a
well-known file, so two copies of the portable folder can run side by side.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys

from .api.app import build_context, create_app
from .config.settings import DataPaths, default_data_root

READY_PREFIX = "AICLASSROOM_READY "
LOOPBACK = "127.0.0.1"

logger = logging.getLogger(__name__)


def find_free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout carries the handshake line only
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aiclassroom-backend")
    parser.add_argument("--port", type=int, default=0, help="0 elige un puerto libre")
    parser.add_argument("--token", default=None, help="token de sesión; se genera si se omite")
    parser.add_argument("--data-dir", default=None, help="carpeta de datos de la aplicación")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="comprueba que el ejecutable arranca en este equipo y termina",
    )
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="con --selftest, falla si no se puede cargar la biblioteca de audio",
    )
    parser.add_argument(
        "--require-wakeword",
        action="store_true",
        help="con --selftest, falla si el motor de palabra clave no está empaquetado",
    )
    return parser.parse_args(argv)


def secret_store_works() -> tuple[bool, str]:
    """Round-trip a secret through the passphrase store.

    `cryptography` carries a native module, and a build where it failed to
    bundle would look healthy until the teacher tried to save an API key. This
    is a core dependency, so a failure here fails the whole selftest.
    """
    from .config.secrets import PassphraseSecretStore

    probe = "sk-comprobacion"
    try:
        store = PassphraseSecretStore("contraseña de comprobación")
        if store.unprotect(store.protect(probe)) != probe:
            return False, "el cifrado no devuelve el mismo valor"
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        return False, str(exc)
    return True, "almacén de claves disponible"


def wakeword_engine_available() -> tuple[bool, str]:
    """Whether openWakeWord can be imported from this build.

    Packaging it is fiddly -- it drags in scipy and scikit-learn through its own
    __init__ -- and a bundle missing one of them looks perfectly healthy until
    the teacher presses "Iniciar clase".
    """
    try:
        from openwakeword.model import Model  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any import failure is the answer
        return False, str(exc)
    return True, "openWakeWord disponible"


def selftest(
    paths: DataPaths, require_audio: bool = False, require_wakeword: bool = False
) -> int:
    """Prove the frozen executable runs here, without opening any device.

    The portable build is verified in CI with this (D-09): it exercises the
    bundled Python, the data folder and the application wiring on a machine
    with no audio hardware and no API key.

    With `require_audio` it also fails when PortAudio is missing from the
    bundle. A machine with no sound card is fine -- zero devices, or a host
    stack that does not answer, is a valid answer on a build runner. A library
    that did not make it into the bundle is not: that reaches the classroom as
    an application which can never open a microphone.
    """
    from fastapi.testclient import TestClient

    from .audio.devices import probe_devices

    context = build_context(token="selftest")
    application = create_app(context)
    with TestClient(application) as client:
        health = client.get("/health")
        if health.status_code != 200:
            print(f"FALLO: /health devolvió {health.status_code}", file=sys.stderr)
            return 1
        unauthorised = client.get("/api/state")
        if unauthorised.status_code != 401:
            print("FALLO: /api/state respondió sin token", file=sys.stderr)
            return 1
        state = client.get("/api/state", headers={"X-AIClassroom-Token": "selftest"})
        if state.status_code != 200:
            print(f"FALLO: /api/state devolvió {state.status_code}", file=sys.stderr)
            return 1
        payload = state.json()

    inventory = probe_devices()
    if require_audio and not inventory.library_available:
        print(f"FALLO: la biblioteca de audio no está empaquetada: {inventory.error}",
              file=sys.stderr)
        return 1

    secrets_ok, secrets_detail = secret_store_works()
    if not secrets_ok:
        print(f"FALLO: el almacén de claves no funciona: {secrets_detail}", file=sys.stderr)
        return 1

    wakeword_ok, wakeword_detail = wakeword_engine_available()
    if require_wakeword and not wakeword_ok:
        print(f"FALLO: el motor de palabra clave no está empaquetado: {wakeword_detail}",
              file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "version": health.json()["version"],
                "state": payload["state"],
                "data_dir": str(paths.root),
                "frozen": bool(getattr(sys, "frozen", False)),
                "python": sys.version.split()[0],
                "audio_library": inventory.library_available,
                "audio_error": inventory.error,
                "audio_inputs": len(inventory.inputs),
                "audio_outputs": len(inventory.outputs),
                "wakeword_engine": wakeword_ok,
                "secret_store": secrets_ok,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    configure_logging(arguments.verbose)

    root = default_data_root() if arguments.data_dir is None else arguments.data_dir
    paths = DataPaths(root=root).ensure()

    if arguments.selftest:
        return selftest(
            paths,
            require_audio=arguments.require_audio,
            require_wakeword=arguments.require_wakeword,
        )

    import uvicorn

    from .config.store import SettingsStore

    port = arguments.port or find_free_port()
    store = SettingsStore(paths=paths)
    context = build_context(store=store, token=arguments.token)
    application = create_app(context)

    # The shell waits for this line before showing the window.
    print(
        READY_PREFIX + json.dumps({"port": port, "token": context.token}),
        flush=True,
    )
    logger.info("Backend escuchando en http://%s:%s", LOOPBACK, port)

    uvicorn.run(application, host=LOOPBACK, port=port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
