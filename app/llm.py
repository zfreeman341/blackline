"""LLM provider seam.

The service depends only on LLMClient (text in -> raw model output out).
MockLLMClient is the default binding (MOCK_LLM=true): deterministic,
network-free, and importantly it returns a raw JSON *string*, so the
mock path exercises the same parse-and-validate pipeline as a real
provider. AnthropicClient is one env var away (MOCK_LLM=false +
ANTHROPIC_API_KEY). Which client, which model, and where it runs
(in-tenant for legal clients) are per-client policy; see INFRA.md.

Failure taxonomy, deliberately split:
- LLMError (provider down, malformed output)  -> 502: our dependency
  misbehaved; the caller did nothing wrong.
- Schema-valid proposal that doesn't resolve  -> 422 with the standard
  candidates machinery: the instruction couldn't be safely mapped onto
  this document. Never silently repaired.
"""

import json
import os
import re
from typing import Protocol

from pydantic import ValidationError

from app.models import ProposedChanges

PROPOSAL_PROMPT = """\
You convert a redlining instruction into a structured change proposal for a \
document service. Respond with ONLY a JSON object, no prose, no code fences:

{{"changes": [{{"operation": "replace", "target": {{"text": "<exact text from the document>", "occurrence": <1-indexed int, only if the text appears more than once>}}, "replacement": "<new text>"}}]}}

Rules:
- "operation" must be "replace".
- "target.text" must be copied character-for-character from the document.
- Omit "occurrence" unless the instruction singles out one of several matches.
- Propose the minimal set of changes that fulfils the instruction.
- If the instruction cannot be fulfilled by replacing text that exists in the
  document, respond with exactly {{"changes": []}}. Never invent text.

Document:
<document>
{document}
</document>

Instruction: {instruction}"""


class LLMError(Exception):
    """Provider unavailable or output unusable. Maps to 502: the core
    service (CRUD, PATCH, search) is unaffected by LLM failure."""


class LLMClient(Protocol):
    def propose(self, document_text: str, instruction: str) -> str: ...


class MockLLMClient:
    """Deterministic canned proposals keyed off instruction content.

    Understands two instruction shapes:
      'replace X with Y'          (X/Y optionally double-quoted)
      'change ... from X to Y'
    Anything else yields an empty proposal — the same "this instruction
    doesn't map onto the document" contract the real prompt demands, which
    the route surfaces as a 422.
    """

    _PATTERNS = [
        re.compile(r'replace\s+"?(?P<old>.+?)"?\s+with\s+"?(?P<new>.+?)"?[.!]?$', re.IGNORECASE),
        re.compile(r'\bfrom\s+"?(?P<old>.+?)"?\s+to\s+"?(?P<new>.+?)"?[.!]?$', re.IGNORECASE),
    ]

    def propose(self, document_text: str, instruction: str) -> str:
        for pattern in self._PATTERNS:
            match = pattern.search(instruction.strip())
            if match:
                return json.dumps(
                    {
                        "changes": [
                            {
                                "operation": "replace",
                                "target": {"text": match.group("old")},
                                "replacement": match.group("new"),
                            }
                        ]
                    }
                )
        return json.dumps({"changes": []})


class AnthropicClient:
    def __init__(self, api_key: str, model: str):
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover
            raise LLMError(
                "anthropic package not installed; run with MOCK_LLM=true or install it"
            ) from error
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def propose(self, document_text: str, instruction: str) -> str:
        try:
            # No sampling params: current Claude models reject temperature/top_p;
            # output shape is constrained by the prompt + downstream validation.
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": PROPOSAL_PROMPT.format(
                            document=document_text, instruction=instruction
                        ),
                    }
                ],
            )
        except Exception as error:
            raise LLMError(f"LLM provider request failed: {error}") from error
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


def parse_proposal(raw_output: str) -> ProposedChanges:
    """Model text -> validated proposal, via the same Change schema PATCH
    accepts. A hallucinated field or operation dies here, before the
    document is ever consulted."""
    cleaned = raw_output.strip()
    # Models sometimes fence JSON despite instructions; tolerate that one tic.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return ProposedChanges.model_validate(json.loads(cleaned))
    except (json.JSONDecodeError, ValidationError) as error:
        # Deliberately terse: the caller did nothing wrong (that's why this is
        # a 502), and library validation internals are not theirs to see.
        raise LLMError("LLM returned a malformed proposal") from error


def client_from_env() -> LLMClient:
    if os.environ.get("MOCK_LLM", "true").strip().lower() != "false":
        return MockLLMClient()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("MOCK_LLM=false requires ANTHROPIC_API_KEY to be set")
    return AnthropicClient(
        api_key=api_key, model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    )
