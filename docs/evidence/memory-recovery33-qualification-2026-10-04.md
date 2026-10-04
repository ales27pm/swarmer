# Schema-33 recovery package — local qualification

The private recovery package can reopen the database produced by the installed
`f4fbeb00250db4b0b94004a209b72d66adeac4d7` application without discarding its
execution receipts. It remains a legacy recovery runtime, native schema 29 and
strictly readable through schema 33. It is **not deployed**, is not the M09
candidate, and does not replace the frozen schema-32 recovery kit for `243e913`.

## Behavior

- Validate the complete expected schema before ordinary startup writes; reject
  partial, changed or future schema objects without rewriting data.
- Preserve historical execution receipts. Refuse new receipt-bearing results
  and captures. An exact terminal replay checks existing evidence and cannot
  recreate an erased acceptance or revision link.
- Omit M09 history that this runtime cannot qualify, retaining an omission count.
  Recheck already-selected legacy experience at queue, claim and result time.
  Revocation cancels stale work without accepting its output.
- Defer symbolic-context jobs and unpublished construction jobs before leasing
  or consuming an attempt, without starving an eligible legacy job.
- Keep ledger erasure transactional even with SQLite foreign keys disabled.
  This helper does not erase raw historical job/snapshot JSON and makes no
  complete-erasure claim.

The source contains 133 application files, with 11 changed/new files relative
to the schema-32 recovery source. Dependencies remain byte-identical to that
private baseline. Recovery still needs a coherent legacy API/worker/configuration
cohort; this does not authorize an old worker to consume new context protocols.

## Evidence

| Check | Result |
| --- | --- |
| Runtime smoke and neighboring dispatcher/project tests | 59 passed |
| Independent source boundary tests | 29 passed, 133 files unchanged |
| Same boundary tests against installed wheel | 29 passed; 72 imported application modules pinned to installed files |
| Builder boundary tests | 7 passed |
| Targeted Ruff and mypy | Passed on nine runtime modules |
| Dependency consistency | Both installed packages pass `pip check` |

These counts overlap and are not a combined unique-test total. The corrected
schema-32 baseline has 18 expected failing boundary cases and 11 passing cases.
It already refuses schema 33 at startup; those additional failures describe
requirements for the new recovery binary, not a deployed schema-32 vulnerability.

A fresh synthetic report was issued through the **installed f4 services**,
accepted and captured into a revision, then completed through the normal result
handler. The database passed installed f4 → installed recovery33 → installed f4,
with two startup/read operations at each phase. Schema 33, the receipt and the
complete logical database digest—including hidden rowids, sequences and raw
JSON—remained identical. Integrity and foreign-key checks passed. All application
imports came from the appropriate installed wheel; fixture helpers came from
the exact f4 source archive.

This uses a synthetic worker report. It is not proof that a real runner, model,
GPU or phone performed the reported task. No production database or remote
service was touched by this qualification.

## Artifact identity

Private root:
`Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/m09-receipt-lessons-20261003/recovery33-20261004/`.

| Artifact | SHA256 |
| --- | --- |
| Runtime freeze | `d693bd8e3dedae1223af34f677f7caa8f12bd252cff078aed207bb4b85f77705` |
| Independent review | `1a0e44a04d9fb30680d5d13f87dad860328ebfb2fa62f7855c1d1182d81c2486` |
| Build input seal | `d41818fbc97c23db240c05329d897289dfbb6824084ba35d7eb1bd8af33dc408` |
| Recovery wheel | `f29aab90511f37fd7d3f3d92f1772cdb359fbd7d8d3e3f249d3986a8a04a7e7e` |
| Recovery provenance | `f9e6976d60538b05f11cde85515b4c432181671057256a7fc5cad4b42d362f68` |
| Installed tests receipt | `66239fc6c6a3907a5bcf0fec9fc7c8d5a1c755798f9b5bdd0c6595137a1c85d7` |
| Installed f4 provenance | `2204b7285a3010ceb2ceae420194d20c68d342489864a4c45aea116b91c20e48` |
| Installed round-trip receipt | `09a7eca3f2a5c4c6109b5baa1b4b9c74d54bc8812fd95c05bc1470a7d2bf3a2f` |

The package is a locally qualified recovery prerequisite. Linux installation,
cohort cutover, actual worker production and the remaining M09 lesson-promotion
work remain separate qualifications.
