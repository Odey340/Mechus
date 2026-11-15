# Infrastructure and Dependency Management Policy – Koov

This document defines how we manage infrastructure and dependencies for the Koov application. It supports Zoom App Beta compliance and operational consistency.

---

## 1. Purpose and Scope

- **Purpose:** To ensure infrastructure and dependencies are selected, updated, and operated in a secure and maintainable way, and to minimise risk from supply-chain and configuration issues.
- **Scope:** Applies to (a) third-party libraries and packages used by Koov (Python and related), (b) container images and base OS, (c) deployment and runtime environment (e.g. cloud, load balancer, secrets), and (d) tooling used for build and CI.

---

## 2. Dependency Management

### 2.1 Selection and approval

- **Minimal set:** We keep the set of dependencies as small as practical. New dependencies are justified (e.g. feature need, maintainability) and reviewed for licence, maintenance status, and known security issues before adoption.
- **Sources:** We use well-known, maintained packages (e.g. from PyPI). We prefer widely used libraries with a history of security updates. Zoom SDK and other vendor packages are obtained from official sources.
- **Licences:** We ensure dependencies have licences compatible with our use and distribution (e.g. permissive or documented commercial terms).

### 2.2 Versioning and pinning

- **Declaration:** Dependencies are declared in `requirements.txt`, `requirements-zoom.txt`, and `requirements-prod.txt` (or equivalent). We pin major/minor (or exact) versions to get reproducible builds and controlled upgrades.
- **Updates:** We periodically review and update dependencies to get security fixes and supported versions. Updates are tested before being merged; breaking changes are handled with code or config changes as needed.

### 2.3 Vulnerability handling

- **Scanning:** We use dependency scanning (e.g. `pip audit`, Snyk, Dependabot) to identify known vulnerabilities. Findings are triaged and remediated under our [VULNERABILITY_MANAGEMENT.md](VULNERABILITY_MANAGEMENT.md) procedures.
- **Critical/High:** We prioritise upgrades or patches for Critical and High CVEs in dependencies and aim to resolve them within the SLAs defined in the vulnerability management policy.

---

## 3. Container and Build

- **Base images:** We use official or trusted base images (e.g. Python slim or Alpine) and keep them updated. We avoid unnecessary components and run as non-root where possible (as in the project Dockerfile).
- **Build process:** Builds are performed in a controlled environment (local or CI). Build artifacts do not include secrets or unnecessary sensitive data. Docker multi-stage builds may be used to keep final image size and surface area small.
- **Image updates:** When base images or Dockerfiles change (e.g. for security or compatibility), we rebuild, test, and redeploy in line with our release process.

---

## 4. Infrastructure and Deployment

### 4.1 Runtime environment

- **Execution:** Koov runs as a single process (e.g. gunicorn with one worker for Zoom SDK compatibility) or in containers. We use health (`/health`) and readiness (`/ready`) endpoints for orchestration and load balancers.
- **Configuration:** Production configuration is driven by environment variables or a secure config mechanism. We follow [PRODUCTION.md](../PRODUCTION.md): API key when exposed, CORS restricted, TLS at the edge, secrets from a secrets store.

### 4.2 Networking and TLS

- **TLS:** External traffic uses TLS 1.2 or higher. TLS is terminated at the load balancer, reverse proxy, or ingress; the application listens on HTTP inside a trusted network. We do not disable TLS or use deprecated protocols.
- **Network:** We restrict which ports and endpoints are exposed. Only necessary routes (e.g. `/health`, `/ready`, `/api/*`) are exposed to the load balancer; internal services are not unnecessarily exposed to the internet.

### 4.3 Secrets and access

- **Secrets:** API keys, Zoom client credentials, and other secrets are not stored in code or in plain text in config. They are injected via environment variables or a platform secrets manager (e.g. AWS Secrets Manager, Azure Key Vault, Kubernetes Secrets).
- **Access:** Access to production infrastructure and secrets is limited to authorised personnel and automated systems that need it; access is reviewed periodically.

### 4.4 Scaling and availability

- **Scaling:** We document scaling approaches (e.g. horizontal scaling with sticky sessions, one worker per process for Zoom SDK) in PRODUCTION.md and CAPACITY_AND_COST.md. Scaling decisions consider security and session affinity.
- **Availability:** We use restart policies, health checks, and, where applicable, multiple instances behind a load balancer to support availability objectives.

---

## 5. Monitoring and Maintenance

- **Updates:** We apply security and dependency updates in a timely manner, guided by our vulnerability management procedures. We test updates in a non-production environment before production deployment.
- **Obsolescence:** We track end-of-life or deprecated components (e.g. Python version, base image) and plan upgrades before they become unsupported.
- **Monitoring:** Infrastructure and application are monitored (e.g. health checks, logs, optional metrics) so that we can detect and respond to failures and security events.

---

## 6. Documentation and Compliance

- **Documentation:** Key infrastructure and dependency choices are documented in PRODUCTION.md, ZOOM_SDK_SETUP.md, and this policy. Changes that affect security or deployment are reflected in the docs.
- **Compliance:** For Zoom App Beta and similar requirements, we provide this policy together with SSDLC, vulnerability management, and security policies as evidence of our infrastructure and dependency management practices.

---

## 7. Review

This policy is reviewed periodically and when we make significant changes to infrastructure or dependency strategy. For Zoom App Beta, this document is provided as one of the optional supporting policies.
