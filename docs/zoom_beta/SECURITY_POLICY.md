# Security Policy – Mechus

This document defines the security policy for the Mechus application and supporting infrastructure. It supports Zoom App Beta compliance and internal security governance.

---

## 1. Purpose and Scope

- **Purpose:** To establish security expectations and controls for Mechus so that we protect data (including meeting and transcript data), maintain availability, and meet compliance requirements.
- **Scope:** Applies to Mechus source code, deployment environments, APIs, and any infrastructure or third-party services used to operate Mechus (e.g. Zoom, optional STT/TTS/LLM providers).

---

## 2. Security Principles

- **Confidentiality:** Meeting links, transcripts, notes, and voice data are treated as sensitive. Access is limited to authorised personnel and systems; logging and storage are configured to minimise exposure (see DATA_PRIVACY.md).
- **Integrity:** We protect systems and data from unauthorised modification. Dependencies and builds are controlled; changes are reviewed and tested.
- **Availability:** We design for reliability (health checks, restart policies, scaling guidance) and respond to incidents in accordance with our incident response policy.
- **Accountability:** Access to production and sensitive data is logged and restricted; security roles and responsibilities are defined.

---

## 3. Access Control

- **Production and secrets:** Access to production systems and to secrets (API keys, Zoom credentials, etc.) is restricted to personnel who need it for their role. Secrets are stored in a secure mechanism (e.g. secrets manager, environment injection) and never committed to source control.
- **API:** When the Mechus API is exposed beyond localhost, authentication is required (e.g. API key via `X-API-Key` or `Authorization: Bearer`). Public routes are limited to `/health` and `/ready` (see PRODUCTION.md).
- **CORS:** In production, CORS is restricted to allowed origins (e.g. the dashboard domain) to prevent unauthorised cross-origin access.

---

## 4. Cryptography and Transport

- **TLS:** All external traffic to and from the application uses TLS 1.2 or higher. TLS is terminated at the load balancer, reverse proxy, or ingress; the application runs over HTTP only within a trusted network.
- **Outbound:** Outbound HTTPS (e.g. to Zoom, LLM, or TTS APIs) uses the system’s default TLS context (and, where available, certificate verification via certifi). We do not disable certificate verification in production.
- **Secrets at rest:** Where applicable, secrets and sensitive configuration are stored in a platform secrets store with access controls; we avoid storing secrets in plain text in config files or code.

---

## 5. Application Security

- **Input validation:** All external inputs (API payloads, meeting links, configuration) are validated and sanitised. We avoid constructing commands or queries from unsanitised input.
- **Dependencies:** We maintain a minimal dependency set, pin versions where appropriate, and address known vulnerabilities in line with our vulnerability management procedures.
- **Security testing:** We use SAST (e.g. Bandit, Semgrep) and DAST (e.g. OWASP ZAP) as part of our SSDLC; findings are triaged and remediated before release and for compliance submissions.

---

## 6. Data Protection and Privacy

- **Data handling:** Handling of meeting and transcript data follows our [DATA_PRIVACY.md](../DATA_PRIVACY.md) and [PRIVACY_POLICY.md](PRIVACY_POLICY.md). PII is not logged at standard (INFO) level; full transcript text and meeting URLs are limited to DEBUG and protected environments.
- **Storage:** Transcript and note files are stored with restricted filesystem permissions; retention and deletion follow our policies and configuration.
- **Third parties:** When we use third-party services (Zoom, STT/TTS/LLM), we select providers with acceptable security and privacy practices and document data flows.

---

## 7. Monitoring and Logging

- **Logging:** Logs are written to stdout/stderr and captured by the hosting platform. Log level in production is INFO or WARNING to avoid logging PII. DEBUG is used only for controlled troubleshooting.
- **Monitoring:** We use health and readiness endpoints for availability monitoring. Additional monitoring (e.g. metrics, alerting) may be added as needed for production operations.
- **Incidents:** Security and privacy incidents are handled according to our [INCIDENT_MANAGEMENT_AND_RESPONSE.md](INCIDENT_MANAGEMENT_AND_RESPONSE.md) policy.

---

## 8. Roles and Responsibilities

- **Development:** Implement and maintain secure coding practices, participate in code review, and remediate SAST/DAST findings.
- **Operations / deployment:** Ensure production configuration follows PRODUCTION.md (API key, CORS, TLS, secrets), run supported deployment methods (e.g. gunicorn, Docker), and maintain health checks.
- **Security / compliance:** Maintain this policy and related procedures (incident response, vulnerability management), support SSDLC evidence (SAST/DAST, policies), and respond to security issues.

---

## 9. Policy Review

This policy is reviewed periodically and after significant security events or changes in scope. Updates are documented and communicated to relevant stakeholders. For Zoom Beta, this document is provided as supporting evidence of our security posture.
