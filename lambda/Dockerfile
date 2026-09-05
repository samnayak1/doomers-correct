# The AWS Lambda Python base image is glibc-based (Amazon Linux), which is what
# tls-client needs - see the long note in worker/Dockerfile about why musl cannot
# load it at all.
FROM public.ecr.aws/lambda/python:3.12

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && SP="$(python -c 'import site;print(site.getsitepackages()[0])')" \
 && rm -f "$SP/tls_client/dependencies/"*.dll "$SP/tls_client/dependencies/"*.dylib \
 && find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} + || true

COPY common/       ${LAMBDA_TASK_ROOT}/common/
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
