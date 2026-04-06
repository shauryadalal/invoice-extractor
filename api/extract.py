"""
Vercel serverless function — /api/extract
Proxies invoice image/PDF to Gemini and returns extracted fields as JSON.

Vercel Python runtime requires a BaseHTTPRequestHandler subclass named 'handler'.
Logic is identical to server.py's do_POST.

Environment variable required:
    GEMINI_API_KEY  — set in Vercel project settings → Environment Variables
"""
import json
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

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


class handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default Apache-style access logs

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            self._error(500, "GEMINI_API_KEY not configured — add it in Vercel project settings")
            return

        # Read request body
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload  = json.loads(self.rfile.read(length))
            b64      = payload["b64"]
            mimeType = payload["mimeType"]
        except Exception as e:
            self._error(400, "Bad request: " + str(e))
            return

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
            self._error(e.code, "Gemini API error " + str(e.code) + ": " + msg)
            return
        except Exception as e:
            self._error(502, "Network error reaching Gemini: " + str(e))
            return

        # Parse Gemini response
        try:
            if "candidates" not in gemini_out or not gemini_out["candidates"]:
                block = gemini_out.get("promptFeedback", {}).get("blockReason", "unknown")
                self._error(422, "Gemini returned no output. Block reason: " + block)
                return

            candidate = gemini_out["candidates"][0]
            finish    = candidate.get("finishReason", "")
            if finish not in ("STOP", "MAX_TOKENS", ""):
                self._error(422, "Gemini stopped early. Finish reason: " + finish)
                return

            text = candidate["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            self._error(502, "Unexpected Gemini response structure: " + str(e))
            return

        # Strip markdown fences if the model added them anyway
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[-1]
            if clean.lower().startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0].strip()

        try:
            extracted = json.loads(clean)
        except json.JSONDecodeError as e:
            self._error(502, "Gemini returned non-JSON: " + str(e) + " | received: " + text[:200])
            return

        self._ok(extracted)

    def _ok(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
