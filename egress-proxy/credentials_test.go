package main

import (
	"net/http"
	"testing"
)

func TestIsGitHubHost(t *testing.T) {
	ci := &credentialInjector{}

	tests := []struct {
		host string
		want bool
	}{
		{"github.com", true},
		{"api.github.com", true},
		{"evil.github.com", false},
		{"notgithub.com", false},
	}

	for _, tt := range tests {
		if got := ci.isGitHubHost(tt.host); got != tt.want {
			t.Errorf("isGitHubHost(%q) = %v, want %v", tt.host, got, tt.want)
		}
	}
}

func TestIsGoogleHost(t *testing.T) {
	ci := &credentialInjector{}

	tests := []struct {
		host string
		want bool
	}{
		{"us-east1-aiplatform.googleapis.com", true},
		{"oauth2.googleapis.com", true},
		{"storage.googleapis.com", true},
		{"googleapis.com", false},
		{"evil-googleapis.com", false},
	}

	for _, tt := range tests {
		if got := ci.isGoogleHost(tt.host); got != tt.want {
			t.Errorf("isGoogleHost(%q) = %v, want %v", tt.host, got, tt.want)
		}
	}
}

func TestInjectGitHubCredentials(t *testing.T) {
	ci := &credentialInjector{githubToken: "ghp_test123"}

	req, _ := http.NewRequest("GET", "https://api.github.com/repos/test", nil)
	credType := ci.InjectCredentials(req, "api.github.com")

	if credType != "github" {
		t.Errorf("credential type = %q, want github", credType)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer ghp_test123" {
		t.Errorf("Authorization = %q, want Bearer ghp_test123", got)
	}
}

func TestInjectReplacesExistingAuth(t *testing.T) {
	ci := &credentialInjector{githubToken: "ghp_real"}

	req, _ := http.NewRequest("GET", "https://api.github.com/repos/test", nil)
	req.Header.Set("Authorization", "Bearer ghp_old")
	ci.InjectCredentials(req, "api.github.com")

	if got := req.Header.Get("Authorization"); got != "Bearer ghp_real" {
		t.Errorf("Authorization = %q, want Bearer ghp_real", got)
	}
}

func TestInjectNoneForUnknownHost(t *testing.T) {
	ci := &credentialInjector{}

	req, _ := http.NewRequest("GET", "https://example.com/test", nil)
	credType := ci.InjectCredentials(req, "example.com")

	if credType != "none" {
		t.Errorf("credential type = %q, want none", credType)
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization should be empty, got %q", got)
	}
}
