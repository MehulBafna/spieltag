# The pipeline as a Lambda container image. Python matches the interpreter the tests run on.
FROM public.ecr.aws/lambda/python:3.13

# Dependencies are pinned in constraints.txt, so the image runs the versions the tests ran on.
COPY pyproject.toml README.md constraints.txt ${LAMBDA_TASK_ROOT}/
COPY src ${LAMBDA_TASK_ROOT}/src
RUN pip install --no-cache-dir -c ${LAMBDA_TASK_ROOT}/constraints.txt ${LAMBDA_TASK_ROOT} \
    && rm -rf ${LAMBDA_TASK_ROOT}/src ${LAMBDA_TASK_ROOT}/build

CMD ["spieltag.handler.lambda_handler"]
