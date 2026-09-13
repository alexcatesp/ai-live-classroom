#!/usr/bin/env bash
# End-to-end validation of Phase 0 on a Linux development machine.
#
# Runs everything that can be checked without a Windows PC and without a
# classroom: the unit suites, the packaged executable, the trained detector on
# real synthesized speech, and the portable folder layout. What it cannot do is
# open a microphone or speak to the API -- those are the two things that have
# to be tried in the institute.
#
#     ./scripts/validate-phase0.sh
#
# Set MODELS to reuse models you already have; otherwise they are fetched and
# the detector is trained, which takes a few minutes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS="${MODELS:-$ROOT/data/models}"
AUDIO="${AUDIO:-/tmp/aiclassroom-validacion/audio}"
PORTABLE="${PORTABLE:-/tmp/aiclassroom-validacion/portable}"
PY="$ROOT/backend/.venv/bin/python"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
fail() { printf '\033[1;31mFALLO: %s\033[0m\n' "$1" >&2; exit 1; }

[ -x "$PY" ] || fail "no hay entorno en backend/.venv (python -m venv backend/.venv)"

step "1/8  Tests del backend"
(cd "$ROOT/backend" && "$PY" -m pytest -q)

step "2/8  Lint del backend"
(cd "$ROOT/backend" && "$PY" -m ruff check src tests)

step "3/8  Tests y build del frontend"
(cd "$ROOT/frontend" && npm run test --silent && npm run build --silent)

step "4/8  Modelos del detector"
if [ ! -f "$MODELS/openwakeword/melspectrogram.onnx" ]; then
    "$PY" "$ROOT/scripts/fetch_wakeword_runtime.py" --output "$MODELS"
fi
if [ ! -f "$MODELS/oye_chat.onnx" ]; then
    echo "Entrenando el detector (unos minutos)..."
    "$PY" "$ROOT/scripts/train_wakeword.py" --phrase "Oye Chat" --models "$MODELS"
fi

step "5/8  Empaquetado del backend"
(cd "$ROOT/backend" && "$PY" -m PyInstaller aiclassroom-backend.spec --noconfirm --clean --log-level ERROR)
"$ROOT/backend/dist/backend/aiclassroom-backend" --selftest --require-wakeword > /dev/null \
    || fail "el ejecutable empaquetado no supera el autotest"

step "6/8  Carpeta portable"
rm -rf "$PORTABLE"
mkdir -p "$PORTABLE/AI-Classroom-Live/runtime"
cp -r "$ROOT/backend/dist/backend" "$PORTABLE/AI-Classroom-Live/runtime/"
for folder in config materials sessions metrics models; do
    mkdir -p "$PORTABLE/AI-Classroom-Live/data/$folder"
done
cp -r "$MODELS/openwakeword" "$PORTABLE/AI-Classroom-Live/data/models/"
cp "$MODELS/oye_chat.onnx" "$PORTABLE/AI-Classroom-Live/data/models/"
cp "$ROOT/docs/README-portable.txt" "$PORTABLE/AI-Classroom-Live/README.txt"

REPORT=$("$PORTABLE/AI-Classroom-Live/runtime/backend/aiclassroom-backend" --selftest --require-wakeword)
echo "$REPORT"
EXPECTED="$PORTABLE/AI-Classroom-Live/data"
echo "$REPORT" | grep -q "\"data_dir\": \"$EXPECTED\"" \
    || fail "el backend no resuelve data/ dentro de la carpeta portable"

(cd "$PORTABLE/AI-Classroom-Live" \
    && find . -type f ! -name SHA256SUMS.txt -print0 | xargs -0 sha256sum | sed 's| \./| |' > SHA256SUMS.txt \
    && sha256sum -c SHA256SUMS.txt --quiet) || fail "las sumas de verificación no cuadran"
echo "Sumas de verificación: $(wc -l < "$PORTABLE/AI-Classroom-Live/SHA256SUMS.txt") archivos."

step "7/8  El detector sobre voz real"
[ -d "$AUDIO/positives" ] || "$PY" "$ROOT/scripts/make_test_audio.py" --output "$AUDIO" --phrase "Oye Chat"
(cd "$ROOT/backend" && AICLASSROOM_TEST_MODELS="$MODELS" "$PY" -m pytest tests/test_end_to_end.py -q)

step "8/8  Falsos positivos medidos"
"$PY" "$ROOT/scripts/measure_wakeword.py" --models "$MODELS" --phrase "Oye Chat" \
    --negatives "$AUDIO/negatives" --positives "$AUDIO/positives"

printf '\n\033[1;32mFase 0 validada en esta máquina.\033[0m\n'
cat <<'PENDING'

Lo que esta máquina NO puede comprobar, y hay que hacer en el instituto:

  - Abrir un micrófono y unos altavoces de verdad (aquí no hay tarjeta de sonido).
  - Hablar con la API Realtime (aquí el proxy la bloquea).
  - Ejecutar el .exe en Windows sin permisos de administrador.
  - Medir los falsos positivos con una clase grabada, no con voz sintética.
PENDING
