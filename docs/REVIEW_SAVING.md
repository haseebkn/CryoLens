# Review saving and IIP import corrections

The standard reference viewer now records a selected point as part of **Save**
when its assessment and rationale are valid. Invalid or incomplete point fields
block Save; they cannot silently produce a complete survey with an empty object
list. **Record marked object** remains useful for multiple points. **Clear selected
point** explicitly discards the pending selection without removing recorded marks.
Inspecting a genuinely empty core still permits a complete response with no marks.

Drafts, including pending points, notes and recorded marks, are backed up under a
separate browser-storage key and recovered when navigating or reloading. Recovered
drafts require a new personal-inspection attestation before saving. They never
count as imported submissions. A failed draft write blocks navigation. A failed
saved-response write leaves that response only in memory, with a warning to export
the session before leaving; successful draft writes do not remove that protection.
Export still contains only explicitly saved responses, using the existing schema.
Both-channel inspection and independent-evidence requirements remain unchanged.

Saved-response and draft writes share an exclusive browser lock around the read,
conflict check, merge and write. Navigation waits for its draft write; leaving
while storage work is pending triggers an unload warning. Open the local review
server in a browser with Web Locks support; when locking is unavailable,
explicitly saved responses stay in memory
for export and the viewer warns before leaving. Failed saved revisions remain
in memory until a successful write persists them, including when saving another
task. Each pending response retains its original saved baseline independently of
recovered drafts; if another tab has changed that response, flushing is blocked
and the pending response remains available for export. Unpersisted responses do
not survive closing or reloading, so export them before leaving despite the warning.
Each draft retains its originating saved response for conflict checks after
reload. Older drafts without that metadata cannot replace an existing response;
their contents remain recoverable for review.

These changes apply to newly prepared review packets. Existing packets contain
hash-sealed viewer copies: **do not overwrite their assets**, their manifests, or
their submissions to apply this fix. Prepare new packets through the normal
versioned workflow using the corrected source. The five adopted pilot corrections
remain in the append-only workflow; this code does not rewrite or infer any labels.
Repeat-review eligibility and all scientific thresholds remain unchanged.

The IIP importer now recognizes the publisher's `SIGHTING_LATITUDE` and
`SIGHTING_LONGITUDE` columns as well as normalized `LATITUDE` and `LONGITUDE`.
Date/time aliases, UTC handling and the NL polygon filter are retained. Missing,
duplicated or ambiguous required headers fail before database writes. Malformed
rows are counted in logs; a file containing only malformed rows fails explicitly.
A valid file containing only out-of-region sightings can legitimately import zero
rows. This fix does not import new sightings automatically or change existing rows.
IIP remains regional context, not automatically independent target truth.

## Regression checks

The Python IIP tests execute an attributed publisher CSV excerpt and normalized
legacy files. The reference tests exercise synthetic workspaces and append-only
import/correction behavior, never the existing human ledgers or test imagery.

Chromium tests run the actual HTML and JavaScript against synthetic 64 × 64 images:

```powershell
cd tests/browser
npm ci --ignore-scripts
npx --no-install playwright install chromium
npm test
```

They exercise Save without a separate Record click, navigation/reload recovery,
multiple points, invalid evidence, missing rationale, storage failure, explicit
empty surveys, channel-inspection requirements, candidate edits, and preservation
of existing responses. CI runs this suite separately from the Python tests.
