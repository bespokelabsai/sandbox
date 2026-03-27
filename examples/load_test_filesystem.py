from __future__ import annotations

import argparse
import json
import statistics
import textwrap
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from bespokelabs.sandbox import Sandbox


@dataclass(frozen=True)
class WorkloadSpec:
    name: str
    large_file_bytes: int
    small_file_count: int
    small_file_bytes: int


@dataclass(frozen=True)
class MetricSummary:
    mean: float | None
    p50: float | None
    p95: float | None
    min: float | None
    max: float | None

    @classmethod
    def from_values(cls, values: list[float]) -> MetricSummary:
        if not values:
            return cls(None, None, None, None, None)
        return cls(
            mean=round(statistics.fmean(values), 1),
            p50=round(_pct(values, 0.50), 1),
            p95=round(_pct(values, 0.95), 1),
            min=round(min(values), 1),
            max=round(max(values), 1),
        )


@dataclass
class Sample:
    backend: str
    workload: str
    ok: bool
    create_ms: float | None
    exec_ms: float | None
    total_ms: float
    exit_code: int | None
    error: str | None
    metrics: dict[str, float | int | bool] | None


@dataclass
class BackendWorkloadReport:
    backend: str
    workload: str
    requests: int
    concurrency: int
    successes: int
    failures: int
    success_rate: float
    create_ms: MetricSummary
    exec_ms: MetricSummary
    total_ms: MetricSummary
    large_write_ms: MetricSummary
    large_read_ms: MetricSummary
    small_write_ms: MetricSummary
    list_stat_ms: MetricSummary
    small_read_ms: MetricSummary
    top_errors: list[tuple[str, int]]
    samples: list[Sample]


@dataclass
class LoadTestReport:
    generated_at: str
    backends: list[str]
    workloads: list[WorkloadSpec]
    requests: int
    concurrency: int
    timeout_secs: int
    cpu: float
    memory_mb: int
    fsync: bool
    results: list[BackendWorkloadReport]


WORKLOADS = {
    "light": WorkloadSpec(
        name="light",
        large_file_bytes=16 * 1024 * 1024,
        small_file_count=200,
        small_file_bytes=16 * 1024,
    ),
    "heavy": WorkloadSpec(
        name="heavy",
        large_file_bytes=64 * 1024 * 1024,
        small_file_count=1000,
        small_file_bytes=16 * 1024,
    ),
}


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def _bytes_label(num_bytes: int) -> str:
    mib = num_bytes / (1024 * 1024)
    if mib >= 1:
        return f"{mib:.0f} MiB"
    kib = num_bytes / 1024
    return f"{kib:.0f} KiB"


