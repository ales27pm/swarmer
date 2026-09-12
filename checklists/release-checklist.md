# Release Checklist

- [ ] Mobile typecheck passed.
- [ ] Mobile lint passed.
- [ ] Mobile tests passed.
- [ ] Expo doctor passed.
- [ ] Exported iOS IPA passed `python3 mobile/scripts/verify-ios-archive.py /path/to/App.ipa` before submission (no test-only frameworks or unresolved bundled-library dependencies).
- [ ] TestFlight processing and physical-device launch were verified separately; a successful archive/upload does not establish launch success.
- [ ] Backend lint/types/tests passed.
- [ ] Schema validation passed.
- [ ] OpenAPI validation passed.
- [ ] Permission fixture tests passed.
- [ ] Sync offline test passed.
- [ ] Approval flow test passed.
- [ ] Backup created.
- [ ] Version tagged locally.
- [ ] Rollback path documented.
