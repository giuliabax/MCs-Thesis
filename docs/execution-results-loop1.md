# Loop 1 — execution results

**Run:** `data/runs/20260814T141507Z-replanned`
**Date:** 16 August 2026
**Stage:** first execution of the generated suites; no feedback iteration has run yet.

This is the baseline against which the feedback loop will be compared. It reports what
each of the eighteen projects produced when its generated suite was executed against it,
and — as the July spec-provenance table did — it carries a provenance column, because a
comparison across projects is only defensible if a reader can see which ran as their teams
delivered them and which we had to complete.

---

## 1. Coverage of the campaign

Thirteen of the eighteen projects started, answered their readiness probe and ran their
suite. Five did not, each for a specific and separately recorded reason.

| | projects |
| --- | --- |
| Executed | 01, 02, 04, 05, 07, 09, 10, 13, 14, 15, 16, 17, 18 |
| Not executed | 03, 06, 08, 11, 12 |

**272 tests executed: 21 passed, 251 failed.** A further 103 tests were generated for the
five projects that never started and are recorded as `not_run`; they are excluded from
every rate below, and counted here instead, so that a project which could not be started
neither flatters nor damages the figures.

## 2. Per project

`pass` is passed over executed. `ops` is the share of the operations the strategy named
that the suite actually called, computed from recorded HTTP traffic rather than from test
outcomes. `codes` is the share of documented response codes observed. `5xx` counts server
errors, which are findings about the application rather than about the tests.

| project | provenance | generated | passed | failed | pass | ops | codes | 5xx |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| team01 | compose_extended | 20 | 0 | 20 | 0.00 | 0.08 | 0.09 | 0 |
| team02 | env_supplied | 23 | 2 | 21 | 0.09 | 0.42 | 0.57 | 0 |
| team04 | env_supplied | 9 | 1 | 8 | 0.11 | **0.67** | 0.33 | **5** |
| team05 | compose_extended | 28 | 3 | 25 | 0.11 | 0.56 | 0.29 | 0 |
| team07 | original | 19 | 2 | 17 | 0.11 | 0.44 | 0.40 | 1 |
| team09 | compose_extended | 29 | 0 | 29 | 0.00 | 0.56 | 0.67 | 0 |
| team10 | original | 28 | 1 | 27 | 0.04 | 0.33 | 0.56 | 0 |
| team13 | compose_extended | 13 | 3 | 10 | **0.23** | 0.22 | 0.18 | 0 |
| team14 | env_supplied | 18 | 1 | 17 | 0.06 | 0.55 | 0.40 | 0 |
| team15 | env_supplied | 24 | 4 | 20 | 0.17 | 0.29 | **1.00** | 0 |
| team16 | original | 25 | 1 | 24 | 0.04 | 0.50 | 0.43 | 0 |
| team17 | env_supplied | 12 | 2 | 10 | 0.17 | 0.36 | 0.40 | 0 |
| team18 | compose_extended | 24 | 1 | 23 | 0.04 | 0.27 | 0.22 | 0 |

### The three axes disagree, which is the point

A pass rate alone would rank team13 first and team04 seventh. But team04 reached **67% of
the operations its strategy named** — the widest coverage in the corpus — while team13
reached 22%. team15 observed **every response code its contract documents**, so its suite
exercises error branches and not only the happy path, despite a middling pass rate.

These are independent properties, and reporting only the first would reward a loop that
deleted whatever was hard to satisfy. That is why the loop is judged on all three.

## 3. Provenance

| provenance | n | mean pass rate | range |
| --- | ---: | ---: | --- |
| `original` — ran as delivered | 3 | 0.060 | 0.04 – 0.11 |
| `env_supplied` — we wrote its environment file | 5 | **0.117** | 0.06 – 0.17 |
| `compose_extended` — we defined a service it lacked | 5 | 0.076 | 0.00 – 0.23 |

The infrastructure we supplied did not give those projects an advantage; if anything the
three that ran untouched scored lowest. With n = 3 in that group this is not a result, but
it answers the question the design has to ask of itself, and it contrasts with the July
finding on specifications, where reconstructed contracts cost 0.100 in F1.

## 4. Why five projects did not run

The blockers are recorded per project rather than as one blanket justification, because
the situations are not equivalent and reporting them together is itself a result.

