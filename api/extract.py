"""
Vercel serverless function — /api/extract
Raw WSGI app (no framework, pure stdlib). Vercel's Python runtime requires
a top-level callable named 'app', 'application', or 'handler'.

Environment variable required:
    GEMINI_API_KEY  — set in Vercel project settings → Environment Variables
"""
import json
import os
import urllib.request
import urllib.error

MODEL      = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + MODEL
    + ":generateContent?key={key}"
)

PROMPT = """You are a precise invoice data extraction engine.
Extract every field visible in this invoice document and return ONLY a raw JSON object.
No markdown fences. No explanation. No text before or after the JSON.

Return this exact schema (use empty string "" for any field not found):
{
  "invoice_number": "",
  "invoice_date": "",
  "due_date": "",
  "vendor_name": "",
  "vendor_address": "",
  "vendor_gstin": "",
  "bill_to": "",
  "bill_to_gstin": "",
  "line_items": [
    { "description": "", "quantity": "", "unit_price": "", "amount": "" }
  ],
  "subtotal": "",
  "discount": "",
  "tax_label": "",
  "gst_amount": "",
  "total_amount": "",
  "currency": "",
  "payment_terms": "",
  "account_code": "",
  "notes": ""
}

Rules:
- invoice_date, due_date: use DD/MM/YYYY format
- total_amount, gst_amount, subtotal, discount: digits and decimal point only, strip all currency symbols and commas
- line_items: extract ALL line items from the invoice, not just the first one
- Return ONLY the JSON object."""


def _respond(start_response, status, data, extra_headers=None):
    body = json.dumps(data).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Access-Control-Allow-Origin", "*"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")

    # CORS pre-flight
    if method == "OPTIONS":
        start_response("204 No Content", [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return [b""]

    if method != "POST":
        return _respond(start_response, "405 Method Not Allowed", {"error": "Method not allowed"})

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return _respond(start_response, "500 Internal Server Error",
                        {"error": "GEMINI_API_KEY not configured — add it in Vercel project settings"})

    # Read request body
    try:
        length  = int(environ.get("CONTENT_LENGTH") or 0)
        payload = json.loads(environ["wsgi.input"].read(length))
        b64      = payload["b64"]
        mimeType = payload["mimeType"]
    except Exception as e:
        return _respond(start_response, "400 Bad Request", {"error": "Bad request: " + str(e)})

    # Build Gemini request
    gemini_body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mimeType, "data": b64}},
                {"text": PROMPT},
            ]
        }],
        "generationConfig": {"temperature": 0},
    }

    req = urllib.request.Request(
        GEMINI_URL.format(key=api_key),
        data=json.dumps(gemini_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Call Gemini
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            gemini_out = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_bytes = e.read()
        try:
            msg = json.loads(err_bytes).get("error", {}).get("message", str(err_bytes))
        except Exception:
            msg = err_bytes.decode("utf-8", errors="replace")
        return _respond(start_response, str(e.code) + " Gemini Error",
                        {"error": "Gemini API error " + str(e.code) + ": " + msg})
    except Exception as e:
        return _respond(start_response, "502 Bad Gateway",
                        {"error": "Network error reaching Gemini: " + str(e)})

    # Parse Gemini response
    try:
        if "candidates" not in gemini_out or not gemini_out["candidates"]:
            block = gemini_out.get("promptFeedback", {}).get("blockReason", "unknown")
            return _respond(start_response, "422 Unprocessable Entity",
                            {"error": "Gemini returned no output. Block reason: " + block})

        candidate = gemini_out["candidates"][0]
        finish    = candidate.get("finishReason", "")
        if finish not in ("STOP", "MAX_TOKENS", ""):
            return _respond(start_response, "422 Unprocessable Entity",
                            {"error": "Gemini stopped early. Finish reason: " + finish})

        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        return _respond(start_response, "502 Bad Gateway",
                        {"error": "Unexpected Gemini response structure: " + str(e)})

    # Strip markdown fences if model added them anyway
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("```", 2)[-1]
        if clean.lower().startswith("json"):
            clean = clean[4:]
        clean = clean.rsplit("```", 1)[0].strip()

    try:
        extracted = json.loads(clean)
    except json.JSONDecodeError as e:
        return _respond(start_response, "502 Bad Gateway",
                        {"error": "Gemini returned non-JSON: " + str(e) + " | received: " + text[:200]})

    return _respond(start_response, "200 OK", extracted)
