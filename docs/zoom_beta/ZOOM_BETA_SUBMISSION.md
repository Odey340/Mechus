# Zoom App Beta – Compliance Submission Guide

This folder contains supporting evidence and policies for Zoom’s **Security and Privacy Compliance Review** when applying for **Beta** status. Use this as a checklist and reference when resubmitting your application.

---

## What Zoom Requires (Summary)

| Requirement | Status | Document / Action |
|-------------|--------|--------------------|
| **Evidence of SSDLC** | ✅ | [SSDLC.md](SSDLC.md) |
| **SAST & DAST scan results** | ⚠️ You provide | Run tools (see below); attach reports/summaries |
| **Privacy Policy** | ✅ | [PRIVACY_POLICY.md](PRIVACY_POLICY.md) – host at a public URL |
| **TLS 1.2 or higher** | ✅ | See [TLS 1.2+](#tls-12-or-higher) below |
| **Any three of:** | ✅ | See [Optional three](#any-three-of-the-following) below |

### Any three of the following

We provide all five so you can choose any three (or submit all):

1. **Executive summary of a penetration test** – Not in repo; obtain from a vendor and attach to submission.
2. **Security policy** – [SECURITY_POLICY.md](SECURITY_POLICY.md)
3. **Incident management and response policy** – [INCIDENT_MANAGEMENT_AND_RESPONSE.md](INCIDENT_MANAGEMENT_AND_RESPONSE.md)
4. **Vulnerability management procedures** – [VULNERABILITY_MANAGEMENT.md](VULNERABILITY_MANAGEMENT.md)
5. **Infrastructure/dependency management policy** – [INFRASTRUCTURE_AND_DEPENDENCY_MANAGEMENT.md](INFRASTRUCTURE_AND_DEPENDENCY_MANAGEMENT.md)

---

## SAST & DAST – What You Need to Do

Zoom requires **supporting evidence** in the form of scan results. You must run the scans and attach summaries (or full reports) to your Beta application.

### SAST (Static Application Security Testing)

- **Purpose:** Find vulnerabilities in source code (e.g. injection, hardcoded secrets, unsafe APIs).
- **Options:**
  - **Bandit** (Python): `pip install bandit && bandit -r mechus -f json -o sast-bandit.json` then share a short summary or sanitized report.
  - **Semgrep:** Use [semgrep.dev](https://semgrep.dev) or CLI; run against the repo and export results.
  - **Snyk Code** or **GitHub Advanced Security** (if available): Run and export the summary.
- **Deliverable:** Attach an executive summary (e.g. “SAST run on [date], X findings, Y critical/high, remediation status”) and optionally a redacted report.

### DAST (Dynamic Application Security Testing)

- **Purpose:** Test the running application (API) for issues like misconfigurations, exposed endpoints, and common web flaws.
- **Options:**
  - **OWASP ZAP:** Run against your deployed API base URL (e.g. `https://your-mechus-api.example.com`). Export report or summary.
  - **Burp Suite** (or similar): Scan the API and produce a short summary.
- **Deliverable:** Attach an executive summary (e.g. “DAST run on [date] against [URL], critical/high findings and remediation”).

**Tip:** Run SAST on the codebase before resubmission. Run DAST against a staging or production API that uses TLS 1.2+.

---

## TLS 1.2 or higher

- **Requirement:** Your application must support TLS 1.2 or higher for all external traffic.
- **Mechus:** The app runs as an HTTP service (e.g. Flask/gunicorn). TLS is terminated at the **load balancer, reverse proxy, or ingress** (see [PRODUCTION.md](../PRODUCTION.md)). You must configure your infrastructure to:
  - Use TLS 1.2 (or 1.3) only; disable TLS 1.0/1.1.
  - Use a valid certificate (e.g. Let’s Encrypt or your CA).
- **Outbound:** Mechus uses Python’s `ssl` module with `ssl.create_default_context()` (and `certifi` where available) for HTTPS calls to Zoom and other APIs; this uses the system’s OpenSSL, which supports TLS 1.2+ when the OS is up to date.
- **Statement for Zoom:** “Our application is deployed behind a load balancer (or reverse proxy) that terminates TLS. We support only TLS 1.2 and 1.3. All Zoom API and user-facing traffic is over HTTPS with TLS 1.2 or higher.”

---

## Resubmission Checklist

Before resubmitting your Beta application:

- [ ] **SSDLC:** Upload or link to [SSDLC.md](SSDLC.md) (or your own document that follows the same structure).
- [ ] **SAST:** Run SAST (e.g. Bandit/Semgrep), fix or document findings, and attach an executive summary (and optionally report).
- [ ] **DAST:** Run DAST against your API endpoint, fix or document findings, and attach an executive summary (and optionally report).
- [ ] **Privacy Policy:** Publish [PRIVACY_POLICY.md](PRIVACY_POLICY.md) at a public URL (e.g. `https://yourdomain.com/privacy` or `https://yourdomain.com/zoom-app-privacy`) and provide that URL in the application.
- [ ] **TLS 1.2+:** Confirm your deployment uses only TLS 1.2+ at the edge and state this in the Technical Design section.
- [ ] **Three of five:** Attach or link to at least three of: Security policy, Incident management and response, Vulnerability management, Infrastructure/dependency management, or Pen test executive summary (if you have one).

---

## Document Index

| Document | Description |
|----------|-------------|
| [ZOOM_BETA_SUBMISSION.md](ZOOM_BETA_SUBMISSION.md) | This guide |
| [PRIVACY_POLICY.md](PRIVACY_POLICY.md) | Privacy policy (host at a public URL) |
| [SSDLC.md](SSDLC.md) | Secure Software Development Lifecycle evidence |
| [SECURITY_POLICY.md](SECURITY_POLICY.md) | Security policy |
| [INCIDENT_MANAGEMENT_AND_RESPONSE.md](INCIDENT_MANAGEMENT_AND_RESPONSE.md) | Incident management and response policy |
| [VULNERABILITY_MANAGEMENT.md](VULNERABILITY_MANAGEMENT.md) | Vulnerability management procedures |
| [INFRASTRUCTURE_AND_DEPENDENCY_MANAGEMENT.md](INFRASTRUCTURE_AND_DEPENDENCY_MANAGEMENT.md) | Infrastructure and dependency management policy |

Related project docs: [PRODUCTION.md](../PRODUCTION.md), [DATA_PRIVACY.md](../DATA_PRIVACY.md).
