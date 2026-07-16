# Participium REST Tester

This MSc thesis project investigates automated black-box REST API test generation using
LLM-based agents and a metric-guided feedback loop. The systems under test are independent student
implementations of the Participium requirements, exposed through a common REST API contract.

## Current scope

The repository currently implements the workflow-preparation stage:

1. load the Participium description, FAQ, and user stories;
2. normalize a student project's OpenAPI or Swagger document;
3. run requirements, API-understanding, and test-strategy planning agents;
4. assemble a validated workflow plan and save reproducible run artifacts.

The XLSX remains authoritative for user-story IDs, roles, business values, and core text. LLM
analysis enriches those rows but cannot omit, renumber, or add requirements from the description or
FAQ. The two PDFs provide context for constraints, domain rules, edge cases, and assumptions only.
API operations are likewise reconciled to Swagger, deterministic dependency edges are added for
common state/resource flows, and the strategy planner must pass semantic quality gates before a plan
is accepted.

The current planning flow is:

```text
PDF/XLSX requirements -> deterministic loading -> Requirements Analyst -> source reconciliation
Swagger/OpenAPI       -> deterministic loading -> API Understanding   -> dependency enrichment
validated analyses + budget                         -> Strategy Planner -> quality finalization
                                                                  -> WorkflowPlan
```

Agents exchange validated Python objects through the Orchestrator. JSON files are persisted as
audit and reproducibility artifacts; they are not used as an ad-hoc message bus between agents.

It does **not** generate executable tests, call the SUT, reset SUT state, calculate metrics, or run
the iterative feedback loop yet.

The default future Python test format is **pytest + requests**. Generated tests will use explicit
timeouts, configuration-driven base URLs, isolated fixtures, and cleanup teardown. Newman/Postman
remains available as a second runner backend.

## Inputs

Place local inputs at the paths referenced by your YAML configuration. The example expects:

- `data/requirements/participium-description.pdf`
- `data/requirements/participium-userstories.xlsx`
- `data/requirements/participium-faq.pdf`
- `projects/participium-team09/swagger.yaml`
- a running SUT base URL, defaulting to `http://localhost:8080`

Requirement documents and student projects are local inputs and are ignored by Git. Relative paths
are resolved from the repository working directory. For additional student systems, create one
configuration per project and change `project_name`, `openapi_path`, and `sut_base_url`, for example:

```text
configs/participium-team09.yaml
configs/participium-team10.yaml
```

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
   run, for example Qwen3.5 9B quantized `Q4_K_M`. An 8GB-VRAM GPU (e.g. RTX 3070) can run a 9B
   `Q4_K_M` model.
2. When loading the model, set its context length as high as your GPU comfortably allows (e.g.
   16384) — see "Context length and `max_tokens`" below for why this matters.
3. Start the local server from LM Studio's Developer tab (default `http://localhost:1234`).
4. Find the exact model identifier LM Studio expects for API calls — either from the server logs
   or `GET http://localhost:1234/v1/models`. The model is always configuration-driven; no model
   identifier is hardcoded in Python.
5. In `.env`, set `LMSTUDIO_MODEL` to that identifier. Only set `LMSTUDIO_BASE_URL` if your
   server does not run on the default host/port.

Local inference on a consumer GPU is much slower than Groq's cloud API and the first call after
loading a model can take a while. A real planning run with a non-trivial `budget.max_llm_calls`
may take several minutes; the LM Studio client's default request timeout is 1200 seconds
(20 minutes, `llm.timeout_seconds` in the YAML config) to accommodate reasoning-heavy models.

#### Context length and `max_tokens`

Qwen3.5 is a hybrid-reasoning model: LM Studio returns its chain-of-thought in a separate
`reasoning_content` field (not mixed into the final `content`), but that reasoning still consumes
real tokens from the same shared context window as the prompt and the final answer. In testing, a
trivial one-line request used almost 500 reasoning tokens before producing any visible output —
non-trivial planning prompts should be expected to use substantially more.

The model's loaded context length (set in LM Studio when loading the model) is the total budget
shared by the input prompt, the reasoning, and the output content combined. `llm.max_tokens` in
the YAML config only caps output (reasoning + content); it does not by itself guarantee there is
room left in the context window. Measured against this repository's sample requirement documents
and a real student project's OpenAPI document, prompts ranged from ~5,100 tokens (Requirements
Analyst) to ~6,850 tokens (Requirement/API Matcher, which scales with operation and requirement
count); reasoning alone consumed most of an 8,000-token completion budget on the matcher call for
a moderately sized project.

