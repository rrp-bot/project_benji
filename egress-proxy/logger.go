package main

import (
	"bufio"
	"encoding/json"
	"io"
	"sync"
	"time"
)

type logEntry struct {
	Timestamp      string `json:"timestamp"`
	SourceIP       string `json:"source_ip"`
	Destination    string `json:"destination"`
	Method         string `json:"method"`
	Path           string `json:"path,omitempty"`
	Status         int    `json:"status"`
	DurationMs     int    `json:"duration_ms"`
	CredentialType string `json:"credential_type"`
}

type jsonLogger struct {
	mu  sync.Mutex
	enc *json.Encoder
}

func newJSONLogger(w io.Writer) *jsonLogger {
	return &jsonLogger{enc: json.NewEncoder(w)}
}

func (l *jsonLogger) Log(entry logEntry) {
	if entry.Timestamp == "" {
		entry.Timestamp = time.Now().UTC().Format(time.RFC3339)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.enc.Encode(entry)
}

func newBufioReader(r io.Reader) *bufio.Reader {
	return bufio.NewReader(r)
}
