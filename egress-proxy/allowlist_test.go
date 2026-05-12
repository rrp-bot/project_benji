package main

import "testing"

func TestAllowlistExactMatch(t *testing.T) {
	al := defaultAllowlist()

	tests := []struct {
		host    string
		allowed bool
	}{
		{"github.com", true},
		{"api.github.com", true},
		{"evil.github.com", false},
		{"notgithub.com", false},
	}

	for _, tt := range tests {
		if got := al.Allowed(tt.host); got != tt.allowed {
			t.Errorf("Allowed(%q) = %v, want %v", tt.host, got, tt.allowed)
		}
	}
}

func TestAllowlistSuffixMatch(t *testing.T) {
	al := defaultAllowlist()

	tests := []struct {
		host    string
		allowed bool
	}{
		{"us-east1-aiplatform.googleapis.com", true},
		{"oauth2.googleapis.com", true},
		{"storage.googleapis.com", true},
		{"googleapis.com", false},
		{"evil-googleapis.com", false},
		{"notgoogleapis.com", false},
	}

	for _, tt := range tests {
		if got := al.Allowed(tt.host); got != tt.allowed {
			t.Errorf("Allowed(%q) = %v, want %v", tt.host, got, tt.allowed)
		}
	}
}

func TestAllowlistTrailingDot(t *testing.T) {
	al := defaultAllowlist()

	if !al.Allowed("github.com.") {
		t.Error("Allowed(\"github.com.\") should be true (trailing dot stripped)")
	}
	if !al.Allowed("us-east1-aiplatform.googleapis.com.") {
		t.Error("Allowed(\"us-east1-aiplatform.googleapis.com.\") should be true")
	}
}

func TestAllowlistRejectsUnknown(t *testing.T) {
	al := defaultAllowlist()

	rejected := []string{
		"example.com",
		"evil.com",
		"attacker.io",
		"",
	}
	for _, host := range rejected {
		if al.Allowed(host) {
			t.Errorf("Allowed(%q) should be false", host)
		}
	}
}
