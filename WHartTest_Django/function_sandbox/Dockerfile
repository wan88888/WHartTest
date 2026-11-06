ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}
RUN python -c "import ctypes; ctypes.CDLL('libseccomp.so.2')" || \
    (apt-get update && apt-get install -y --no-install-recommends libseccomp2 && rm -rf /var/lib/apt/lists/*)
RUN install -d -o 65534 -g 65534 -m 0755 /run/whart-functions
WORKDIR /sandbox
COPY server.py worker.py ./
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/sandbox/server.py"]
