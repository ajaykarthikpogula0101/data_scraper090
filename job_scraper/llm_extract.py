"""OpenRouter post-processing for job CSVs.

This module never runs as part of scraping. Invoke it explicitly after setting
OPENROUTER_API_KEY. Each result is durably appended to a sidecar, and the final
CSV is rewritten atomically once per run.
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time

import requests
from dotenv import load_dotenv


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
SENIORITY_LEVELS = {"Entry", "Mid", "Senior", "Lead", "Manager", "Director", "Executive"}
EDUCATION_TYPES = {"Bachelor's", "Master's", "Vocational", "PhD"}
OUTPUT_KEYS = {
    "years_of_experience_min", "years_of_experience_max", "seniority_level",
    "education_stream", "education_type", "education_qualification", "skills",
    "salary_disclosed",
}
SALARY_RE = re.compile(
    r"(?i)(?:(?:USD|EUR|GBP|INR|AUD|CAD|CHF|JPY|CNY|BRL|MXN)\s*|[$€£₹¥]\s*)"
    r"\d[\d.,]*(?:\s*[kKmM])?(?:\s*(?:-|–|—|to)\s*(?:USD|EUR|GBP|INR|AUD|CAD|CHF|JPY|CNY|BRL|MXN|[$€£₹¥])?\s*\d[\d.,]*(?:\s*[kKmM])?)?"
    r"|\d[\d.,]*(?:\s*[kKmM])?\s*(?:USD|EUR|GBP|INR|AUD|CAD|CHF|JPY|CNY|BRL|MXN)\b"
)

SYSTEM_PROMPT = """Extract only facts supported by the supplied job title and description.
Return exactly one JSON object with these keys:
years_of_experience_min (integer or null), years_of_experience_max (integer or null),
seniority_level (Entry|Mid|Senior|Lead|Manager|Director|Executive or null),
education_stream (string or null), education_type (Bachelor's|Master's|Vocational|PhD or null),
education_qualification (string or null), skills (array of strings), salary_disclosed (boolean).
Do not guess missing facts. Seniority may be derived only from an explicit title or explicit
experience requirement. salary_disclosed is true only if the source contains an explicit
numeric salary figure. Never calculate, estimate, or return salary numbers."""


class ExtractionValidationError(ValueError):
    pass


def row_id(row):
    identity = "\x1f".join(str(row.get(k, "")) for k in ("company_name", "job_title", "job_url"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _nullable_string(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExtractionValidationError("%s must be a string or null" % field)
    return value.strip()


def validate_extraction(data, source_text):
    if not isinstance(data, dict) or set(data) != OUTPUT_KEYS:
        raise ExtractionValidationError("response must contain exactly the required keys")
    out = {}
    for field in ("years_of_experience_min", "years_of_experience_max"):
        value = data[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 80):
            raise ExtractionValidationError("%s must be an integer from 0 to 80 or null" % field)
        out[field] = "" if value is None else value
    if out["years_of_experience_min"] != "" and out["years_of_experience_max"] != "":
        if out["years_of_experience_min"] > out["years_of_experience_max"]:
            raise ExtractionValidationError("experience minimum exceeds maximum")
    seniority = data["seniority_level"]
    if seniority is not None and seniority not in SENIORITY_LEVELS:
        raise ExtractionValidationError("invalid seniority_level")
    out["seniority_level"] = seniority or ""
    education_type = data["education_type"]
    if education_type is not None and education_type not in EDUCATION_TYPES:
        raise ExtractionValidationError("invalid education_type")
    out["education_type"] = education_type or ""
    out["education_stream"] = _nullable_string(data["education_stream"], "education_stream")
    out["education_qualification"] = _nullable_string(data["education_qualification"], "education_qualification")
    skills = data["skills"]
    if not isinstance(skills, list) or any(not isinstance(skill, str) for skill in skills):
        raise ExtractionValidationError("skills must be an array of strings")
    out["skills"] = "; ".join(dict.fromkeys(s.strip() for s in skills if s.strip()))
    if not isinstance(data["salary_disclosed"], bool):
        raise ExtractionValidationError("salary_disclosed must be boolean")
    # Deterministic evidence gate: an LLM cannot assert salary disclosure without a figure.
    out["salary_disclosed"] = bool(data["salary_disclosed"] and SALARY_RE.search(source_text or ""))
    return out


def _extract_json(content):
    value = (content or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    return json.loads(value)


def request_extraction(title, description, api_key, model=DEFAULT_MODEL, max_attempts=5, session=None):
    client = session or requests.Session()
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Job title: %s\n\nDescription:\n%s" % (title or "", description or "")},
        ],
    }
    headers = {"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"}
    for attempt in range(max_attempts):
        try:
            response = client.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError("retryable HTTP %s" % response.status_code, response=response)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return validate_extraction(_extract_json(content), description)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
            if attempt + 1 == max_attempts:
                raise RuntimeError("OpenRouter request failed after retries: %s" % exc) from exc
            time.sleep((2 ** attempt) + random.random())


def _read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _atomic_write_csv(path, rows, fieldnames):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".llm_extract_", suffix=".csv", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_checkpoint(path):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def _append_checkpoint(path, identifier):
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(identifier + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _append_result(path, identifier, result):
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"row_id": identifier, "result": result}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_results(path):
    results = {}
    if not os.path.exists(path):
        return results
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
                if item.get("row_id") and isinstance(item.get("result"), dict):
                    results[item["row_id"]] = item["result"]
            except (json.JSONDecodeError, AttributeError):
                continue
    return results


def _log_error(path, identifier, row, error):
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["row_id", "job_url", "job_title", "error"])
        if not exists:
            writer.writeheader()
        writer.writerow({"row_id": identifier, "job_url": row.get("job_url", ""),
                         "job_title": row.get("job_title", ""), "error": str(error)[:2000]})


def process_csv(input_path, output_path=None, checkpoint_path=None, errors_path=None,
                model=DEFAULT_MODEL, api_key=None, request_fn=request_extraction):
    load_dotenv()
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    output_path = output_path or input_path
    checkpoint_path = checkpoint_path or (output_path + ".checkpoint")
    results_path = output_path + ".llm_results.jsonl"
    errors_path = errors_path or os.path.join(os.path.dirname(os.path.abspath(output_path)), "extraction_errors.csv")
    rows = _read_csv(output_path if os.path.exists(output_path) else input_path)
    required_columns = ["years_of_experience_min", "years_of_experience_max", "seniority_level",
                        "education_stream", "education_type", "education_qualification", "skills", "salary_disclosed"]
    fieldnames = list(rows[0].keys()) if rows else []
    for column in required_columns:
        if column not in fieldnames:
            fieldnames.append(column)
    processed = _load_checkpoint(checkpoint_path)
    saved_results = _load_results(results_path)
    for row in rows:
        saved = saved_results.get(row_id(row))
        if saved:
            row.update(saved)
    for row in rows:
        identifier = row_id(row)
        if identifier in processed:
            continue
        try:
            result = request_fn(row.get("job_title", ""), row.get("job_description_clean", ""), key, model=model)
            validated = validate_extraction({
                "years_of_experience_min": result.get("years_of_experience_min") if result.get("years_of_experience_min") != "" else None,
                "years_of_experience_max": result.get("years_of_experience_max") if result.get("years_of_experience_max") != "" else None,
                "seniority_level": result.get("seniority_level") or None,
                "education_stream": result.get("education_stream") or None,
                "education_type": result.get("education_type") or None,
                "education_qualification": result.get("education_qualification") or None,
                "skills": [s.strip() for s in result.get("skills", "").split(";") if s.strip()],
                "salary_disclosed": bool(result.get("salary_disclosed")),
            }, row.get("job_description_clean", ""))
            row.update(validated)
            # Append a durable result before checkpointing. A crash can resume
            # without rewriting or re-calling completed rows.
            _append_result(results_path, identifier, validated)
            _append_checkpoint(checkpoint_path, identifier)
            processed.add(identifier)
        except Exception as exc:
            logging.exception("Extraction failed for %s", identifier)
            _log_error(errors_path, identifier, row, exc)
    _atomic_write_csv(output_path, rows, fieldnames)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Backfill job fields using OpenRouter")
    parser.add_argument("input")
    parser.add_argument("--output")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    process_csv(args.input, args.output, model=args.model)


if __name__ == "__main__":
    main()
