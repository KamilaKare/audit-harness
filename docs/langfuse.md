# Langfuse tracing

The implementation follows the installed [Langfuse skill](../skills/langfuse/SKILL.md), its [instrumentation reference](../skills/langfuse/references/instrumentation.md), and the current [tracing best practices](https://langfuse.com/docs/observability/best-practices). Public SDK context managers provide explicit capture; automatic LangChain callbacks would serialize this application's graph state, including confidential evidence. For this custom HTTP provider, explicit generation observations retain provider independence and control over data leaving the process.

## Configuration

Install the optional SDK dependency:

```sh
uv sync --locked --extra dev --extra observability
```

Add your project keys to `.env` (preserve existing LLM and application settings):

```dotenv
AUDIT_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-your-project-key
LANGFUSE_SECRET_KEY=sk-lf-your-project-secret
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_RELEASE=audit-harness@0.1.0
LANGFUSE_SAMPLE_RATE=1.0
```

Use the base URL for your own Langfuse deployment. Values are read explicitly from `.env` by `LangfuseSettings`, even when they are not exported in your shell. Secrets are represented by Pydantic `SecretStr` and never emitted as observation attributes. Restart the API after changing keys because the SDK client is cached per process. An LLM key and a Langfuse project key are separate credentials.

The lockfile currently resolves SDK **4.18.0**, which is installed and tested in this environment. The dependency range permits **5.x**; upstream's latest release was **5.0.4** when this integration was added. Registry access is unavailable inside the agent's terminal, so a v5 download and runtime test could not be completed here. Public APIs were checked against the [v5 migration guide](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5). Upgrade in a connected terminal and rerun the suite before changing the locked SDK:

```sh
uv lock --upgrade-package langfuse
uv sync --locked --extra dev --extra observability
uv run --extra dev --extra observability pytest
```

## Send a test run

Run this from your normal terminal. It uses synthetic data and the offline simulator, so it needs Langfuse credentials but no LLM credentials:

```sh
AUDIT_LANGFUSE_ENABLED=true AUDIT_PROVIDER=offline \
  uv run --locked --extra observability audit-harness run --output artifacts/traced-run.json
```

The output bundle includes `run_id` and a `traces` array mapping transaction IDs to trace IDs. In Langfuse, use the session matching that run ID to inspect the transaction traces. To verify actual generations and token usage, repeat with `AUDIT_PROVIDER=openai-compatible` and your configured LLM key. The simulator is labeled `simulated=true` and does not create paid-generation observations.

For programmatic inspection, use the Langfuse CLI. Discover the installed CLI's action names and arguments first:

```sh
npx langfuse-cli api __schema
npx langfuse-cli api observations --help
```

Supply the same project credentials and base URL in the CLI environment. Do not paste keys into chat or log them. Follow the action help to filter observations by a trace ID from the artifact. Use the modern observations resource; legacy trace-read endpoints are not used by this application.

## Expected structure

```text
audit.test-transaction                     one trace per transaction
  control.evaluate                        C1, C2, C3 are sibling controls
    harness.select                        scope and threshold result
    harness.collect [retriever]           evidence kinds, IDs, hashes, error count
    harness.test [evaluator]              deterministic test statuses
    harness.analyze
      llm.context [generation or span]     model, usage, prompt version, confidence
    harness.decide [guardrail]            outcome, review flag, applicability

audit.persist-run                         separate trace, same run session
```

Each observation is created once; there are no duplicate OpenTelemetry wrapper spans. Stable names support filtering and later evaluation. Every transaction root propagates the run session ID, control configuration hash, transaction ID, provider/model context and Purchase-to-Pay tags to its descendants. Environment, release and sampling come from the SDK configuration. No unauthenticated reviewer identifier is represented as an authenticated Langfuse user.

Token counts are taken from the actual HTTP provider response and kept in a context variable per generation, so concurrent calls cannot overwrite one another. Reported input, output and total tokens are preserved without guessed costs. Langfuse can price supported models from that usage. Providers that omit usage do not get fabricated token counts.

## Privacy and failure behavior

Only explicitly selected, structured summaries are traced. Evidence contents and complete model messages remain in the local audit bundle; traces carry evidence IDs and SHA-256 hashes for correlation. The model's free-text rationale is not exported. This limits trace-only qualitative evaluation: reviewers need the protected local bundle for source content.

The SDK exports its own spans only, not arbitrary HTTP/framework spans. A second masking hook strips accidental sensitive fields from input/output and metadata at export. Business exceptions are reported using their class name and ERROR status, without SDK automatic exception events that might contain document text, API response bodies or keys. Telemetry errors are logged as fixed categories and do not replace control outcomes.

CLI execution flushes in `finally`, and API shutdown flushes off the event loop. The application does not shut down a shared global OTel provider. A successful `flush()` means the client attempted export, not proof that the cloud accepted it. Fetch the resulting observations or inspect the session before declaring live tracing verified.

## Verification status

The automated suite exercises the real Langfuse SDK with an in-memory exporter, complete graph execution, all 120 synthetic transactions, a mocked real HTTP provider response, parent-child relationships, generation usage, privacy filtering and concurrent calls. Tests ignore a developer's external-service settings so they cannot accidentally send test fixtures using local keys.

A cloud smoke execution generated trace ID `56280a085515da2380469b35ac7a3665`; receipt has **not** been verified. The agent's command environment cannot resolve external hosts, including Langfuse, and cannot download the Langfuse CLI. Your regular terminal successfully reached PyPI and Langfuse, so no Mac DNS change is indicated. Run the test command above there and inspect the returned run session to complete the live audit required by the skill.
