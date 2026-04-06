"""
Invoice Processor - Local Server (Gemini backend)
=================================================
1. Get a FREE Gemini API key at: https://aistudio.google.com/
2. Run in PowerShell from this folder:

      $env:GEMINI_API_KEY = "AIza..."
      py server.py

3. Open http://localhost:8765 in Chrome or Edge
4. Ctrl+C to stop
"""
import json
import os
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT    = 8765
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# gemini-2.5-flash-preview-04-17 has active free tier quota (2.0-flash quota is exhausted)
MODEL = "gemini-2.5-flash"

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


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print("  " + fmt % args)

    # ── GET: serve index.html ─────────────────────────────────────────────────
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                body = open("index.html", "rb").read()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"index.html not found - both files must be in the same folder")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    # ── POST /extract: proxy to Gemini ────────────────────────────────────────
    def do_POST(self):
        if self.path != "/extract":
            self.send_response(404)
            self.end_headers()
            return

        if not API_KEY:
            self._error(500, "GEMINI_API_KEY not set - set it and restart server.py")
            return

        # Read request from browser
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload  = json.loads(self.rfile.read(length))
            b64      = payload["b64"]
            mimeType = payload["mimeType"]
        except Exception as e:
            self._error(400, "Bad request from browser: " + str(e))
            return

        # Build Gemini request body
        gemini_body = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mimeType,
                            "data": b64,
                        }
                    },
                    {"text": PROMPT}
                ]
            }],
            "generationConfig": {
                "temperature": 0,
            }
        }

        req = urllib.request.Request(
            GEMINI_URL.format(key=API_KEY),
            data=json.dumps(gemini_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        # Call Gemini
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw_bytes  = resp.read()
                gemini_out = json.loads(raw_bytes)
        except urllib.error.HTTPError as e:
            err_bytes = e.read()
            try:
                err_json = json.loads(err_bytes)
                msg = err_json.get("error", {}).get("message", str(err_bytes))
            except Exception:
                msg = err_bytes.decode("utf-8", errors="replace")
            print("  Gemini HTTP error", e.code, ":", msg)
            self._error(e.code, "Gemini API error " + str(e.code) + ": " + msg)
            return
        except Exception as e:
            print("  Network error:", e)
            self._error(502, "Network error reaching Gemini: " + str(e))
            return

        # Parse Gemini response
        try:
            # Check for blocked response
            if "candidates" not in gemini_out or not gemini_out["candidates"]:
                # Log the full response so we can debug
                print("  Gemini returned no candidates:", json.dumps(gemini_out)[:500])
                block_reason = gemini_out.get("promptFeedback", {}).get("blockReason", "unknown")
                self._error(422, "Gemini returned no output. Block reason: " + block_reason
                            + ". Full response: " + json.dumps(gemini_out)[:300])
                return

            candidate = gemini_out["candidates"][0]

            # Check finish reason
            finish = candidate.get("finishReason", "")
            if finish not in ("STOP", "MAX_TOKENS", ""):
                print("  Gemini finish reason:", finish)
                self._error(422, "Gemini stopped early. Finish reason: " + finish)
                return

            text = candidate["content"]["parts"][0]["text"]
            print("  Gemini raw response:", text[:200])

        except (KeyError, IndexError) as e:
            print("  Failed to parse Gemini response structure:", gemini_out)
            self._error(502, "Unexpected Gemini response structure: " + str(e)
                        + " | raw: " + json.dumps(gemini_out)[:300])
            return

        # Parse the JSON the model returned
        try:
            # Strip markdown fences if model added them despite instructions
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("```", 2)[-1]           # drop opening fence
                if clean.lower().startswith("json"):
                    clean = clean[4:]                        # drop "json" label
                clean = clean.rsplit("```", 1)[0].strip()   # drop closing fence
            extracted = json.loads(clean)
        except json.JSONDecodeError as e:
            print("  JSON parse error:", e, "| text was:", text[:300])
            self._error(502, "Gemini returned non-JSON output: " + str(e)
                        + " | received: " + text[:200])
            return

        self._ok(extracted)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _ok(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        print("  Sending error", code, ":", msg[:200])
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not API_KEY:
        print("\n  GEMINI_API_KEY is not set.\n")
        print("  Get a FREE key at: https://aistudio.google.com/\n")
        print("  PowerShell:")
        print('      $env:GEMINI_API_KEY = "AIza..."')
        print("      py server.py\n")
        sys.exit(1)

    print("\n  Invoice Processor - Gemini " + MODEL)
    print("  API key: " + API_KEY[:12] + "...")
    print("  Open http://localhost:" + str(PORT) + " in Chrome or Edge")
    print("  Ctrl+C to stop\n")
    print("  Watch this window for per-request logs if anything fails.\n")

    httpd = HTTPServer(("localhost", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
