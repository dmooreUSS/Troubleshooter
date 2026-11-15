"""Database-driven network troubleshooter.

Required tables/columns are checked at startup; schema version numbers are
not enforced. Rule advice uses only explicitly accepted facts. BGE meanings
remain tentative. Case updates append notes without revising session evidence.
"""

import ipaddress
import math
from contextlib import closing
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
try:
    from agent_interpreter import interpret_phrasing
except ImportError as exc:
    print(f"Optional phrasing interpreter unavailable: {exc}")

    def interpret_phrasing(response, available_types, existing_type_ids=()):
        return []


DATABASE_FILENAME = "securecrt_commands_ML.db"

REQUIRED_COLUMNS = {
    "workflows": {"workflow_id", "workflow_name", "description"},
    "workflow_steps": {
        "workflow_step_id", "workflow_id", "step_number", "device_type_id",
        "command_id", "explanation", "step_type", "prompt",
        "yes_next_step_id", "no_next_step_id", "next_step_id",
    },
    "commands": {"command_id", "description", "command", "use"},
    "device_types": {"device_type_id", "device_name"},
    "input_types": {"input_type_id", "input_name", "prompt", "data_type"},
    "workflow_inputs": {"workflow_id", "input_type_id"},
    "troubleshooting_sessions": {
        "session_id", "workflow_id", "ticket_number", "started_at",
        "completed_at", "status", "summary_text",
    },
    "troubleshooting_session_inputs": {
        "session_input_id", "session_id", "input_name", "input_value", "data_type",
    },
    "troubleshooting_observations": {
        "observation_id", "session_id", "workflow_step_id", "prompt",
        "response", "recorded_at",
    },
    "troubleshooting_findings": {
        "finding_id", "observation_id", "observation_type_id",
        "value_text", "value_number", "unit", "confidence",
    },
    "observation_types": {
        "observation_type_id", "observation_key", "display_name",
        "data_type", "default_unit", "category", "active",
    },
    "rules": {
        "rule_id", "rule_code", "title", "priority", "suggestion_text",
        "workflow_id", "enabled",
    },
    "rule_conditions": {
        "condition_id", "rule_id", "workflow_step_id", "observation_type_id", "operator",
        "expected_number", "expected_text",
    },
    "rule_matches": {
        "match_id", "session_id", "rule_id", "finding_id",
        "suggestion_text", "matched_at",
    },
    "historical_cases": {
        "case_number", "subject", "description", "circuit_id",
        "category", "component",
    },
    "historical_messages": {"feed_id", "case_number", "clean_text"},
    "historical_observations": {
        "feed_id", "observation_type_id", "value_text", "value_number",
    },
    "baseline_cases": {
        "case_number", "subject", "circuit_id", "technology_type",
        "network_conclusion", "alternate_fault_domain", "final_resolution",
    },
    "baseline_findings": {"case_number", "observation_type_id", "value_text", "value_number"},
}

PLACEHOLDER_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")


def read_finding(message):
    """Read a typed answer or a pasted multiline command output as one finding."""
    try:
        from prompt_toolkit import prompt as terminal_prompt
    except ImportError:
        return input(message)
    return terminal_prompt(message)


def application_directory():
    """Return the executable directory, or this source script's directory."""
    return (Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)


def normalize_newlines(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def timestamp_now():
    """Record local time with its UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def find_database():
    """Find the existing database beside the program."""
    path = application_directory() / DATABASE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"Database not found beside the program: {path}. "
            "Keep the program and database together."
        )
    return path


def validate_database_schema(connection):
    """Check required tables and columns, without a numbered version gate."""
    problems = []
    for table_name, required in REQUIRED_COLUMNS.items():
        columns = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        if not columns:
            problems.append(f"missing table: {table_name}")
            continue
        missing = required - {row["name"] for row in columns}
        if missing:
            problems.append(f"{table_name} missing: {', '.join(sorted(missing))}")
    if problems:
        raise RuntimeError("Incompatible database structure:\n- " + "\n- ".join(problems))





def connect_database(database_path):
    """Open an existing database and validate/setup startup in one transaction."""
    connection = sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=rw", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        validate_database_schema(connection)
        with connection:
            # Obtain a write reservation; the actual setup writes below may
            # still report filesystem or journal errors. Roll back on failure.
            connection.execute("BEGIN IMMEDIATE")
            ensure_interpretation_types(connection)
            ensure_case_updates(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Database contains invalid foreign-key references.")
            workflows = connection.execute(
                "SELECT workflow_id, workflow_name FROM workflows ORDER BY workflow_id"
            ).fetchall()
            if len(workflows) != 7:
                raise RuntimeError(f"Expected seven workflows; found {len(workflows)}.")
            for workflow in workflows:
                try:
                    validate_workflow(
                        load_workflow_steps(connection, workflow["workflow_id"]),
                        load_workflow_inputs(connection, workflow["workflow_id"]),
                    )
                except RuntimeError as exc:
                    raise RuntimeError(f"Workflow {workflow['workflow_name']} is invalid: {exc}") from exc
            load_rules(connection)
    except BaseException:
        connection.close()
        raise
    return connection



def ensure_case_updates(connection):
    """Add the append-only update table to a compatible agent database."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS troubleshooting_case_updates (
               update_id INTEGER PRIMARY KEY,
               session_id INTEGER NOT NULL REFERENCES troubleshooting_sessions(session_id),
               update_text TEXT NOT NULL,
               recorded_at TEXT NOT NULL
           )"""
    )
    columns = {
        row["name"] for row in connection.execute(
            "PRAGMA table_info(troubleshooting_case_updates)"
        )
    }
    missing = {"update_id", "session_id", "update_text", "recorded_at"} - columns
    if missing:
        raise RuntimeError(
            "Incompatible database schema at startup: "
            "troubleshooting_case_updates missing " + ", ".join(sorted(missing))
        )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS case_updates_session_idx
           ON troubleshooting_case_updates(session_id, update_id)"""
    )



def ensure_interpretation_types(connection):
    """Add vocabulary for interface last-traffic ages while preserving existing definitions."""
    connection.executemany(
        """INSERT OR IGNORE INTO observation_types
           (observation_key, display_name, data_type, default_unit, category, active)
           VALUES (?, ?, 'number', 'minutes', 'traffic', 1)""",
        (("last_input_age_minutes", "Time Since Last Input"),
         ("last_output_age_minutes", "Time Since Last Output")),
    )



def normalize_name(value):
    """Normalize input and placeholder names for matching."""
    return "".join(
        character.lower()
        for character in value
        if character.isalnum()
    )


def load_rules(connection):
    """Load enabled rules and validate each condition at startup."""
    rows = connection.execute(
        """SELECT r.rule_id, r.rule_code, r.title, r.priority,
                  r.suggestion_text, r.workflow_id,
                  c.condition_id, c.workflow_step_id, c.observation_type_id, c.operator,
                  c.expected_number, c.expected_text,
                  t.data_type, t.active
           FROM rules r
           LEFT JOIN rule_conditions c ON c.rule_id = r.rule_id
           LEFT JOIN observation_types t
             ON t.observation_type_id = c.observation_type_id
           WHERE r.enabled = 1
           ORDER BY r.priority, r.rule_id, c.condition_id"""
    ).fetchall()

    numeric_operators = {"eq", "gt", "gte", "lt", "lte"}
    text_operators = {"eq", "contains"}

    for rule in rows:
        operator = rule["operator"]
        kind = rule["data_type"]
        expected_number = rule["expected_number"]
        expected_text = rule["expected_text"]

        valid_numeric = (
            kind in ("number", "integer")
            and operator in numeric_operators
            and isinstance(expected_number, (int, float))
            and math.isfinite(expected_number)
            and expected_text is None
        )

        valid_text = (
            kind == "text"
            and operator in text_operators
            and isinstance(expected_text, str)
            and bool(expected_text.strip())
            and expected_number is None
        )

        valid = (
            rule["active"] == 1
            and (valid_numeric or valid_text)
        )

        if not valid:
            raise RuntimeError(
                f"Rule {rule['rule_code']}, "
                f"condition {rule['condition_id']}: invalid condition "
                f"(operator={operator!r}, data_type={kind!r}, "
                f"expected_number={expected_number!r}, "
                f"expected_text={expected_text!r}, "
                f"active={rule['active']!r})."
            )

    for rule in rows:
        step = connection.execute(
            "SELECT workflow_id FROM workflow_steps WHERE workflow_step_id = ?",
            (rule["workflow_step_id"],),
        ).fetchone()
        if step is None or (rule["workflow_id"] is not None
                            and step["workflow_id"] != rule["workflow_id"]):
            raise RuntimeError(
                f"Rule {rule['rule_code']}, condition {rule['condition_id']}: "
                "workflow step is missing or belongs to another workflow."
            )
    return rows



