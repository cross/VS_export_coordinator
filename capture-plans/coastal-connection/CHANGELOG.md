# Coastal connection capture plan changelog

`plan.json` is the current capture specification. This changelog describes the
revisions in plain language; Git stores their complete contents at that same
path. The accepted shoreline geometry remains v12 throughout these revisions.

## 2026-11-01 — Initial banded plan

- Replaced the legacy centerline corridor with the accepted v12 west/east
  coastlines, northern mouth anchor X=-34500, and southern mouth anchor
  X=-32680. Mouth anchors define capture coverage rather than cut/fill limits.
- Required at least 100 blocks of horizontal margin in both X and Z.
  Broader rectangular bands include additional edge terrain to reduce the
  number of exports without omitting the required footprint.
- Requested inclusive Y bounds of 65–180 independently of the horizontal
  buffer, with no freshwater body supplements supplied initially.
- Planned 88 nonoverlapping sections with X at most 1023 and Z at most 50;
  the largest selection was 1023 × 116 × 50, or 5,933,400 block positions.
- Requested 311,148,656 block positions and planned an additional 8,473,916
  positions (2.72%) around the edges.
- Recorded geometry/content hashes and provided a footprint preview and an
  independent three-dimensional coverage audit. Shortened height is reported
  as unresolved coverage, and blocks at an upper ceiling are flagged.
- Plan SHA256:
  `7116eb3390b37000e65ca17bb074926cf29597c4728274a0001fef73b9f2757d`.

To retrieve the full contents of an earlier revision:

```sh
git log --oneline -- capture-plans/coastal-connection/plan.json
git show COMMIT:capture-plans/coastal-connection/plan.json
```

When changing the capture specification, update this changelog and commit it
together with `plan.json`. Keep a frozen plan copy with any unfinished run's
progress records so a later revision cannot change what that run must capture.
