import argparse
import datetime
import hid
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog
from pathlib import Path
import uuid
from typing import Any

try:
    import opuslib
except ImportError:  # pragma: no cover - optional dependency
    opuslib = None

try:
    from funasr_client import FunASRRealtimeClient, DEFAULT_FUNASR_MODEL
except ImportError:  # pragma: no cover - optional dependency
    FunASRRealtimeClient = None
    DEFAULT_FUNASR_MODEL = "fun-asr-realtime-2025-11-07"

try:
    from live_meeting_summarizer import LiveMeetingSummarizer
except ImportError:  # pragma: no cover - optional dependency
    LiveMeetingSummarizer = None

# ASR backend: FunASR only

# Device configuration
VID = 0x0A12
PID = 0x4007
USAGE_PAGE = 0xFFA0  # Vendor-defined page for recorder commands
USAGE = 0x0001       # Vendor-defined usage

# HID report IDs
CMD_REPORT_ID = 0x08
STATUS_REPORT_ID = 0x0A
FILE_CHUNK_FEATURE_REPORT_ID = 0x0B
STREAM_REPORT_ID = 0x0C

# HID report sizes (total bytes, including report ID)
HID_VENDOR_REPORT_TOTAL = 63  # 1-byte ID + 62-byte payload

# Stream report format (matches device: usb_dongle_recording.c)
# Format: [report_id(1)][session_id(1)][frame_seq(1)][info(1)][payload_len(1)][payload(N)]
STREAM_HEADER_BYTES = 4  # session_id(1) + frame_seq(1) + info(1) + payload_len(1)
STREAM_MAX_PAYLOAD = HID_VENDOR_REPORT_TOTAL - 1 - STREAM_HEADER_BYTES  # 58 bytes max

# Info byte bit definitions
STREAM_INFO_CHANNEL_MASK = 0x03      # Bits 0-1: channel (0=RX, 1=TX)
STREAM_INFO_LAST_PKT = 0x04          # Bit 2: Last packet of voice frame
STREAM_INFO_DISCONTINUITY = 0x08     # Bit 3: Data was dropped
STREAM_INFO_START = 0x10             # Bit 4: Session start
STREAM_INFO_END = 0x20               # Bit 5: Session end

# Channel definitions
CHANNEL_RX = 0  # Microphone input (from headset)
CHANNEL_TX = 1  # Speaker output (to headset)
CHANNEL_NAMES = {CHANNEL_RX: "rx", CHANNEL_TX: "tx"}

# Transcription defaults (Opus -> PCM)
TRANSCRIBE_SAMPLE_RATE = int(os.getenv("TRANSCRIBE_SAMPLE_RATE", "16000"))
TRANSCRIBE_CHANNELS = 1
TRANSCRIBE_FRAME_MS = int(os.getenv("OPUS_FRAME_MS", "20"))
TRANSCRIBE_STABLE_WINDOW_SEC = max(
    0.2, min(9.5, float(os.getenv("TRANSCRIBE_STABLE_WINDOW_SEC", "2.0")))
)
TRANSCRIBE_RENDER_INTERVAL_SEC = max(
    0.5,
    min(
        9.5,
        float(os.getenv("TRANSCRIBE_RENDER_INTERVAL_SEC", os.getenv("TRANSCRIBE_COMMIT_INTERVAL_SEC", "3.0"))),
    ),
)
SUMMARY_INTERVAL_SEC = max(5.0, float(os.getenv("SUMMARY_INTERVAL_SEC", "30")))

# Recording file format (usb_dongle_recording.c)
# Frame header: [channel(1)][length_le(2)] followed by payload bytes.
RECORDING_FRAME_HEADER_SIZE = 3
# Commands
CMD_HOST_CONNECTION = 0x00
CMD_START_RECORDING = 0x01
CMD_STOP_RECORDING = 0x02
CMD_ENUM_FILES = 0x03
CMD_DELETE_FILE = 0x04
CMD_OPEN_FILE = 0x05
CMD_READ_FILE = 0x06
CMD_CLOSE_FILE = 0x07
CMD_STREAM_CONTROL = 0x09

# Recorder events from device
EVENT_RECORDING_STARTED = 0x01
EVENT_RECORDING_STOPPED = 0x02
EVENT_COMMAND_REJECTED = 0x03
EVENT_ERROR = 0x04
EVENT_FILE_ENTRY = 0x05
EVENT_FILE_LIST_DONE = 0x06
EVENT_FILE_DATA = 0x07
EVENT_FILE_READ_DONE = 0x08
EVENT_FILE_CHUNK_READY = 0x09
EVENT_FILE_DELETED = 0x0A
EVENT_FILE_READ_DONE_WITH_CRC = EVENT_FILE_READ_DONE  # same id, payload carries CRCs

# Status codes (USB_RECORDER_STATUS_T)
STATUS_TEXT = {
    0: "OK",
    1: "ERROR",
    2: "INVALID_STATE",
    3: "BUSY",
    4: "FILE_ERROR",
    5: "TIMEOUT",
}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_language_hints(default_language: str, specific_env_name: str) -> list[str]:
    global_hints = os.getenv("ASR_LANGUAGE_HINTS", "").strip()
    if global_hints:
        parsed = [item.strip() for item in global_hints.split(",") if item.strip()]
        if parsed:
            return parsed

    raw = os.getenv(specific_env_name, default_language).strip()
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return parsed if parsed else ["zh"]


