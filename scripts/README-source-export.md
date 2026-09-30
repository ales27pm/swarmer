# Reproductible source export

`export_source.py` requires Python 3.10 or later (qualified with the project’s Python 3.12 environment) and exports a selected text-source snapshot. It never runs exported code, creates commits, scans untracked directories or overwrites an existing output.

## Input contract

- Git index inventory at the repository root, with the **current working-tree bytes** of tracked regular files. An unresolved index conflict aborts generation.
- Optional `--include relative/exact/file.py` entries for individually reviewed, non-ignored untracked files. No directories or globs. Additional untracked files are never discovered or included.
- UTF-8 text source formats only. CRLF, Unicode, empty source files and executable modes are preserved.
- Runtime data, private configuration, credential paths, binaries, vendor/build/cache folders, symlinks, submodules and previous source exports are excluded. The tracked public activity catalogue is the explicit runtime-directory exception.
- Deleted files and exclusions are listed with reasons in the manifest. Explicitly requesting an excluded file fails the export.
- High-confidence key/token patterns cause an exclusion, even in fixtures. Review flagged fixtures individually before freezing the release inventory; the tool provides no bypass. Pattern screening is not a guarantee that arbitrary application data contains no secrets.
- New output directory only. Generation does not modify existing source exports. An I/O failure may leave a partial new directory; it never replaces another export. The manifest is written last and verified before reporting success.

Freeze the intended sources and review the inventory before distributing the export. Each file is checked for concurrent modification while reading; this is not an atomic snapshot of a concurrently edited Git working tree.

## Commands

```sh
server/.venv/bin/python scripts/export_source.py inventory --repo /absolute/swarmer
server/.venv/bin/python scripts/export_source.py inventory --repo /absolute/swarmer --include scripts/export_source.py
server/.venv/bin/python scripts/export_source.py create --repo /absolute/swarmer --version 0.14.2-20260929 --output-dir /absolute/NEW-export
server/.venv/bin/python scripts/export_source.py verify /absolute/NEW-export/swarmer-source-VERSION-COMMIT-INVENTORY.manifest.json
server/.venv/bin/python scripts/export_source.py reconstruct /absolute/NEW-export/swarmer-source-VERSION-COMMIT-INVENTORY.manifest.json --output /absolute/NEW-source.txt
server/.venv/bin/python scripts/export_source.py extract /absolute/NEW-export/swarmer-source-VERSION-COMMIT-INVENTORY.manifest.json --output-dir /absolute/NEW-source-tree
```

Use the actual manifest filename returned by `create`. Before the generator is tracked, include each intended new source explicitly; after it is tracked, no extra inclusion is needed.

Part filenames contain the requested release label, base commit, source inventory fingerprint, ordinal and total. Each part starts with a readable header and is nonempty, valid UTF-8 and **at most 5,000,000 bytes** including the header. Splits prefer line boundaries and always preserve UTF-8 boundaries. Files larger than one part continue in the following part. Do not concatenate part headers manually: `reconstruct` removes verified framing and recreates the exact canonical snapshot; `extract` restores its selected source tree.

The manifest records SHA-256 and byte counts per source, part and complete snapshot. Verification rejects changed/missing/reordered parts, traversal, ambiguous source paths and mismatched boundaries before extraction. Integrity hashes do not authenticate the sender. Use the script from a trusted checkout.

No binary assets are exported, and no build, deployment or physical-iPhone opening is implied. The resulting tree is the declared text-source inventory, not a complete installation package.

## Verification

```sh
server/.venv/bin/python scripts/test_export_source.py
server/.venv/bin/ruff check --config server/pyproject.toml scripts/export_source.py scripts/test_export_source.py
server/.venv/bin/mypy --strict scripts/export_source.py scripts/test_export_source.py
```

The regression suite creates only temporary fixture repositories. It covers deterministic output, exact reconstruction and modes, the decimal five-megabyte boundary with Unicode, explicit selection, exclusions, source-change detection, symlink and path boundaries, corrupt parts and refusal to overwrite. Opening generated parts on a physical iPhone remains a separate release check.
