FROM python:3.11-slim

ARG VERSION
ARG VCS_REF

LABEL org.opencontainers.image.source="https://github.com/DecentraLabsCom/FMU-Executor" \
      org.opencontainers.image.description="Shared DecentraLabs FMI 2/3 Co-Simulation executor" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FMU_EXECUTOR_HOST=0.0.0.0 \
    FMU_EXECUTOR_PORT=8091

WORKDIR /opt/fmu-executor

COPY pyproject.toml README.md VERSION ./
COPY app ./app

RUN python -m pip install --no-cache-dir .

EXPOSE 8091

CMD ["python", "-m", "app"]
