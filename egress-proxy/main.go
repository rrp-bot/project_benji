package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	healthCheck := flag.Bool("health-check", false, "Run health check and exit")
	flag.Parse()

	bindAddr := os.Getenv("BIND_ADDR")
	if bindAddr == "" {
		bindAddr = "0.0.0.0:3128"
	}

	if *healthCheck {
		if err := runHealthCheck(bindAddr); err != nil {
			fmt.Fprintf(os.Stderr, "health check failed: %v\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}

	githubToken := os.Getenv("GITHUB_TOKEN")
	gcpSAJSON := os.Getenv("GCP_SA_JSON")
	if githubToken == "" {
		log.Fatal("GITHUB_TOKEN environment variable is required")
	}

	ca, err := newCA()
	if err != nil {
		log.Fatalf("failed to generate CA: %v", err)
	}

	if err := ca.WriteCertFile("/tmp/proxy-ca.crt"); err != nil {
		log.Fatalf("failed to write CA cert: %v", err)
	}

	creds, err := newCredentialInjector(githubToken, gcpSAJSON)
	if err != nil {
		log.Printf("WARNING: GCP credentials not configured, Google API auth disabled: %v", err)
		creds = &credentialInjector{githubToken: githubToken}
	}

	logger := newJSONLogger(os.Stdout)

	proxy := &proxyHandler{
		ca:        ca,
		creds:     creds,
		logger:    logger,
		allowlist: defaultAllowlist(),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		handleHealthz(w, r, creds)
	})

	server := &http.Server{
		Addr: bindAddr,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodConnect {
				proxy.handleConnect(w, r)
			} else if r.URL.Path == "/healthz" {
				handleHealthz(w, r, creds)
			} else if r.URL.Path == "/ca.crt" {
				w.Header().Set("Content-Type", "application/x-pem-file")
				w.Write(ca.CertPEM())
			} else {
				http.Error(w, "only CONNECT method is supported", http.StatusMethodNotAllowed)
			}
		}),
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("egress proxy listening on %s", bindAddr)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	server.Shutdown(shutdownCtx)
}

func runHealthCheck(bindAddr string) error {
	resp, err := http.Get(fmt.Sprintf("http://localhost:%s/healthz", portFromAddr(bindAddr)))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unhealthy: status %d", resp.StatusCode)
	}
	return nil
}

func portFromAddr(addr string) string {
	for i := len(addr) - 1; i >= 0; i-- {
		if addr[i] == ':' {
			return addr[i+1:]
		}
	}
	return addr
}
