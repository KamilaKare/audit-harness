# Project guidance

Follow the repository conventions in the parent `AGENTS.md`.

For Langfuse changes, use the project-local skill at `skills/langfuse/SKILL.md`.
For instrumentation, also read `skills/langfuse/references/instrumentation.md`.
The skill and its references are installed locally so they remain available on future turns.

Keep tests independent of developer credentials and external services. Verify exported
traces with the real SDK and an in-memory exporter before attempting cloud delivery.
