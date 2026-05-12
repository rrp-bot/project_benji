FROM registry.access.redhat.com/ubi9/ubi:9.7-1776834047

RUN dnf install -y python3.12 python3.12-pip && dnf clean all

RUN pip3.12 install --target /var/task awslambdaric==4.0.0 boto3==1.42.97

COPY provisioner/handler.py /var/task/
COPY cloudformation/agent-environment.yaml /var/task/agent-environment.yaml

WORKDIR /var/task

ENTRYPOINT ["python3.12", "-m", "awslambdaric"]
CMD ["handler.lambda_handler"]
