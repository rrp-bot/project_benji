package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

type credentialInjector struct {
	githubToken   string
	googleTokenSrc oauth2.TokenSource
}

func newCredentialInjector(githubToken, gcpSAJSON string) (*credentialInjector, error) {
	if gcpSAJSON == "" {
		return nil, fmt.Errorf("GCP_SA_JSON is empty")
	}

	creds, err := google.CredentialsFromJSONWithType(
		context.Background(),
		[]byte(gcpSAJSON),
		google.ServiceAccount,
		"https://www.googleapis.com/auth/cloud-platform",
	)
	if err != nil {
		return nil, fmt.Errorf("parse GCP credentials: %w", err)
	}

	return &credentialInjector{
		githubToken:    githubToken,
		googleTokenSrc: oauth2.ReuseTokenSource(nil, creds.TokenSource),
	}, nil
}

func (ci *credentialInjector) InjectCredentials(req *http.Request, host string) string {
	if ci.isGitHubHost(host) {
		if host == "github.com" {
			req.SetBasicAuth("x-access-token", ci.githubToken)
		} else {
			req.Header.Set("Authorization", "Bearer "+ci.githubToken)
		}
		return "github"
	}

	if ci.isGoogleHost(host) && ci.googleTokenSrc != nil {
		tok, err := ci.googleTokenSrc.Token()
		if err != nil {
			return "google"
		}
		req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
		return "google"
	}

	return "none"
}

func (ci *credentialInjector) isGitHubHost(host string) bool {
	return host == "github.com" || host == "api.github.com" || host == "codeload.github.com"
}

func (ci *credentialInjector) isGoogleHost(host string) bool {
	return strings.HasSuffix(host, ".googleapis.com")
}

func (ci *credentialInjector) CheckGitHub() error {
	req, err := http.NewRequest("GET", "https://api.github.com/rate_limit", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+ci.githubToken)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("github check failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("github check returned %d", resp.StatusCode)
	}
	return nil
}

func (ci *credentialInjector) CheckGoogle() error {
	if ci.googleTokenSrc == nil {
		return fmt.Errorf("GCP credentials not configured")
	}
	_, err := ci.googleTokenSrc.Token()
	if err != nil {
		return fmt.Errorf("google token check failed: %w", err)
	}
	return nil
}

type healthResult struct {
	Status           string `json:"status"`
	GitHubTokenValid bool   `json:"github_token_valid"`
	GoogleTokenValid bool   `json:"google_token_valid"`
	Error            string `json:"error,omitempty"`
}

func handleHealthz(w http.ResponseWriter, r *http.Request, creds *credentialInjector) {
	var (
		wg           sync.WaitGroup
		githubErr    error
		googleErr    error
	)

	wg.Add(2)
	go func() {
		defer wg.Done()
		githubErr = creds.CheckGitHub()
	}()
	go func() {
		defer wg.Done()
		googleErr = creds.CheckGoogle()
	}()
	wg.Wait()

	result := healthResult{
		Status:           "ok",
		GitHubTokenValid: githubErr == nil,
		GoogleTokenValid: googleErr == nil,
	}

	status := http.StatusOK
	if githubErr != nil || googleErr != nil {
		result.Status = "degraded"
		var errs []string
		if githubErr != nil {
			errs = append(errs, githubErr.Error())
		}
		if googleErr != nil {
			errs = append(errs, googleErr.Error())
		}
		result.Error = strings.Join(errs, "; ")
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(result)
}
