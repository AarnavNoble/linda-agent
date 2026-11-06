"""Claude vision extraction: all pages of one packet in one request, schema-constrained output.

Packet-level (not page-level) so the model sees cover sheets and attachments in context,
but the output is still per page so the deterministic decider can apply the mixed-packet
and completeness rules itself.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import anthropic

from .render import png_bytes, render_pages
from .schema import PacketExtraction

PROMPT_VERSION = "v4"

SYSTEM = """You transcribe and label scanned administrative fax pages for a document-routing \
system. You do not route documents and you make no clinical judgments.

Rules:
- Report only what is printed. Quote verbatim. Use null when a field is not present. Never guess IDs, \
names or dates.
- category is the administrative document type of each page:
  referral = new-patient referral or specialist consult request;
  lab_result = laboratory result/report; imaging_report = imaging or radiology report;
  prior_authorization = prior authorization / coverage documentation request;
  refill_request = pharmacy prescription refill/renewal request;
  records_request = request for release or copy of medical records (incl. its signed release authorization);
  eob = explanation of benefits / insurer remittance;
  none = cover sheet with no document content; unknown = cannot tell.
- page_role: a page that exists to support another document in the same packet (e.g. a signed \
authorization that references the request's ID) is a supporting_attachment with the SAME category \
as the document it supports. A page that is its own standalone document is primary_document.
- explicit_flags: copy affirmative urgency/criticality markers exactly as printed (e.g. "CRITICAL RESULT", \
"URGENT", "STAT"). If the page says there is NO urgent flag, put that sentence in \
negated_flag_statements instead. Do not infer urgency from lab values, findings or wording.
- completeness_issues: only text that explicitly says the transmission or scan is incomplete (cut off, \
missing/continuation pages not received, unreadable) or that the category/destination cannot be determined. \
A mere reference to other content ("see full report", "see table") goes in references_to_absent_content \
instead; the page footer's "Page x of y" marker is the authority on whether pages are missing.
- If a page is hard to read, lower legibility and confidence rather than guessing."""


@dataclass
class VisionResult:
    extraction: PacketExtraction
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None


class VisionUnavailable(RuntimeError):
    pass


# $/1M tokens (input, output) — for the cost column in results.
PRICING = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


def cost_usd(model: str, inp: int, out: int) -> float | None:
    p = PRICING.get(model)
    return round((inp * p[0] + out * p[1]) / 1_000_000, 5) if p else None


def extract_packet(pdf_path, model: str, expected_pages: int) -> VisionResult:
    images = render_pages(pdf_path)
    content: list[dict] = []
    for i, img in enumerate(images, 1):
        content.append({"type": "text", "text": f"Page image {i} of {len(images)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes(img)).decode()}})
    content.append({"type": "text", "text": (
        f"This PDF has {len(images)} page image(s). Return exactly one entry in `pages` per image, "
        "in order, with page_number set to the image index.")})

    client = anthropic.Anthropic(max_retries=4, timeout=120.0)  # SDK retries 429/5xx with backoff
    t0 = time.monotonic()
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": content}],
            output_format=PacketExtraction,
        )
    except anthropic.AuthenticationError as e:
        raise VisionUnavailable("ANTHROPIC_API_KEY missing or invalid") from e
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as e:
        raise VisionUnavailable(f"vision API failed after retries: {type(e).__name__}: {e}") from e
    latency = int((time.monotonic() - t0) * 1000)
    if resp.stop_reason == "refusal":
        raise VisionUnavailable("model declined the request (stop_reason=refusal)")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise VisionUnavailable(f"no parseable structured output (stop_reason={resp.stop_reason})")
    ext: PacketExtraction = resp.parsed_output
    if len(ext.pages) != len(images):
        raise VisionUnavailable(f"model returned {len(ext.pages)} page entries for {len(images)} images")
    return VisionResult(ext, model, resp.usage.input_tokens, resp.usage.output_tokens, latency,
                        getattr(resp, "_request_id", None))