def _build_benchmark_code(workload: WorkloadSpec, fsync: bool) -> str:
    if fsync:
        helpers = textwrap.dedent(
            """
            def write_file(path: Path, data: bytes) -> None:
                with open(path, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())

            def fsync_dir(path: Path) -> None:
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            """
        ).strip()
        large_write = textwrap.dedent(
            """
                write_file(large_path, large_blob)
                fsync_dir(root)
            """
        ).strip()
        small_write = textwrap.dedent(
            """
                for i in range(small_count):
                    write_file(small_dir / ("f_%04d.bin" % i), small_blob)
                fsync_dir(small_dir)
            """
        ).strip()
    else:
        helpers = textwrap.dedent(
            """
            def write_file(path: Path, data: bytes) -> None:
                path.write_bytes(data)
            """
        ).strip()
        large_write = "write_file(large_path, large_blob)"
        small_write = textwrap.dedent(
            """
                for i in range(small_count):
                    write_file(small_dir / ("f_%04d.bin" % i), small_blob)
            """
        ).strip()

    template = """
import json
import os
import shutil
import time
from pathlib import Path

root = Path(".fsbench_%%d" %% time.time_ns())
root.mkdir()
large_path = root / "large.bin"
small_dir = root / "small"
small_dir.mkdir()
large_size = %(large_size)d
small_count = %(small_count)d
small_size = %(small_size)d
large_blob = b"L" * large_size
small_blob = b"s" * small_size

metrics = {
    "large_bytes": large_size,
    "small_count": small_count,
    "small_bytes_each": small_size,
    "small_total_bytes": small_count * small_size,
}

%(helpers)s

try:
    t0 = time.perf_counter()
%(large_write)s
    metrics["large_write_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    large_read = large_path.read_bytes()
    metrics["large_read_ms"] = (time.perf_counter() - t0) * 1000
    metrics["large_read_ok"] = len(large_read) == large_size

    t0 = time.perf_counter()
%(small_write)s
    metrics["small_write_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    listed = sorted(small_dir.glob("*.bin"))
    total_stat = 0
    for path in listed:
        total_stat += path.stat().st_size
    metrics["list_stat_ms"] = (time.perf_counter() - t0) * 1000
    metrics["listed_count"] = len(listed)
    metrics["listed_bytes"] = total_stat

    t0 = time.perf_counter()
    total_read = 0
    for path in listed:
        total_read += len(path.read_bytes())
    metrics["small_read_ms"] = (time.perf_counter() - t0) * 1000
    metrics["small_read_bytes"] = total_read
finally:
    shutil.rmtree(root, ignore_errors=True)

print(json.dumps(metrics, sort_keys=True))
"""
    return template % {
        "large_size": workload.large_file_bytes,
        "small_count": workload.small_file_count,
        "small_size": workload.small_file_bytes,
        "helpers": textwrap.indent(helpers, ""),
        "large_write": textwrap.indent(large_write, "    "),
        "small_write": textwrap.indent(small_write, "    "),
    }


def run_once(
    backend: str,
    workload: WorkloadSpec,
    *,
    timeout_secs: int,
    cpu: float,
    memory_mb: int,
    fsync: bool,
) -> Sample:
    started = time.perf_counter()
    sb: Sandbox | None = None
    create_ms: float | None = None
    exec_ms: float | None = None
    code = _build_benchmark_code(workload, fsync)

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

        if result.exit_code != 0:
            return Sample(
                backend=backend,
                workload=workload.name,
                ok=False,
                create_ms=create_ms,
                exec_ms=exec_ms,
                total_ms=total_ms,
                exit_code=result.exit_code,
                error=result.stderr.strip() or result.stdout.strip() or "non-zero exit",
                metrics=None,
            )

        return Sample(
            backend=backend,
            workload=workload.name,
            ok=True,
            create_ms=create_ms,
            exec_ms=exec_ms,
            total_ms=total_ms,
            exit_code=result.exit_code,
            error=None,
            metrics=json.loads(result.stdout),
        )
    except Exception as exc:
        total_ms = (time.perf_counter() - started) * 1000
        return Sample(
            backend=backend,
            workload=workload.name,
            ok=False,
            create_ms=create_ms,
            exec_ms=exec_ms,
            total_ms=total_ms,
            exit_code=None,
            error=f"{type(exc).__name__}: {exc}",
            metrics=None,
        )
    finally:
        if sb is not None:
            try:
                sb.destroy()
            except Exception:
                pass


