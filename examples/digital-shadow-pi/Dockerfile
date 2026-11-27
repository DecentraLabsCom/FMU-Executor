FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/fmu-executor

COPY examples/digital-shadow-pi/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && groupadd --system --gid 10001 fmu-demo \
    && useradd --system --uid 10001 --gid fmu-demo --home-dir /nonexistent --shell /usr/sbin/nologin fmu-demo

COPY app ./app
COPY VERSION .
COPY examples/reference-fmus/StateSpace.fmu ./examples/reference-fmus/StateSpace.fmu
COPY examples/reference-fmus/Feedthrough.fmu ./examples/reference-fmus/Feedthrough.fmu
COPY examples/digital-shadow-pi/run_demo.py ./examples/digital-shadow-pi/run_demo.py

USER 10001:10001

ENTRYPOINT ["python", "/opt/fmu-executor/examples/digital-shadow-pi/run_demo.py"]
