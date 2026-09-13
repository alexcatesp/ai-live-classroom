# PyInstaller specification for the portable backend (spec section 17, D-09).
#
# Produces a single directory whose executable carries its own Python, so the
# school PC needs neither Python nor administrator rights (spec section 4.1).
# The folder form is used rather than one-file because one-file unpacks to the
# temporary directory on every launch, which is slow and is exactly the kind of
# behaviour a locked-down school profile tends to block.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# PortAudio, which sounddevice needs in order to open the microphone (D-05), is
# collected by the sounddevice hook in pyinstaller-hooks-contrib. Rather than
# trust that silently, `aiclassroom-backend --selftest --require-audio` fails
# the build when the packaged executable cannot load the audio library.

hidden_imports = [
    # uvicorn resolves these by name at runtime, so static analysis misses them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # Only what inference needs. `openwakeword.train` and `openwakeword.data`
    # are the training pipeline (torch, speechbrain) and never run here; the
    # model is trained separately with scripts/train_wakeword.py.
    "openwakeword",
    "openwakeword.model",
    "openwakeword.utils",
    "openwakeword.vad",
    # Imported unconditionally by openwakeword/__init__.py, which is why scipy
    # and scikit-learn end up in the bundle even though no custom verifier is
    # ever loaded.
    "openwakeword.custom_verifier_model",
    # "Entrenar con mi voz" (D-12): the classifier, the split and the exporter.
    # Imported inside functions, which static analysis does not always follow;
    # --selftest --require-training proves they made it.
    "sklearn.neural_network",
    "sklearn.model_selection",
    "onnx",
    "onnx.helper",
    "onnx.numpy_helper",
    "onnx.checker",
]

# openWakeWord's own package data. The two shared models it would otherwise
# download are NOT bundled here: they ship in data/models/openwakeword/, where
# they can be replaced without rebuilding the executable
# (scripts/fetch_wakeword_runtime.py).
datas = collect_data_files("openwakeword", excludes=["**/resources/models/*"])

analysis = Analysis(
    ["entrypoint.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    # Trimming what a portable folder on a USB stick does not need (risk R-4).
    # The detector is built with inference_framework="onnx" (D-04), so the
    # TensorFlow Lite runtime is never reached; the training stack is only used
    # by scripts/train_wakeword.py on a build machine.
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "pytest",
        "torch",
        "torchaudio",
        "tensorflow",
        "tflite_runtime",
        "speechbrain",
        "openwakeword.train",
        "openwakeword.data",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="aiclassroom-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # the shell reads the handshake line from stdout
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="backend",
)