| project | blocker | what stops it |
| --- | --- | --- |
| team03 | `incomplete_repository` | its own `Dockerfile.db` runs `npm ci`, and no `package-lock.json` exists anywhere in the repository. Generating one would resolve versions the team never used. |
| team06 | `external_dependency` | grammy validates the Telegram bot token at start-up by calling `getMe`, inside the API process; it dies on the 401. |
| team08 | `external_dependency` | the data layer is Supabase cloud through the Supabase SDK, and its compose defines no database at all. |
| team11 | `image_unavailable` | its compose pulls `carmelogulino/participium`, which the registry refuses, and the project ships no Dockerfile to build in its place. |
| team12 | `external_dependency` | the same Telegram `getMe` check as team06. Everything else was sound: postgres started and TypeORM's retry would have connected. |

**Of eighteen applications delivered with Docker configuration, three cannot be
self-hosted at all, one cannot build from its own repository, and one depends on an image
that no longer exists.**

### What the recoverable ones needed, and the pattern behind it

Nearly every start-up failure had the same shape: a third-party library validating a
credential during initialisation, inside the API's own process. What separates a
recoverable project from an excluded one is *how* that library checks.

| the check verifies | example | recoverable |
| --- | --- | --- |
| the variable **exists** | team15, whose Resend client refuses to be constructed without a key | yes — any placeholder |
| the value's **shape** | team17, where firebase-admin parses the key as PEM at import | yes — a throwaway RSA key; `cert()` parses locally and contacts no Google service |
| the value's **length** | team17's `PENDING_ENCRYPTION_KEY`, which AES-256 requires to be exactly 32 bytes | yes, once the length is right |
| the value **against the service** | team06 and team12, calling Telegram's `getMe` | **no** |

Only the last is insurmountable, and not for want of effort: it asks a third party whether
a credential is real.

A second, orthogonal pattern accounted for three healthy projects being turned away. team04,
team09 and team16 each serve a web client from the same origin as their API, so the default
readiness probe on `/` received an HTML page and refused it. The refusal is correct — an
nginx index answers 200 long before an API works — but in this corpus the projects that
expose only an API are the minority, and each of the three had to be given a documented
operation to probe instead.

## 5. What the failures are attributed to

Attribution is made by deterministic rules over the recorded evidence; each diagnosis names
the rule that fired and quotes what it matched, so it can be audited.

| cause | count | acted on by the loop |
| --- | ---: | --- |
| `generation` | 83 | yes — regenerate the affected tests |
| `planning` | 58 | yes — replan the affected requirements |
| `unknown` | 48 | no rule matched |
| `environment` | 40 | no — the machine lacks something the service needs |
| `contract_mismatch` | 14 | no — the application disagrees with its own documentation |
| `sut_defect` | 8 | no — a finding about the application |

The most frequent individual rules:

```
50  expected_codes_absent_from_contract   planning
48  unmatched
40  login_with_unregistered_credentials   generation
18  request_never_completed               environment
15  assumed_pre_existing_data             generation
15  authentication_missing                generation
14  conformant_body_rejected              contract_mismatch
12  external_service_unavailable          environment
10  rate_limited                          environment
```

The two dominant defects both have precise repairs. The strategy expects status codes the
contract does not document for the operation it names, which no amount of rewriting the
test can fix. And forty tests try to authenticate as a user they never created — every
project starts from an empty database, so invented credentials can only be refused.

**All thirteen executed projects are actionable**, meaning another iteration could
plausibly improve each of them.

### 22 findings about the systems under test

Eight server errors and fourteen contract mismatches are not defects in the pipeline: they
are what it exists to produce. The mismatches are the more interesting half, because they
are invisible to any tool that trusts the specification. On team05 the contract states that
login takes `email` and `password`; the generated test sends exactly those two fields and
the service answers `400 "Username and password are required"`. Its signup demands
`emailNotificationsEnabled`, which the contract never mentions.

## 6. Caveats

- **These figures rest on a single planning run**, `20260807T151226Z`, which is the
  highest-scoring of the three measured. Downstream results inherit a favourable draw and
  should be read as an upper bound with respect to planning variation.
- **The excluded projects are not a random sample.** Three of the five depend on a live
  external service, which may correlate with other properties of those projects.
- **The `unknown` bucket is large.** Forty-eight failures matched no rule; they are
  reported as such rather than folded into a neighbouring category, and they are the gap a
  model-written feedback step is meant to close.
- **The blocker recorded in `execution_report.json` for the five excluded projects predates
  the manifest revision made after this run**; `data/sut_manifest.yaml` is authoritative and
  is what the table in §4 reports.
