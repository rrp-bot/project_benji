package main

import (
	"crypto/tls"
	"crypto/x509"
	"testing"
)

func TestNewCA(t *testing.T) {
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA() error: %v", err)
	}

	if ca.cert == nil {
		t.Fatal("CA cert is nil")
	}
	if !ca.cert.IsCA {
		t.Error("CA cert IsCA should be true")
	}
	if len(ca.certPEM) == 0 {
		t.Error("CA certPEM is empty")
	}
}

func TestTLSCertForHost(t *testing.T) {
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA() error: %v", err)
	}

	cert, err := ca.TLSCertForHost("github.com")
	if err != nil {
		t.Fatalf("TLSCertForHost() error: %v", err)
	}

	parsed, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		t.Fatalf("ParseCertificate error: %v", err)
	}

	if parsed.Subject.CommonName != "github.com" {
		t.Errorf("CN = %q, want github.com", parsed.Subject.CommonName)
	}

	pool := x509.NewCertPool()
	pool.AddCert(ca.cert)
	if _, err := parsed.Verify(x509.VerifyOptions{Roots: pool}); err != nil {
		t.Errorf("cert does not verify against CA: %v", err)
	}
}

func TestTLSCertCaching(t *testing.T) {
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA() error: %v", err)
	}

	cert1, _ := ca.TLSCertForHost("github.com")
	cert2, _ := ca.TLSCertForHost("github.com")

	if cert1 != cert2 {
		t.Error("expected same cert pointer from cache")
	}
}

func TestTLSCertDifferentHosts(t *testing.T) {
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA() error: %v", err)
	}

	cert1, _ := ca.TLSCertForHost("github.com")
	cert2, _ := ca.TLSCertForHost("api.github.com")

	if cert1 == cert2 {
		t.Error("expected different certs for different hosts")
	}
}

func TestTLSCertUsable(t *testing.T) {
	ca, err := newCA()
	if err != nil {
		t.Fatalf("newCA() error: %v", err)
	}

	cert, err := ca.TLSCertForHost("example.com")
	if err != nil {
		t.Fatalf("TLSCertForHost() error: %v", err)
	}

	tlsCfg := &tls.Config{
		Certificates: []tls.Certificate{*cert},
	}
	if len(tlsCfg.Certificates) != 1 {
		t.Error("expected exactly 1 certificate in TLS config")
	}
}
