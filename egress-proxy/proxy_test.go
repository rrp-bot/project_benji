package main

import (
	"bufio"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
)

func setupTestProxy(t *testing.T) (*proxyHandler, *CA) {
	t.Helper()
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA: %v", err)
	}

	creds := &credentialInjector{githubToken: "ghp_test_proxy"}

	logger := newJSONLogger(io.Discard)

	return &proxyHandler{
		ca:        ca,
		creds:     creds,
		logger:    logger,
		allowlist: defaultAllowlist(),
	}, ca
}

func TestConnectAllowlistReject(t *testing.T) {
	proxy, _ := setupTestProxy(t)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			proxy.handleConnect(w, r)
		}
	}))
	defer server.Close()

	conn, err := net.Dial("tcp", server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	fmt.Fprintf(conn, "CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n")

	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want 403", resp.StatusCode)
	}
}

func TestConnectAllowlistAllow(t *testing.T) {
	proxy, ca := setupTestProxy(t)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			proxy.handleConnect(w, r)
		}
	}))
	defer server.Close()

	conn, err := net.Dial("tcp", server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	fmt.Fprintf(conn, "CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}

	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca.CertPEM())

	tlsConn := tls.Client(conn, &tls.Config{
		ServerName: "api.github.com",
		RootCAs:    pool,
	})
	if err := tlsConn.Handshake(); err != nil {
		t.Fatalf("TLS handshake failed: %v", err)
	}
	defer tlsConn.Close()
}

func TestHostPort(t *testing.T) {
	tests := []struct {
		addr     string
		wantHost string
		wantPort string
	}{
		{"github.com:443", "github.com", "443"},
		{"github.com:8080", "github.com", "8080"},
		{"github.com", "github.com", "443"},
	}

	for _, tt := range tests {
		host, port := hostPort(tt.addr)
		if host != tt.wantHost || port != tt.wantPort {
			t.Errorf("hostPort(%q) = (%q, %q), want (%q, %q)",
				tt.addr, host, port, tt.wantHost, tt.wantPort)
		}
	}
}
