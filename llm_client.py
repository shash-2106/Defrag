"""
LLM Client — Centralized LLM access for all agents.
Primary: Google Gemini (google-genai SDK)
Fallback: OpenAI GPT-4o-mini
No-credential mode: Returns None, agents handle gracefully via rule-based fallback.
"""

import os
import json
import logging
import re
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Try new Gemini SDK (google-genai)
try:
    from google import genai as genai_new
    HAS_GEMINI_NEW = True
except ImportError:
    genai_new = None
    HAS_GEMINI_NEW = False

# Try OpenAI
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    OpenAI = None
    HAS_OPENAI = False

# Preferred model candidates — tried in order, first working one wins.
# The list is ordered: best quality → fastest/cheapest fallbacks.
GEMINI_MODEL_CANDIDATES = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-pro-latest",
]


class LLMClient:
    """
    Provides a single `call(prompt: str) -> dict | None` interface.
    Configured from environment variables.
    Falls back gracefully if no credentials are provided.
    """

    def __init__(self):
        self.provider = None
        self._gemini_client = None
        self._gemini_model_name = None
        self._openai_client = None

        gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")

        if HAS_GEMINI_NEW and gemini_key:
            try:
                client = genai_new.Client(api_key=gemini_key)
                # If LLM_MODEL is explicitly set, use it — otherwise auto-discover
                explicit_model = os.environ.get("LLM_MODEL")
                working_model = None

                if explicit_model:
                    # Trust the explicit override; verify it works with a cheap probe
                    try:
                        client.models.generate_content(
                            model=explicit_model,
                            contents="ping"
                        )
                        working_model = explicit_model
                    except Exception as e:
                        logger.warning(f"Explicit LLM_MODEL={explicit_model} failed: {e}. Auto-discovering...")

                if not working_model:
                    # Auto-discover first working model
                    for candidate in GEMINI_MODEL_CANDIDATES:
                        try:
                            client.models.generate_content(
                                model=candidate,
                                contents="ping"
                            )
                            working_model = candidate
                            logger.info(f"LLM: Auto-selected Gemini model: {working_model}")
                            break
                        except Exception:
                            continue

                if working_model:
                    self._gemini_client = client
                    self._gemini_model_name = working_model
                    self.provider = "gemini"
                    logger.info(f"LLM: Gemini ready — model={self._gemini_model_name}")
                else:
                    logger.warning("LLM: Gemini key valid but no model responded. Check API quota.")
            except Exception as e:
                logger.warning(f"Gemini init failed: {e}")

        if self.provider is None and HAS_OPENAI and openai_key:
            try:
                self._openai_client = OpenAI(api_key=openai_key)
                self.provider = "openai"
                logger.info("LLM: OpenAI configured")
            except Exception as e:
                logger.warning(f"OpenAI init failed: {e}")

        if self.provider is None:
            logger.warning(
                "LLM: No credentials found or no working model available. "
                "Agents will use rule-based fallbacks. "
                "Set GEMINI_API_KEY or OPENAI_API_KEY for full LLM functionality."
            )

    @property
    def available(self) -> bool:
        return self.provider is not None

    @property
    def model_name(self) -> Optional[str]:
        return self._gemini_model_name

    def call(self, prompt: str, expect_json: bool = True) -> Optional[Dict[str, Any]]:
        """
        Send a prompt to the configured LLM.
        Returns parsed JSON dict if expect_json=True.
        Returns None if LLM is unavailable or call fails (agents fall back to rules).
        """
        if self.provider == "gemini":
            return self._call_gemini(prompt, expect_json)
        elif self.provider == "openai":
            return self._call_openai(prompt, expect_json)
        return None

    def _extract_json(self, text: str) -> Optional[Dict]:
        """Extract JSON from a response that may contain markdown code fences."""
        if not text:
            return None
        # Direct parse
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        # Strip markdown code fences
        pattern = r"```(?:json)?\s*([\s\S]*?)```"
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            try:
                return json.loads(match.strip())
            except Exception:
                pass
        # Find first {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        logger.warning(f"Could not extract JSON from LLM response: {text[:200]}")
        return None

    def _call_gemini(self, prompt: str, expect_json: bool) -> Optional[Dict]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self._gemini_client.models.generate_content(
                    model=self._gemini_model_name,
                    contents=prompt,
                )
                text = response.text
                if expect_json:
                    return self._extract_json(text)
                return {"text": text}
            except Exception as e:
                err_str = str(e)
                if "503" in err_str and attempt < max_retries - 1:
                    wait_secs = 2 ** attempt  # 1s, 2s
                    import time
                    logger.warning(f"Gemini 503 (attempt {attempt+1}/{max_retries}), retrying in {wait_secs}s...")
                    time.sleep(wait_secs)
                    continue
                logger.error(f"Gemini call failed: {e}")
                return None

    def _call_openai(self, prompt: str, expect_json: bool) -> Optional[Dict]:
        try:
            model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
            resp = self._openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"} if expect_json else None,
            )
            text = resp.choices[0].message.content
            if expect_json:
                return self._extract_json(text)
            return {"text": text}
        except Exception as e:
            logger.error(f"OpenAI call failed: {e}")
            return None


# Singleton — import this in all agents
llm = LLMClient()
