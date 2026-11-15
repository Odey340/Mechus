# Incident Management and Response Policy – Mechus

This document defines how we identify, assess, contain, and recover from security and privacy incidents involving the Mechus application. It supports Zoom App Beta compliance and operational readiness.

---

## 1. Purpose and Scope

- **Purpose:** To ensure a consistent, timely response to security and privacy incidents, minimise impact, and support communication and post-incident improvement.
- **Scope:** Applies to incidents affecting Mechus (application, API, deployment), related infrastructure, and any processing of meeting/transcript/voice data. Includes unauthorised access, data exposure, availability failures, and abuse of the service or Zoom integration.

---

## 2. Definitions

- **Security incident:** An event that could compromise the confidentiality, integrity, or availability of Mechus or related data (e.g. breach, malware, unauthorised access, abuse).
- **Privacy incident:** An event that could lead to unauthorised access to or disclosure of personal or meeting data (e.g. logging of PII at scale, accidental exposure, misuse).
- **Severity:** Incidents are classified (e.g. Critical, High, Medium, Low) based on impact and scope; classification drives response timing and escalation.

---

## 3. Roles and Responsibilities

- **Incident owner:** Responsible for coordinating response, communication, and closure. Typically the person who detects the incident or the designated on-call/lead.
- **Technical response:** Personnel with access to systems and logs perform containment, evidence preservation, and remediation under the direction of the incident owner.
- **Communication:** The incident owner ensures internal stakeholders and, where required, affected users or partners (e.g. Zoom) are informed in line with this policy and legal obligations.
- **Post-incident:** The incident owner ensures a post-incident review is conducted and that actions are tracked to completion.

---

## 4. Incident Lifecycle

### 4.1 Detection and reporting

- Incidents may be detected via monitoring, user reports, security scans, or internal review.
- Anyone who identifies a potential incident should report it promptly to the designated contact (e.g. [insert contact email or channel].
- Reports should include: what was observed, when, which systems or data may be affected, and any immediate actions already taken.

### 4.2 Triage and classification

- The incident owner (or first responder) triages the report and assigns an initial severity:
  - **Critical:** Active breach, broad data exposure, or severe service outage affecting security or privacy.
  - **High:** Significant risk (e.g. limited data exposure, credential compromise) or major degradation.
  - **Medium:** Localised impact or moderate risk that requires coordinated response.
  - **Low:** Minor impact or low likelihood; can be handled with standard procedures.
- Classification may be updated as more information becomes available.

### 4.3 Containment and mitigation

- **Immediate:** Take steps to stop ongoing harm (e.g. revoke exposed credentials, block abusive access, isolate affected systems). Preserve evidence (logs, snapshots) where relevant for investigation or legal requirements.
- **Short-term:** Apply patches, configuration changes, or workarounds to reduce risk while a permanent fix is prepared.
- **Documentation:** Record actions taken, timestamps, and owners so that the timeline is clear for post-incident review and any regulatory or partner reporting.

### 4.4 Eradication and recovery

- Remove root cause (e.g. fix vulnerability, correct misconfiguration, remove unauthorised access). Verify that the fix is deployed and that systems and data are restored to a known-good state.
- Restore normal operation only after confirming that the immediate risk has been addressed.

### 4.5 Post-incident review

- Conduct a post-incident review for High and Critical incidents (and for Medium where useful). Include: what happened, root cause, what was done well, what could be improved, and actionable follow-up items.
- Update runbooks, monitoring, or policies as needed. Track follow-up items to completion.
- Retain incident records and evidence in accordance with legal and compliance requirements.

---

## 5. Communication

- **Internal:** Notify relevant technical and leadership stakeholders as soon as the incident is confirmed and classified. Provide updates at key milestones (containment, recovery, closure).
- **Users and partners:** If user or meeting data is affected, or if Zoom or other partners need to be informed, the incident owner ensures notification in line with our Privacy Policy, terms of service, and any contractual or regulatory obligations (e.g. breach notification timelines).
- **Regulatory:** Where applicable law requires notification to regulators or data subjects, the incident owner coordinates with legal/compliance to meet those requirements.

---

## 6. Integration with Other Policies

- **Vulnerability management:** Incidents stemming from a known vulnerability are handled under this policy; remediation follows our [VULNERABILITY_MANAGEMENT.md](VULNERABILITY_MANAGEMENT.md) procedures.
- **Security policy:** Response actions must align with our [SECURITY_POLICY.md](SECURITY_POLICY.md) (e.g. access control, logging, TLS).
- **Privacy:** Any incident involving personal or meeting data is treated as both a security and privacy incident; handling must respect our [PRIVACY_POLICY.md](PRIVACY_POLICY.md) and DATA_PRIVACY practices.

---

## 7. Contact and Escalation

- **Primary contact:** [Insert email or channel for security/privacy incidents]
- **Escalation:** [Insert escalation path for Critical/High incidents, e.g. on-call, management]

This policy is reviewed periodically and after significant incidents. For Zoom App Beta, this document is provided as evidence of our incident management and response capability.