def benchmark_backend_workload(
    backend: str,
    workload: WorkloadSpec,
    *,
    requests: int,
    concurrency: int,
    timeout_secs: int,
    cpu: float,
    memory_mb: int,
    fsync: bool,
) -> BackendWorkloadReport:
    samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                run_once,
                backend,
                workload,
                timeout_secs=timeout_secs,
                cpu=cpu,
                memory_mb=memory_mb,
                fsync=fsync,
            )
            for _ in range(requests)
        ]
        for future in as_completed(futures):
            samples.append(future.result())

    ok_samples = [sample for sample in samples if sample.ok and sample.metrics is not None]
    errors = Counter(sample.error for sample in samples if not sample.ok and sample.error)

    def summarize_sample_field(field: str) -> MetricSummary:
        values = [
            float(value)
            for sample in ok_samples
            for value in [getattr(sample, field)]
            if value is not None
        ]
        return MetricSummary.from_values(values)

    def summarize_metric(name: str) -> MetricSummary:
        values = [float(sample.metrics[name]) for sample in ok_samples if name in sample.metrics]
        return MetricSummary.from_values(values)

    return BackendWorkloadReport(
        backend=backend,
        workload=workload.name,
        requests=requests,
        concurrency=concurrency,
        successes=len(ok_samples),
        failures=len(samples) - len(ok_samples),
        success_rate=round(len(ok_samples) / len(samples), 3) if samples else 0.0,
        create_ms=summarize_sample_field("create_ms"),
        exec_ms=summarize_sample_field("exec_ms"),
        total_ms=summarize_sample_field("total_ms"),
        large_write_ms=summarize_metric("large_write_ms"),
        large_read_ms=summarize_metric("large_read_ms"),
        small_write_ms=summarize_metric("small_write_ms"),
        list_stat_ms=summarize_metric("list_stat_ms"),
        small_read_ms=summarize_metric("small_read_ms"),
        top_errors=errors.most_common(3),
        samples=samples,
    )


def run_load_test(
    *,
    backends: list[str],
    workloads: list[WorkloadSpec],
    requests: int,
    concurrency: int,
    timeout_secs: int,
    cpu: float,
    memory_mb: int,
    fsync: bool,
) -> LoadTestReport:
    results: list[BackendWorkloadReport] = []
    for workload in workloads:
        for backend in backends:
            results.append(
                benchmark_backend_workload(
                    backend,
                    workload,
                    requests=requests,
                    concurrency=concurrency,
                    timeout_secs=timeout_secs,
                    cpu=cpu,
                    memory_mb=memory_mb,
                    fsync=fsync,
                )
            )

    return LoadTestReport(
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        backends=backends,
        workloads=workloads,
        requests=requests,
        concurrency=concurrency,
        timeout_secs=timeout_secs,
        cpu=cpu,
        memory_mb=memory_mb,
        fsync=fsync,
        results=results,
    )


def _fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _print_table(
    headers: list[str],
    rows: list[list[str]],
    align: list[str] | None = None,
    group_dividers: list[int] | None = None,
) -> None:
    """Print a Unicode box-drawing table.

    align:  'l' or 'r' per column (default 'l' for col 0, 'r' for the rest).
    group_dividers:  column indices where a thicker group separator (║) is used.
    """
    n_cols = len(headers)
    if align is None:
        align = ["l"] + ["r"] * (n_cols - 1)
    if group_dividers is None:
        group_dividers = []
    dividers = set(group_dividers)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _pad(text: str, width: int, a: str) -> str:
        return text.rjust(width) if a == "r" else text.ljust(width)

    def _line(left: str, mid: str, mid_thick: str, right: str, fill: str) -> str:
        parts: list[str] = []
        for i in range(n_cols):
            seg = fill * (widths[i] + 2)
            if i == 0:
                parts.append(left + seg)
            else:
                sep = mid_thick if i in dividers else mid
                parts.append(sep + seg)
        return "".join(parts) + right

    def _row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            padded = _pad(cell, widths[i], align[i])
            if i == 0:
                parts.append("│ " + padded + " ")
            else:
                sep = "║" if i in dividers else "│"
                parts.append(sep + " " + padded + " ")
        return "".join(parts) + "│"

    print(_line("┌", "┬", "╥", "┐", "─"))
    print(_row(headers))
    print(_line("├", "┼", "╫", "┤", "─"))
    for row in rows:
        print(_row(row))
    print(_line("└", "┴", "╨", "┘", "─"))


