# Security Policy

## Supported Versions

| Version | Supported |
| --- | --- |
| Latest tagged release (see [Releases](https://github.com/lasertsole/sherry_agent/releases)) | :white_check_mark: |
| `main` (development branch) | :white_check_mark: |
| Older tagged releases | :x: |

Security fixes land on `main` and in the next tagged release. If you run a
packaged desktop build, update to the newest release before reporting an issue
that may already be fixed.

## Reporting a Vulnerability

Please report suspected vulnerabilities **privately** through GitHub's
[Private Vulnerability Reporting](https://github.com/lasertsole/sherry_agent/security/advisories/new).
Do not open a public issue, discussion, or pull request that describes an
unfixed vulnerability.

A useful report includes:

- the affected release tag or commit;
- the affected component (backend, web UI, desktop shell, sandbox/tool layer,
  or a bundled skill);
- a minimal reproduction and the observed impact;
- relevant logs with API keys, tokens, `.models` / `.network` contents, and user
  data removed.

Reports are handled on a best-effort basis. This is a single-maintainer project;
there is no bug bounty. We will investigate, keep you informed of remediation,
and coordinate disclosure timing with you.

> 中文：请通过 GitHub 的私密漏洞报告入口提交，不要公开 Issue 或讨论区披露尚未修复的漏洞，并先移除日志中的密钥与个人数据。

## Scope

**In scope:** vulnerabilities in this repository's own code and shipped
configuration, including the agent runtime, tool sandbox boundaries, HTTP/WS
gateway, login system, web client, desktop shell, and packaged backend.

**Out of scope:** issues that require a compromised local machine or physical
access, social engineering, denial-of-service against a deployment you do not
own, and vulnerabilities in third-party model providers or services. Public
dependency advisories are tracked through Dependabot; report one here only if
this project uses the dependency in a way that creates additional impact.

## Safe Harbor

Good-faith security research is welcome when it is limited to your own
deployment. Do not access other users' data, degrade a service others rely on,
or publish details of an unfixed vulnerability. We will not pursue action
against researchers who follow this policy.
