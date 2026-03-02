"""
ai_configure.py
AI-powered UI configuration endpoint.
Connie types natural language ("make awaiting payment pink"),
Claude interprets it and returns structured config changes.

Session 7 — Mar 2, 2026
"""

import json
import urllib.request
import urllib.error
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import ANTHROPIC_API_KEY

router = APIRouter(prefix="/ai", tags=["ai-configure"])

UI_SCHEMA = {
    "statuses": {
        "needs_payment_link": {"label": "1-Need Invoice", "cssClass": "needs-invoice"},
        "awaiting_payment": {"label": "2-Awaiting Pay", "cssClass": "awaiting-pay"},
        "needs_warehouse_order": {"label": "3-Need to Order", "cssClass": "needs-order"},
        "awaiting_warehouse": {"label": "4-At Warehouse", "cssClass": "at-warehouse"},
        "needs_bol": {"label": "5-Need BOL", "cssClass": "needs-bol"},
        "awaiting_shipment": {"label": "6-Ready Ship", "cssClass": "ready-ship"},
        "complete": {"label": "Complete", "cssClass": "complete"},
    },
    "defaultColors": {
        "needs_payment_link": "#e91e63",
        "awaiting_payment": "#ff9800",
        "needs_warehouse_order": "#2196f3",
        "awaiting_warehouse": "#9c27b0",
        "needs_bol": "#f44336",
        "awaiting_shipment": "#00bcd4",
        "complete": "#4caf50",
    },
    "layout": {
        "theme": "light",
        "cardStyle": "grid",
        "fontSize": "14px",
        "headerColor": "#1a1a2e",
    },
}

SYSTEM_PROMPT = """You are a UI configuration assistant for CFC Orders, a cabinet wholesale order management app.
The user (Connie) will describe changes she wants to the UI in plain English.
You MUST respond with ONLY valid JSON (no markdown, no explanation, no backticks).

The JSON response must have this structure:
{
  "understood": true,
  "description": "Brief description of what you're changing",
  "changes": {
    "statusColors": {},
    "statusLabels": {},
    "theme": null,
    "headerColor": null,
    "fontSize": null,
    "accentColor": null,
    "cardStyle": null,
    "customCSS": null
  }
}

If the request doesn't make sense or you can't do it, respond:
{
  "understood": false,
  "description": "Explanation of why",
  "changes": {}
}

Current statuses and their internal keys:
- needs_payment_link = "1-Need Invoice"
- awaiting_payment = "2-Awaiting Pay"
- needs_warehouse_order = "3-Need to Order"
- awaiting_warehouse = "4-At Warehouse"
- needs_bol = "5-Need BOL"
- awaiting_shipment = "6-Ready Ship"
- complete = "Complete"

Default colors: needs_payment_link=#e91e63, awaiting_payment=#ff9800, needs_warehouse_order=#2196f3, awaiting_warehouse=#9c27b0, needs_bol=#f44336, awaiting_shipment=#00bcd4, complete=#4caf50

RULES:
- Only include keys in "changes" that actually change. Use null for unchanged fields.
- Colors must be valid hex codes (e.g. "#ff69b4" for pink).
- For status keys, use the internal key (e.g. "awaiting_payment" not "Awaiting Pay").
- If the user says a color name, translate to hex.
- If the user says "dark mode", set theme to "dark".
- customCSS is for anything that doesn't fit the structured fields.
- Respond with ONLY the JSON object. No other text."""


class ConfigureRequest(BaseModel):
    prompt: str


@router.post("/configure")
async def ai_configure(req: ConfigureRequest):
    """Accept natural language UI config request, return structured changes."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": req.prompt.strip()}
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    http_req = urllib.request.Request(url, data=data, method="POST")
    http_req.add_header("Content-Type", "application/json")
    http_req.add_header("x-api-key", ANTHROPIC_API_KEY)
    http_req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(http_req, timeout=30) as response:
            result = json.loads(response.read().decode())
            if result.get("content") and len(result["content"]) > 0:
                raw_text = result["content"][0].get("text", "")
            else:
                raise HTTPException(status_code=500, detail="No response from AI")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        raise HTTPException(status_code=502, detail=f"AI API error: {e.code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Parse JSON response
    try:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "understood": False,
            "description": f"AI returned invalid JSON",
            "changes": {},
            "raw_prompt": req.prompt,
        }

    return {
        "understood": parsed.get("understood", False),
        "description": parsed.get("description", ""),
        "changes": parsed.get("changes", {}),
        "raw_prompt": req.prompt,
    }


@router.get("/ui-schema")
async def get_ui_schema():
    """Return current UI schema so frontend knows what's configurable."""
    return UI_SCHEMA
