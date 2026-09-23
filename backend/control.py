from __future__ import annotations
import json
import os
import queue
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from .config import settings

class ControlError(Exception):
    pass

@dataclass
class Ticket:
    id: str
    operation: str
    payload: dict
    synchronous: bool
    expires_at: float = field(default_factory=lambda: time.time() + 20)
    event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None

QUEUE: queue.Queue[Ticket] = queue.Queue()
TICKETS: dict[str, Ticket] = {}
LOCK = threading.RLock()
SYNC = {"status", "preview", "open_repository", "open_native_obsidian"}

def bridge_next():
    try: ticket = QUEUE.get(timeout=1)
    except queue.Empty: return None
    return {"id": ticket.id, "operation": ticket.operation, "payload": ticket.payload, "synchronous": ticket.synchronous, "expires_at": ticket.expires_at}

def bridge_result(ticket_id: str, result: dict):
    with LOCK:
        ticket = TICKETS.get(ticket_id)
        if not ticket or not ticket.synchronous: raise ControlError("Unknown bridge ticket")
        ticket.result = result
        ticket.event.set()

def _bridge_request(operation: str, payload: dict):
    if operation == "operation":
        op_id = payload.get("operation_id")
        if not isinstance(op_id, str) or len(op_id) != 16 or any(c not in "0123456789abcdef" for c in op_id):
            raise ControlError("Invalid operation ID")
        try:
            result = json.loads((settings.runtime / "operations" / (op_id + ".json")).read_text())
            if result.get("status") in ("complete", "failed"):
                with LOCK:
                    TICKETS.pop(op_id, None)
            return result
        except FileNotFoundError:
            with LOCK:
                ticket = TICKETS.get(op_id)
                if ticket and time.time() <= ticket.expires_at:
                    return {"id": op_id, "status": "running", "events": [{"step": "Waiting for host agent", "status": "running"}]}
                if ticket:
                    TICKETS.pop(op_id, None)
                    return {"id": op_id, "status": "failed", "events": [{"step": "Host agent did not respond", "status": "failed", "detail": "Restart the host agent and try again"}]}
            return None
    ticket = Ticket(secrets.token_hex(8), operation, payload, operation in SYNC)
    with LOCK: TICKETS[ticket.id] = ticket
    QUEUE.put(ticket)
    if not ticket.synchronous: return {"operation_id": ticket.id}
    if not ticket.event.wait(20):
        with LOCK: TICKETS.pop(ticket.id, None)
        raise ControlError("Host agent did not respond")
    with LOCK: TICKETS.pop(ticket.id, None)
    result = ticket.result or {"ok": False, "error": "Empty host-agent result"}
    if not result.get("ok"): raise ControlError(result.get("error", "Host operation failed"))
    return result.get("data")

def _socket_request(operation: str, payload: dict):
    try:
        token = settings.agent_token.read_text().strip()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(15)
            channel.connect(str(settings.socket))
            channel.sendall((json.dumps({"token": token, "operation": operation, "payload": payload}) + "\n").encode())
            result = bytearray()
            while not result.endswith(b"\n"):
                chunk = channel.recv(65536)
                if not chunk or len(result) > 4_000_000: raise ControlError("Host agent returned an incomplete response")
                result.extend(chunk)
        response = json.loads(result)
        if not response.get("ok"): raise ControlError(response.get("error", "Host operation failed"))
        return response.get("data")
    except (OSError, ValueError) as exc: raise ControlError(f"Host agent unavailable: {exc}") from exc

def request(operation: str, **payload):
    if os.environ.get("HUB_CONTROL_TRANSPORT", "socket") == "bridge": return _bridge_request(operation, payload)
    return _socket_request(operation, payload)
