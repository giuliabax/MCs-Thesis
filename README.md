# Participium REST Tester

This MSc thesis project investigates automated black-box REST API test generation using
LLM-based agents and a metric-guided feedback loop. The systems under test are independent student
implementations of the Participium requirements, exposed through a common REST API contract.

## What it does

The pipeline is complete end to end. Given a set of requirement documents and one OpenAPI
document per project, it plans what to test, writes an executable pytest suite, starts each
application in Docker, runs the suite against it, classifies every failure by cause, and
feeds the repairable ones back into planning and generation for another iteration.

```text
requirements (PDF/XLSX) --> Requirements Analyst ------OpenAPI/Swagger         --> API Understanding ----------> Matcher -> Strategy Planner
                                                                        |
                                                                   WorkflowPlan
                                                                        |
                                          Test Writer -> reconciliation -> pytest suite
                                                                        |
                                            Docker compose up -> run -> down
                                                                        |
                                    metrics + deterministic failure attribution
                                                                        |
                          Orchestrator: replan what planning caused, rewrite what generation caused
```

Two commitments shape the whole design and are worth stating before the details.

**The model proposes and deterministic code decides.** Every agent returns JSON validated
against a strict schema, and every proposal is reconciled against the sources that are
authoritative: the XLSX for requirement identifiers, roles and text, and the OpenAPI
document for operations and request bodies. A model cannot renumber a requirement, invent an
endpoint, or emit a request body the contract contradicts. The same boundary holds in
evaluation: failures are classified by ordered deterministic rules over recorded evidence,
never by a model, so the loop's control flow and its reported numbers are both reproducible.

**Everything runs locally.** No stage depends on a hosted model. Repeating a whole campaign
therefore costs time rather than money, which is what makes the variability of a
non-deterministic pipeline something to measure rather than assume.

## Inputs

Place local inputs at the paths referenced by your YAML configuration. The example expects:

- `data/requirements/participium-description.pdf`
- `data/requirements/participium-userstories.xlsx`
- `data/requirements/participium-faq.pdf`
- one OpenAPI or Swagger document per project, e.g. `projects/participium-team09/swagger.yaml`

Requirement documents and student projects are local inputs and are ignored by Git, so
`data/sut_manifest.yaml` is the only durable record of how the applications were run.
Relative paths resolve from the repository working directory.

One configuration lists every project, rather than one configuration per project: the
requirement corpus is shared, so analysing it once per run and reusing it guarantees that
each project is assessed against exactly the same reading of the requirements. Five projects
in `participium.example.yaml` point at a fuller specification deeper in their repository
than the one at their root -- the same operations, but with the schemas the root document
only names by reference actually defined.

## Setup

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Environment variables exported by the shell take precedence over `.env`. Never commit `.env`.

### Local LLM setup (LM Studio, default)

