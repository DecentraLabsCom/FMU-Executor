FROM public.ecr.aws/docker/library/python:3.11-slim

ARG VERSION
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/DecentraLabsCom/FMU-Executor" \
      org.opencontainers.image.description="Shared DecentraLabs FMI 2/3 Co-Simulation executor" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FMU_EXECUTOR_COMMIT=${VCS_REF} \
    FMU_EXECUTOR_HOST=0.0.0.0 \
    FMU_EXECUTOR_PORT=8091

WORKDIR /opt/fmu-executor

COPY pyproject.toml README.md VERSION ./
COPY app ./app

RUN python -m pip install --no-cache-dir .

# FMU code is untrusted native code. Keep the container supervisor and its
# workers under a dedicated non-root identity by default.
RUN groupadd --system --gid 10001 fmu-executor \
    && useradd --system --uid 10001 --gid fmu-executor --home-dir /nonexistent --shell /usr/sbin/nologin fmu-executor \
    && install -d -o 10001 -g 10001 /opt/fmu-executor/fmu-data/.tmp /opt/fmu-executor/state

USER 10001:10001

EXPOSE 8091

CMD ["python", "-m", "app"]
