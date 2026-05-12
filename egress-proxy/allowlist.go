package main

import "strings"

type allowlist struct {
	exact  map[string]bool
	suffix []string
}

func defaultAllowlist() *allowlist {
	return &allowlist{
		exact: map[string]bool{
			"github.com":                true,
			"api.github.com":            true,
			"codeload.github.com":       true,
			"registry.npmjs.org":         true,
			"api.anthropic.com":          true,
			"get.helm.sh":               true,
			"dl.k8s.io":                 true,
			"cli.github.com":            true,
			"pypi.org":                  true,
			"files.pythonhosted.org":    true,
			"astral.sh":                 true,
			"mirror.openshift.com":      true,
		},
		suffix: []string{
			".googleapis.com",
			".githubusercontent.com",
			".redhat.com",
			".quay.io",
			".hashicorp.com",
			".terraform.io",
			".amazonaws.com",
			".azurefd.net",
			".dl.k8s.io",
			".astral.sh",
			".pypi.org",
			".pythonhosted.org",
		},
	}
}

func (a *allowlist) Allowed(host string) bool {
	host = strings.TrimSuffix(host, ".")
	if a.exact[host] {
		return true
	}
	for _, s := range a.suffix {
		if strings.HasSuffix(host, s) {
			return true
		}
	}
	return false
}
