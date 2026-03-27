from __future__ import annotations

import json
import math
import sys
import textwrap
from pathlib import Path


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT = 54
RIGHT = 54
TOP = 54
BOTTOM = 54


def escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class PDFBuilder:
    def __init__(self) -> None:
        self.pages: list[bytes] = []
        self._ops: list[str] = []
        self._y = PAGE_HEIGHT - TOP

    def _finish_page(self) -> None:
        if self._ops:
            self.pages.append("\n".join(self._ops).encode("latin-1", "replace"))
            self._ops = []
            self._y = PAGE_HEIGHT - TOP

    def _ensure_space(self, needed: float) -> None:
        if self._y - needed < BOTTOM:
            self._finish_page()

    def text(self, text: str, *, font: str = "F1", size: int = 10, leading: int | None = None) -> None:
        leading = leading or int(size * 1.35)
        width_chars = max(40, int((PAGE_WIDTH - LEFT - RIGHT) / (size * 0.58)))
        lines = textwrap.wrap(
            text,
            width=width_chars,
            replace_whitespace=False,
            drop_whitespace=False,
        ) or [""]
        self._ensure_space(len(lines) * leading + 4)
        for line in lines:
            safe = escape_pdf_text(line.rstrip())
            self._ops.append(f"BT /{font} {size} Tf 1 0 0 1 {LEFT} {self._y:.1f} Tm ({safe}) Tj ET")
            self._y -= leading

    def preformatted(self, lines: list[str], *, size: int = 9) -> None:
        leading = int(size * 1.45)
        for line in lines:
            self._ensure_space(leading + 2)
            safe = escape_pdf_text(line)
            self._ops.append(f"BT /F3 {size} Tf 1 0 0 1 {LEFT} {self._y:.1f} Tm ({safe}) Tj ET")
            self._y -= leading

    def spacer(self, height: int = 10) -> None:
        self._ensure_space(height)
        self._y -= height

    def rule(self) -> None:
        self._ensure_space(12)
        y = self._y
        self._ops.append(f"0.75 w {LEFT} {y:.1f} m {PAGE_WIDTH - RIGHT} {y:.1f} l S")
        self._y -= 12

    def build(self) -> bytes:
        self._finish_page()

        objects: list[bytes] = []

        def add_object(data: bytes) -> int:
            objects.append(data)
            return len(objects)

        font1 = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        font2 = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        font3 = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

        content_ids: list[int] = []
        page_ids: list[int] = []
        pages_obj_index = len(objects) + 1

        for content in self.pages:
            stream = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
            content_id = add_object(stream)
            content_ids.append(content_id)
            page_obj = (
                f"<< /Type /Page /Parent {pages_obj_index} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R /F3 {font3} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("latin-1")
            page_ids.append(add_object(page_obj))

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids).encode("latin-1")
        pages = b"<< /Type /Pages /Count " + str(len(page_ids)).encode() + b" /Kids [" + kids + b"] >>"
        add_object(pages)
        catalog = add_object(f"<< /Type /Catalog /Pages {pages_obj_index} 0 R >>".encode("latin-1"))

        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        xref_offsets = [0]
        for obj_id, data in enumerate(objects, start=1):
            xref_offsets.append(len(output))
            output.extend(f"{obj_id} 0 obj\n".encode("latin-1"))
            output.extend(data)
            output.extend(b"\nendobj\n")

        xref_start = len(output)
        output.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        output.extend(b"0000000000 65535 f \n")
        for offset in xref_offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))

        trailer = (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
        ).encode("latin-1")
        output.extend(trailer)
        return bytes(output)


def bytes_to_label(num_bytes: int) -> str:
    mib = num_bytes / (1024 * 1024)
    if mib >= 1:
        return f"{mib:.0f} MiB"
    kib = num_bytes / 1024
    return f"{kib:.0f} KiB"


