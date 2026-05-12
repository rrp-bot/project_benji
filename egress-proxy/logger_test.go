package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestLoggerOutput(t *testing.T) {
	var buf bytes.Buffer
	logger := newJSONLogger(&buf)

	logger.Log(logEntry{
		Timestamp:      "2026-04-24T10:15:30Z",
		SourceIP:       "10.0.1.42",
		Destination:    "api.github.com:443",
		Method:         "GET",
		Path:           "/repos/test/repo",
		Status:         200,
		DurationMs:     42,
		CredentialType: "github",
	})

	var entry logEntry
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("failed to parse log entry: %v", err)
	}

	if entry.SourceIP != "10.0.1.42" {
		t.Errorf("source_ip = %q, want 10.0.1.42", entry.SourceIP)
	}
	if entry.Destination != "api.github.com:443" {
		t.Errorf("destination = %q, want api.github.com:443", entry.Destination)
	}
	if entry.Status != 200 {
		t.Errorf("status = %d, want 200", entry.Status)
	}
	if entry.CredentialType != "github" {
		t.Errorf("credential_type = %q, want github", entry.CredentialType)
	}
}

func TestLoggerNeverLogsCredentials(t *testing.T) {
	var buf bytes.Buffer
	logger := newJSONLogger(&buf)

	logger.Log(logEntry{
		SourceIP:       "10.0.1.42",
		Destination:    "api.github.com:443",
		Method:         "GET",
		Path:           "/repos/test/repo",
		Status:         200,
		DurationMs:     42,
		CredentialType: "github",
	})

	output := buf.String()
	sensitivePatterns := []string{
		"Bearer",
		"ghp_",
		"Authorization",
		"token",
		"secret",
		"password",
	}

	for _, p := range sensitivePatterns {
		if strings.Contains(strings.ToLower(output), strings.ToLower(p)) {
			t.Errorf("log output contains sensitive pattern %q: %s", p, output)
		}
	}
}

func TestLoggerSetsTimestamp(t *testing.T) {
	var buf bytes.Buffer
	logger := newJSONLogger(&buf)

	logger.Log(logEntry{
		SourceIP:    "10.0.1.42",
		Destination: "api.github.com:443",
		Method:      "GET",
		Status:      200,
	})

	var entry logEntry
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("failed to parse log entry: %v", err)
	}

	if entry.Timestamp == "" {
		t.Error("timestamp should be auto-set when empty")
	}
}
