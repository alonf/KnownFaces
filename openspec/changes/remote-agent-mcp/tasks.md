## 1. Home implementation

- [x] 1.1 Isolate the MCP process, protected-resource metadata and allowlisted relay route.
- [x] 1.2 Validate Entra signatures and principal binding and enforce independent tool grants.
- [x] 1.3 Implement approved comparison, bounded history and transactional library edits.
- [x] 1.4 Add local auditing, retention, withdrawal and setup/rollback guidance.
- [x] 1.5 Cover reads, denied writes, invalid tokens, surface isolation, replay, evidence and local outage operation with synthetic tests.

## 2. Approval-dependent pilot

- [ ] 2.1 Obtain an approved canonical URL, dedicated registrations and access to the target MCP client. Verify direct discovery and client configuration.
- [ ] 2.2 Verify authorization code with S256 and token acquisition at the intended resource URL.
- [ ] 2.3 Perform one synthetic read and one denied edit while management is disabled. Confirm the audit and unchanged library. Verify refresh in the actual connector.
- [ ] 2.4 Record the exact failure if any. Evaluate an isolated compatibility adapter only for a demonstrated blocker.