def rule_applies(rule, fact):
    """Evaluate a condition against a fact from the required step."""
    if rule["workflow_step_id"] != fact["workflow_step_id"]:
        return False

    if rule["observation_type_id"] != fact["observation_type_id"]:
        return False

    operator = rule["operator"]
    kind = rule["data_type"]


    if kind in ("number", "integer"):
        actual = fact["value_number"]
        expected = rule["expected_number"]

        if actual is None or expected is None:
            return False

        if operator == "eq":
            return actual == expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        return False

    if kind == "text":
        actual = fact["value_text"]
        expected = rule["expected_text"]

        if actual is None or expected is None:
            return False

        if operator == "eq":
            return actual.casefold() == expected.casefold()
        if operator == "contains":
            return expected.casefold() in actual.casefold()

    return False


def _evaluate_rules(connection, session_id, workflow_id):
    """Evaluate and refresh matches inside the caller's transaction."""
    rules = load_rules(connection)
    rows = connection.execute(
        """SELECT f.finding_id, f.observation_type_id, f.value_text,
                  f.value_number, f.unit, o.observation_id, o.workflow_step_id,
                  t.observation_key, t.display_name
           FROM troubleshooting_findings f
           JOIN troubleshooting_observations o USING (observation_id)
           JOIN observation_types t USING (observation_type_id)
           WHERE o.session_id = ? AND f.confidence = 1.0 AND t.active = 1
           ORDER BY o.observation_id DESC, f.finding_id DESC""",
        (session_id,),
    ).fetchall()
    latest = {}
    for fact in rows:
        subject = None
        if (fact["observation_key"] == "interface_state"
                and fact["value_text"] and "=" in fact["value_text"]):
            subject = fact["value_text"].split("=", 1)[0].strip().casefold()
        latest.setdefault((fact["workflow_step_id"], fact["observation_type_id"], subject), fact)

    grouped = {}
    for condition in rules:
        if condition["workflow_id"] in (None, workflow_id):
            grouped.setdefault(condition["rule_id"], []).append(condition)
    desired = {}
    for rule_id, conditions in grouped.items():
        evidence = []
        for condition in conditions:
            fact = next((fact for fact in latest.values()
                         if rule_applies(condition, fact)), None)
            if fact is None:
                break
            evidence.append(fact)
        else:
            rule = dict(conditions[0])
            # Keep the public (rule, representative_fact) return contract.
            # Full supporting evidence is available for display, without
            # changing the database's one-finding-per-match structure.
            rule["evidence"] = evidence
            desired[rule_id] = (rule, evidence[0])

    # Synchronize stale/duplicate rows and retain unchanged match timestamps.
    kept = set()
    for previous in connection.execute(
        "SELECT match_id, rule_id, finding_id, suggestion_text FROM rule_matches WHERE session_id = ?",
        (session_id,),
    ).fetchall():
        selected = desired.get(previous["rule_id"])
        unchanged = (selected is not None
                     and previous["finding_id"] == selected[1]["finding_id"]
                     and previous["suggestion_text"] == selected[0]["suggestion_text"]
                     and previous["rule_id"] not in kept)
        if unchanged:
            kept.add(previous["rule_id"])
        else:
            connection.execute("DELETE FROM rule_matches WHERE match_id = ?", (previous["match_id"],))
    for rule_id, (rule, fact) in desired.items():
        if rule_id not in kept:
            connection.execute(
                """INSERT INTO rule_matches
                   (session_id, rule_id, finding_id, suggestion_text, matched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, rule_id, fact["finding_id"], rule["suggestion_text"], timestamp_now()),
            )
    return desired


def evaluate_rules(connection, session_id, workflow_id):
    """Refresh rule matches atomically from confirmed evidence."""
    with connection:
        return _evaluate_rules(connection, session_id, workflow_id)


def format_fact(fact):
    if fact["value_number"] is not None:
        value = f"{fact['value_number']:g} {fact['unit'] or ''}".strip()
        return f"{fact['value_text']}: {value}" if fact["value_text"] else value
    return fact["value_text"] or ""


def show_rule_matches(matches, previously_shown):
    """Show all supporting facts, changes, and withdrawals."""
    for rule_id in previously_shown.keys() - matches.keys():
        print(f"\nSuggested check withdrawn (rule {rule_id}): current evidence no longer meets all conditions.")
    signatures = {}
    for rule_id, (rule, representative) in matches.items():
        evidence = rule.get("evidence", [representative])
        signature = (
            rule["suggestion_text"],
            tuple((fact["finding_id"], fact["workflow_step_id"],
                   fact["value_text"], fact["value_number"], fact["unit"])
                  for fact in evidence),
        )
        signatures[rule_id] = signature
        if previously_shown.get(rule_id) == signature:
            continue
        print(f"\nSuggested check: {rule['title']}")
        for fact in evidence:
            print(f"Evidence (step ID {fact['workflow_step_id']}): {fact['display_name']} = {format_fact(fact)}")
        print(rule["suggestion_text"])
    return signatures



def circuit_ids(text):
    """Extract service identifiers from explicit IDs and case prose."""
    found = re.findall(
        r"(?<![A-Za-z0-9])(?:USS[0-9]{5,}|A[A-Za-z0-9]{4,7})(?![A-Za-z0-9])",
        text or "", re.I,
    )
    return {item.upper() for item in found
            if item.upper().startswith("USS") or any(ch.isdigit() for ch in item)}


def parse_observations(response, available_types, prompt="", input_values=None):
    """Extract explicit measurements from an agent response; keep raw text."""
    if not response or response.startswith("[Skipped]"):
        return []
    candidates = []
    seen = set()

    def add(key, value, unit=None, subject=None):
        definition = available_types.get(key)
        if definition is None or not definition["active"]:
            return
        kind = definition["data_type"]
        if kind == "text":
            if not isinstance(value, str) or not value.strip():
                return
            number, text_value = None, value.strip()
        elif kind in ("integer", "number"):
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return
            if not 0 <= number <= 1e15 and key not in ("rx_power", "tx_power"):
                return
            if kind == "integer" and not number.is_integer():
                return
            if key == "packet_loss_percent" and number > 100:
                return
            if key in ("rx_power", "tx_power") and not -100 <= number <= 50:
                return
            text_value = subject
            unit = unit or definition["default_unit"]
            if unit != definition["default_unit"]:
                return
        else:
            return
        identity = (key, text_value, number, unit)
        if identity in seen:
            return
        seen.add(identity)
        candidates.append({
            "observation_type_id": definition["observation_type_id"],
            "display_name": definition["display_name"],
            "value_text": text_value,
            "value_number": number,
            "unit": unit,
        })

    for match in re.finditer(
        r"\b(?:packet\s+loss|loss)\s*(?:rate\s*)?(?:is|:|=)?\s*(\d+(?:\.\d+)?)\s*(?:%|percent)(?!\w)",
        response, re.I,
    ):
        add("packet_loss_percent", match.group(1))
    for match in re.finditer(
        r"\bsuccess\s+rate\s*(?:is|:|=)\s*(\d+(?:\.\d+)?)\s*(?:%|percent)(?!\w)",
        response, re.I,
    ):
        success = float(match.group(1))
        if 0 <= success <= 100:
            add("packet_loss_percent", 100 - success)
            if success in (0, 100):
                add("reachability", "unreachable" if success == 0 else "reachable")
    for match in re.finditer(
        r"\b(?:round.trip\s+)?min/avg/max\s*(?:=|:)\s*"
        r"\d+(?:\.\d+)?\s*/\s*(\d+(?:\.\d+)?)\s*/\s*"
        r"\d+(?:\.\d+)?\s*ms\b|\b(?:average|avg)\s+"
        r"(?:latency|rtt)\s*(?:is|:|=)?\s*(\d+(?:\.\d+)?)\s*ms\b",
        response, re.I,
    ):
        add("latency_ms", next(value for value in match.groups() if value))
    for key, label in (
        ("input_errors", "input errors"), ("output_errors", "output errors"),
        ("crc_errors", "crc"), ("input_drops", "input drops"),
        ("output_drops", "output drops"),
        ("carrier_transitions", "carrier transitions"),
    ):
        escaped = re.escape(label)
        for match in re.finditer(
            rf"\b(\d[\d,]*)\s+(?:total\s+)?{escaped}\b|"
            rf"\b{escaped}\s*(?:is|:|=)\s*(\d[\d,]*)\b",
            response, re.I,
        ):
            add(key, next(value for value in match.groups() if value).replace(",", ""))
    for direction in ("input", "output"):
        for match in re.finditer(
            rf"\b{direction}\s+rate\s*(?:is|:|=)?\s*"
            r"(\d+(?:\.\d+)?)\s*(bits/sec|bps|kbps|mbps|gbps)\b",
            response, re.I,
        ):
            multiplier = {"bits/sec": 1, "bps": 1, "kbps": 1e3,
                          "mbps": 1e6, "gbps": 1e9}[match.group(2).lower()]
            add(f"{direction}_rate", float(match.group(1)) * multiplier)
    for direction in ("rx", "tx"):
        for match in re.finditer(
            rf"\b{direction}\s+(?:optical\s+)?(?:power|level)\s*"
            r"(?:is|:|=)?\s*(-?\d+(?:\.\d+)?)\s*dBm\b",
            response, re.I,
        ):
            add(f"{direction}_power", match.group(1))
    for match in re.finditer(
        r"\b(?:interface\s+)?([\w./:-]*\d[\w./:-]*)\s+is\s+"
        r"(administratively down|up|down),?\s*line protocol is\s+"
        r"(administratively down|up|down)\b",
        response, re.I,
    ):
        def state(value):
            return "admin-down" if value.lower() == "administratively down" else value.lower()
        add("interface_state", f"{match.group(1)}={state(match.group(2))}/{state(match.group(3))}")
    for match in re.finditer(
        r"\b([A-Za-z][\w./:-]*\d[\w./:-]*)\s+(?:is\s+)?"
        r"(administratively down|admin down|up|down)\s*[/, ]+\s*"
        r"(up|down)\b", response, re.I,
    ):
        first = match.group(2).lower().replace("administratively down", "admin-down")
        first = first.replace("admin down", "admin-down")
        add("interface_state", f"{match.group(1)}={first}/{match.group(3).lower()}")
    age_matches = list(re.finditer(
        r"\b(?:last\s+)?(input|output)\s*(?:is|was|says|:|=)?\s*"
        r"(\d+w\d+d|\d{1,2}:\d{2}:\d{2})\b", response, re.I,
    ))
    for match in age_matches:
        direction, value = match.groups()

        if ":" in value:
            hours, minutes, seconds = map(int, value.split(":"))

            if minutes >= 60 or seconds >= 60:
                continue

            age_minutes = hours * 60 + minutes + seconds / 60

        else:
            duration = re.fullmatch(r"(\d+)w(\d+)d", value, re.I)
            if duration is None:
                continue

            weeks, days = map(int, duration.groups())
            age_minutes = (weeks * 7 + days) * 24 * 60

        subject = find_input_by_name(input_values or {}, "BVI")

        add(
            f"last_{direction.lower()}_age_minutes",
            age_minutes,
            subject=subject,
        )
    for match in re.finditer(
        r"\bBGP\s+(?:neighbor|peer|session)\s+(?:\d{1,3}(?:\.\d{1,3}){3}\s+)?"
        r"(?:is|:|=|returned)\s*(established|up|down|idle|active)\b",
        response, re.I,
    ):
        add("bgp_state", match.group(1).lower())
    for match in re.finditer(
        r"\bBGP\s+(established|up|down|idle)\b", response, re.I,
    ):
        add("bgp_state", match.group(1).lower())
    for match in re.finditer(
        r"\bSNMP\s+(?:is\s+)?(reachable|unreachable)\b", response, re.I,
    ):
        add("snmp_reachability", match.group(1).lower())
    for match in re.finditer(
        r"\balarm\s+(?:is\s+)?(active|cleared)\b", response, re.I,
    ):
        add("alarm_state", match.group(1).lower())
    for match in re.finditer(
        r"(?m)^\s*Internet\s+(\d{1,3}(?:\.\d{1,3}){3})\s+"
        r"(\d+|-)\s+(Incomplete|[0-9a-f.:-]{12,})\b",
        response, re.I,
    ):
        address, age, mac = match.groups()
        add("arp_state", f"{address}=" + (
            "incomplete" if mac.lower() == "incomplete" else "present"
        ))
        if age != "-" and mac.lower() != "incomplete":
            add("arp_age", age, subject=address)
    for match in re.finditer(
        r"\binterface\s+([\w./:-]+)\s+(?:has\s+)?flapped\b",
        response, re.I,
    ):
        add("interface_flap", f"{match.group(1)}=flapped")
    for line in response.splitlines():
        if re.search(r"\b(?:ALARM_MAJOR|ALARM_CRITICAL|ALARM_MINOR)\b", line, re.I):
            add("alarm_text", line.strip()[:500])
    return candidates


def review_observations(response, available_types, prompt="", input_values=None):
    """Allow the agent to accept, reject, or edit an extracted response."""
    while True:
        candidates = parse_observations(response, available_types, prompt, input_values)
        if not candidates:
            return response, []
        print("\nFacts detected in your response:")
        for candidate in candidates:
            if candidate["value_number"] is not None:
                value = (
                    f"{candidate['value_number']:g} "
                    f"{candidate['unit'] or ''}"
                ).strip()

                if candidate["value_text"]:
                    value = f"{candidate['value_text']}: {value}"
            else:
                value = candidate["value_text"]
            print(f"  {candidate['display_name']}: {value}")
        action = input("Save these facts? [Y]es / [N]o / [E]dit response (Enter=Yes): ").strip().lower()
        if action in ("", "y", "yes"):
            for candidate in candidates:
                candidate["confidence"] = 1.0
            return response, candidates
        if action in ("n", "no"):
            return response, []
        if action in ("e", "edit"):
            response = read_finding("Enter corrected response: ").strip()
        else:
            print("Enter Y, N, or E.")


def choose_workflow(connection):
    """Display configured workflows and let the user choose one."""
    workflows = connection.execute(
        """
        SELECT
            workflow_id,
            workflow_name,
            description
        FROM workflows
        ORDER BY workflow_name;
        """
    ).fetchall()

    if not workflows:
        raise RuntimeError(
            "No workflows are configured in the database."
        )

    while True:
        print("\nSelect A Workflow")
        print("-----------------")
        for index, workflow in enumerate(workflows, start=1):
            print(f"{index}. {workflow['workflow_name']}")
            if workflow["description"]:
                print(f"   {normalize_newlines(workflow['description'])}")
        print("H. Help me choose a workflow")
        print("U. Update case")
        print("0. Exit troubleshooter")

        choice = input("\nSelect a workflow, U to update case, H for guidance, or 0 to exit: ").strip()

        if choice.casefold() in ("u", "update"):
            return "update_case"

        if choice.casefold() in ("h", "help", "?"):
            guided = guide_workflow(workflows)
            if guided is not None:
                return guided
            continue

        if choice in ("0", "q", "Q"):
            return None

        if choice.isdigit():
            selected = int(choice)

            if 1 <= selected <= len(workflows):
                return workflows[selected - 1]

        print("Please enter a workflow number, U to update case, H for guidance, or 0 to exit.")


def choose_case_to_update(connection):
    """Find a previously worked ticket and select the session to update."""
    while True:
        ticket_number = input("\nCase/ticket number to update (blank=menu): ").strip()
        if not ticket_number:
            return None
        sessions = connection.execute(
            """SELECT s.session_id, s.ticket_number, s.workflow_id,
                      s.started_at, s.status, s.summary_text,
                      w.workflow_name, w.description
               FROM troubleshooting_sessions AS s
               JOIN workflows AS w ON w.workflow_id = s.workflow_id
               WHERE s.ticket_number = ? COLLATE NOCASE
               ORDER BY s.started_at DESC, s.session_id DESC""",
            (ticket_number,),
        ).fetchall()
        if sessions:
            break
        print("No previous troubleshooting sessions found for that case number.")

    print(f"\nSaved work for case {sessions[0]['ticket_number']}")
    print("----------------------------------------")
    for index, session in enumerate(sessions, 1):
        print(f"{index}. Session {session['session_id']} | "
              f"{session['workflow_name']} | {session['status']} | "
              f"{session['started_at']}")
    while True:
        choice = input("Select a session to update from (0=menu): ").strip()
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            selected = sessions[int(choice) - 1]
            break
        print(f"Enter a number from 0 to {len(sessions)}.")

    if selected["summary_text"]:
        print("\nPreviously saved summary")
        print("------------------------")
        print(normalize_newlines(selected["summary_text"]))
    else:
        print("\nThis session does not have a saved summary.")

    updates = connection.execute(
        """SELECT recorded_at, update_text FROM troubleshooting_case_updates
           WHERE session_id = ? ORDER BY update_id""",
        (selected["session_id"],),
    ).fetchall()
    for update in updates:
        print(f"\nPrevious update ({update['recorded_at']}):")
        print(normalize_newlines(update["update_text"]))

    return selected


def update_case(connection):
    """Append a dated note to an existing troubleshooting session."""
    session = choose_case_to_update(connection)
    if session is None:
        return
    print("\nAdditional information for this case")
    print("------------------------------------")
    print("Paste command output or type a note. Leave blank to cancel.")
    update_text = read_finding("Case update: ").strip()
    if not update_text:
        print("No update saved.")
        return
    recorded_at = timestamp_now()
    with connection:
        cursor = connection.execute(
            """INSERT INTO troubleshooting_case_updates
               (session_id, update_text, recorded_at) VALUES (?, ?, ?)""",
            (session["session_id"], update_text, recorded_at),
        )
    print(f"\nUpdate {cursor.lastrowid} saved to case "
          f"{session['ticket_number']}, session {session['session_id']}.")
    print("\nCOPY-READY CASE UPDATE")
    print("----------------------")
    print(f"Case {session['ticket_number']} | {recorded_at}")
    print(update_text)


def guide_workflow(workflows):
    """Suggest a workflow from this database without starting a session."""
    by_id = {row["workflow_id"]: row for row in workflows}
    options = (
        ("NMD alert, circuit outage, or circuit status", None),
        ("Packet loss or bandwidth", 2),
        ("Cellular failover (CFS)", 4),
        ("ADVA equipment or alarms", 5),
        ("Determine an RFO for a time frame", 6),
        ("View or clear the ARP cache", 7),
    )
    print("\nWorkflow guide")
    print("--------------")
    print("What is the main issue or task?")
    for index, (label, _) in enumerate(options, 1):
        print(f"{index}. {label}")
    print("0. Return to the workflow menu")

    while True:
        answer = input("Choose the closest option: ").strip()
        if answer in ("0", "q", "Q"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            break
        print("Enter a number from 0 to 6.")

    selected = int(answer)
    if selected == 1:
        print("\nWhich device are you investigating?")
        print("1. AGG or PEC")
        print("2. Switch or MSW")
        print("3. Not sure yet")
        print("0. Return to the workflow menu")
        while True:
            device = input("Choose the device type: ").strip()
            if device in ("0", "q", "Q"):
                return None
            if device == "1":
                workflow_id = 1
                reason = "The NMD alert or circuit issue is on an AGG or PEC."
                break
            if device == "2":
                workflow_id = 3
                reason = "The NMD alert or circuit issue is on a switch or MSW."
                break
            if device == "3":
                print("Identify the device type before choosing the AGG/PEC "
                      "or switch workflow. You can return here afterward.")
                return None
            print("Enter 0, 1, 2, or 3.")
    else:
        reason, workflow_id = options[selected - 1]

    workflow = by_id.get(workflow_id)
    if workflow is None:
        print("That workflow is not configured in this database. "
              "Choose from the available workflows instead.")
        return None
    print(f"\nSuggested workflow: {workflow['workflow_name']}")
    print(f"Why: {reason}")
    if workflow["description"]:
        print(normalize_newlines(workflow["description"]))
    while True:
        action = input("Start this workflow? [Y]es / [N]o (Enter=Yes): ").strip().casefold()
        if action in ("", "y", "yes"):
            return workflow
        if action in ("n", "no"):
            return None
        print("Enter Y or N.")


def load_workflow_inputs(connection, workflow_id):
    """Load all input definitions linked to the selected workflow."""
    return connection.execute(
        """
        SELECT
            it.input_type_id,
            it.input_name,
            it.prompt,
            it.data_type
        FROM workflow_inputs AS wi
        JOIN input_types AS it
            ON it.input_type_id = wi.input_type_id
        WHERE wi.workflow_id = ?
        ORDER BY it.input_type_id;
        """,
        (workflow_id,),
    ).fetchall()


def validate_input(value, data_type):
    """Validate an entered workflow value using its database data_type."""
    normalized_type = (data_type or "text").strip().lower()

    if normalized_type == "ip":
        try:
            ipaddress.ip_address(value)
            return True, ""
        except ValueError:
            return False, "Please enter a valid IPv4 or IPv6 address."

    if normalized_type == "integer":
        try:
            int(value)
            return True, ""
        except ValueError:
            return False, "Please enter a whole number."

    if normalized_type == "text":
        return True, ""
    return False, f"Unsupported input data type: {data_type!r}."


def collect_workflow_inputs(connection, workflow_id, values=None):
    """Ask only for inputs associated with the selected workflow."""
    definitions = load_workflow_inputs(
        connection,
        workflow_id,
    )
    if not any(normalize_name(row["input_name"]) == "circuitid" for row in definitions):
        # Existing agent databases may predate the workflow input mapping.
        definitions = list(definitions) + [{
            "input_name": "Circuit ID",
            "prompt": "Circuit ID (A... or USS..., blank to skip):",
            "data_type": "text",
        }]

    if values is None:
        values = {}

    if not definitions:
        return values

    print("\nWorkflow Inputs")
    print("---------------")

    for definition in definitions:
        input_name = definition["input_name"]
        prompt = definition["prompt"]
        data_type = definition["data_type"]

        while True:
            value = input(f"{prompt} ").strip()

            if not value:
                if normalize_name(input_name) == "circuitid":
                    values.pop(input_name, None)
                    break
                print(f"{input_name} is required.")
                continue

            if normalize_name(input_name) == "circuitid":
                if value.upper() not in circuit_ids(value):
                    print("Enter one circuit ID beginning with A or USS, or press Enter to skip.")
                    continue
                value = value.upper()

            valid, error_message = validate_input(
                value,
                data_type,
            )

            if valid:
                values[input_name] = {
                    "value": value,
                    "data_type": data_type,
                }
                break

            print(error_message)

    return values


def load_workflow_steps(connection, workflow_id):
    """Load workflow steps using the current database schema."""
    rows = connection.execute(
        """
        SELECT
            ws.workflow_step_id,
            ws.workflow_id,
            ws.step_number,
            ws.device_type_id,
            ws.command_id,
            ws.explanation,
            ws.step_type,
            ws.prompt,
            ws.yes_next_step_id,
            ws.no_next_step_id,
            ws.next_step_id,
            d.device_name,
            c.description,
            c.command,
            c.use
        FROM workflow_steps AS ws
        LEFT JOIN device_types AS d
            ON d.device_type_id = ws.device_type_id
        LEFT JOIN commands AS c
            ON c.command_id = ws.command_id
        WHERE ws.workflow_id = ?
        ORDER BY ws.step_number;
        """,
        (workflow_id,),
    ).fetchall()

    if not rows:
        raise RuntimeError(
            "The selected workflow does not contain any steps."
        )

    return {
        row["workflow_step_id"]: row
        for row in rows
    }


def get_next_step_id(step):
    """Return the explicit next step for instruction/observation steps."""
    return step["next_step_id"]


def get_step_edges(step):
    """Return all configured destinations from a workflow step."""
    step_type = step["step_type"]

    if step_type in ("instruction", "observation"):
        next_id = step["next_step_id"]
        return [next_id] if next_id is not None else []

    if step_type == "decision":
        return [
            step["yes_next_step_id"],
            step["no_next_step_id"],
        ]

    return []


def validate_workflow(steps, input_definitions):
    """Validate workflow structure, routing, reachability, and placeholders."""
    step_ids = set(steps)
    errors = []
    if not steps:
        raise RuntimeError("Workflow has no steps.")
    numbers = [step["step_number"] for step in steps.values()]
    if any(not isinstance(number, int) for number in numbers):
        raise RuntimeError("Workflow step numbers must be integers.")
    if len(numbers) != len(set(numbers)):
        errors.append("Workflow has duplicate step numbers.")
    normalized_inputs = [normalize_name(row["input_name"]) for row in input_definitions]
    if any(not name for name in normalized_inputs) or len(normalized_inputs) != len(set(normalized_inputs)):
        errors.append("Workflow input names are empty or ambiguous after normalization.")
    for definition in input_definitions:
        if (definition["data_type"] or "text").strip().lower() not in {"ip", "integer", "text"}:
            errors.append(f"Unsupported input type for {definition['input_name']}.")

    end_step_ids = {
        step_id
        for step_id, step in steps.items()
        if step["step_type"] == "end"
    }

    if not end_step_ids:
        errors.append("Workflow does not contain an end step.")

    for step_id, step in steps.items():
        step_number = step["step_number"]
        step_type = step["step_type"]

        if step_type == "instruction":
            if step["next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} is an instruction "
                    "but does not have a valid next_step_id."
                )

            if (
                step["yes_next_step_id"] is not None
                or step["no_next_step_id"] is not None
            ):
                errors.append(
                    f"Step {step_number} is an instruction "
                    "but has YES/NO routing configured."
                )

        elif step_type == "observation":
            if not (step["prompt"] or "").strip():
                errors.append(
                    f"Step {step_number} is an observation "
                    "but does not have a prompt."
                )

            if step["next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} is an observation "
                    "but does not have a valid next_step_id."
                )

            if (
                step["yes_next_step_id"] is not None
                or step["no_next_step_id"] is not None
            ):
                errors.append(
                    f"Step {step_number} is an observation "
                    "but has YES/NO routing configured."
                )

        elif step_type == "decision":
            if not (step["prompt"] or "").strip():
                errors.append(
                    f"Step {step_number} is missing its decision prompt."
                )

            if step["yes_next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} has an invalid YES path."
                )

            if step["no_next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} has an invalid NO path."
                )

            if step["next_step_id"] is not None:
                errors.append(
                    f"Step {step_number} is a decision "
                    "but also has next_step_id configured."
                )

        elif step_type == "end":
            if (
                step["yes_next_step_id"] is not None
                or step["no_next_step_id"] is not None
                or step["next_step_id"] is not None
            ):
                errors.append(
                    f"Step {step_number} is an end step "
                    "but still has routing configured."
                )

        else:
            errors.append(
                f"Step {step_number} has an invalid "
                f"step type: {step_type}"
            )

        command_text = step["command"] or ""
        if step["command_id"] is not None and step["command"] is None:
            errors.append(f"Step {step_number} references a missing or empty command.")
        remaining = PLACEHOLDER_PATTERN.sub("", command_text)
        if "{" in remaining or "}" in remaining:
            errors.append(f"Step {step_number} has malformed command placeholders.")
        for match in PLACEHOLDER_PATTERN.finditer(command_text):
            if not normalize_name(match.group(1)):
                errors.append(f"Step {step_number} has an empty placeholder name.")
        # Unmapped names are intentionally allowed: the renderer prompts for
        # them and resolves aliases such as cid -> Circuit ID.
        

    # Graph-level checks only run after local routing fields are valid.
    if not errors:
        start_step_id = min(
            steps,
            key=lambda step_id: steps[step_id]["step_number"],
        )

        reachable = set()
        stack = [start_step_id]

        while stack:
            step_id = stack.pop()

            if step_id in reachable:
                continue

            reachable.add(step_id)

            for destination in get_step_edges(steps[step_id]):
                if destination in step_ids:
                    stack.append(destination)

        unreachable = step_ids - reachable

        for step_id in sorted(
            unreachable,
            key=lambda item: steps[item]["step_number"],
        ):
            errors.append(
                f"Step {steps[step_id]['step_number']} is unreachable "
                "from the workflow start."
            )

        # Work backwards from all end steps to verify every reachable step
        # has at least one possible path to an end step.
        reverse_edges = {
            step_id: set()
            for step_id in step_ids
        }

        for step_id, step in steps.items():
            for destination in get_step_edges(step):
                if destination in step_ids:
                    reverse_edges[destination].add(step_id)

        can_reach_end = set(end_step_ids)
        stack = list(end_step_ids)

        while stack:
            step_id = stack.pop()

            for predecessor in reverse_edges[step_id]:
                if predecessor not in can_reach_end:
                    can_reach_end.add(predecessor)
                    stack.append(predecessor)

        for step_id in sorted(
            reachable - can_reach_end,
            key=lambda item: steps[item]["step_number"],
        ):
            errors.append(
                f"Step {steps[step_id]['step_number']} cannot reach "
                "an end step."
            )

    if errors:
        raise RuntimeError(
            "Workflow validation failed:\n- "
            + "\n- ".join(errors)
        )


def find_input_by_name(input_values, requested_name):
    """Find a collected input by normalized name."""
    requested = normalize_name(requested_name)

    for input_name, details in input_values.items():
        if normalize_name(input_name) == requested:
            return details["value"]

    return None


def find_inputs_by_data_type(input_values, data_type):
    """Return inputs matching a normalized database data type."""
    wanted = (data_type or "text").strip().lower()
    return [(name, details["value"]) for name, details in input_values.items()
            if (details["data_type"] or "text").strip().lower() == wanted]



def resolve_legacy_alias(placeholder, input_values):
    """Resolve older command placeholder names against current inputs."""
    key = normalize_name(placeholder)

    alias_candidates = {
        "bvi": ["BVI"],
        "vlan": ["VLAN"],
        "vrf": ["VRF"],
        "cid": ["Circuit ID"],
        "circuitid": ["Circuit ID"],
        "cellularinterface": ["Cellular Interface"],
        "interface": [
            "interface",
        ],
    }

    for candidate in alias_candidates.get(key, []):
        value = find_input_by_name(
            input_values,
            candidate,
        )

        if value is not None:
            return value

    return None


def prompt_placeholder_value(placeholder, data_type="text"):
    while True:
        value = input(f"Enter value for command placeholder '{{{{{placeholder}}}}}': ").strip()
        if not value:
            print("A value is required.")
            continue
        valid, message = validate_input(value, data_type)
        if valid:
            return value
        print(message)


def choose_value_for_ambiguous_placeholder(placeholder, candidates):
    """Resolve a generic IP placeholder, validating manual IP entry."""
    print(f"\nChoose the address for {{{{{placeholder}}}}}:")
    for index, (name, value) in enumerate(candidates, 1):
        print(f"{index}. {name}: {value}")
    print(f"{len(candidates) + 1}. Enter an IP address manually")
    while True:
        choice = input("Select the value to use: ").strip()
        if choice.isascii() and choice.isdecimal():
            selected = int(choice)
            if 1 <= selected <= len(candidates):
                value = candidates[selected - 1][1]
                if validate_input(value, "ip")[0]:
                    return value
                print("That saved value is not a valid IP address.")
                continue
            if selected == len(candidates) + 1:
                return prompt_placeholder_value(placeholder, "ip")
        print("Please enter one of the displayed numbers.")



def resolve_placeholder(placeholder, input_values):
    """Resolve and retain values; do not guess between multiple IP inputs."""
    exact = find_input_by_name(input_values, placeholder)
    if exact is not None:
        return exact
    alias = resolve_legacy_alias(placeholder, input_values)
    if alias is not None:
        return alias
    key = normalize_name(placeholder)
    generic_ip = key in {"ip", "ipaddress", "ipaddr", "address"}
    kind = "ip" if generic_ip or key.endswith("ipaddress") else "text"
    if key == "vlan":
        kind = "integer"
    candidates = find_inputs_by_data_type(input_values, "ip") if generic_ip else []
    if len(candidates) == 1:
        value = candidates[0][1]
        if not validate_input(value, "ip")[0]:
            value = prompt_placeholder_value(placeholder, "ip")
    elif candidates:
        value = choose_value_for_ambiguous_placeholder(placeholder, candidates)
    else:
        value = prompt_placeholder_value(placeholder, kind)
    input_values[placeholder.strip()] = {"value": value, "data_type": kind}
    return value



def insert_user_values(command_text, input_values):
    """Resolve double-brace placeholders without replacing inside new values."""
    if not command_text:
        return ""
    command_text = normalize_newlines(command_text)
    # This legacy template describes alternatives, not a literal CLI command.
    # Ask for one device-compatible start time; do not invent a date format.
    command_text = re.sub(
        r"\{\{today\}\}\s+or\s+\{\{month dd hh:mm:ss\}\}",
        "{{logging start time}}", command_text, flags=re.I,
    )
    resolved = {}
    def replace_match(match):
        placeholder = match.group(1)
        key = normalize_name(placeholder)
        if key not in resolved:
            resolved[key] = resolve_placeholder(placeholder, input_values)
        return resolved[key]
    return PLACEHOLDER_PATTERN.sub(replace_match, command_text)



def ask_step_action(can_go_back, allow_skip=True):
    """Offer navigation controls before an instruction or end response."""
    options = ["Enter=Continue"]

    if can_go_back:
        options.append("B=Back")

    if allow_skip:
        options.append("S=Skip")

    options.append("Q=Quit")

    prompt = " [" + " / ".join(options) + "]: "

    while True:
        answer = input(prompt).strip().lower()

        if answer == "":
            return "continue"

        if answer in ("b", "back") and can_go_back:
            return "back"

        if answer in ("s", "skip") and allow_skip:
            return "skip"

        if answer in ("q", "quit"):
            return "quit"

        print("Please choose one of the displayed options.")


def ask_observation(prompt, can_go_back):
    """Take the agent's observation directly; Enter saves and advances."""
    options = ["Enter=Save/Continue", "S=Skip", "Q=Quit"]
    if can_go_back:
        options.append("B=Back")
    print("\nObservation")
    print("-----------")
    response = read_finding(
        f"{prompt}\nYour observation (blank=continue) "
        f"[{' / '.join(options)}]: "
    ).strip()
    action = response.casefold()
    if action in ("s", "/skip"):
        return "skip", ""
    if action in ("q", "/quit"):
        return "quit", ""
    if can_go_back and action in ("b", "/back"):
        return "back", ""
    return "record", response


def ask_decision(question, can_go_back):
    """Ask a YES/NO decision with Back/Skip/Quit controls."""
    options = ["Y", "N"]

    if can_go_back:
        options.append("B")

    options.extend(["S", "Q"])
    prompt = f"\n{question} [{' / '.join(options)}]: "

    while True:
        answer = input(prompt).strip().lower()

        if answer in ("y", "yes"):
            return "yes"

        if answer in ("n", "no"):
            return "no"

        if answer in ("b", "back") and can_go_back:
            return "back"

        if answer in ("s", "skip"):
            return "skip"

        if answer in ("q", "quit"):
            return "quit"

        print("Please choose one of the displayed options.")


def display_step(step, input_values):
    """Show step context, then resolve and display its command."""
    print("\n" + "=" * 72)
    print(f"STEP {step['step_number']}")
    print("=" * 72)
    print(f"Device: {step['device_name'] or 'Not specified'}")
    print(f"Purpose: {normalize_newlines(step['description']) or 'Not specified'}")
    if step["use"]:
        print(f"Use: {normalize_newlines(step['use'])}")
    print("\nExplanation\n-----------")
    print(normalize_newlines(step["explanation"]) or "No explanation has been entered.")
    command = insert_user_values(step["command"], input_values)
    if command:
        print("\nCommand\n-------")
        print(command)
    return command



def get_ticket_number():
    """Collect the ticket or case number used for this troubleshooting run."""
    while True:
        ticket_number = input(
            "\nEnter ticket/case number: "
        ).strip()

        if ticket_number:
            return ticket_number

        print("A ticket/case number is required.")


def create_session(connection, workflow_id, ticket_number):
    """Create a troubleshooting session and return its database ID."""
    cursor = connection.execute(
        """
        INSERT INTO troubleshooting_sessions (
            workflow_id,
            ticket_number,
            started_at,
            status
        )
        VALUES (?, ?, ?, 'in_progress');
        """,
        (
            workflow_id,
            ticket_number,
            timestamp_now(),
        ),
    )
    connection.commit()
    return cursor.lastrowid


def _save_session_inputs(connection, session_id, input_values):
    connection.execute("DELETE FROM troubleshooting_session_inputs WHERE session_id = ?", (session_id,))
    connection.executemany(
        """INSERT INTO troubleshooting_session_inputs
           (session_id, input_name, input_value, data_type) VALUES (?, ?, ?, ?)""",
        [(session_id, name, details["value"], details["data_type"])
         for name, details in input_values.items()],
    )


def save_session_inputs(connection, session_id, input_values):
    """Atomically replace all saved inputs for this session."""
    with connection:
        _save_session_inputs(connection, session_id, input_values)



def _serialized_finding(finding):
    response = finding["response"] or ""
    if finding["skipped"]:
        return "[Skipped]"
    if finding["decision"] is not None:
        response = f"[Decision: {'Yes' if finding['decision'] else 'No'}] " + response
    return response.strip()


def _delete_observation_facts(connection, observation_id):
    # Explicit child cleanup also supports databases without delete cascades.
    connection.execute(
        """DELETE FROM rule_matches WHERE finding_id IN
           (SELECT finding_id FROM troubleshooting_findings WHERE observation_id = ?)""",
        (observation_id,),
    )
    connection.execute("DELETE FROM troubleshooting_findings WHERE observation_id = ?", (observation_id,))


def _sync_session_findings(connection, session_id, findings):
    """Sync a COMPLETE in-memory session; caller owns commit and ID publication."""
    if connection.execute("SELECT 1 FROM troubleshooting_sessions WHERE session_id = ?", (session_id,)).fetchone() is None:
        raise RuntimeError(f"Session {session_id} does not exist.")
    existing = {row["observation_id"]: row for row in connection.execute(
        "SELECT * FROM troubleshooting_observations WHERE session_id = ?", (session_id,)
    )}
    ids = [f["observation_id"] for f in findings if f.get("observation_id") is not None]
    if len(ids) != len(set(ids)) or not set(ids).issubset(existing):
        raise RuntimeError("Finding IDs are duplicated, missing, or belong to another session.")
    for obsolete in existing.keys() - set(ids):
        _delete_observation_facts(connection, obsolete)
        connection.execute("DELETE FROM troubleshooting_observations WHERE observation_id = ?", (obsolete,))
    pending = []
    for finding in findings:
        step_id = finding["workflow_step_id"]
        if connection.execute(
            """SELECT 1 FROM workflow_steps ws JOIN troubleshooting_sessions s
               ON s.workflow_id = ws.workflow_id WHERE s.session_id = ? AND ws.workflow_step_id = ?""",
            (session_id, step_id),
        ).fetchone() is None:
            raise RuntimeError("Finding step does not belong to this session's workflow.")
        observation_id = finding.get("observation_id")
        prompt = finding.get("prompt") or finding["description"]
        response = _serialized_finding(finding)
        facts = finding.get("typed_facts", []) if not finding["skipped"] else []
        desired_facts = [
            (f["observation_type_id"], f["value_text"], f["value_number"], f["unit"], f.get("confidence", 0.5))
            for f in facts
        ]
        if observation_id is None:
            cursor = connection.execute(
                """INSERT INTO troubleshooting_observations
                   (session_id, workflow_step_id, prompt, response, recorded_at) VALUES (?, ?, ?, ?, ?)""",
                (session_id, step_id, prompt, response, timestamp_now()),
            )
            observation_id = cursor.lastrowid
            pending.append((finding, observation_id))
        else:
            previous = existing[observation_id]
            saved_facts = [tuple(row) for row in connection.execute(
                """SELECT observation_type_id, value_text, value_number, unit, confidence
                   FROM troubleshooting_findings WHERE observation_id = ? ORDER BY finding_id""",
                (observation_id,),
            )]
            if (previous["workflow_step_id"] == step_id and previous["prompt"] == prompt
                    and previous["response"] == response and saved_facts == desired_facts):
                continue
            _delete_observation_facts(connection, observation_id)
            connection.execute(
                """UPDATE troubleshooting_observations SET workflow_step_id = ?, prompt = ?,
                   response = ?, recorded_at = ? WHERE observation_id = ?""",
                (step_id, prompt, response, timestamp_now(), observation_id),
            )
        connection.executemany(
            """INSERT INTO troubleshooting_findings
               (observation_id, observation_type_id, value_text, value_number, unit, confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(observation_id, *fact) for fact in desired_facts],
        )
    return pending


def _publish_observation_ids(pending):
    for finding, observation_id in pending:
        finding["observation_id"] = observation_id


def save_session_findings(connection, session_id, findings):
    """Sync the complete session and refresh rules in one transaction."""
    with connection:
        pending = _sync_session_findings(connection, session_id, findings)
        workflow_id = connection.execute(
            "SELECT workflow_id FROM troubleshooting_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        matches = _evaluate_rules(connection, session_id, workflow_id)
    _publish_observation_ids(pending)
    return matches



def build_summary_text(
    ticket_number,
    workflow_name,
    findings,
    input_values,
):
    """Build a copy-ready ticket note from the troubleshooting run."""
    lines = [
        f"Ticket/Case: {ticket_number}",
        f"Workflow: {workflow_name}",
    ]

    if input_values:
        lines.extend(
            [
                "",
                "Workflow Inputs",
                "---------------",
            ]
        )

        for input_name, details in input_values.items():
            lines.append(
                f"{input_name}: {details['value']}"
            )

    lines.extend(
        [
            "",
            "Troubleshooting",
            "---------------",
        ]
    )

    for finding in findings:
        description = (
            finding["description"]
            or "No description"
        )

        lines.append(
            f"Step {finding['step_number']} - {description}"
        )

        if finding.get("command_text"):
            lines.append("Command shown: " + finding["command_text"])
        if finding["skipped"]:
            lines.append("Status: Skipped")
        else:
            if finding["decision"] is not None:
                lines.append(
                    "Decision: "
                    + (
                        "Yes"
                        if finding["decision"]
                        else "No"
                    )
                )

            if finding["step_type"] == "end":
                lines.append(
                    "Conclusion: "
                    + (
                        finding["response"]
                        or "No conclusion was entered."
                    )
                )
            else:
                lines.append(
                    "Findings: "
                    + (
                        finding["response"]
                        or "No findings were entered."
                    )
                )

        lines.append("")

    return "\n".join(lines).rstrip()


def write_summary_file(ticket_number, session_id, summary_text):
    """Write a text copy beside the program, replacing this session's export."""
    safe_ticket = re.sub(r"[^A-Za-z0-9._-]+", "_", ticket_number).strip("_.")[:100] or "ticket"
    directory = application_directory() / "troubleshooting_summaries"
    directory.mkdir(exist_ok=True)
    path = directory / f"{safe_ticket}_session_{session_id}.txt"
    path.write_text(normalize_newlines(summary_text), encoding="utf-8")
    return path



def finish_session(connection, session_id, status, findings, summary_text, input_values=None):
    """Commit final evidence, rule matches, inputs and session status together."""
    if status not in {"completed", "quit", "interrupted", "error"}:
        raise ValueError(f"Unsupported final session status: {status}")
    with connection:
        pending = _sync_session_findings(connection, session_id, findings)
        workflow_id = connection.execute(
            "SELECT workflow_id FROM troubleshooting_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        _evaluate_rules(connection, session_id, workflow_id)
        if input_values is not None:
            _save_session_inputs(connection, session_id, input_values)
        cursor = connection.execute(
            """UPDATE troubleshooting_sessions SET completed_at = ?, status = ?, summary_text = ?
               WHERE session_id = ?""",
            (timestamp_now(), status, summary_text, session_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Session {session_id} could not be finalized.")
    _publish_observation_ids(pending)




def go_back(history, findings):
    """Remove the previous result only when history and findings agree."""
    if not history:
        return None
    if not findings or findings[-1]["workflow_step_id"] != history[-1]:
        raise RuntimeError("Workflow history and findings are out of sync.")
    findings.pop()
    return history.pop()



def run_workflow(connection, workflow, ticket_number):
    """Run and persist a database-driven troubleshooting workflow."""
    input_definitions = load_workflow_inputs(
        connection,
        workflow["workflow_id"],
    )

    steps = load_workflow_steps(
        connection,
        workflow["workflow_id"],
    )

    validate_workflow(
        steps,
        input_definitions,
    )

    available_types = {
        row["observation_key"]: row for row in connection.execute(
            "SELECT observation_type_id, observation_key, display_name, "
            "data_type, default_unit, active FROM observation_types"
        )
    }

    session_id = create_session(
        connection,
        workflow["workflow_id"],
        ticket_number,
    )

    input_values = {}
    findings = []
    shown_matches = {}

    try:
        input_values = collect_workflow_inputs(
            connection,
            workflow["workflow_id"],
            values=input_values,
        )

        save_session_inputs(
            connection,
            session_id,
            input_values,
        )

        current_step_id = min(
            steps,
            key=lambda step_id: steps[step_id]["step_number"],
        )

        history = []
        status = "completed"

        while current_step_id is not None:
            if current_step_id not in steps:
                raise RuntimeError(
                    f"Workflow routed to missing step ID "
                    f"{current_step_id}."
                )

            step = steps[current_step_id]

            command_text = display_step(step, input_values)
            save_session_inputs(connection, session_id, input_values)

            decision = None
            response = ""
            skipped = False
            can_go_back = bool(history)

            if step["step_type"] == "decision":
                action = ask_decision(
                    step["prompt"],
                    can_go_back,
                )

                if action == "back":
                    current_step_id = go_back(
                        history,
                        findings,
                    )
                    matches = save_session_findings(connection, session_id, findings)
                    shown_matches = show_rule_matches(matches, shown_matches)
                    continue

                if action == "quit":
                    status = "quit"
                    break

                if action == "skip":
                    print(
                        "\nDecision steps cannot be skipped because "
                        "the workflow needs a YES or NO route."
                    )
                    continue

                decision = action == "yes"

                print("\nWhat did you find?")
                print("------------------")
                response = read_finding(
                    "Enter your findings for this step: "
                ).strip()

                next_step_id = (
                    step["yes_next_step_id"]
                    if decision
                    else step["no_next_step_id"]
                )

            elif step["step_type"] == "observation":
                action, response = ask_observation(step["prompt"], can_go_back)

                if action == "back":
                    current_step_id = go_back(
                        history,
                        findings,
                    )
                    matches = save_session_findings(connection, session_id, findings)
                    shown_matches = show_rule_matches(matches, shown_matches)
                    continue

                if action == "quit":
                    status = "quit"
                    break

                if action == "skip":
                    skipped = True

                next_step_id = step["next_step_id"]

            elif step["step_type"] == "instruction":
                if step["prompt"]:
                    print("\nInstruction")
                    print("-----------")
                    print(step["prompt"])

                action = ask_step_action(
                    can_go_back,
                    allow_skip=True,
                )

                if action == "back":
                    current_step_id = go_back(
                        history,
                        findings,
                    )
                    matches = save_session_findings(connection, session_id, findings)
                    shown_matches = show_rule_matches(matches, shown_matches)
                    continue

                if action == "quit":
                    status = "quit"
                    break

                if action == "skip":
                    skipped = True
                else:
                    print("\nWhat did you find?")
                    print("------------------")
                    response = read_finding(
                        "Enter your findings for this step: "
                    ).strip()

                next_step_id = step["next_step_id"]

            elif step["step_type"] == "end":
                action = ask_step_action(
                    can_go_back,
                    allow_skip=False,
                )

                if action == "back":
                    current_step_id = go_back(
                        history,
                        findings,
                    )
                    matches = save_session_findings(connection, session_id, findings)
                    shown_matches = show_rule_matches(matches, shown_matches)
                    continue

                if action == "quit":
                    status = "quit"
                    break

                print("\nConclusion")
                print("----------")
                response = read_finding(
                    "Enter your conclusion: "
                ).strip()

                next_step_id = None

            else:
                raise RuntimeError(
                    f"Unsupported step type: "
                    f"{step['step_type']}"
                )

            typed_facts = []
            if response and not skipped:
                response, typed_facts = review_observations(
                    response, available_types, step["prompt"] or "", input_values,
                )
                # Suppress BGE suggestions for every explicitly parsed type,
                # including types the agent rejected. BGE stays tentative.
                parsed_type_ids = {
                    fact["observation_type_id"] for fact in parse_observations(
                        response, available_types, step["prompt"] or "", input_values,
                    )
                }
                suggestions = interpret_phrasing(response, available_types, parsed_type_ids)
                for suggestion in suggestions:
                    suggestion["confidence"] = 0.5
                    print(f"Possible interpretation (unverified): "
                          f"{suggestion['display_name']} = {format_fact(suggestion)}")
                typed_facts.extend(suggestions)

            findings.append(
                {
                    "workflow_step_id": current_step_id,
                    "step_number": step["step_number"],
                    "description": (
                        step["description"]
                        or step["prompt"]
                        or "No description"
                    ),
                    "prompt": step["prompt"],
                    "step_type": step["step_type"],
                    "response": response,
                    "decision": decision,
                    "skipped": skipped,
                    "typed_facts": typed_facts,
                    "command_text": command_text,
                }
            )

            history.append(current_step_id)

            matches = save_session_findings(connection, session_id, findings)
            shown_matches = show_rule_matches(matches, shown_matches)
            current_step_id = next_step_id

        summary_text = build_summary_text(
            ticket_number,
            workflow["workflow_name"],
            findings,
            input_values,
        )

        finish_session(
            connection,
            session_id,
            status,
            findings,
            summary_text,
            input_values=input_values,
        )

    except (KeyboardInterrupt, EOFError):
        status = "interrupted"
        summary_text = build_summary_text(
            ticket_number, workflow["workflow_name"], findings, input_values,
        )
        try:
            finish_session(connection, session_id, status, findings, summary_text,
                           input_values=input_values)
        except Exception as save_error:
            connection.rollback()
            try:
                with connection:
                    connection.execute(
                        "UPDATE troubleshooting_sessions SET status = 'interrupted', completed_at = ? WHERE session_id = ?",
                        (timestamp_now(), session_id),
                    )
            except sqlite3.Error as status_error:
                print(f"Could not update session status: {status_error}")
            raise RuntimeError(
                f"Session {session_id} interrupted; final progress could not be saved: {save_error}. "
                "Earlier committed progress remains available."
            ) from save_error
        print("\nSession interrupted; recorded progress saved.")

    except Exception as exc:
        connection.rollback()
        summary_text = build_summary_text(
            ticket_number, workflow["workflow_name"], findings, input_values,
        )
        try:
            with connection:
                connection.execute(
                    """UPDATE troubleshooting_sessions SET status = 'error',
                       completed_at = ?, summary_text = ? WHERE session_id = ?""",
                    (timestamp_now(), summary_text, session_id),
                )
        except sqlite3.Error as save_error:
            print(f"Could not mark session {session_id} as failed: {save_error}")
        print(f"Session {session_id} stopped; previously committed progress remains in the database.")
        raise RuntimeError(f"Session {session_id} failed: {exc}") from exc

    print("\n" + "=" * 72)
    print("COPY-READY TICKET SUMMARY")
    print("=" * 72)
    print(summary_text)

    print(f"\nSession {session_id} saved to the database.")
    try:
        summary_path = write_summary_file(ticket_number, session_id, summary_text)
    except OSError as exc:
        print(f"Could not export the text summary: {exc}. The database save succeeded.")
    else:
        print(f"Ticket summary saved to: {summary_path}")

    input("\nPress Enter to return to the workflow menu...")



def main():
    try:
        database_path = find_database()

        with closing(connect_database(database_path)) as connection:
            while True:
                workflow = choose_workflow(connection)

                if workflow is None:
                    print("Troubleshooter closed.")
                    break

                if workflow == "update_case":
                    update_case(connection)
                    continue

                ticket_number = get_ticket_number()
                run_workflow(
                    connection,
                    workflow,
                    ticket_number,
                )

    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    except (KeyboardInterrupt, EOFError):
        print("\nTroubleshooter closed.")


if __name__ == "__main__":
    main()
