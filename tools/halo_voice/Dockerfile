# tools/halo_voice.py's master and qa steps: NumPy, SciPy and soundfile;
# faster-whisper (CTranslate2, on the CPU) for the transcription checks, and
# Resemblyzer (a GE2E speaker encoder, CPU PyTorch) for how alike a take's
# voice is to the clips'. Apart from the GPU image that generates the takes.
#
#   docker build -t halo-voice-tools tools/halo_voice
#   docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/tools:/tools:ro" \
#       -v "$JOB:/job" -v "$EXTRACTED:/extracted:ro" -v halo-voice-models:/models \
#       halo-voice-tools /tools/halo_voice.py qa --job /job --originals /extracted --models /models
FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 HF_HOME=/models NUMBA_CACHE_DIR=/tmp
RUN python -m pip install --no-cache-dir numpy scipy soundfile requests faster-whisper==1.1.1 && \
    python -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    python -m pip install --no-cache-dir librosa webrtcvad-wheels && \
    python -m pip install --no-cache-dir --no-deps resemblyzer==0.1.4
ENTRYPOINT ["python"]
