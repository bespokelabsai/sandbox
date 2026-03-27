from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from bespokelabs.sandbox import Sandbox


DEFAULT_CODE = "print(sum(i * i for i in range(20000)))"


@dataclass
class Sample:
    backend: str
    ok: bool
    create_ms: float | None
    exec_ms: float | None
    total_ms: float
    exit_code: int | None
    error: str | None


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def run_once(
    backend: str,
    code: str,
    timeout_secs: int,
    cpu: float,
    memory_mb: int,
) -> Sample:
    started = time.perf_counter()
    sb: Sandbox | None = None
    create_ms: float | None = None
    exec_ms: float | None = None

    try:
        create_started = time.perf_counter()
        sb = Sandbox(
            backend,
            timeout_secs=timeout_secs,
            cpu=cpu,
            memory_mb=memory_mb,
        )
        create_ms = (time.perf_counter() - create_started) * 1000

        exec_started = time.perf_counter()
        result = sb.execute_code(code)
        exec_ms = (time.perf_counter() - exec_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000

        return Sample(
            backend=backend,
            ok=(result.exit_code == 0),
            create_ms=create_ms,
            exec_ms=exec_ms,
            total_ms=total_ms,
            exit_code=result.exit_code,
            error=None if result.exit_code == 0 else result.stderr.strip() or result.stdout.strip() or "non-zero exit",
        )
    except Exception as exc:
        total_ms = (time.perf_counter() - started) * 1000
        return Sample(
            backend=backend,
            ok=False,
            create_ms=create_ms,
            exec_ms=exec_ms,
            total_ms=total_ms,
            exit_code=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if sb is not None:
            try:
                sb.destroy()
            except Exception:
                pass


def benchmark_backend(
    backend: str,
    *,
    requests: int,
    concurrency: int,
    code: str,
    timeout_secs: int,
    cpu: float,
    memory_mb: int,
) -> dict:
    samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                run_once,
                backend,
                code,
                timeout_secs,
                cpu,
                memory_mb,
            )
            for _ in range(requests)
        ]
        for future in as_completed(futures):
            samples.append(future.result())

    ok_samples = [s for s in samples if s.ok]
    create_values = [s.create_ms for s in ok_samples if s.create_ms is not None]
    exec_values = [s.exec_ms for s in ok_samples if s.exec_ms is not None]
    total_values = [s.total_ms for s in ok_samples]
    errors = Counter(s.error for s in samples if not s.ok and s.error)

    return {
        "backend": backend,
        "requests": requests,
        "concurrency": concurrency,
        "successes": len(ok_samples),
        "failures": len(samples) - len(ok_samples),
        "success_rate": round(len(ok_samples) / len(samples), 3) if samples else 0.0,
        "create_ms": {
            "mean": round(statistics.fmean(create_values), 1) if create_values else None,
            "p50": round(_pct(create_values, 0.50), 1) if create_values else None,
            "p95": round(_pct(create_values, 0.95), 1) if create_values else None,
        },
        "exec_ms": {
            "mean": round(statistics.fmean(exec_values), 1) if exec_values else None,
            "p50": round(_pct(exec_values, 0.50), 1) if exec_values else None,
            "p95": round(_pct(exec_values, 0.95), 1) if exec_values else None,
        },
        "total_ms": {
            "mean": round(statistics.fmean(total_values), 1) if total_values else None,
            "p50": round(_pct(total_values, 0.50), 1) if total_values else None,
            "p95": round(_pct(total_values, 0.95), 1) if total_values else None,
        },
        "top_errors": errors.most_common(3),
        "samples": [asdict(sample) for sample in samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Daytona vs Tensorlake with identical sandbox workloads.")
    parser.add_argument(
        "--backends",
        default="daytona,tensorlake",
        help="Comma-separated backends to benchmark",
    )
    parser.add_argument("--requests", type=int, default=6, help="Total requests per backend")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent in-flight requests per backend")
    parser.add_argument("--timeout-secs", type=int, default=300, help="Sandbox timeout passed to the backend")
    parser.add_argument("--cpu", type=float, default=1.0, help="CPU value passed to the backend")
    parser.add_argument("--memory-mb", type=int, default=1024, help="Memory value passed to the backend")
    parser.add_argument("--code", default=DEFAULT_CODE, help="Code snippet to execute inside each sandbox")
    args = parser.parse_args()

    backends = [backend.strip() for backend in args.backends.split(",") if backend.strip()]
    report = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "timeout_secs": args.timeout_secs,
        "cpu": args.cpu,
        "memory_mb": args.memory_mb,
        "code": args.code,
        "results": [],
    }

    for backend in backends:
        print(f"=== {backend} ===", flush=True)
        result = benchmark_backend(
            backend,
            requests=args.requests,
            concurrency=args.concurrency,
            code=args.code,
            timeout_secs=args.timeout_secs,
            cpu=args.cpu,
            memory_mb=args.memory_mb,
        )
        report["results"].append(result)
        print(
            json.dumps(
                {
                    "backend": result["backend"],
                    "success_rate": result["success_rate"],
                    "create_ms": result["create_ms"],
                    "exec_ms": result["exec_ms"],
                    "total_ms": result["total_ms"],
                    "top_errors": result["top_errors"],
                },
                indent=2,
            ),
            flush=True,
        )

    print("=== full-report ===", flush=True)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
