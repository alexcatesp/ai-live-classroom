# PyInstaller specification for the portable backend (spec section 17, D-09).
#
# Produces a single directory whose executable carries its own Python, so the
# school PC needs neither Python nor administrator rights (spec section 4.1).
# The folder form is used rather than one-file because one-file unpacks to the
# temporary directory on every launch, which is slow and is exactly the kind of
# behaviour a locked-down school profile tends to block.

block_cipher = None

# PortAudio, which sounddevice needs in order to open the microphone (D-05), is
# collected by the sounddevice hook in pyinstaller-hooks-contrib. Rather than
# trust that silently, `aiclassroom-backend --selftest --require-audio` fails
# the build when the packaged executable cannot load the audio library.

hidden_imports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

analysis = Analysis(
    ["entrypoint.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    # openWakeWord is installed separately in the CI so its model files land in
    # data/models rather than inside the executable, where they could not be
    # replaced without a rebuild.
    excludes=["tkinter", "matplotlib", "PIL", "pytest"],
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