def print_report(report: LoadTestReport) -> None:
    fsync_label = "yes" if report.fsync else "no"
    print()
    print("┌─────────────────────────────────────────┐")
    print("│       Filesystem Load Test Report        │")
    print("└─────────────────────────────────────────┘")
    print(f"  Generated : {report.generated_at}")
    print(f"  Backends  : {', '.join(report.backends)}")
    print(f"  Requests  : {report.requests}  (concurrency {report.concurrency})")
    print(f"  Resources : {report.cpu} CPU / {report.memory_mb} MB")
    print(f"  fsync     : {fsync_label}")

    for workload in report.workloads:
        print()
        large = _bytes_label(workload.large_file_bytes)
        small = f"{workload.small_file_count} x {_bytes_label(workload.small_file_bytes)}"
        title = f" {workload.name.upper()} workload ({large} large, {small} small) "
        print(f"{'':─<2}{'':─<{len(title)}}{'':─<2}")
        print(f"  {title}")
        print(f"{'':─<2}{'':─<{len(title)}}{'':─<2}")

        workload_results = [r for r in report.results if r.workload == workload.name]
        rows: list[list[str]] = []
        for r in workload_results:
            rows.append(
                [
                    r.backend,
                    f"{r.successes}/{r.requests}",
                    f"{r.success_rate * 100:.0f}%",
                    _fmt_ms(r.create_ms.p50),
                    _fmt_ms(r.exec_ms.mean),
                    _fmt_ms(r.total_ms.p50),
                    _fmt_ms(r.large_write_ms.mean),
                    _fmt_ms(r.large_read_ms.mean),
                    _fmt_ms(r.small_write_ms.mean),
                    _fmt_ms(r.list_stat_ms.mean),
                    _fmt_ms(r.small_read_ms.mean),
                ]
            )

        headers = [
            "Backend",
            "OK",
            "Rate",
            "Create p50",
            "Exec mean",
            "Total p50",
            "Lg Write",
            "Lg Read",
            "Sm Write",
            "List+Stat",
            "Sm Read",
        ]
        # Group dividers after Rate (col 3) and after Total p50 (col 6)
        _print_table(headers, rows, group_dividers=[3, 6])

        for r in workload_results:
            if r.top_errors:
                errors = ", ".join(f"{count}x {err}" for err, count in r.top_errors)
                print(f"  ⚠ {r.backend}: {errors}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run filesystem load tests inside sandboxes and print a dataclass-backed report."
    )
    parser.add_argument(
        "--backends",
        default="daytona,tensorlake",
        help="Comma-separated backends to benchmark",
    )
    parser.add_argument(
        "--workloads",
        default="light,heavy",
        help="Comma-separated workload presets to run: light, heavy",
    )
    parser.add_argument("--requests", type=int, default=6, help="Total requests per backend per workload")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent in-flight requests")
    parser.add_argument("--timeout-secs", type=int, default=300, help="Sandbox timeout")
    parser.add_argument("--cpu", type=float, default=1.0, help="CPU value passed to the backend")
    parser.add_argument("--memory-mb", type=int, default=1024, help="Memory value passed to the backend")
    parser.add_argument("--fsync", action="store_true", help="Flush and fsync file writes inside the workload")
    parser.add_argument("--print-json", action="store_true", help="Also print the full dataclass report as JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backends = [backend.strip() for backend in args.backends.split(",") if backend.strip()]
    workload_names = [name.strip() for name in args.workloads.split(",") if name.strip()]

    unknown = [name for name in workload_names if name not in WORKLOADS]
    if unknown:
        raise SystemExit(f"Unknown workload(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(WORKLOADS))}")

    report = run_load_test(
        backends=backends,
        workloads=[WORKLOADS[name] for name in workload_names],
        requests=args.requests,
        concurrency=args.concurrency,
        timeout_secs=args.timeout_secs,
        cpu=args.cpu,
        memory_mb=args.memory_mb,
        fsync=args.fsync,
    )

    print_report(report)
    if args.print_json:
        print()
        print("=== json ===")
        print(json.dumps(asdict(report), indent=2))


if __name__ == "__main__":
    main()