class RecorderProtocol:
    """Low-level HID protocol helper."""

    def __init__(self, event_queue: queue.Queue):
        self._event_queue = event_queue
        self._device = None
        self._lock = threading.Lock()
        self._reader = None
        self._running = False
        self._token = 1
        self._read_sessions = {}  # token -> {filename, chunks, total_bytes}
        self._stream_sessions = {}  # session_id -> {path, file, bytes, last_seq, drops}
        self._stream_dir = Path("stream_capture")
        self._transcriber = None

    @property
    def connected(self) -> bool:
        return self._device is not None

    def connect(self) -> bool:
        with self._lock:
            if self._device:
                return True
            try:
                # Enumerate all HID devices to find the specific interface
                devices = hid.enumerate(VID, PID)
                target_path = None
                
                for device_info in devices:
                    # Match by usage_page and usage to identify the recorder command interface
                    if (device_info.get('usage_page') == USAGE_PAGE and 
                        device_info.get('usage') == USAGE):
                        target_path = device_info['path']
                        print(f"Found recorder interface: {device_info.get('interface_number', -1)}, "
                              f"usage_page=0x{USAGE_PAGE:04X}, usage=0x{USAGE:04X}")
                        break
                
                if not target_path:
                    raise RuntimeError(
                        f"Recorder interface not found (VID=0x{VID:04X}, PID=0x{PID:04X}, "
                        f"usage_page=0x{USAGE_PAGE:04X}, usage=0x{USAGE:04X})"
                    )
                
                # Open the specific interface by path
                dev = hid.device()
                dev.open_path(target_path)
                dev.set_nonblocking(False)
                self._device = dev
                self._running = True
                self._reader = threading.Thread(target=self._read_loop, daemon=True)
                self._reader.start()
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put({"type": "connect_failed", "error": str(exc)})
                self._device = None
                self._running = False
                return False

        self._event_queue.put({"type": "connected"})
        self._send_raw([CMD_REPORT_ID, CMD_HOST_CONNECTION])
        return True

    def close(self) -> None:
        self._running = False
        reader = self._reader
        if reader:
            reader.join(timeout=1.0)
        for session in list(self._stream_sessions.values()):
            try:
                session["file"].close()
            except Exception:  # noqa: BLE001
                pass
        self._stream_sessions.clear()
        with self._lock:
            if self._device:
                try:
                    self._device.close()
                except Exception:  # noqa: BLE001
                    pass
                self._device = None

    def set_transcriber(self, transcriber) -> None:
        self._transcriber = transcriber

    def start_recording(self) -> int:
        token = self._next_token()
        timestamp = time.strftime("%Y%m%d-%H-%M-%S")
        filename = f"{timestamp}.raw"
        name_bytes = list(filename.encode("ascii", errors="ignore"))
        payload = [CMD_REPORT_ID, CMD_START_RECORDING, token & 0xFF, (token >> 8) & 0xFF] + name_bytes
        self._send_raw(payload)
        return token

    def stop_recording(self) -> int:
        token = self._next_token()
        self._send_raw([CMD_REPORT_ID, CMD_STOP_RECORDING, token & 0xFF, (token >> 8) & 0xFF])
        return token

    def set_streaming(self, enable: bool) -> int:
        token = self._next_token()
        payload = [CMD_REPORT_ID, CMD_STREAM_CONTROL, token & 0xFF, (token >> 8) & 0xFF, 1 if enable else 0]
        self._send_raw(payload)
        return token

    def request_file_list(self) -> int:
        token = self._next_token()
        self._send_raw([CMD_REPORT_ID, CMD_ENUM_FILES, token & 0xFF, (token >> 8) & 0xFF])
        return token

    def read_file(self, filename: str, offset: int = 0, length: int = 0xFFFFFFFF) -> int:
        """Request to read a file from the device.
        
        Args:
            filename: Name of file to read
            offset: Byte offset to start reading from (default 0)
            length: Number of bytes to read (default 0xFFFFFFFF for entire file, max uint32)
            
        Returns:
            Token for tracking this request
        """
        token = self._next_token()
        name_bytes = list(filename.encode("ascii", errors="ignore")[:48])
        name_len = len(name_bytes)
        
        # Build payload: [REPORT_ID][CMD][token(2)][filename_len][filename][offset(4)][length(4)]
        payload = [
            CMD_REPORT_ID,
            CMD_READ_FILE,
            token & 0xFF,
            (token >> 8) & 0xFF,
            name_len,
        ]
        payload.extend(name_bytes)
        
        # Add offset (4 bytes LE)
        payload.extend([
            offset & 0xFF,
            (offset >> 8) & 0xFF,
            (offset >> 16) & 0xFF,
            (offset >> 24) & 0xFF,
        ])
        
        # Add length (4 bytes LE)
        payload.extend([
            length & 0xFF,
            (length >> 8) & 0xFF,
            (length >> 16) & 0xFF,
            (length >> 24) & 0xFF,
        ])
        
        # Initialize read session tracking
        self._read_sessions[token] = {
            "filename": filename,
            "chunks": [],
            "total_bytes": 0,
            "offset": offset,
            "length": length,
        }
        
        self._send_raw(payload)
        return token

    def delete_file(self, filename: str) -> int:
        """Request to delete a file on the device."""
        token = self._next_token()
        name_bytes = list(filename.encode("ascii", errors="ignore")[:48])
        name_len = len(name_bytes)
        payload = [CMD_REPORT_ID, CMD_DELETE_FILE, token & 0xFF, (token >> 8) & 0xFF, name_len]
        payload.extend(name_bytes)
        self._send_raw(payload)
        return token

    def close_file(self) -> None:
        """Send command to close the currently open file on device."""
        self._send_raw([CMD_REPORT_ID, CMD_CLOSE_FILE])

    def _retrieve_chunk_via_feature(self, token: int, seq: int, chunk_len: int, 
                                     remaining: int, eof: bool) -> None:
        """Retrieve chunk data via USB feature report (called in background thread).
        
        Args:
            token: Session token
            seq: Sequence number
            chunk_len: Expected chunk length
            remaining: Remaining bytes after this chunk
            eof: End of file flag
        """
        try:
            with self._lock:
                if not self._device:
                    return
                
                # Get feature report 0x0B (512 bytes: 1 + 2 + 4 + 2 + 502 + 1)
                # Format: [report_id][token(2)][seq(4)][chunk_len(2)][data(502)][flags(1)]
                print(f"DEBUG: device.get_feature_report")
                data = self._device.get_feature_report(FILE_CHUNK_FEATURE_REPORT_ID, 513)
            
            if not data or len(data) < 10:
                self._event_queue.put({"type": "error", "origin": "feature_read",
                                      "error": "Invalid feature report"})
                return
            
            # Parse feature report
            report_id = data[0]
            ret_token = data[1] | (data[2] << 8)
            ret_seq = data[3] | (data[4] << 8) | (data[5] << 16) | (data[6] << 24)
            ret_chunk_len = data[7] | (data[8] << 8)
            chunk_data = bytes(data[9:9 + ret_chunk_len])
            ret_eof = data[511] if len(data) >= 512 else 0  # EOF flag at last byte
            
            # Validate
            if ret_token != token or ret_seq != seq:
                self._event_queue.put({
                    "type": "error",
                    "origin": "chunk_mismatch",
                    "error": f"Token/seq mismatch: expected {token}/{seq}, got {ret_token}/{ret_seq}"
                })
                return
            
            # Store chunk in session
            if token in self._read_sessions:
                session = self._read_sessions[token]
                session["chunks"].append(chunk_data)
                session["total_bytes"] += len(chunk_data)
                
                # Emit progress event
                self._event_queue.put({
                    "type": "file_data_chunk",
                    "token": token,
                    "filename": session["filename"],
                    "chunk_size": len(chunk_data),
                    "total_bytes": session["total_bytes"],
                    "remaining": remaining,
                    "eof": bool(ret_eof),
                    "seq": ret_seq,
                })
                
                # If EOF, emit completion event
                if ret_eof:
                    full_data = b"".join(session["chunks"])
                    self._event_queue.put({
                        "type": "file_read_complete",
                        "token": token,
                        "filename": session["filename"],
                        "data": full_data,
                        "total_bytes": len(full_data),
                        "status": 0,
                    })
                    del self._read_sessions[token]
        
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            self._event_queue.put({
                "type": "error",
                "origin": "chunk_retrieval",
                "error": f"{exc}",
                "details": tb,
            })

    def _next_token(self) -> int:
        token = self._token
        self._token += 1
        if self._token > 0xFFFF:
            self._token = 1
        return token

    def _send_raw(self, payload: list[int]) -> None:
        with self._lock:
            if not self._device:
                raise RuntimeError("Device not connected")
            if len(payload) > HID_VENDOR_REPORT_TOTAL:
                raise ValueError(f"HID payload too large ({len(payload)} > {HID_VENDOR_REPORT_TOTAL})")
            report = [0] * HID_VENDOR_REPORT_TOTAL
            report[: len(payload)] = payload
            try:
                self._device.write(report)
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put({"type": "disconnected", "error": str(exc)})
                self._device = None
                self._running = False
                raise

    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self._device.read(HID_VENDOR_REPORT_TOTAL, timeout_ms=500)
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put({"type": "disconnected", "error": str(exc)})
                with self._lock:
                    self._device = None
                self._running = False
                break

            if not data:
                continue

            try:
                self._handle_report(data)
            except Exception as exc:  # noqa: BLE001
                import traceback
                error_details = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                self._event_queue.put({"type": "error", "origin": "host", "error": error_details})

    @staticmethod
    def _le_value(buf: list[int], offset: int, size: int) -> int:
        value = 0
        for idx in range(size):
            value |= buf[offset + idx] << (8 * idx)
        return value

    def _handle_report(self, data: list[int]) -> None:
        if len(data) < 2:
            return

        if data[0] == STREAM_REPORT_ID:
            self._handle_stream_report(data)
            return

        if len(data) < 5 or data[0] != STATUS_REPORT_ID:
            print(f"DEBUG: Ignoring report, len={len(data)}, report_id={data[0] if data else 'N/A'}")
            return
        print(f"DEBUG: Handling status report, event_id={data[1] if len(data) > 1 else 'N/A'}")
        event_id = data[1]
        status = data[2]
        token = data[3] | (data[4] << 8)
        payload = data[5:]

        if event_id == EVENT_RECORDING_STARTED:
            if status == 0 and len(payload) >= 7:
                name_len = payload[0]
                name = bytes(payload[1 : 1 + name_len]).decode(errors="ignore")
                offset = 1 + name_len
                if len(payload) >= offset + 6:
                    sample_rate = self._le_value(payload, offset, 4)
                    frame_size = self._le_value(payload, offset + 4, 2)
                else:
                    sample_rate = 0
                    frame_size = 0
                self._event_queue.put(
                    {
                        "type": "recording_started",
                        "token": token,
                        "filename": name,
                        "sample_rate": sample_rate,
                        "frame_size": frame_size,
                        "status": status,
                    }
                )
            else:
                self._event_queue.put(
                    {"type": "start_failed", "token": token, "status": status}
                )

        elif event_id == EVENT_RECORDING_STOPPED:
            if status == 0 and len(payload) >= 9:
                name_len = payload[0]
                name = bytes(payload[1 : 1 + name_len]).decode(errors="ignore")
                offset = 1 + name_len
                file_size = self._le_value(payload, offset, 4) if len(payload) >= offset + 4 else 0
                duration = self._le_value(payload, offset + 4, 4) if len(payload) >= offset + 8 else 0
                crc = self._le_value(payload, offset + 8, 4) if len(payload) >= offset + 12 else None
                self._event_queue.put(
                    {
                        "type": "recording_stopped",
                        "token": token,
                        "filename": name,
                        "file_size": file_size,
                        "duration_ms": duration,
                        "crc": crc,
                        "status": status,
                    }
                )
            else:
                self._event_queue.put(
                    {"type": "stop_failed", "token": token, "status": status}
                )

        elif event_id == EVENT_COMMAND_REJECTED:
            self._event_queue.put(
                {"type": "command_rejected", "token": token, "status": status}
            )

        elif event_id == EVENT_ERROR:
            self._event_queue.put({"type": "device_error", "token": token, "status": status})

        elif event_id == EVENT_FILE_ENTRY:
            if payload and len(payload) >= 6:
                name_len = payload[0]
                name = bytes(payload[1 : 1 + name_len]).decode(errors="ignore")
                print(name)
                offset = 1 + name_len
                file_size = self._le_value(payload, offset, 4) if len(payload) >= offset + 4 else 0
                is_dir = payload[offset + 4] if len(payload) > offset + 4 else 0
                self._event_queue.put(
                    {
                        "type": "file_entry",
                        "token": token,
                        "name": name,
                        "file_size": file_size,
                        "is_dir": bool(is_dir),
                    }
                )

        elif event_id == EVENT_FILE_LIST_DONE:
            total_entries = self._le_value(payload, 0, 2) if payload else 0
            self._event_queue.put(
                {
                    "type": "file_list_done",
                    "token": token,
                    "status": status,
                    "total": total_entries,
                }
            )

        elif event_id == EVENT_FILE_DATA:
            # Payload: [chunk_len(2)][data...][has_more(1)]
            if token in self._read_sessions and len(payload) >= 3:
                chunk_len = self._le_value(payload, 0, 2)
                chunk_data = bytes(payload[2 : 2 + chunk_len])
                has_more = payload[2 + chunk_len] if len(payload) > 2 + chunk_len else 0
                
                session = self._read_sessions[token]
                session["chunks"].append(chunk_data)
                session["total_bytes"] += len(chunk_data)
                
                self._event_queue.put(
                    {
                        "type": "file_data_chunk",
                        "token": token,
                        "filename": session["filename"],
                        "chunk_size": len(chunk_data),
                        "total_bytes": session["total_bytes"],
                        "has_more": bool(has_more),
                        "status": status,
                    }
                )

        elif event_id == EVENT_FILE_READ_DONE:
            crc_actual = self._le_value(payload, 0, 4) if len(payload) >= 4 else None
            crc_expected = self._le_value(payload, 4, 4) if len(payload) >= 8 else None
            if token in self._read_sessions:
                session = self._read_sessions[token]
                # Reassemble all chunks
                full_data = b"".join(session["chunks"])
                
                self._event_queue.put(
                    {
                        "type": "file_read_complete",
                        "token": token,
                        "filename": session["filename"],
                        "data": full_data,
                        "total_bytes": len(full_data),
                        "status": status,
                        "crc_actual": crc_actual,
                        "crc_expected": crc_expected,
                    }
                )
                
                # Clean up session
                del self._read_sessions[token]
            else:
                # No active session - just notify
                self._event_queue.put(
                    {
                        "type": "file_read_complete",
                        "token": token,
                        "filename": "",
                        "data": b"",
                        "total_bytes": 0,
                        "status": status,
                        "crc_actual": crc_actual,
                        "crc_expected": crc_expected,
                    }
                )

        elif event_id == EVENT_FILE_DELETED:
            name = ""
            if payload:
                name_len = payload[0]
                name = bytes(payload[1 : 1 + name_len]).decode(errors="ignore")
            self._event_queue.put(
                {
                    "type": "file_deleted",
                    "token": token,
                    "status": status,
                    "name": name,
                }
            )

        elif event_id == EVENT_FILE_CHUNK_READY:
            # Payload: [seq(4)][chunk_len(2)][remaining(4)][eof(1)]
            if len(payload) >= 11:
                seq = self._le_value(payload, 0, 4)
                chunk_len = self._le_value(payload, 4, 2)
                remaining = self._le_value(payload, 6, 4)
                eof = payload[10] if len(payload) > 10 else 0
                
                # Spawn thread to retrieve chunk via feature report
                threading.Thread(
                    target=self._retrieve_chunk_via_feature,
                    args=(token, seq, chunk_len, remaining, bool(eof)),
                    daemon=True
                ).start()

    def _handle_stream_report(self, data: list[int]) -> None:
        """Handle incoming stream report from device.
        
        Device format: [report_id(1)][session_id(1)][frame_seq(1)][info(1)][payload_len(1)][payload(N)]
        
        info byte:
          - bits 0-1: channel (0=RX mic input, 1=TX speaker output)
          - bit 2: LAST_PKT (last packet of voice frame)
          - bit 3: DISCONTINUITY (data was dropped)
          - bit 4: START (session start)
          - bit 5: END (session end)
        """
        if len(data) < 1 + STREAM_HEADER_BYTES:
            return

        # Parse header (data[0] is report_id which we already checked)
        session_id = data[1]
        frame_seq = data[2]
        info = data[3]
        payload_len = data[4]

        # Parse info byte
        channel = info & STREAM_INFO_CHANNEL_MASK
        is_last_pkt = bool(info & STREAM_INFO_LAST_PKT)
        is_discontinuity = bool(info & STREAM_INFO_DISCONTINUITY)
        is_start = bool(info & STREAM_INFO_START)
        is_end = bool(info & STREAM_INFO_END)

        if payload_len > STREAM_MAX_PAYLOAD:
            payload_len = STREAM_MAX_PAYLOAD

        payload = bytes(data[5 : 5 + payload_len]) if payload_len else b""

        # Use composite key: (session_id, channel) for separate RX/TX files
        session_key = (session_id, channel)
        channel_name = CHANNEL_NAMES.get(channel, f"ch{channel}")
        
        session = self._stream_sessions.get(session_key)
        if session is None or is_start:
            self._stream_dir.mkdir(exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            # Create separate files for RX and TX
            path = self._stream_dir / f"stream_{session_id:02X}_{channel_name}_{timestamp}.opus"
            try:
                handle = path.open("ab")
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put(
                    {"type": "stream_error", "session_id": session_id, 
                     "channel": channel_name, "error": str(exc)}
                )
                return

            session = {
                "path": path,
                "file": handle,
                "bytes": 0,
                "last_seq": None,
                "drops": 0,
                "channel": channel,
                "channel_name": channel_name,
                "frame_buffer": b"",  # Buffer to reassemble voice frames
            }
            self._stream_sessions[session_key] = session
            self._event_queue.put(
                {"type": "stream_started", "session_id": session_id, 
                 "channel": channel_name, "path": str(path)}
            )

        # Check for sequence discontinuity (per-channel frame sequence)
        if session["last_seq"] is not None:
            # frame_seq wraps at 256
            expected = (session["last_seq"] + 1) & 0xFF
            if frame_seq != expected and not is_start:
                session["drops"] += 1
                self._event_queue.put(
                    {
                        "type": "stream_drop",
                        "session_id": session_id,
                        "channel": channel_name,
                        "seq": frame_seq,
                        "expected": expected,
                    }
                )

        if is_discontinuity:
            session["drops"] += 1
            self._event_queue.put(
                {"type": "stream_drop", "session_id": session_id, 
                 "channel": channel_name, "seq": frame_seq, "discontinuity": True}
            )

        # Accumulate payload into frame buffer
        if payload:
            session["frame_buffer"] += payload

        # Write complete frame when LAST_PKT is set
        if is_last_pkt and session["frame_buffer"]:
            try:
                session["file"].write(session["frame_buffer"])
                session["bytes"] += len(session["frame_buffer"])
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put(
                    {"type": "stream_error", "session_id": session_id,
                     "channel": channel_name, "error": str(exc)}
                )
            if self._transcriber:
                self._transcriber.push_frame(channel, session["frame_buffer"])
            session["frame_buffer"] = b""
            session["last_seq"] = frame_seq

        if is_end:
            # Flush any remaining data
            if session["frame_buffer"]:
                try:
                    session["file"].write(session["frame_buffer"])
                    session["bytes"] += len(session["frame_buffer"])
                except Exception:  # noqa: BLE001
                    pass
            try:
                session["file"].close()
            except Exception:  # noqa: BLE001
                pass
            self._event_queue.put(
                {
                    "type": "stream_stopped",
                    "session_id": session_id,
                    "channel": channel_name,
                    "bytes": session["bytes"],
                    "drops": session["drops"],
                    "path": str(session["path"]),
                }
            )
            self._stream_sessions.pop(session_key, None)


class StreamTranscriber:
    """Audio transcriber using FunASR only."""
    
    def __init__(self, event_queue: queue.Queue):
        self._event_queue = event_queue
        self._enabled = False
        self._queue = queue.Queue(maxsize=200)
        self._worker = None
        self._stop_event = threading.Event()
        self._clients: dict[int, object] = {}
        self._decoders: dict[int, "opuslib.Decoder"] = {}
        self._frame_size_samples = int(TRANSCRIBE_SAMPLE_RATE * (TRANSCRIBE_FRAME_MS / 1000.0))

    def get_backend_name(self) -> str:
        """Return the current ASR backend name."""
        return "funasr"

    def enable(self) -> bool:
        if self._enabled:
            return True
        if opuslib is None:
            self._emit_error("Missing dependency: opuslib (pip install opuslib)")
            return False

        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            self._emit_error("Missing DASHSCOPE_API_KEY environment variable")
            return False

        success = self._enable_funasr(api_key)

        if not success:
            return False

        self._decoders = {
            CHANNEL_RX: opuslib.Decoder(TRANSCRIBE_SAMPLE_RATE, TRANSCRIBE_CHANNELS),
            CHANNEL_TX: opuslib.Decoder(TRANSCRIBE_SAMPLE_RATE, TRANSCRIBE_CHANNELS),
        }

        self._stop_event.clear()
        self._worker = threading.Thread(target=self._decode_loop, daemon=True)
        self._worker.start()
        self._enabled = True
        return True

    def _enable_funasr(self, api_key: str) -> bool:
        """Enable FunASR backend (online, via DashScope)."""
        if FunASRRealtimeClient is None:
            self._emit_error("Missing dependency: funasr_client.py")
            return False

        language_hints = parse_language_hints("zh", "FUNASR_LANGUAGE")
        model = os.getenv("FUNASR_MODEL", DEFAULT_FUNASR_MODEL)
        ws_url = os.getenv("FUNASR_WS_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/inference")

        self._clients = {
            CHANNEL_RX: FunASRRealtimeClient(
                api_key=api_key,
                event_queue=self._event_queue,
                channel="rx",
                sample_rate=TRANSCRIBE_SAMPLE_RATE,
                ws_url=ws_url,
                model=model,
                language=language_hints[0],
                language_hints=language_hints,
            ),
            CHANNEL_TX: FunASRRealtimeClient(
                api_key=api_key,
                event_queue=self._event_queue,
                channel="tx",
                sample_rate=TRANSCRIBE_SAMPLE_RATE,
                ws_url=ws_url,
                model=model,
                language=language_hints[0],
                language_hints=language_hints,
            ),
        }

        for client in self._clients.values():
            if not client.start():
                self.disable()
                return False

        return True

    def disable(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker:
            self._worker.join(timeout=2.0)
        for client in self._clients.values():
            client.stop()
        self._clients.clear()
        self._decoders.clear()

    def push_frame(self, channel: int, frame: bytes) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait((channel, frame))
        except queue.Full:
            self._emit_error("Transcription queue full; dropping audio")

    def _decode_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            channel, frame = item
            decoder = self._decoders.get(channel)
            client = self._clients.get(channel)
            if not decoder or not client:
                continue
            try:
                pcm = decoder.decode(frame, self._frame_size_samples)
            except Exception as exc:  # noqa: BLE001
                self._emit_error(f"Opus decode error ({CHANNEL_NAMES.get(channel,'?')}): {exc}")
                continue
            client.send_audio(pcm)

    def _emit_error(self, message: str) -> None:
        self._event_queue.put(
            {"type": "transcript_error", "channel": "system", "error": message}
        )

class RecorderTester:
    def __init__(self, root: tk.Tk, simulate_transcript: bool = False):
        self.root = root
        self.root.title("Voice Recorder Tester")
        self.root.geometry("900x860")

        self.events = queue.Queue()
        self.protocol = RecorderProtocol(self.events)
        self.transcriber = StreamTranscriber(self.events)
        self.protocol.set_transcriber(self.transcriber)
        self.simulate_transcript = simulate_transcript
        self.recording = False
        self.known_files: list[str] = []  # Changed to list to maintain order
        self.pending_start = False
        self.pending_stop = False
        self.pending_read = False
        self.pending_delete = False
        self.pending_stream_token = None
        self.stream_enabled = False
        self.stream_prev_enabled = False
        self.transcription_enabled = False

        self.status_var = tk.StringVar(value="Disconnected")
        self.token_var = tk.StringVar(value="Token: -")
        self.file_count_var = tk.StringVar(value="Files (known): 0")
        self.selected_file_var = tk.StringVar(value="")
        self.read_progress_var = tk.StringVar(value="")
        self.transcript_status_var = tk.StringVar(value="Transcription: off")
        # Transcript state tracking
        self._latest_text = {"rx": "", "tx": ""}
        self._latest_timestamp = {"rx": "", "tx": ""}
        self._last_update_time = {"rx": 0.0, "tx": 0.0}
        self._last_render_time = {"rx": 0.0, "tx": 0.0}
        self._last_render_text = {"rx": "", "tx": ""}
        self._summary_interval_sec = SUMMARY_INTERVAL_SEC
        self._summarizer = self._create_summarizer()
        self._summary_state = self._empty_summary_state()
        self._active_stream_channels: set[str] = set()
        self._session_active = False
        self._session_dir: Path | None = None
        self._transcript_jsonl_path: Path | None = None
        self._summary_jsonl_path: Path | None = None
        self.committed_segments: list[dict[str, Any]] = []
        self._simulate_stop = threading.Event()
        self._simulate_worker: threading.Thread | None = None

        self._build_ui()
        self._render_summary_state(self._summary_state, "")
        if self._summarizer is None:
            self._log("Live summary disabled: missing dependencies or module")
        self._update_buttons()
        if self.simulate_transcript:
            self._set_status("Simulation mode", "blue")
            self._log("Simulation mode enabled (no device required)")
            self._start_live_summary_session("simulation")
            self._start_simulation_transcripts()
        else:
            self.connect_device()
        self.root.after(200, self._drain_events)

    def _create_summarizer(self):
        if LiveMeetingSummarizer is None:
            return None
        return LiveMeetingSummarizer(
            event_queue=self.events,
            summary_interval_sec=self._summary_interval_sec,
        )

    @staticmethod
    def _empty_summary_state() -> dict[str, Any]:
        return {
            "running_summary": "",
            "bullets": [],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "meta": {"window_start": "", "window_end": "", "language": "zh-en"},
        }

    def _build_ui(self) -> None:
        status_frame = tk.Frame(self.root)
        status_frame.pack(fill=tk.X, pady=4)

        self.status_label = tk.Label(status_frame, textvariable=self.status_var, fg="red")
        self.status_label.pack(side=tk.LEFT, padx=4)
        tk.Label(status_frame, textvariable=self.token_var).pack(side=tk.LEFT, padx=10)
        tk.Label(status_frame, textvariable=self.file_count_var).pack(side=tk.LEFT, padx=10)

        # Recording controls
        buttons = tk.Frame(self.root)
        buttons.pack(fill=tk.X, pady=6)

        self.btn_start = tk.Button(buttons, text="Start Recording", command=self.start_recording)
        self.btn_stop = tk.Button(buttons, text="Stop Recording", command=self.stop_recording)
        self.btn_stream = tk.Button(buttons, text="Enable HID Stream", command=self.toggle_streaming)
        self.btn_transcribe = tk.Button(buttons, text="Enable Transcription", command=self.toggle_transcription)
        self.btn_list = tk.Button(buttons, text="List Files", command=self.list_files)
        self.btn_reconnect = tk.Button(buttons, text="Reconnect", command=self.connect_device)

        self.btn_start.pack(side=tk.LEFT, padx=5)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        self.btn_stream.pack(side=tk.LEFT, padx=5)
        self.btn_transcribe.pack(side=tk.LEFT, padx=5)
        self.btn_list.pack(side=tk.LEFT, padx=5)
        self.btn_reconnect.pack(side=tk.RIGHT, padx=5)

        transcribe_status = tk.Frame(self.root)
        transcribe_status.pack(fill=tk.X, pady=2)
        tk.Label(transcribe_status, textvariable=self.transcript_status_var).pack(side=tk.LEFT, padx=6)

        # File reading section
        read_frame = tk.LabelFrame(self.root, text="File Operations", padx=10, pady=10)
        read_frame.pack(fill=tk.X, padx=6, pady=6)

        file_select_frame = tk.Frame(read_frame)
        file_select_frame.pack(fill=tk.X, pady=4)
        
        tk.Label(file_select_frame, text="File:").pack(side=tk.LEFT, padx=5)
        self.file_combo = tk.Entry(file_select_frame, textvariable=self.selected_file_var, width=30)
        self.file_combo.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        read_buttons = tk.Frame(read_frame)
        read_buttons.pack(fill=tk.X, pady=4)
        
        self.btn_read = tk.Button(read_buttons, text="Read & Save File", command=self.read_file)
        self.btn_read.pack(side=tk.LEFT, padx=5)

        self.btn_delete = tk.Button(read_buttons, text="Delete File", command=self.delete_file)
        self.btn_delete.pack(side=tk.LEFT, padx=5)
        
        self.btn_close_file = tk.Button(read_buttons, text="Close File", command=self.close_file)
        self.btn_close_file.pack(side=tk.LEFT, padx=5)
        
        tk.Label(read_buttons, textvariable=self.read_progress_var).pack(side=tk.LEFT, padx=10)

        # Transcript section (merged RX/TX)
        transcript_frame = tk.LabelFrame(self.root, text="Live Transcript", padx=6, pady=6)
        transcript_frame.pack(fill=tk.BOTH, expand=False, padx=6, pady=6)
        
        # Live partial display (Labels for real-time updates - avoids ScrolledText replace issues)
        live_frame = tk.Frame(transcript_frame)
        live_frame.pack(fill=tk.X, pady=(0, 4))
        
        self._live_text_vars = {
            "rx": tk.StringVar(value=""),
            "tx": tk.StringVar(value=""),
        }
        
        # RX live label (wrapping enabled)
        rx_live_frame = tk.Frame(live_frame)
        rx_live_frame.pack(fill=tk.X)
        tk.Label(rx_live_frame, text="RX:", fg="blue", width=4, anchor=tk.W).pack(side=tk.LEFT)
        self._rx_live_label = tk.Label(rx_live_frame, textvariable=self._live_text_vars["rx"],
                                        anchor=tk.W, justify=tk.LEFT, wraplength=650)
        self._rx_live_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # TX live label
        tx_live_frame = tk.Frame(live_frame)
        tx_live_frame.pack(fill=tk.X)
        tk.Label(tx_live_frame, text="TX:", fg="green", width=4, anchor=tk.W).pack(side=tk.LEFT)
        self._tx_live_label = tk.Label(tx_live_frame, textvariable=self._live_text_vars["tx"],
                                        anchor=tk.W, justify=tk.LEFT, wraplength=650)
        self._tx_live_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # Finalized transcript history
        self.transcript_box = scrolledtext.ScrolledText(transcript_frame, state=tk.DISABLED, height=6)
        self.transcript_box.pack(fill=tk.BOTH, expand=True)

        # Real-time meeting summary section
        summary_frame = tk.LabelFrame(
            self.root,
            text=f"Live Summary ({int(self._summary_interval_sec)}s)",
            padx=6,
            pady=6,
        )
        summary_frame.pack(fill=tk.BOTH, expand=False, padx=6, pady=6)

        self.summary_box = scrolledtext.ScrolledText(summary_frame, state=tk.DISABLED, height=9)
        self.summary_box.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        tk.Label(summary_frame, text="Action Items").pack(anchor=tk.W)
        self.action_items_box = scrolledtext.ScrolledText(summary_frame, state=tk.DISABLED, height=5)
        self.action_items_box.pack(fill=tk.BOTH, expand=True)

        # Log section
        log_frame = tk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_box = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED, height=10)
        self.log_box.pack(fill=tk.BOTH, expand=True)

    def connect_device(self) -> None:
        ok = self.protocol.connect()
        if ok:
            self._set_status("Connected", "green")
        else:
            self._set_status("Connect failed - retrying", "red")
            self.root.after(1500, self.connect_device)
        self._update_buttons()

    def start_recording(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        try:
            token = self.protocol.start_recording()
            self.token_var.set(f"Token: {token}")
            self._set_status("Start command sent", "blue")
            self.pending_start = True
            self.pending_stop = False
            self._update_buttons()
            self._log(f"Start command sent (token {token})")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send start: {exc}")
            self._set_status("Start failed", "red")
            self.pending_start = False
            self.pending_stop = False
            self._update_buttons()

    def stop_recording(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        try:
            token = self.protocol.stop_recording()
            self.token_var.set(f"Token: {token}")
            self._set_status("Stop command sent", "blue")
            self.pending_stop = True
            self._update_buttons()
            self._log(f"Stop command sent (token {token})")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send stop: {exc}")
            self._set_status("Stop failed", "red")

    def toggle_streaming(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        try:
            enable = not self.stream_enabled
            self.stream_prev_enabled = self.stream_enabled
            token = self.protocol.set_streaming(enable)
            self.token_var.set(f"Token: {token}")
            self._set_status("Stream command sent", "blue")
            self.pending_stream_token = token
            self.stream_enabled = enable
            self.btn_stream.config(text="Disable HID Stream" if self.stream_enabled else "Enable HID Stream")
            if not enable and self.transcription_enabled:
                self.toggle_transcription()
            if enable:
                self._start_live_summary_session("stream_enable")
            else:
                self._end_live_summary_session("stream_disable")
            self._log(f"Stream {'enable' if enable else 'disable'} sent (token {token})")
            self._update_buttons()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send stream command: {exc}")
            self._set_status("Stream command failed", "red")

    def toggle_transcription(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        if not self.transcription_enabled:
            if not self.stream_enabled:
                messagebox.showwarning("Recorder", "Enable HID Stream before transcription.")
                return
            ok = self.transcriber.enable()
            if not ok:
                messagebox.showwarning("Recorder", "Transcription enable failed. Check log.")
                return
            self.transcription_enabled = True
            self.btn_transcribe.config(text="Disable Transcription")
            self.transcript_status_var.set("Transcription: on (Alibaba Cloud)")
            self._log("Transcription enabled (Alibaba Cloud)")
        else:
            self.transcriber.disable()
            self.transcription_enabled = False
            self.btn_transcribe.config(text="Enable Transcription")
            self.transcript_status_var.set("Transcription: off")
            self._log("Transcription disabled")

    def list_files(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        try:
            token = self.protocol.request_file_list()
            self._log(f"Requested file listing (token {token}); device prints entries in its log.")
            self._set_status("File list requested", "blue")
        except Exception as exc:  # noqa: BLE001
            self._log(f"List request failed: {exc}")
            self._set_status("List request failed", "red")

    def read_file(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        
        filename = self.selected_file_var.get().strip()
        if not filename:
            messagebox.showwarning("Recorder", "Please enter or select a filename.")
            return
        
        try:
            # Request to read entire file (up to 4GB, max uint32)
            token = self.protocol.read_file(filename, offset=0, length=0xFFFFFFFF)
            self.token_var.set(f"Token: {token}")
            self._set_status(f"Reading {filename}...", "blue")
            self.read_progress_var.set("Reading...")
            self.pending_read = True
            self._update_buttons()
            self._log(f"Read file command sent for '{filename}' (token {token})")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send read command: {exc}")
            self._set_status("Read failed", "red")
            self.read_progress_var.set("")

    def delete_file(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return

        filename = self.selected_file_var.get().strip()
        if not filename:
            messagebox.showwarning("Recorder", "Please enter or select a filename to delete.")
            return

        try:
            token = self.protocol.delete_file(filename)
            self.token_var.set(f"Token: {token}")
            self.pending_delete = True
            self._set_status(f"Deleting {filename}...", "blue")
            self._log(f"Delete command sent for '{filename}' (token {token})")
            self._update_buttons()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send delete command: {exc}")
            self._set_status("Delete failed", "red")

    def close_file(self) -> None:
        if not self.protocol.connected:
            messagebox.showwarning("Recorder", "Device not connected yet.")
            return
        try:
            self.protocol.close_file()
            self._log("Close file command sent")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to send close command: {exc}")

    def _drain_events(self) -> None:
        while True:
            try:
                evt = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(evt)
        self._flush_live_lines()
        self._maybe_trigger_summary()
        self.root.after(150, self._drain_events)

    def _handle_event(self, evt: dict) -> None:
        etype = evt.get("type")

        if etype == "connected":
            self._set_status("Connected", "green")
            self._update_buttons()
            self._log("Device connected")

        elif etype == "connect_failed":
            self._set_status("Connect failed", "red")
            self._log(f"Connect failed: {evt.get('error')}")

        elif etype == "disconnected":
            self._set_status("Disconnected", "red")
            self._log(f"Device disconnected: {evt.get('error')}")
            self._active_stream_channels.clear()
            self._end_live_summary_session("device_disconnected")
            self.recording = False
            self.pending_start = False
            self.pending_stop = False
            self.pending_read = False
            self.pending_delete = False
            self.pending_stream_token = None
            self.stream_enabled = False
            self.stream_prev_enabled = False
            self.btn_stream.config(text="Enable HID Stream")
            if self.transcription_enabled:
                self.transcriber.disable()
                self.transcription_enabled = False
                self.btn_transcribe.config(text="Enable Transcription")
                self.transcript_status_var.set("Transcription: off")
            self._update_buttons()
            self.root.after(1500, self.connect_device)

        elif etype == "recording_started":
            self.recording = True
            self.pending_start = False
            filename = evt.get("filename", "")
            if filename and filename not in self.known_files:
                self.known_files.append(filename)
            self._set_status(f"Recording: {filename}", "green")
            self._log(
                f"Recording started ({filename}) "
                f"{evt.get('sample_rate', 0)} Hz, frame {evt.get('frame_size', 0)}"
            )
            self.file_count_var.set(f"Files (known): {len(self.known_files)}")
            self._update_buttons()

        elif etype == "recording_stopped":
            self.recording = False
            self.pending_stop = False
            self.pending_stream_token = None
            self.stream_enabled = False
            self.stream_prev_enabled = False
            self.btn_stream.config(text="Enable HID Stream")
            filename = evt.get("filename", "")
            if filename and filename not in self.known_files:
                self.known_files.append(filename)
            crc_val = evt.get("crc")
            crc_str = f" crc=0x{crc_val:08X}" if crc_val is not None else ""
            self._set_status("Stopped", "green")
            self._log(
                f"Recording stopped ({filename}) "
                f"size={evt.get('file_size', 0)} bytes duration={evt.get('duration_ms', 0)} ms{crc_str}"
            )
            self.file_count_var.set(f"Files (known): {len(self.known_files)}")
            self._update_buttons()

        elif etype in {"start_failed", "stop_failed", "command_rejected", "device_error"}:
            status_code = evt.get("status", -1)
            status_text = STATUS_TEXT.get(status_code, f"0x{status_code:02X}")
            label = etype.replace("_", " ").title()
            self._set_status(label, "red")
            self._log(f"{label} (status={status_text}, token={evt.get('token')})")
            self.recording = False
            self.pending_start = False
            self.pending_stop = False
            self.pending_delete = False
            if etype == "command_rejected" and evt.get("token") == self.pending_stream_token:
                self.pending_stream_token = None
                self.stream_enabled = self.stream_prev_enabled
                self.btn_stream.config(text="Disable HID Stream" if self.stream_enabled else "Enable HID Stream")
            self._update_buttons()

        elif etype == "error":
            self._log(f"Host error: {evt.get('error')}")

        elif etype == "stream_started":
            session_id = evt.get("session_id")
            channel = evt.get("channel", "?")
            path = evt.get("path", "")
            if channel in {"rx", "tx"}:
                self._active_stream_channels.add(channel)
            self._start_live_summary_session("stream_started")
            self._log(f"Stream started (session=0x{session_id:02X}, {channel.upper()}) -> {path}")

        elif etype == "stream_stopped":
            session_id = evt.get("session_id")
            channel = evt.get("channel", "?")
            total = evt.get("bytes", 0)
            drops = evt.get("drops", 0)
            path = evt.get("path", "")
            drop_info = f", {drops} drops" if drops else ""
            if channel in self._active_stream_channels:
                self._active_stream_channels.discard(channel)
            self._log(f"Stream stopped (session=0x{session_id:02X}, {channel.upper()}) {total} bytes{drop_info} -> {path}")
            if not self._active_stream_channels:
                self._end_live_summary_session("stream_stopped")

        elif etype == "stream_drop":
            session_id = evt.get("session_id")
            channel = evt.get("channel", "?")
            seq = evt.get("seq", 0)
            expected = evt.get("expected")
            is_discontinuity = evt.get("discontinuity", False)
            if is_discontinuity:
                self._log(f"Stream discontinuity (session=0x{session_id:02X}, {channel.upper()}) seq={seq}")
            elif expected is not None:
                self._log(f"Stream drop (session=0x{session_id:02X}, {channel.upper()}) seq={seq} expected={expected}")
            else:
                self._log(f"Stream drop (session=0x{session_id:02X}, {channel.upper()}) seq={seq}")

        elif etype == "stream_error":
            session_id = evt.get("session_id")
            channel = evt.get("channel", "?")
            self._log(f"Stream error (session=0x{session_id:02X}, {channel.upper()}): {evt.get('error')}")

        elif etype in {"transcript_partial", "transcript_final"}:
            channel = evt.get("channel", "")
            text = evt.get("text", "")
            timestamp = evt.get("timestamp", time.strftime("%H:%M:%S"))
            is_final = etype == "transcript_final"
            self._update_transcript(channel, text, timestamp, is_final)
            if is_final:
                self._commit_final_segment(channel, text, timestamp)

        elif etype == "transcript_error":
            channel = evt.get("channel", "?")
            self._log(f"Transcription error ({channel}): {evt.get('error')}")

        elif etype == "summary_updated":
            state = evt.get("state", {}) or {}
            if not isinstance(state, dict):
                state = {}
            timestamp = evt.get("timestamp", time.strftime("%H:%M:%S"))
            self._summary_state = state
            self._render_summary_state(state, timestamp)
            self._append_summary_log(state, timestamp, evt.get("segments", 0))
            self._log(
                f"Live summary updated ({evt.get('segments', 0)} segments, {timestamp})"
            )

        elif etype == "summary_error":
            self._log(f"Live summary error: {evt.get('error')}")

        elif etype == "file_entry":
            name = evt.get("name", "")
            size = evt.get("file_size", 0)
            is_dir = evt.get("is_dir", False)
            if name and not is_dir and name not in self.known_files:
                self.known_files.append(name)
                self.file_count_var.set(f"Files (known): {len(self.known_files)}")
                # Auto-select first file if none selected
                if not self.selected_file_var.get():
                    self.selected_file_var.set(name)
            tag = "DIR" if is_dir else "FILE"
            self._log(f"List entry [{tag}] {name} ({size} bytes)")

        elif etype == "file_list_done":
            status_code = evt.get("status", -1)
            status_text = STATUS_TEXT.get(status_code, f"0x{status_code:02X}")
            total = evt.get("total", 0)
            color = "green" if status_code == 0 else "red"
            self._set_status(f"List complete ({total})", color)
            self._log(f"List complete (total={total}, status={status_text}, token={evt.get('token')}")

        elif etype == "file_data_chunk":
            filename = evt.get("filename", "")
            chunk_size = evt.get("chunk_size", 0)
            total_bytes = evt.get("total_bytes", 0)
            remaining = evt.get("remaining", 0)
            eof = evt.get("eof", False)
            seq = evt.get("seq", 0)
            
            self.read_progress_var.set(f"{total_bytes} bytes (rem: {remaining})...")
            status_str = "EOF" if eof else f"seq {seq}"
            self._log(f"Chunk: {chunk_size} bytes (total {total_bytes}, rem {remaining}) [{status_str}]")

        elif etype == "file_read_complete":
            self.pending_read = False
            filename = evt.get("filename", "")
            data = evt.get("data", b"")
            total_bytes = evt.get("total_bytes", 0)
            status_code = evt.get("status", -1)
            status_text = STATUS_TEXT.get(status_code, f"0x{status_code:02X}")
            crc_actual = evt.get("crc_actual")
            crc_expected = evt.get("crc_expected")
            crc_match = None
            if crc_actual is not None and crc_expected is not None:
                crc_match = (crc_actual == crc_expected)
            crc_info = ""
            if crc_actual is not None:
                crc_info = f" crc=0x{crc_actual:08X}"
                if crc_expected is not None:
                    crc_info += f" expected=0x{crc_expected:08X}"
                    if crc_match is False:
                        crc_info += " (mismatch)"
                    elif crc_match is True:
                        crc_info += " (match)"
            
            if status_code == 0 and data:
                # Prompt user to save file
                save_path = filedialog.asksaveasfilename(
                    initialfile=filename,
                    defaultextension=".raw",
                    filetypes=[("Raw Audio", "*.raw"), ("All Files", "*.*")]
                )
                
                if save_path:
                    try:
                        rx_data, tx_data, stats = self._split_interleaved_raw(data)
                        base_path = Path(save_path)
                        suffix = base_path.suffix if base_path.suffix else ".raw"
                        stem = base_path.stem
                        rx_path = base_path.with_name(f"{stem}_rx{suffix}")
                        tx_path = base_path.with_name(f"{stem}_tx{suffix}")

                        rx_path.write_bytes(rx_data)
                        tx_path.write_bytes(tx_data)

                        extra = ""
                        if stats.get("unknown", 0):
                            extra += f", unknown_frames={stats['unknown']}"
                        if stats.get("trailing", 0):
                            extra += f", trailing_bytes={stats['trailing']}"
                        if stats.get("truncated"):
                            extra += ", truncated_frame=1"

                        self._log(
                            f"Split saved: {rx_path} ({len(rx_data)} bytes), "
                            f"{tx_path} ({len(tx_data)} bytes){crc_info}{extra}"
                        )
                        self._set_status("Split saved (RX/TX)", "green")
                        self.read_progress_var.set(f"Saved RX {len(rx_data)} / TX {len(tx_data)} bytes")
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"Failed to save file: {exc}")
                        self._set_status("Save failed", "red")
                        self.read_progress_var.set("")
                else:
                    self._log("File read complete but save cancelled")
                    self.read_progress_var.set("")
            else:
                color = "red" if status_code != 0 else "orange"
                if crc_match is False:
                    color = "red"
                self._set_status(f"Read complete: {status_text}", color)
                self._log(f"File read complete (status={status_text}, {total_bytes} bytes){crc_info}")
                self.read_progress_var.set("")
            
            self._update_buttons()

        elif etype == "file_deleted":
            self.pending_delete = False
            status_code = evt.get("status", -1)
            status_text = STATUS_TEXT.get(status_code, f"0x{status_code:02X}")
            name = evt.get("name", "")

            if status_code == 0:
                if name in self.known_files:
                    self.known_files.remove(name)
                    self.file_count_var.set(f"Files (known): {len(self.known_files)}")
                    if self.selected_file_var.get() == name:
                        self.selected_file_var.set(self.known_files[0] if self.known_files else "")
                self._set_status(f"Deleted {name or 'file'}", "green")
                self._log(f"Delete complete ({name}) token={evt.get('token')}")
            else:
                self._set_status(f"Delete failed: {status_text}", "red")
                self._log(f"Delete failed ({status_text}) token={evt.get('token')} name={name}")

            self._update_buttons()

    @staticmethod
    def _split_interleaved_raw(data: bytes) -> tuple[bytes, bytes, dict]:
        """Split interleaved raw frames into RX/TX payload streams (headers stripped)."""
        mv = memoryview(data)
        idx = 0
        rx = bytearray()
        tx = bytearray()
        frames = 0
        unknown = 0
        trailing = 0
        truncated = False

        while idx + RECORDING_FRAME_HEADER_SIZE <= len(mv):
            channel = mv[idx]
            length = mv[idx + 1] | (mv[idx + 2] << 8)
            idx += RECORDING_FRAME_HEADER_SIZE
            end = idx + length
            if end > len(mv):
                truncated = True
                trailing = len(mv) - idx
                break
            payload = mv[idx:end]
            if channel == CHANNEL_RX:
                rx.extend(payload)
            elif channel == CHANNEL_TX:
                tx.extend(payload)
            else:
                unknown += 1
            idx = end
            frames += 1

        if not truncated:
            trailing = len(mv) - idx

        return bytes(rx), bytes(tx), {
            "frames": frames,
            "unknown": unknown,
            "trailing": trailing,
            "truncated": truncated,
        }

    def _start_live_summary_session(self, reason: str) -> None:
        if self._session_active:
            return
        logs_root = Path("logs")
        logs_root.mkdir(parents=True, exist_ok=True)
        session_name = time.strftime("session_%Y%m%d_%H%M%S")
        session_dir = logs_root / session_name
        suffix = 1
        while session_dir.exists():
            suffix += 1
            session_dir = logs_root / f"{session_name}_{suffix}"
        session_dir.mkdir(parents=True, exist_ok=True)

        self._session_dir = session_dir
        self._transcript_jsonl_path = session_dir / "transcript.jsonl"
        self._summary_jsonl_path = session_dir / "summary.jsonl"
        self._summary_state = self._empty_summary_state()
        self._render_summary_state(self._summary_state, "")
        self.committed_segments = []

        if self._summarizer is None:
            self._summarizer = self._create_summarizer()
        if self._summarizer is not None:
            self._summarizer.reset()
        else:
            self._log("Live summary unavailable: missing summarizer dependency")

        self._session_active = True
        self._append_jsonl(
            self._summary_jsonl_path,
            {
                "type": "session_started",
                "reason": reason,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "summary_interval_sec": self._summary_interval_sec,
            },
        )
        self._log(f"Live summary session started -> {self._session_dir} ({reason})")

    def _end_live_summary_session(self, reason: str) -> None:
        if not self._session_active and self._summarizer is None:
            return

        flush_ok = True
        if self._summarizer is not None:
            flush_ok = self._summarizer.shutdown(flush=True, timeout_sec=20.0)
            self._summary_state = self._summarizer.get_state()
            self._summarizer = None

        self._session_active = False
        self._active_stream_channels.clear()
        self._render_summary_state(self._summary_state, time.strftime("%H:%M:%S"))
        self._append_jsonl(
            self._summary_jsonl_path,
            {
                "type": "session_stopped",
                "reason": reason,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "flush_ok": flush_ok,
            },
        )
        if not flush_ok:
            self._log("Live summary stop: pending segments were not fully flushed")
        self._log(f"Live summary session stopped ({reason})")

    def _maybe_trigger_summary(self) -> None:
        if not self._session_active:
            return
        if self._summarizer is None:
            return
        self._summarizer.maybe_trigger()

    def _commit_final_segment(self, channel: str, text: str, timestamp: str) -> None:
        if not text or channel not in {"rx", "tx"}:
            return
        if not self._session_active:
            self._start_live_summary_session("auto_transcript")

        speaker = "remote" if channel == "rx" else "local"
        segment = {
            "id": uuid.uuid4().hex,
            "ts_wall": datetime.datetime.now().isoformat(timespec="seconds"),
            "ts_mono": round(time.monotonic(), 3),
            "speaker": speaker,
            "channel": channel,
            "text": text,
            "display_time": timestamp,
        }
        self.committed_segments.append(segment)
        self._append_jsonl(self._transcript_jsonl_path, segment)
        if self._summarizer is not None:
            self._summarizer.add_segment(segment)

    def _append_jsonl(self, path: Path | None, payload: dict[str, Any]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to append JSONL ({path}): {exc}")

    def _append_summary_log(self, state: dict[str, Any], timestamp: str, segment_count: int) -> None:
        self._append_jsonl(
            self._summary_jsonl_path,
            {
                "type": "summary_updated",
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "display_time": timestamp,
                "segment_count": segment_count,
                "state": state,
            },
        )

    def _render_summary_state(self, state: dict[str, Any], timestamp: str) -> None:
        running_summary = str(state.get("running_summary", "")).strip()
        bullets = state.get("bullets", [])
        decisions = state.get("decisions", [])
        open_questions = state.get("open_questions", [])
        action_items = state.get("action_items", [])

        lines: list[str] = []
        if timestamp:
            lines.append(f"Updated: {timestamp}")
        if running_summary:
            lines.append("Summary:")
            lines.append(running_summary)
        if isinstance(bullets, list) and bullets:
            lines.append("Key Points:")
            for item in bullets:
                lines.append(f"- {item}")
        if isinstance(decisions, list) and decisions:
            lines.append("Decisions:")
            for item in decisions:
                lines.append(f"- {item}")
        if isinstance(open_questions, list) and open_questions:
            lines.append("Open Questions:")
            for item in open_questions:
                lines.append(f"- {item}")
        if not lines:
            lines.append("Waiting for committed transcript segments...")

        self.summary_box.configure(state=tk.NORMAL)
        self.summary_box.delete("1.0", tk.END)
        self.summary_box.insert(tk.END, "\n".join(lines))
        self.summary_box.configure(state=tk.DISABLED)

        action_lines: list[str] = []
        if isinstance(action_items, list) and action_items:
            for item in action_items:
                if not isinstance(item, dict):
                    continue
                owner = item.get("owner", "unknown")
                status = item.get("status", "open")
                task = item.get("task", "")
                due = item.get("due", "")
                suffix = f" (due: {due})" if due else ""
                action_lines.append(f"- [{owner}/{status}] {task}{suffix}")
        if not action_lines:
            action_lines.append("No action items yet.")

        self.action_items_box.configure(state=tk.NORMAL)
        self.action_items_box.delete("1.0", tk.END)
        self.action_items_box.insert(tk.END, "\n".join(action_lines))
        self.action_items_box.configure(state=tk.DISABLED)

    def _start_simulation_transcripts(self) -> None:
        if self._simulate_worker and self._simulate_worker.is_alive():
            return
        self._simulate_stop.clear()
        self._simulate_worker = threading.Thread(
            target=self._simulate_transcript_loop,
            daemon=True,
        )
        self._simulate_worker.start()

    def _simulate_transcript_loop(self) -> None:
        scripted_lines = [
            ("rx", "Remote: We should ship beta next Friday, but QA still has two blockers."),
            ("tx", "Local: understood, I can prepare a hotfix branch and sync with QA today."),
            ("rx", "Remote: Action item, please update API docs and add regression tests."),
            ("tx", "Local: OK, docs owner is local, tests due tomorrow afternoon."),
            ("rx", "Remote: Budget topic, cloud cost increased by twelve percent this month."),
            ("tx", "Local: We can optimize cache policy and review idle instances this week."),
            ("rx", "Remote: Next meeting agenda should include release risk and rollout plan."),
        ]
        self.events.put({"type": "stream_started", "session_id": 1, "channel": "rx", "path": "simulate://rx"})
        self.events.put({"type": "stream_started", "session_id": 1, "channel": "tx", "path": "simulate://tx"})
        index = 0
        while not self._simulate_stop.is_set():
            channel, text = scripted_lines[index % len(scripted_lines)]
            self.events.put(
                {
                    "type": "transcript_final",
                    "channel": channel,
                    "text": text,
                    "timestamp": time.strftime("%H:%M:%S"),
                }
            )
            index += 1
            if self._simulate_stop.wait(2.5):
                break
        self.events.put({"type": "stream_stopped", "session_id": 1, "channel": "rx", "bytes": 0, "drops": 0, "path": "simulate://rx"})
        self.events.put({"type": "stream_stopped", "session_id": 1, "channel": "tx", "bytes": 0, "drops": 0, "path": "simulate://tx"})

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.config(fg=color)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, line)
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _update_transcript(self, channel: str, text: str, timestamp: str, is_final: bool) -> None:
        if not text:
            return
        now = time.monotonic()
        self._latest_text[channel] = text
        self._latest_timestamp[channel] = timestamp
        self._last_update_time[channel] = now

        if is_final:
            self._render_live_line(channel, text, timestamp)
            self._finalize_live_line(channel)
            # Clear state so _flush_live_lines doesn't re-render
            self._latest_text[channel] = ""
            self._last_render_time[channel] = 0.0
            return

        last_render = self._last_render_time.get(channel, 0.0)
        if now - last_render >= TRANSCRIBE_RENDER_INTERVAL_SEC:
            self._render_live_line(channel, text, timestamp)
            self._last_render_time[channel] = now

    def _render_live_line(self, channel: str, text: str, timestamp: str) -> None:
        """Render the live (partial) transcript in the Label widget.
        
        Uses Label.textvariable for instant updates without ScrolledText complexity.
        """
        if not text:
            return
        # Skip if text unchanged (avoids flicker)
        if text == self._last_render_text.get(channel, ""):
            return

        # Update the live label for this channel
        display_text = f"[{timestamp}] {text}"
        var = self._live_text_vars.get(channel)
        if var:
            var.set(display_text)
        
        self._last_render_text[channel] = text

    def _finalize_live_line(self, channel: str) -> None:
        """Finalize the live line - append to history and clear the live label."""
        text = self._last_render_text.get(channel, "")
        timestamp = self._latest_timestamp.get(channel, time.strftime("%H:%M:%S"))
        
        if text:
            # Append to transcript history (ScrolledText)
            label = channel.upper() if channel else "UNK"
            line = f"[{timestamp}] {label}: {text}\n"
            self.transcript_box.configure(state=tk.NORMAL)
            self.transcript_box.insert(tk.END, line)
            self.transcript_box.see(tk.END)
            self.transcript_box.configure(state=tk.DISABLED)
        
        # Clear the live label
        var = self._live_text_vars.get(channel)
        if var:
            var.set("")
        
        # Clear state for next sentence
        self._last_render_text[channel] = ""

    def _flush_live_lines(self) -> None:
        now = time.monotonic()
        for channel, text in self._latest_text.items():
            if not text:
                continue
            last_update = self._last_update_time.get(channel, 0.0)
            if now - last_update < TRANSCRIBE_STABLE_WINDOW_SEC:
                continue
            self._render_live_line(channel, text, time.strftime("%H:%M:%S"))
            self._finalize_live_line(channel)
            self._latest_text[channel] = ""

    def _update_buttons(self) -> None:
        if not self.protocol.connected:
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_stream.config(state=tk.DISABLED)
            self.btn_transcribe.config(state=tk.DISABLED)
            self.btn_list.config(state=tk.DISABLED)
            self.btn_read.config(state=tk.DISABLED)
            self.btn_delete.config(state=tk.DISABLED)
            self.btn_close_file.config(state=tk.DISABLED)
            return
        start_disabled = self.recording or self.pending_start or self.pending_stop or self.pending_read or self.pending_delete
        self.btn_start.config(state=tk.DISABLED if start_disabled else tk.NORMAL)
        stop_enabled = (self.recording or self.pending_start) and not self.pending_stop and not self.pending_delete
        self.btn_stop.config(state=tk.NORMAL if stop_enabled else tk.DISABLED)
        self.btn_stream.config(state=tk.NORMAL)
        self.btn_transcribe.config(state=tk.NORMAL)
        self.btn_list.config(state=tk.NORMAL if not self.pending_read and not self.pending_delete else tk.DISABLED)
        # Allow file read when not recording and not already reading
        read_enabled = (
            not self.recording
            and not self.pending_start
            and not self.pending_stop
            and not self.pending_read
            and not self.pending_delete
        )
        self.btn_read.config(state=tk.NORMAL if read_enabled else tk.DISABLED)
        self.btn_delete.config(state=tk.NORMAL if read_enabled else tk.DISABLED)
        self.btn_close_file.config(state=tk.NORMAL if not self.pending_delete else tk.DISABLED)

    def on_closing(self) -> None:
        if self.simulate_transcript:
            self._simulate_stop.set()
            if self._simulate_worker:
                self._simulate_worker.join(timeout=1.0)
        if self.transcription_enabled:
            self.transcriber.disable()
        self._end_live_summary_session("app_closing")
        self.protocol.close()
        self.root.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="USB dongle recorder tester")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Generate simulated transcript_final events without a device",
    )
    cli_args = parser.parse_args()
    simulate_mode = cli_args.simulate or env_flag("SIMULATE_TRANSCRIPT", default=False)
    root = tk.Tk()
    app = RecorderTester(root, simulate_transcript=simulate_mode)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
