package main

import (
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

type proxyHandler struct {
	ca        *CA
	creds     *credentialInjector
	logger    *jsonLogger
	allowlist *allowlist
}

func (p *proxyHandler) handleConnect(w http.ResponseWriter, r *http.Request) {
	host, port := hostPort(r.Host)

	if !p.allowlist.Allowed(host) {
		p.logger.Log(logEntry{
			SourceIP:    r.RemoteAddr,
			Destination: r.Host,
			Method:      "CONNECT",
			Status:      http.StatusForbidden,
		})
		http.Error(w, "Forbidden: destination not in allowlist", http.StatusForbidden)
		return
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijacking not supported", http.StatusInternalServerError)
		return
	}

	clientConn, _, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer clientConn.Close()

	fmt.Fprintf(clientConn, "HTTP/1.1 200 Connection Established\r\n\r\n")

	tlsCert, err := p.ca.TLSCertForHost(host)
	if err != nil {
		p.logger.Log(logEntry{
			SourceIP:    r.RemoteAddr,
			Destination: r.Host,
			Method:      "CONNECT",
			Status:      http.StatusBadGateway,
		})
		return
	}

	tlsConfig := &tls.Config{
		Certificates: []tls.Certificate{*tlsCert},
	}
	tlsConn := tls.Server(clientConn, tlsConfig)
	if err := tlsConn.Handshake(); err != nil {
		return
	}
	defer tlsConn.Close()

	upstream := fmt.Sprintf("%s:%s", host, port)
	p.proxyRequests(tlsConn, upstream, r.RemoteAddr, host)
}

func (p *proxyHandler) proxyRequests(clientTLS *tls.Conn, upstream, sourceAddr, host string) {
	tr := &http.Transport{
		TLSClientConfig:    &tls.Config{ServerName: host},
		DisableCompression: true,
	}
	defer tr.CloseIdleConnections()

	br := newBufioReader(clientTLS)
	for {
		req, err := http.ReadRequest(br)
		if err != nil {
			return
		}

		start := time.Now()

		credType := p.creds.InjectCredentials(req, host)

		req.URL.Scheme = "https"
		req.URL.Host = upstream
		req.RequestURI = ""

		resp, err := tr.RoundTrip(req)
		if err != nil {
			writeErrorResponse(clientTLS, http.StatusBadGateway, "Bad Gateway: upstream request failed")
			p.logger.Log(logEntry{
				SourceIP:       sourceAddr,
				Destination:    upstream,
				Method:         req.Method,
				Path:           req.URL.Path,
				Status:         http.StatusBadGateway,
				DurationMs:     int(time.Since(start).Milliseconds()),
				CredentialType: credType,
			})
			return
		}

		p.logger.Log(logEntry{
			SourceIP:       sourceAddr,
			Destination:    upstream,
			Method:         req.Method,
			Path:           req.URL.Path,
			Status:         resp.StatusCode,
			DurationMs:     int(time.Since(start).Milliseconds()),
			CredentialType: credType,
		})

		resp.Write(clientTLS)
		resp.Body.Close()
	}
}

func hostPort(addr string) (string, string) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return addr, "443"
	}
	if port == "" {
		port = "443"
	}
	return host, port
}

func writeErrorResponse(w io.Writer, status int, body string) {
	resp := &http.Response{
		StatusCode: status,
		ProtoMajor: 1,
		ProtoMinor: 1,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
	}
	resp.Header.Set("Content-Type", "text/plain")
	resp.Write(w)
}
