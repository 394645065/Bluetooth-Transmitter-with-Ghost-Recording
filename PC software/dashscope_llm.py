import json
import os
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - optional dependency
    requests = None


DEFAULT_DASHSCOPE_LLM_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class DashScopeMeetingSummaryClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str = DEFAULT_DASHSCOPE_LLM_URL,
        timeout_sec: float = 30.0,
    ) -> None:
        self._api_key = (api_key or os.getenv("DASHSCOPE_API_KEY", "")).strip()
        self._model = (model or os.getenv("DASHSCOPE_LLM_MODEL", "qwen-plus")).strip()
        self._endpoint = endpoint
        self._timeout_sec = timeout_sec

    def summarize_incremental(
        self,
        previous_state: dict[str, Any],
        new_segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if requests is None:
            raise RuntimeError("Missing dependency: requests")
        if not self._api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY")
        if not self._model:
            raise RuntimeError("Missing DASHSCOPE_LLM_MODEL")
        if not new_segments:
            return self._normalize_state(previous_state, previous_state, new_segments)

        payload = {
            "model": self._model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": self._user_prompt(previous_state, new_segments),
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            self._endpoint,
            headers=headers,
            json=payload,
            timeout=self._timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"DashScope LLM request failed ({response.status_code}): {response.text[:200]}"
            )
        response_json = response.json()
        content = self._extract_content(response_json)
        parsed_state = self._parse_json_payload(content)
        return self._normalize_state(parsed_state, previous_state, new_segments)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a real-time meeting summarizer for a 1:1 call.\n"
            "Input includes previous summary state and only newly committed transcript segments.\n"
            "Return STRICT JSON only. Do not output markdown, prose, or code fences.\n"
            "Keep summary in Chinese, but keep technical English terms unchanged.\n"
            "Always preserve schema keys: running_summary, bullets, decisions, action_items, open_questions, meta."
        )

    @staticmethod
    def _user_prompt(previous_state: dict[str, Any], new_segments: list[dict[str, Any]]) -> str:
        schema = {
            "running_summary": "string",
            "bullets": ["string"],
            "decisions": ["string"],
            "action_items": [
                {
                    "owner": "local|remote|unknown",
                    "task": "string",
                    "due": "string",
                    "status": "open|done",
                    "evidence_segment_ids": ["string"],
                }
            ],
            "open_questions": ["string"],
            "meta": {
                "window_start": "ISO timestamp string",
                "window_end": "ISO timestamp string",
                "language": "zh-en",
            },
        }
        return (
            "Update the meeting summary state incrementally.\n"
            "Rules:\n"
            "1) Use only facts supported by new segments.\n"
            "2) Keep or refine previous state when needed.\n"
            "3) action_items must include evidence_segment_ids from input segment ids.\n"
            "4) owner must be one of local/remote/unknown.\n"
            "5) decisions/open_questions can be empty arrays.\n\n"
            f"Output schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"previous_state:\n{json.dumps(previous_state, ensure_ascii=False)}\n\n"
            f"new_segments:\n{json.dumps(new_segments, ensure_ascii=False)}"
        )

    @staticmethod
    def _extract_content(response_json: dict[str, Any]) -> str:
        choices = response_json.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Invalid DashScope response: missing choices")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            joined = "".join(parts).strip()
            if joined:
                return joined
        raise RuntimeError("Invalid DashScope response: missing message content")

    @staticmethod
    def _parse_json_payload(content: str) -> dict[str, Any]:
        raw = content.strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidate = raw[start : end + 1]
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        raise RuntimeError("LLM response is not valid JSON")

    def _normalize_state(
        self,
        candidate: dict[str, Any],
        previous_state: dict[str, Any],
        new_segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        window_start = new_segments[0].get("ts_wall", "")
        window_end = new_segments[-1].get("ts_wall", "")
        meta_input = candidate.get("meta", {})
        if not isinstance(meta_input, dict):
            meta_input = {}

        normalized = {
            "running_summary": self._as_text(
                candidate.get("running_summary"),
                previous_state.get("running_summary", ""),
            ),
            "bullets": self._as_text_list(candidate.get("bullets")),
            "decisions": self._as_text_list(candidate.get("decisions")),
            "action_items": self._normalize_action_items(candidate.get("action_items")),
            "open_questions": self._as_text_list(candidate.get("open_questions")),
            "meta": {
                "window_start": self._as_text(meta_input.get("window_start"), window_start),
                "window_end": self._as_text(meta_input.get("window_end"), window_end),
                "language": self._as_text(meta_input.get("language"), "zh-en"),
            },
        }
        return normalized

    @staticmethod
    def _as_text(value: Any, default: str = "") -> str:
        if isinstance(value, str):
            return value.strip()
        return default

    @staticmethod
    def _as_text_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    result.append(text)
        return result

    @staticmethod
    def _normalize_action_items(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner", "unknown")
            if owner not in {"local", "remote", "unknown"}:
                owner = "unknown"
            status = item.get("status", "open")
            if status not in {"open", "done"}:
                status = "open"
            evidence_ids = item.get("evidence_segment_ids", [])
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            evidence_ids = [str(seg_id).strip() for seg_id in evidence_ids if str(seg_id).strip()]
            task = str(item.get("task", "")).strip()
            if not task:
                continue
            result.append(
                {
                    "owner": owner,
                    "task": task,
                    "due": str(item.get("due", "")).strip(),
                    "status": status,
                    "evidence_segment_ids": evidence_ids,
                }
            )
        return result