The default provider (`llm.provider: lmstudio`) runs entirely locally via
[LM Studio](https://lmstudio.ai/)'s built-in OpenAI-compatible server. No API key is required.

1. Install LM Studio and use its model search to download a GGUF build of the model you want to
   run, for example Qwen3.5 9B quantized `Q4_K_M`, which needs roughly 6.5 GB for its
   weights.
2. When loading the model, set its context length as high as your GPU comfortably allows (e.g.
   65536 on a 16 GB card) — see "Context length and `max_tokens`" below for why this matters.
3. Start the local server from LM Studio's Developer tab (default `http://localhost:1234`).
4. Find the exact model identifier LM Studio expects for API calls — either from the server logs
   or `GET http://localhost:1234/v1/models`. The model is always configuration-driven; no model
   identifier is hardcoded in Python.
5. In `.env`, set `LMSTUDIO_MODEL` to that identifier. Only set `LMSTUDIO_BASE_URL` if your
   server does not run on the default host/port.

#### LM Studio load settings (16 GB VRAM)

These settings run Qwen3.5 9B `Q4_K_M` fully on the GPU with a 64k context window, and are
the configuration behind every figure in this repository:

| Setting | Value | Why |
| --- | --- | --- |
| Quantization | `Q4_K_M` | ~6.5 GB of weights |
| Context Length | `65536` | Fits alongside the weights on a 16 GB card |
| GPU Offload | max (all layers) | Partial CPU offload costs roughly an order of magnitude in speed |
| Flash Attention | on | Reduces attention memory |
| K/V Cache Quantization | none | Unquantized; there is room for it at this VRAM |

After loading, verify GPU Offload sits at its maximum. If LM Studio silently leaves layers
on the CPU, lower the context length until the whole model fits: this was measured, not
assumed, and the difference was ~27 tokens/sec fully resident against ~4 tokens/sec with
partial offload.

Local inference is slower than a hosted API and the first call after loading a model can
take a while. The LM Studio client's default request timeout is 1200 seconds
(`llm.timeout_seconds`) to accommodate reasoning-heavy calls.

#### Context length and `max_tokens`

Qwen3.5 is a hybrid-reasoning model. LM Studio returns its chain-of-thought in a separate
`reasoning_content` field rather than mixing it into the answer, but that reasoning consumes
tokens from the same window as the prompt and the output.

The consequence is that `max_tokens` is not merely a length cap for a reasoning agent: it is
also that agent's deliberation budget, and the right value differs per agent rather than
globally. The example configuration sets 20000 for the run and overrides the
Requirement/API Matcher down to 9000, because raising the matcher's budget made it markedly
more literal — on one project it began returning `not_assessable` where the lower budget
returned matches, costing recall. `llm.reasoning_agents` controls which agents may reason at
all; only the matcher does by default.

As a safety net, `LMStudioLLMClient` retries once with a 50% larger budget when a call
returns empty content with `finish_reason: "length"`, and if reasoning still runs away it
makes a final attempt with reasoning disabled so the model emits its answer rather than
failing the run.

### Optional: using Groq instead

Groq remains available as a cloud fallback provider. Set `llm.provider: groq` in the YAML config,
then configure a model identifier and API key in `.env`:

```dotenv
GROQ_MODEL=your-configured-groq-model
GROQ_API_KEY=your-secret-key
```

One model previously used for development runs is:

```dotenv
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
```

## The commands

Every subcommand takes `--config` and, except for `plan`, a `--run-dir` naming a run to
continue. A run directory is the unit of work: each stage reads the previous stage's
artifacts from it and writes its own beside them.

```bash
# 1. Plan. Creates data/runs/<run_id>/ with one folder per project.
python -m thesis_rest_tester.cli plan --config configs/participium.example.yaml

# 2. Generate an executable pytest suite per project.
python -m thesis_rest_tester.cli generate   --config configs/participium.example.yaml --run-dir data/runs/<run_id>

# 3. Execute each suite against its application, started in Docker.
python -m thesis_rest_tester.cli execute --run-dir data/runs/<run_id> --reset-state

# 4. Score the run and classify every failure by cause.
python -m thesis_rest_tester.cli evaluate --run-dir data/runs/<run_id>

# 5. Repair, re-execute and re-evaluate until the suites stop improving.
python -m thesis_rest_tester.cli loop   --config configs/participium.example.yaml --run-dir data/runs/<run_id>
```

`--dry-run` on `plan` and `generate` replaces every model call with deterministic responses,
so the wiring can be exercised without a server; the input documents are still loaded and
validated. `--project NAME` is repeatable on `generate`, `execute`, `evaluate` and `replan`
and limits the stage to those projects. `replan` plans the strategy again on a completed run
while reusing its coverage decisions, which is what the loop uses internally.

Two flags on `execute` are worth knowing. `--reset-state` deletes each project's declared
`reset_paths` first: bind mounts survive `docker compose down -v`, so without it a second
execution inherits the first one's data. `--attempt-unverified` also tries projects the
manifest marks `unverified` but for which it already records a full recipe — which is how
such a project earns promotion to `runnable`.

> **`evaluate` is a write, not a read.** It recomputes from whatever execution reports are on
> disk and overwrites `evaluation_report.iteration<N>.json`, defaulting to iteration 1. Run
> it against a run whose suites have since been rewritten and you overwrite the baseline
> with the current state. Pass `--iteration` deliberately.

## Bringing the applications up

`data/sut_manifest.yaml` is the durable record of how each system under test is started,
since the projects themselves are gitignored. Each entry carries the compose files to use,
the services to start, the API's base URL including any path prefix, a readiness probe, and
a `provenance` label saying whether the project ran as delivered (`original`), needed an
environment file written for it (`env_supplied`), or needed a service its own compose file
omits (`compose_extended`).

Supporting artifacts live beside it: `data/sut_env/` holds the environment files, and
`data/compose_overlays/` the overlays that add a missing service. Both are gitignored where
they carry credentials; their README records what was supplied and why.

The base URL is the part that most often goes wrong, and the rule is not the obvious one: a
path prefix belongs in the manifest only when the project's own OpenAPI document does not
already spell it into its paths. Getting it wrong 404s every request and looks like a broken
application rather than a broken harness.

## Planning safeguards

The pipeline treats deterministic documentation as authoritative and LLM output as an enrichment:

- all XLSX requirement IDs, roles, business values, and core texts are preserved;
- omitted or renumbered LLM requirements cannot remove or corrupt XLSX traceability;
- description/FAQ-only requirements are not added as standalone requirements;
- every normalized Swagger method/path remains present after API analysis;
- deterministic registration, resource, assignment, messaging, and state dependencies are merged
  with model-inferred dependency edges;
- authenticated operations receive authentication setup;
- path-parameter operations receive resource setup when needed;
- mutating operations receive cleanup guidance;
- stateful tests are added from dependency edges when the model omits them;
- over-budget strategies are reduced while preserving required test types and maximizing distinct
  requirement/operation coverage;
- accepted strategies must include happy-path, edge-case, negative, and—when applicable—stateful
  tests, mixed priorities, valid traceability, and at least 80% budget utilization when possible.

These safeguards improve structure and traceability without silently treating an LLM response as
ground truth. Semantic quality still requires measurement and, later, execution feedback.

## Run artifacts

Everything a run produces stays under `data/runs/<run_id>/`, with shared artifacts at the
top and one folder per project beneath. A run is self-describing: `config.resolved.yaml`
records the configuration that produced it, and every model response is saved verbatim
before it is parsed.

| Stage | Written at the top level | Written per project |
| --- | --- | --- |
| plan | `config.resolved.yaml`, `requirements_analysis.json`, `requirements_compact.txt` | `openapi_operations.json`, `api_analysis.json`, `requirement_coverage.json`, `test_strategy.json`, `workflow_plan.json` |
| generate | `usage.generation.json` | `suite/` -- the pytest module, `conftest.py`, and `generation_report.json` recording which strategy item produced which test |
| execute | `execution_report.json`, `execution_summary.md` | `execution/report.json`, `http_exchanges.json`, `junit.xml`, `pytest_stdout.txt` |
| evaluate | `evaluation_report.iteration<N>.json`, `evaluation.iteration<N>.md` | -- |
| loop | `feedback_loop.json`, `replan.json`, `usage.feedback_loop.json` | `feedback/` -- the notes written for each repair |

`generation_report.json` carries the join that makes targeted repair possible: which
strategy item produced which test. It cannot be reconstructed afterwards, since a test
records its requirement and type but not its endpoint, and two items may share that pair.

If the first strategy draft fails diversity, traceability, stateful-flow, setup, or cleanup checks,
`test_strategy.attempt1.raw.txt` is also retained and the planner receives one corrective call when
the configured LLM-call budget permits it.

Boundary-only Markdown JSON fences are normalized during parsing. If any agent returns malformed
JSON or a schema-invalid value, one automatic repair call is made and the original response is
retained as `<agent>.validation_attempt1.raw.txt`. Arbitrary prose and multiple JSON values remain
invalid so parsing cannot silently accept ambiguous output.

Raw model output is written before JSON parsing, so malformed responses remain available for
debugging. Resolved configuration artifacts never contain the Groq API key (LM Studio does not use
an API key at all).

`workflow_plan.json` is the canonical planning output the Generator consumes. It combines the
validated requirements analysis, API analysis, strategy items, assumptions, risks, and run
metadata.

## Quality checks

```bash
pytest
ruff check .
```

The suite runs without a model server, without Docker and without the gitignored inputs:
every stage is exercised against fixtures or a stub HTTP server. It covers configuration
loading and environment expansion, input parsing, dry-run orchestration, CLI behaviour,
fence normalization and schema repair, source-ID preservation, dependency inference,
strategy correction and the budget and coverage gates, suite rendering, the executor's
compose lifecycle, every failure-attribution rule, targeted regeneration, usage accounting,
and the feedback loop driven from a fixture evaluation report.

Each attribution rule is tested from a fixture reproducing the evidence that motivated it,
so a rule cannot be quietly loosened without a test saying which real failure it stopped
recognising.

## Offline Coverage Evaluation

Manual knowledge about which user stories a team implemented is used only as a post-run oracle, not
as an input to planning. After a run completes, compare the inferred requirement coverage with a
ground-truth YAML file:

```bash
python -m thesis_rest_tester.cli evaluate-coverage \
  --run-dir data/runs/<run_id> \
  --ground-truth data/ground_truth/participium_implemented_stories.yaml
```

This writes `coverage_evaluation.json`, `coverage_evaluation.csv`, and
`coverage_evaluation.md` inside the run directory with true positives, false positives, false
negatives, true negatives, precision, recall, and F1 for each project.

## Analysis scripts

`scripts/` holds the measurements that are not part of the pipeline but are needed to
interpret it. Each is standalone and takes a run directory.

| Script | What it answers |
| --- | --- |
| `attainable_pass_rate.py` | What the pass rate is over the tests a generator could have made pass, excluding failures attributable to the application by deterministic rule |
| `check_setup_discipline.py` | How many tests that authenticate register an account first — the defect that dominated the first campaign's setup failures |
| `refresh_run_operations.py` | Re-reads the contracts into a run's `openapi_operations.json`, so a loader fix can reach a run without replanning it |
| `repeated_runs.py` | Runs the planning pipeline N times and reports mean and standard deviation, since one run cannot support a claim about a configuration change |
| `consolidate_runs.py` | Merges per-project runs into one run directory |
| `rebuild_swagger_from_run.py` | Reconstructs a project's `swagger.yaml` from a run's normalized operations, for when the gitignored input is lost |

## Current limitations

- seeded-fault injection is designed but not implemented, so no fault-detection figure is
  reported;
- `budget.max_llm_calls` is consulted by the planner rather than enforced globally: a run
  that issued more calls than the ceiling allows would not be stopped;
- the loop's stopping rule asks whether *any* project improved, which on a corpus of this
  size is cleared almost by construction, so it effectively never halts before the iteration
  limit;
- failure attribution is deterministic and therefore bounded by the rules written so far;
  the `unknown` bucket has never fallen below a quarter of all failures;
- interrupted runs cannot resume from their validated intermediate artifacts;
- the Requirements Analyst sends the full description, FAQ and every user story in one
  uncapped prompt; no chunking is implemented, so a larger corpus would need it.
