"""Structured extraction contract between perception (vision/OCR) and the deterministic decider.

The model *transcribes and labels*; it never decides routing. Every field that drives a
routing rule is either a verbatim quote from the page or a closed enum.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PageRole = Literal["cover_sheet", "primary_document", "supporting_attachment", "unreadable"]
Category = Literal[
    "referral", "lab_result", "imaging_report", "prior_authorization",
    "refill_request", "records_request", "eob", "none", "unknown",
]
Confidence = Literal["high", "medium", "low"]


class PageExtraction(BaseModel):
    page_number: int = Field(description="1-based index of this image within the PDF")
    footer_transmission_id: str | None = Field(
        description="Fax/transmission ID printed in the page footer (e.g. 'FX-201'), verbatim, or null")
    footer_page_marker: str | None = Field(description="Verbatim 'Page x of y' marker, or null")
    title: str | None = Field(description="Main document heading, verbatim")
    page_role: PageRole
    category: Category = Field(
        description="Document category of THIS page. cover_sheet pages use 'none'. "
                    "A supporting_attachment takes the category of the document it supports.")
    category_confidence: Confidence
    source_doc_id: str | None = Field(
        description="The document's own identifier as printed (e.g. 'RF-3001'), verbatim, or null")
    source_doc_id_label: str | None = Field(description="Printed label next to the ID, e.g. 'Document ID'")
    related_doc_ids: list[str] = Field(
        description="Other document IDs this page references, verbatim with label "
                    "(e.g. 'Related request: MR-4006', 'Original Document ID: RF-3001')")
    patient_name: str | None
    dob: str | None = Field(description="Date of birth as printed, or null")
    explicit_flags: list[str] = Field(
        description="Verbatim AFFIRMATIVE urgency/criticality markers printed on the page "
                    "(e.g. 'CRITICAL RESULT', 'URGENT', 'STAT'). Do not include negated statements "
                    "such as 'No urgent flag'. Never infer urgency from values or findings.")
    negated_flag_statements: list[str] = Field(
        description="Verbatim statements that explicitly say there is NO urgent/critical flag")
    resend_indicators: list[str] = Field(
        description="Verbatim text saying this is a resend/retransmission/duplicate of an earlier fax")
    completeness_issues: list[str] = Field(
        description="Verbatim text indicating missing pages, cut-off scans, illegibility or incompleteness")
    enclosure_list: list[str] = Field(
        description="If a cover sheet lists enclosed documents, each listed item verbatim")
    legibility: Confidence = Field(description="How legible the page image is")
    evidence_quotes: list[str] = Field(description="2-5 short verbatim quotes supporting the category")


class PacketExtraction(BaseModel):
    pages: list[PageExtraction]
    packet_summary: str = Field(description="One neutral sentence describing what the packet contains")