def table_lines(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    header = "  ".join(headers[idx].ljust(widths[idx]) for idx in range(len(headers)))
    rule = "  ".join("-" * widths[idx] for idx in range(len(headers)))
    lines = [header, rule]
    for row in rows:
        lines.append("  ".join(row[idx].ljust(widths[idx]) for idx in range(len(row))))
    return lines


def ms(value: float) -> str:
    return f"{value:.1f}"


def speedup(a: float, b: float) -> str:
    if a <= 0 or b <= 0:
        return "n/a"
    faster = max(a, b) / min(a, b)
    return f"{faster:.1f}x"


def make_summary(pass_data: dict) -> list[str]:
    daytona = pass_data["results"]["daytona"]
    tensorlake = pass_data["results"]["tensorlake"]
    lines = []

    total_ratio = tensorlake["total_ms"]["p50"] / daytona["total_ms"]["p50"]
    exec_ratio = tensorlake["exec_ms"]["mean"] / daytona["exec_ms"]["mean"]
    large_write_ratio = tensorlake["large_write_ms"]["mean"] / daytona["large_write_ms"]["mean"]
    large_read_ratio = tensorlake["large_read_ms"]["mean"] / daytona["large_read_ms"]["mean"]
    small_write_ratio = daytona["small_write_ms"]["mean"] / tensorlake["small_write_ms"]["mean"]
    small_read_ratio = daytona["small_read_ms"]["mean"] / tensorlake["small_read_ms"]["mean"]

    lines.append(
        f"For {pass_data['name'].lower()}, Daytona had the lower end-to-end latency: "
        f"p50 total {ms(daytona['total_ms']['p50'])} ms vs {ms(tensorlake['total_ms']['p50'])} ms "
        f"({total_ratio:.1f}x lower)."
    )
    lines.append(
        f"Daytona also had lower execute_code wall time: mean {ms(daytona['exec_ms']['mean'])} ms vs "
        f"{ms(tensorlake['exec_ms']['mean'])} ms ({exec_ratio:.1f}x lower)."
    )
    lines.append(
        f"Within the sandbox, Daytona was stronger on the large buffered file path: "
        f"{large_write_ratio:.1f}x faster writes and {large_read_ratio:.1f}x faster reads."
    )
    lines.append(
        f"Tensorlake was stronger on the small-file path: "
        f"{small_write_ratio:.1f}x faster small-file writes and {small_read_ratio:.1f}x faster small-file reads."
    )
    if daytona["create_ms"]["p95"] > 3 * daytona["create_ms"]["p50"]:
        lines.append(
            f"Daytona showed a cold-start spike in create time: p50 {ms(daytona['create_ms']['p50'])} ms, "
            f"p95 {ms(daytona['create_ms']['p95'])} ms."
        )
    return lines


def build_report(data: dict) -> PDFBuilder:
    pdf = PDFBuilder()

    pdf.text(data["title"], font="F2", size=20)
    pdf.text(f"Generated from benchmark runs captured at {data['generated_at']}", size=11)
    pdf.spacer(4)
    pdf.rule()

    pdf.text("Executive Summary", font="F2", size=14)
    pdf.text(
        "Daytona delivered lower end-to-end sandbox latency in both passes, while Tensorlake was better on "
        "the small-file operations inside the sandbox. Daytona was stronger on the large buffered sequential "
        "file path, but its create times showed a colder-start profile.",
        size=10,
    )
    pdf.spacer(6)

    pdf.text("Methodology", font="F2", size=14)
    for note in data["notes"]:
        pdf.text(f"- {note}", size=10)
    pdf.spacer(6)

    for pass_data in data["passes"]:
        workload = pass_data["workload"]
        pdf.text(pass_data["name"], font="F2", size=15)
        pdf.text(
            f"Samples per backend: {workload['samples_per_backend']}. "
            f"Large file: {bytes_to_label(workload['large_file_bytes'])}. "
            f"Small files: {workload['small_file_count']} x {bytes_to_label(workload['small_file_bytes'])}.",
            size=10,
        )
        pdf.spacer(4)

        headers = [
            "Backend",
            "Create p50",
            "Exec mean",
            "Total p50",
            "Large wr",
            "Large rd",
            "Small wr",
            "List/stat",
            "Small rd",
        ]
        rows: list[list[str]] = []
        for backend in ("daytona", "tensorlake"):
            metrics = pass_data["results"][backend]
            rows.append(
                [
                    backend,
                    ms(metrics["create_ms"]["p50"]),
                    ms(metrics["exec_ms"]["mean"]),
                    ms(metrics["total_ms"]["p50"]),
                    ms(metrics["large_write_ms"]["mean"]),
                    ms(metrics["large_read_ms"]["mean"]),
                    ms(metrics["small_write_ms"]["mean"]),
                    ms(metrics["list_stat_ms"]["mean"]),
                    ms(metrics["small_read_ms"]["mean"]),
                ]
            )
        pdf.preformatted(table_lines(headers, rows), size=9)
        pdf.spacer(4)

        pdf.text("Observations", font="F2", size=12)
        for line in make_summary(pass_data):
            pdf.text(f"- {line}", size=10)
        pdf.spacer(8)

    pdf.text("Interpretation", font="F2", size=14)
    pdf.text(
        "These numbers separate transport and sandbox lifecycle cost from the filesystem work itself. "
        "The create/execute/total metrics include provider API, provisioning, and execution overhead. "
        "The in-sandbox file metrics isolate the workload timing reported by the code running inside the sandbox.",
        size=10,
    )
    pdf.spacer(4)
    pdf.text(
        "Because the workloads were buffered writes with no fsync(), the write results should be treated as "
        "cache-backed filesystem speed, not a durability guarantee. If you need storage durability numbers, "
        "the next pass should force flushes and likely repeat with larger sample counts.",
        size=10,
    )

    return pdf


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python scripts/generate_filesystem_benchmark_report.py <input.json> <output.pdf>", file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    data = json.loads(input_path.read_text())
    pdf = build_report(data).build()
    output_path.write_bytes(pdf)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