Context length is also a VRAM tradeoff, not just a quality knob: on an 8GB card, raising context
length past what fits alongside the model's weights and KV cache forces LM Studio to offload some
model layers to CPU, which cratered generation speed from ~27 tokens/sec (fully on GPU at a 16384
context length) to ~4 tokens/sec (partial CPU offload at a 24576 context length) in testing on an
RTX 3070. A context length that keeps the whole model on GPU is almost always the better tradeoff
even though it caps how large `max_tokens` can safely be. The example config uses a 16384 context
length (set in LM Studio) with `max_tokens: 9000`, which fits the prompts measured so far with some
headroom. Larger student projects (more OpenAPI operations) may need a larger context length
and/or `max_tokens` — check whether it still fits fully on GPU before committing to a larger value.
`max_tokens` only acts as a ceiling — the model stops on its own (`finish_reason: "stop"`) once it
has produced a complete answer, so setting it generously has no real downside as long as it still
fits the context window. As a safety net, `LMStudioLLMClient` automatically retries once with a 50%
larger `max_tokens` if a call comes back with empty content and `finish_reason: "length"` (i.e. the
model exhausted its budget on reasoning before writing any visible output) — but that retry can
still fail if the context window itself is the real constraint, in which case the fix is a larger
context length (accepting the GPU/CPU offload tradeoff above) rather than a larger `max_tokens`.

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

## Prepare a workflow plan

Dry-run mode replaces all LLM calls with deterministic JSON and does not require an API key. It
still loads and validates the configured PDF, XLSX, and OpenAPI files.

```bash
python -m thesis_rest_tester.cli plan \
  --config configs/participium.example.yaml \
  --dry-run
```

For a real run against the local LM Studio server (default provider), with the server started and
`LMSTUDIO_MODEL` set in `.env`:

```bash
python -m thesis_rest_tester.cli plan --config configs/participium.example.yaml
```

For a real Groq run instead, set `llm.provider: groq` in the config, then:

```bash
export GROQ_API_KEY="..."
export GROQ_MODEL="..."
python -m thesis_rest_tester.cli plan --config configs/participium.example.yaml
```

The model is always configuration-driven; no model identifier is hardcoded in Python.
The Groq SDK retries transient and rate-limit failures. On low-rate-limit tiers, a real planning
run may pause between calls while the token-per-minute window resets.

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

Each plan is stored under `data/runs/<run_id>/`:

- `config.resolved.yaml`
- `requirements_compact.txt`
- `openapi_operations.json`
- `requirements_analysis.raw.txt` and `requirements_analysis.json`
- `api_analysis.raw.txt` and `api_analysis.json`
- `test_strategy.raw.txt` and `test_strategy.json`
- `workflow_plan.json`
- `summary.md`

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

`workflow_plan.json` is the canonical planning output for future generation agents. It combines the
validated requirements analysis, API analysis, strategy items, assumptions, risks, and run metadata.

## Quality checks

```bash
pytest
ruff check .
```

The test suite covers configuration loading, environment expansion, input parsing, dry-run
orchestration, CLI behavior, fence normalization, schema repair, source-ID preservation, dependency
inference, strategy correction, authentication setup, cleanup, and budget/coverage gates.

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

## Current limitations

- generation prompts exist, but generation agents are not implemented;
- no pytest/requests or Newman suite is generated or executed yet;
- the configured SUT base URL and reset command are not used during planning;
- metrics are modeled but their collectors still raise `NotImplementedError`;
- `max_iterations` and the feedback/stop loop are not active yet;
- `max_llm_calls` permits planner correction but is not yet tracked globally;
- interrupted runs cannot currently resume from their validated intermediate artifacts;
- role vocabulary and requirement/API contradictions still need explicit normalization/reporting;
- the Requirements Analyst call sends the full description PDF, FAQ PDF, and every user story in
  one uncapped prompt (`RequirementsLoader._compact()`); Groq's hosted context window absorbs this
  easily, but a local model's practical context length is bounded by available VRAM. Validate this
  empirically against your actual requirement documents when running locally, and increase LM
  Studio's context length setting if the prompt is being truncated; no chunking is implemented yet.

## Planned next steps

The next useful increment is to define an executable test-case model and implement the Happy-Path,
Edge-Case, Adversarial, and Test Writer agents. The Test Writer will emit pytest modules using
`requests`; later increments can implement their static runner, the Newman runner, SUT reset hooks,
metric collection, and the feedback loop that routes evaluation back through the Orchestrator.
The modular provider and runner interfaces are already in place for those additions and for future
collaborative or competitive agent modes.
