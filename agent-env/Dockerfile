FROM registry.access.redhat.com/ubi9/ubi:9.7-1776834047

ARG AWS_CLI_VERSION=2.34.19

RUN curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo -o /etc/yum.repos.d/gh-cli.repo && \
    dnf module enable -y nodejs:20 && \
    dnf install -y \
        git \
        gh \
        make \
        jq \
        unzip \
        openssl \
        openssh-clients \
        podman \
        fuse-overlayfs \
        nodejs \
        npm \
    && dnf clean all

# AWS CLI v2
RUN arch=$(uname -m) && \
    case "${arch}" in \
        x86_64)  AWS_ARCH="x86_64" ;; \
        aarch64) AWS_ARCH="aarch64" ;; \
    esac && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip" -o "awscliv2.zip" && \
    unzip -qo awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip ./aws

# Claude CLI
RUN npm install -g @anthropic-ai/claude-code@2.1.121

# Agent user and workspace
RUN useradd -m -d /home/agent -s /bin/bash agent && \
    mkdir -p /workspace && \
    chown agent:agent /workspace && \
    mkdir -p /home/agent/.aws && \
    chown agent:agent /home/agent/.aws

# Podman storage config for rootless operation in privileged mode
RUN mkdir -p /home/agent/.config/containers && \
    echo '[storage]' > /home/agent/.config/containers/storage.conf && \
    echo 'driver = "overlay"' >> /home/agent/.config/containers/storage.conf && \
    echo '[storage.options.overlay]' >> /home/agent/.config/containers/storage.conf && \
    echo 'mount_program = "/usr/bin/fuse-overlayfs"' >> /home/agent/.config/containers/storage.conf && \
    chown -R agent:agent /home/agent/.config

RUN chmod -R a+w /etc/pki/ca-trust

COPY --chown=agent:agent config/CLAUDE.md /workspace/CLAUDE.md

RUN echo 'export PS1="[agent@\h \W]\$ "' >> /home/agent/.bashrc && \
    echo 'cd /workspace' >> /home/agent/.bashrc

ENV HOME=/home/agent

USER agent
WORKDIR /workspace

ENTRYPOINT ["sleep", "infinity"]
