from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = Path(os.environ.get("STUDY_PLANNER_DATA", BASE_DIR / "planner_data.json"))
HOST = "127.0.0.1"
PORT = 8000
COLORS = ("green", "orange", "blue", "yellow")
TOPICS = ("Learn & summarize", "Active recall", "Practice questions")


class PlannerError(ValueError):
    pass


def generate_plan(hours: float, subjects: list[dict[str, str]], today: date | None = None) -> list[dict[str, Any]]:
    """Allocate study sessions up to each subject's exam date."""
    today = today or date.today()
    active = [
        {**subject, "last_studied": -1, "color": COLORS[index % len(COLORS)]}
        for index, subject in enumerate(subjects)
        if date.fromisoformat(subject["exam"]) >= today
    ]
    if not active:
        return []

    capacity = round(hours * 60)
    final_day = max(date.fromisoformat(subject["exam"]) for subject in active)
    sessions: list[dict[str, Any]] = []
    current_day = today
    day_index = 0

    while current_day <= final_day:
        remaining = capacity
        start_minute = 9 * 60
        eligible = sorted(
            (subject for subject in active if date.fromisoformat(subject["exam"]) >= current_day),
            key=lambda subject: (subject["exam"], subject["last_studied"], subject["name"].casefold()),
        )

        for subject in eligible:
            if remaining < 25:
                break

            duration = min(45, remaining)
            end_minute = start_minute + duration
            session_date = current_day.isoformat()
            sessions.append(
                {
                    "id": f"{session_date}-{subject['name']}",
                    "date": session_date,
                    "subject": subject["name"],
                    "exam": subject["exam"],
                    "minutes": duration,
                    "start": f"{start_minute // 60:02d}:{start_minute % 60:02d}",
                    "end": f"{end_minute // 60:02d}:{end_minute % 60:02d}",
                    "topic": TOPICS[day_index % len(TOPICS)],
                    "color": subject["color"],
                }
            )
            subject["last_studied"] = day_index
            remaining -= duration
            start_minute = end_minute + 15

        current_day += timedelta(days=1)
        day_index += 1

    return sessions


def validate_plan(payload: Any) -> tuple[float, list[dict[str, str]]]:
    if not isinstance(payload, dict):
        raise PlannerError("Request body must be a JSON object.")

    hours = payload.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.5 <= hours <= 12:
        raise PlannerError("Daily study hours must be between 0.5 and 12.")

    raw_subjects = payload.get("subjects")
    if not isinstance(raw_subjects, list) or not 1 <= len(raw_subjects) <= 12:
        raise PlannerError("Add between 1 and 12 subjects.")

    subjects: list[dict[str, str]] = []
    names: set[str] = set()
    today = date.today()
    for subject in raw_subjects:
        if not isinstance(subject, dict):
            raise PlannerError("Each subject needs a name and exam date.")

        name = subject.get("name")
        exam = subject.get("exam")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            raise PlannerError("Subject names must be between 1 and 60 characters.")
        normalized_name = name.strip()
        if normalized_name.casefold() in names:
            raise PlannerError(f"{normalized_name} is listed more than once.")
        names.add(normalized_name.casefold())

        if not isinstance(exam, str):
            raise PlannerError(f"Choose an exam date for {normalized_name}.")
        try:
            exam_date = date.fromisoformat(exam)
        except ValueError as error:
            raise PlannerError(f"{normalized_name} needs a valid exam date.") from error
        if exam_date < today or exam_date > today + timedelta(days=365):
            raise PlannerError(f"{normalized_name}'s exam date must be within the next year.")

        subjects.append({"name": normalized_name, "exam": exam_date.isoformat()})

    return float(hours), subjects


def load_state() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"hours": 2, "subjects": [], "plan": [], "completed": []}
    try:
        state = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not read planner_data.json.") from error
    if not isinstance(state, dict):
        raise RuntimeError("Planner data must contain a JSON object.")
    return state


def save_state(state: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    temporary_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary_file.replace(DATA_FILE)


class PlannerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise PlannerError("Content-Length must be a number.") from error
        if length < 1 or length > 64_000:
            raise PlannerError("Request body must be between 1 byte and 64 KB.")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise PlannerError("Request body must be valid JSON.") from error

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.send_json(200, {"status": "ok"})
            return
        if self.path == "/api/plan":
            try:
                self.send_json(200, load_state())
            except RuntimeError as error:
                self.send_json(500, {"error": str(error)})
            return
        if self.path.startswith("/api/"):
            self.send_json(404, {"error": "API endpoint not found."})
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            state = load_state()
            if self.path == "/api/plan":
                hours, subjects = validate_plan(payload)
                state = {
                    "hours": hours,
                    "subjects": subjects,
                    "plan": generate_plan(hours, subjects),
                    "completed": [],
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                save_state(state)
                self.send_json(201, state)
                return

            if self.path == "/api/progress":
                completed = payload.get("completed") if isinstance(payload, dict) else None
                valid_ids = {session["id"] for session in state.get("plan", [])}
                if not isinstance(completed, list) or any(
                    not isinstance(item, str) or item not in valid_ids for item in completed
                ):
                    raise PlannerError("Completed sessions must belong to the current plan.")
                state["completed"] = sorted(set(completed))
                save_state(state)
                self.send_json(200, state)
                return

            self.send_json(404, {"error": "API endpoint not found."})
        except PlannerError as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(500, {"error": str(error)})


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), PlannerHandler)
    print(f"Study Planner API running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStudy Planner API stopped.")
    finally:
        server.server_close()