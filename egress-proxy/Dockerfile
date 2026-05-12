FROM registry.access.redhat.com/ubi9/go-toolset:1.25.9-1777043046 AS builder

WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -o /tmp/proxy .

FROM registry.access.redhat.com/ubi9/ubi-minimal:9.7-1776833838

COPY --from=builder /tmp/proxy /proxy

EXPOSE 3128

ENTRYPOINT ["/proxy"]
