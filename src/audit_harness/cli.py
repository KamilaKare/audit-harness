import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .synthetic import generate


def main():
    parser = argparse.ArgumentParser(description="Purchase-to-Pay audit harness")
    sub = parser.add_subparsers(dest="command", required=True)
    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--output", type=Path, default=Path("data"))
    generate_parser.add_argument("--count", type=int, default=120)
    generate_parser.add_argument("--seed", type=int, default=42)
    for command in ("run", "evaluate"):
        p = sub.add_parser(command)
        p.add_argument("--output", type=Path, default=Path(f"artifacts/{command}.json"))
    sub.add_parser("serve")
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.output, args.count, args.seed)
        print(f"Generated {args.count} synthetic transactions at {args.output}")
        return
    if args.command == "serve":
        import uvicorn

        uvicorn.run("audit_harness.api:create_app", factory=True, host="0.0.0.0", port=8000)
        return
    from .observability import configure_observability, flush_observability

    configure_observability()
    from .evaluation import evaluate_run
    from .llm import MissingLLMKeyError
    from .service import execute
    from .storage import Store

    settings = Settings()
    store = Store(settings.database_url)
    try:
        run = asyncio.run(execute(settings, store))
        result = (
            evaluate_run(run, settings.data_dir / "ground_truth" / "labels.csv")
            if args.command == "evaluate"
            else run
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"decisions", "population", "controls"}
                },
                indent=2,
            )
        )
        if args.command == "evaluate" and not result["quality_gate_passed"]:
            raise SystemExit(1)
    except MissingLLMKeyError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    finally:
        flush_observability(settings.langfuse_enabled)
        store.engine.dispose()
