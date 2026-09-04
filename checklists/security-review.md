# Security Review Checklist

- [ ] Are secrets excluded from file reads?
- [ ] Are all tool calls schema-validated?
- [ ] Can any worker write DB directly?
- [ ] Can any model execute shell directly?
- [ ] Are file writes gated by approval?
- [ ] Are dangerous shell commands denied?
- [ ] Are iPhone capabilities scoped?
- [ ] Is sensitive iPhone data TTL-limited?
- [ ] Are audit events written for sensitive actions?
- [ ] Can agent tokens be revoked?
- [ ] Is the API bound to LAN/Tailscale only?
- [ ] Are model outputs treated as untrusted?
