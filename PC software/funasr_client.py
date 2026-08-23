"""
FunASR Real-time ASR Client (Online Version)

FunASR is an open-source speech recognition toolkit from Alibaba DAMO Academy.
This client uses the Alibaba Cloud DashScope API for online speech recognition.

Online API (DashScope):
- Endpoint: wss://dashscope.aliyuncs.com/api-ws/v1/inference
- Model: fun-asr-realtime-2025-11-07
- Free quota: 36,000 seconds (10 hours) valid for 90 days
- Pricing after quota: ¥0.00033/second (~¥1.2/hour)

Setup:
1. Get API Key from https://dashscope.console.aliyun.com/
2. Set environment variable: set DASHSCOPE_API_KEY=your-api-key
3. Run: python test_recorder.py
"""

import json
import os
import queue
import threading
import time
import uuid
from typing import Optional

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


# Default settings for online FunASR (via DashScope)
DEFAULT_FUNASR_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_FUNASR_MODEL = "fun-asr-realtime-2025-11-07"

# API Key (reuses DashScope key)
FUNASR_API_KEY = ""  # Set via DASHSCOPE_API_KEY env var


class FunASRRealtimeClient:
    """Real-time ASR client for FunASR via Alibaba Cloud DashScope API."""

    def __init__(
        self,
        api_key: str,
        event_queue: "queue.Queue",
        channel: str,
        sample_rate: int = 16000,
        ws_url: str = DEFAULT_FUNASR_URL,
        model: str = DEFAULT_FUNASR_MODEL,
        language: str = "zh",
        language_hints: Optional[list[str]] = None,
    ) -> None:
        """
        Initialize FunASR online client.
        
        Args:
            api_key: DashScope API key
            event_queue: Queue to push transcript events to
            channel: Channel identifier ("rx" or "tx")
            sample_rate: Audio sample rate (16000 Hz recommended)
            ws_url: WebSocket endpoint URL
            model: Model name (default: fun-asr-realtime-2025-11-07)
            language: Language code (zh, en, ja, etc.)
        """
        self._api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self._event_queue = event_queue
        self._channel = channel
        self._sample_rate = sample_rate
        self._ws_url = ws_url
        self._model = model
        self._language = language
        self._language_hints = self._normalize_language_hints(language_hints, language)

        self._audio_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._last_text = ""

    def start(self) -> bool:
        """Start the ASR client."""
        if self._thread and self._thread.is_alive():
            return True
        if websocket is None:
            self._emit_error("Missing dependency: websocket-client (pip install websocket-client)")
            return False
        if not self._api_key:
            self._emit_error("Missing API key: set DASHSCOPE_API_KEY environment variable")
            return False

        self._running.set()
        self._last_text = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the ASR client."""
        self._running.clear()
        self._audio_queue.put(None)
        if self._thread:
            self._thread.join(timeout=3.0)

    def send_audio(self, pcm_data: bytes) -> None:
        """Send PCM audio data for recognition.
        
        Args:
            pcm_data: 16-bit PCM audio data (16kHz mono)
        """
        if not pcm_data:
            return
        if not self._running.is_set():
            return
        self._audio_queue.put(pcm_data)

    def _emit_error(self, message: str) -> None:
        """Emit an error event."""
        self._event_queue.put({
            "type": "transcript_error",
            "channel": self._channel,
            "error": message,
        })

    def _emit_transcript(self, text: str, is_final: bool) -> None:
        """Emit a transcript event."""
        if not text:
            return
        # Skip duplicate partial results
        if text == self._last_text and not is_final:
            return
        self._last_text = text
        self._event_queue.put({
            "type": "transcript_final" if is_final else "transcript_partial",
            "channel": self._channel,
            "text": text,
            "timestamp": time.strftime("%H:%M:%S"),
        })
        if is_final:
            self._last_text = ""

    def _run(self) -> None:
        """Main WebSocket communication loop."""
        ws = None
        task_id = uuid.uuid4().hex
        task_started = False
        pcm_buffer = bytearray()
        chunk_interval_s = 0.2  # Send audio every 200ms
        chunk_bytes = int(self._sample_rate * chunk_interval_s) * 2  # 16-bit = 2 bytes
        next_send_time = time.monotonic()

        try:
            # Connect with authentication
            headers = [
                f"Authorization: Bearer {self._api_key}",
                "user-agent: funasr-realtime-client",
            ]
            ws = websocket.create_connection(self._ws_url, header=headers, timeout=10)
            ws.settimeout(0.1)
            
            # Send run-task message
            ws.send(self._build_run_task(task_id))

            while self._running.is_set() or pcm_buffer:
                # Receive any pending messages
                try:
                    message = ws.recv()
                    if message:
                        self._handle_message(message)
                        event_type = self._get_event_type(message)
                        if event_type == "task-started":
                            task_started = True
                        elif event_type in {"task-failed", "error"}:
                            break
                        elif event_type == "task-finished":
                            break
                except websocket.WebSocketTimeoutException:
                    pass
                except Exception as exc:
                    self._emit_error(f"WebSocket recv error: {exc}")
                    break

                # Drain audio queue
                try:
                    while True:
                        item = self._audio_queue.get_nowait()
                        if item is None:
                            self._running.clear()
                            break
                        pcm_buffer.extend(item)
                except queue.Empty:
                    pass

                # Send audio in chunks
                now = time.monotonic()
                if task_started and len(pcm_buffer) >= chunk_bytes and now >= next_send_time:
                    chunk = bytes(pcm_buffer[:chunk_bytes])
                    del pcm_buffer[:chunk_bytes]

                    try:
                        ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception as exc:
                        self._emit_error(f"WebSocket send error: {exc}")
                        break

                    next_send_time = max(next_send_time + chunk_interval_s, now + chunk_interval_s * 0.5)

                time.sleep(0.01)

            # Flush remaining audio
            if task_started and pcm_buffer:
                try:
                    ws.send(bytes(pcm_buffer), opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception:
                    pass

            # Send finish-task message
            if task_started:
                try:
                    ws.send(self._build_finish_task(task_id))
                    # Wait for final results
                    for _ in range(30):
                        try:
                            message = ws.recv()
                            if message:
                                self._handle_message(message)
                                if self._get_event_type(message) == "task-finished":
                                    break
                        except websocket.WebSocketTimeoutException:
                            break
                        except Exception:
                            break
                except Exception:
                    pass

        except Exception as exc:
            self._emit_error(f"FunASR connection error: {exc}")
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    def _build_run_task(self, task_id: str) -> str:
        """Build run-task message for FunASR."""
        payload = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex"
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self._model,
                "parameters": {
                    "format": "pcm",
                    "sample_rate": self._sample_rate,
                    "language_hints": self._language_hints,
                },
                "input": {}
            }
        }
        return json.dumps(payload)

    @staticmethod
    def _normalize_language_hints(
        language_hints: Optional[list[str]], language: str
    ) -> list[str]:
        if language_hints:
            cleaned = [item.strip() for item in language_hints if item and item.strip()]
            if cleaned:
                return cleaned
        fallback = (language or "zh").strip()
        return [fallback] if fallback else ["zh"]

    def _build_finish_task(self, task_id: str) -> str:
        """Build finish-task message."""
        payload = {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex"
            },
            "payload": {
                "input": {}
            }
        }
        return json.dumps(payload)

    def _get_event_type(self, message: str) -> str:
        """Extract event type from message."""
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")
            data = json.loads(message)
            return data.get("header", {}).get("event", "")
        except Exception:
            return ""

    def _handle_message(self, message: str) -> None:
        """Handle message from FunASR server."""
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")
            data = json.loads(message)
        except Exception:
            return

        header = data.get("header", {})
        event = header.get("event", "")

        if event == "result-generated":
            output = data.get("payload", {}).get("output", {})
            sentence = output.get("sentence", {})
            text = sentence.get("text", "")
            
            if not text:
                return
            if sentence.get("heartbeat"):
                return

            # Determine if this is a final result
            is_final = self._is_final_sentence(sentence, output)
            self._emit_transcript(text, is_final)

        elif event in {"task-failed", "error"}:
            error_msg = header.get("error_message") or "Task failed"
            self._emit_error(error_msg)

        elif event == "task-finished":
            # Emit final text if we have any
            if self._last_text:
                self._emit_transcript(self._last_text, is_final=True)

    @staticmethod
    def _is_final_sentence(sentence: dict, output: dict) -> bool:
        """Check if this is a final sentence."""
        if sentence.get("is_final") is True or sentence.get("isFinal") is True:
            return True
        if sentence.get("sentence_end") is True or sentence.get("sentenceEnd") is True:
            return True
        status = sentence.get("status") or sentence.get("state")
        if isinstance(status, str) and status.lower() in {"final", "completed", "complete", "done", "end"}:
            return True
        if output.get("is_final") is True or output.get("isFinal") is True:
            return True
        return False


def check_funasr_server(url: str = DEFAULT_FUNASR_URL, api_key: str = None, timeout: float = 5.0) -> bool:
    """Check if FunASR online API is accessible.
    
    Args:
        url: WebSocket URL
        api_key: API key for authentication
        timeout: Connection timeout
        
    Returns:
        True if server is reachable
    """
    if websocket is None:
        return False
    api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        return False
    try:
        headers = [f"Authorization: Bearer {api_key}"]
        ws = websocket.create_connection(url, header=headers, timeout=timeout)
        ws.close()
        return True
    except Exception:
        return False


# For backwards compatibility
DEFAULT_FUNASR_URL = DEFAULT_FUNASR_URL


if __name__ == "__main__":
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("Please set DASHSCOPE_API_KEY environment variable")
        print("Get your API key from: https://dashscope.console.aliyun.com/")
    else:
        print(f"Checking FunASR online API...")
        print(f"Endpoint: {DEFAULT_FUNASR_URL}")
        print(f"Model: {DEFAULT_FUNASR_MODEL}")
        if check_funasr_server(api_key=api_key):
            print("FunASR online API is accessible!")
        else:
            print("Failed to connect to FunASR online API")
