"""Benchmark sandbox backends and find the cheapest one for your workload.

Spins up each backend, runs a benchmark workload, measures cold-start and
execution time, then estimates cost using the bundled pricing data.

Usage:
    python examples/find_cheapest.py
    python examples/find_cheapest.py --backends local,docker,e2b
    python examples/find_cheapest.py --sequential
"""

import argparse
import concurrent.futures
import json
import sys
import time
from dataclasses import asdict, dataclass

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.pricing import cost_per_second, get_backend_pricing, list_backends

BENCHMARK_CODE = """
import math, hashlib, json
result = sum(math.sqrt(i) for i in range(10_000))
h = hashlib.sha256(json.dumps({"result": result}).encode()).hexdigest()
print(h)
"""


@dataclass
class BenchmarkResult:
    backend: str
    cold_start_secs: float
    exec_secs: float
    total_secs: float
    estimated_cost_usd: float
    success: bool
    error: str = ""


def benchmark_backend(backend: str) -> BenchmarkResult:
    sb = None
    try:
        t0 = time.perf_counter()
        sb = Sandbox(backend)
        cold_start = time.perf_counter() - t0

        t1 = time.perf_counter()
        result = sb.execute_code(BENCHMARK_CODE)
        exec_time = time.perf_counter() - t1

        total = cold_start + exec_time
        cps = cost_per_second(backend)
        cost = cps * total

        return BenchmarkResult(
            backend=backend,
            cold_start_secs=round(cold_start, 3),
            exec_secs=round(exec_time, 3),
            total_secs=round(total, 3),
            estimated_cost_usd=cost,
            success=result.exit_code == 0,
        )
    except Exception as e:
        return BenchmarkResult(
            backend=backend,
            cold_start_secs=0,
            exec_secs=0,
            total_secs=0,
            estimated_cost_usd=float("inf"),
            success=False,
            error=str(e),
        )
    finally:
        if sb is not None:
            sb.destroy()


def find_cheapest(backends: list[str], parallel: bool = True) -> list[BenchmarkResult]:
    if parallel:
        with concurrent.futures.ThreadPoolExecutor() as ex:
            results = list(ex.map(benchmark_backend, backends))
    else:
        results = [benchmark_backend(b) for b in backends]

    results.sort(key=lambda r: (not r.success, r.estimated_cost_usd))
    return results


def print_table(results: list[BenchmarkResult]) -> None:
    print(f"\n{'Backend':<12} {'Cold(s)':<10} {'Exec(s)':<10} {'Total(s)':<10} {'Est. Cost $':<14} {'OK'}")
    print("-" * 70)
    for r in results:
        status = "yes" if r.success else f"no  {r.error[:30]}"
        print(f"{r.backend:<12} {r.cold_start_secs:<10} {r.exec_secs:<10} {r.total_secs:<10} {r.estimated_cost_usd:<14.8f} {status}")

    successful = [r for r in results if r.success]
    if successful:
        winner = successful[0]
        print(f"\n-> Cheapest available: {winner.backend} (${winner.estimated_cost_usd:.8f}/run)")


def print_pricing_table(backends: list[str]) -> None:
    print(f"\n{'Backend':<12} {'$/vCPU-hr':<12} {'$/GiB-hr':<12} {'Free tier':<30} {'Source'}")
    print("-" * 90)
    for b in backends:
        info = get_backend_pricing(b)
        if info is None:
            print(f"{b:<12} {'?':<12} {'?':<12} {'?':<30} ?")
            continue
        vcpu = info.get("vcpu_per_hour_usd", 0.0)
        ram = info.get("ram_gib_per_hour_usd", 0.0)
        free = info.get("free_tier") or "-"
        url = info.get("pricing_url") or "-"
        print(f"{b:<12} {vcpu:<12.4f} {ram:<12.4f} {free:<30} {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark sandbox backends and find the cheapest")
    parser.add_argument(
        "--backends",
        default="local,docker",
        help=f"Comma-separated backends to benchmark (available: {', '.join(list_backends())})",
    )
    parser.add_argument("--sequential", action="store_true", help="Run benchmarks sequentially instead of in parallel")
    parser.add_argument("--pricing-only", action="store_true", help="Just print pricing table, don't benchmark")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]

    if args.pricing_only:
        print_pricing_table(backends)
        return

    if not args.json:
        print(f"Benchmarking: {', '.join(backends)}")
        print_pricing_table(backends)
        print("\nRunning benchmarks...")

    results = find_cheapest(backends, parallel=not args.sequential)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print_table(results)


if __name__ == "__main__":
    sys.exit(main() or 0)
