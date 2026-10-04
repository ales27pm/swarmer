# iPhone installation and device diagnostic matrix

On 4 October 2026 UTC, the prepared Debug application from commit
`243e913c3126a1cb15d290fbbac36e6f5024b7fe` was installed on the paired physical
iPhone 16 Pro over its CoreDevice network tunnel. This is an installation
receipt, not successful Core ML qualification or a TestFlight release.

## Installation

The retained signed `.app` matched all 169 files in its manifest and the IPA.
Its development profile covered the freshly discovered device, and
`codesign --verify --deep --strict` passed. `devicectl device install app`
returned success in 8.78 seconds for bundle `org.27pm.mongars`.

| Artifact | SHA256 |
| --- | --- |
| Prepared IPA | `82c433e4534212b1f50454315de3c7b80dd81927bc82f872a281af7f2293a2ef` |
| App manifest | `358702a45060a63d11d05a3051e812c295b519b7eecb3a263050ec08d4dc6662` |
| Bundled JavaScript | `051fe724f48e26f2aedde8876321dbab0482b928bf3ed88fd5df81ac1b51225d` |

The installation used the retained `.app`, as required by the local
`devicectl` command. No rebuild, re-signing or uninstall was performed.

## API boundary

`iphone-api.py launch --console --device-tunnel` returned verified HTTPS
health: `ready`, foreground access, zero active jobs. The subsequent `health`
command failed. A diagnostic connection failed with `ConnectionRefusedError`,
errno 61, before any credential could be sent on that connection.

The tunnel address was unchanged, developer services remained available and
the app process still existed. Activating that same process without termination
did not restore health. A later native `lockState` read reported
`passcodeRequired: true` and `unlockedSinceBoot: true`. This is consistent with
the listener's foreground requirement; no lifecycle trace was captured to
prove the precise callback that closed it. A live process alone is not API
readiness.

**No Core ML probe or model-loading command was sent in that first session.** The pending matrix
remains the independent-input two-block fixture on CPU, CPU+GPU and
CPU+Neural Engine, with the sequential CPU+Neural Engine control. No new
inference, numerical parity or Neural Engine execution is established here.

The new schema-33 memory work is separate from this application candidate.
No Ubuntu activation took place during this installation; the independent
admission check still refused a started project.

Private evidence is under
`Library/Logs/SwarmerQualification/CoreML/two-attention-independent-20261003/device-243e913-20261004/`.
`installation-status.json` has SHA256
`f78da95f410a9b8d464840f079a3bb96cd671f00905a0398a909c65531ff9de1`.
Temporary API credentials are excluded from this report and from its inventory.

## Resumed IPv6 session, 03:44–03:50 UTC

After the user reported the phone connected, fresh CoreDevice discovery
established the exact paired iPhone over the local network, with developer
services available. A new held-console API session returned foreground health,
and separate app/model status reads confirmed an active app with no loaded model.
The catalog exposed the independent-input fixture.

| Fixture / requested units | Native result | Load | Prediction | Comparison |
| --- | --- | --- | --- | --- |
| Two blocks, independent inputs / CPU | Passed | 252.51 ms | 18.92 ms | 61,440 elements; max absolute error 0.0009765625 |
| Same fixture / CPU + Neural Engine | Failed at load, execution-plan −14 | 398.06 ms | Not reached | Zero elements compared |
| Same fixture / CPU + GPU | Request not sent after TLS connection failure | — | — | Pending |
| Sequential two-block control / CPU + Neural Engine | Not sent | — | — | Pending |

Both completed API jobs have a transport state of `succeeded`; the second
contains a **failed native diagnostic**. Those states must not be conflated.
CPU error stays below the unchanged 0.005 tolerance. The failed ANE-mode load
shows that removing the sequential input dependency alone is insufficient for
this fixture. It does not identify the failing operation or establish actual
Neural Engine activity. No complete Dolphin model was loaded or generated here.

After the second result, the GPU call failed before constructing its POST.
A TLS-only diagnostic subsequently connected once, but health failed again;
a later connection was refused. The phone was observed unlocked, and activating
the existing app did not restore health. No automatic command replay or new
model operation was sent. Foreground confirmation is pending; the exact cause
of this connection interruption is unproven.

Private receipts under `resumed-0344/`:

| Receipt | SHA256 |
| --- | --- |
| CPU result | `214fafbb177f78b9b8cc9397bbc964618a4e000031fc4e8b0f739aecd09bb6bb` |
| CPU + Neural Engine result | `ccb88ef0babad99f52f793d3d2b1af8c4e29c3e2788840bb2ee2b59ef31337c5` |
| Partial four-case matrix | `c8711c35105e23bb6bb16147fb99b0858275636ac194b5664ec55e8c9f091dd9` |

## Completed matrix, 04:11–04:13 UTC

The previous launch console exited with an error. A fresh Apple diagnostic
reported the same paired physical iPhone ready over its network tunnel, with
developer services available. A new `--device-tunnel --console` launch returned
foreground HTTPS health. App and model status confirmed the same native build,
an active app and no loaded model. No application rebuild or installation occurred.

The two remaining cases ran once each in the new session:

| Fixture / requested units | Native result | Load | Prediction | Comparison |
| --- | --- | --- | --- | --- |
| Two blocks, independent inputs / CPU + GPU | Passed | 206.00 ms | 1,694.51 ms | 61,440 elements; max absolute error 0.0001220703125 |
| Sequential two-block control / CPU + Neural Engine | Failed at load, execution-plan −14 | 377.36 ms | Not reached | Zero elements compared |

The combined matrix now contains four valid native reports: two passed and two
failed. CPU and CPU + Neural Engine results come from the earlier session;
GPU and sequential control results come from this new session, on the unchanged
installed build. They are not four results from one uninterrupted session.
The final health read returned `ready`, foreground access and zero active jobs.

Both independent and sequential two-block fixtures fail to load in CPU + Neural
Engine mode. Removing the sequential input dependency does not resolve this
failure. The shared cache/state updates and other graph composition remain
hypotheses to isolate, not diagnosed causes. The GPU-mode numerical result stays
below the unchanged 0.005 tolerance. `MLComputePlan` assignments do not measure
hardware execution; these small fixture probes neither establish actual ANE
activity nor qualify the full Dolphin model or compare sustained throughput.

Private receipts under `resumed-0412/`:

| Receipt | SHA256 |
| --- | --- |
| CPU + GPU result | `99f713886689dd7177194cda5533995e1627e8635927f801b404695877e0dd9a` |
| Sequential CPU + Neural Engine result | `d337298c26abfd876ecff8dba5ff62975255b224d4c2f04a928ab2a2aec0d98e` |
| Completed four-case matrix | `f6498af47ff13e6ecc48a043f54dc89fecf25dbc06082b641bf18895b74566e3` |

The held launch console remains session-local and expires after one hour.
Session credentials are excluded from the report and Git. No Ubuntu deployment
or user-project operation was performed by these device probes.
