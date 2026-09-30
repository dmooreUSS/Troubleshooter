import ipaddress
import re
import sqlite3
from datetime import datetime
from pathlib import Path


DATABASE_FILENAME = "Network_yes_no.db"
PLACEHOLDER_PATTERN = re.compile(r"\{\{([^{}]+)\}\}|\{([^{}]+)\}")
LEGACY_PLACEHOLDER_PATTERN = re.compile(r"(?<!\{)\{([^{}]+)\}(?!\})")


def find_database():
    """Find the database in the current folder or beside this script."""
    current_path = Path.cwd() / DATABASE_FILENAME
    script_path = Path(__file__).resolve().parent / DATABASE_FILENAME

    if current_path.exists():
        return current_path

    if script_path.exists():
        return script_path

    raise FileNotFoundError(
        f"Database not found: {DATABASE_FILENAME}"
    )


def ensure_session_tables(connection):
    """Create persistent troubleshooting history tables if needed."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS troubleshooting_sessions (
            session_id INTEGER PRIMARY KEY,
            workflow_id INTEGER NOT NULL,
            ticket_number TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'in_progress',
            summary_text TEXT,
            FOREIGN KEY (workflow_id)
                REFERENCES workflows(workflow_id)
        );

        CREATE TABLE IF NOT EXISTS troubleshooting_session_inputs (
            session_input_id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            input_name TEXT NOT NULL,
            input_value TEXT NOT NULL,
            data_type TEXT,
            FOREIGN KEY (session_id)
                REFERENCES troubleshooting_sessions(session_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS troubleshooting_findings (
            finding_id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            workflow_step_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            description TEXT,
            step_type TEXT NOT NULL,
            response TEXT,
            decision TEXT,
            skipped INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (session_id)
                REFERENCES troubleshooting_sessions(session_id)
                ON DELETE CASCADE,
            FOREIGN KEY (workflow_step_id)
                REFERENCES workflow_steps(workflow_step_id)
        );
        """
    )
    connection.commit()


def connect_database(database_path):
    """Connect to the database and verify the current schema."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")

    required_tables = {
        "commands",
        "device_types",
        "input_types",
        "workflow_inputs",
        "workflows",
        "workflow_steps",
    }

    existing_tables = {
        row["name"]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table';
            """
        ).fetchall()
    }

    missing_tables = required_tables - existing_tables

    if missing_tables:
        connection.close()
        raise RuntimeError(
            "Database is missing required tables: "
            + ", ".join(sorted(missing_tables))
        )

    ensure_session_tables(connection)
    return connection


def normalize_name(value):
    """Normalize input and placeholder names for matching."""
    return "".join(
        character.lower()
        for character in value
        if character.isalnum()
    )


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

    print("\nSelect A Workflow")
    print("-----------------")

    for index, workflow in enumerate(workflows, start=1):
        print(f"{index}. {workflow['workflow_name']}")

        if workflow["description"]:
            print(f"   {workflow['description']}")

    while True:
        choice = input("\nSelect a workflow: ").strip()

        if choice.isdigit():
            selected = int(choice)

            if 1 <= selected <= len(workflows):
                return workflows[selected - 1]

        print("Please enter a valid workflow number.")


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

    return True, ""


def collect_workflow_inputs(connection, workflow_id):
    """Ask only for inputs associated with the selected workflow."""
    definitions = load_workflow_inputs(
        connection,
        workflow_id,
    )

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
                print(f"{input_name} is required.")
                continue

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


def get_step_edges(step):
    """Return all configured destinations from a workflow step."""
    step_type = step["step_type"]

    if step_type in ("instruction", "observation"):
        next_id = step["yes_next_step_id"]
        return [next_id] if next_id is not None else []

    if step_type == "decision":
        return [
            step["yes_next_step_id"],
            step["no_next_step_id"],
        ]

    return []


def validate_workflow(steps):
    """Validate workflow structure, routing, reachability, and placeholders."""
    step_ids = set(steps)
    errors = []

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
            if step["yes_next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} is an instruction "
                    "but does not have a valid YES continuation."
                )

            if step["no_next_step_id"] not in (
                None, step["yes_next_step_id"]
            ):
                errors.append(
                    f"Step {step_number} is an instruction "
                    "but has a different NO continuation."
                )

        elif step_type == "observation":
            if not step["prompt"]:
                errors.append(
                    f"Step {step_number} is an observation "
                    "but does not have a prompt."
                )

            if step["yes_next_step_id"] not in step_ids:
                errors.append(
                    f"Step {step_number} is an observation "
                    "but does not have a valid YES continuation."
                )

            if step["no_next_step_id"] not in (
                None, step["yes_next_step_id"]
            ):
                errors.append(
                    f"Step {step_number} is an observation "
                    "but has a different NO continuation."
                )

        elif step_type == "decision":
            if not step["prompt"]:
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

        elif step_type == "end":
            if (
                step["yes_next_step_id"] is not None
                or step["no_next_step_id"] is not None
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

        legacy_matches = LEGACY_PLACEHOLDER_PATTERN.findall(
            command_text
        )

        for placeholder in legacy_matches:
            errors.append(
                f"Step {step_number} uses legacy placeholder "
                f"'{{{placeholder}}}'. Use '{{{{{placeholder}}}}}'."
            )

        # Unlisted placeholders are requested when the command is displayed.
        # The RFO workflow currently uses BVI without a workflow input.

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
    """Return inputs matching a database data type."""
    wanted = data_type.lower()

    return [
        (name, details["value"])
        for name, details in input_values.items()
        if (details["data_type"] or "text").lower() == wanted
    ]


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


def choose_value_for_ambiguous_placeholder(
    placeholder,
    candidates,
):
    """Let the user choose a value when a command placeholder is ambiguous."""
    print(
        f"\nThe command placeholder '{{{{{placeholder}}}}}' "
        "matches more than one workflow input."
    )

    for index, (name, value) in enumerate(candidates, start=1):
        print(f"{index}. {name}: {value}")

    print(f"{len(candidates) + 1}. Enter a value manually")

    while True:
        choice = input("Select the value to use: ").strip()

        if not choice.isdigit():
            print("Please enter a valid number.")
            continue

        selected = int(choice)

        if 1 <= selected <= len(candidates):
            return candidates[selected - 1][1]

        if selected == len(candidates) + 1:
            while True:
                manual_value = input(
                    f"Enter value for {placeholder}: "
                ).strip()

                if manual_value:
                    return manual_value

                print("A value is required.")

        print("Please enter a valid number.")


def resolve_placeholder(placeholder, input_values):
    """Resolve one command placeholder from collected workflow inputs."""
    exact_value = find_input_by_name(
        input_values,
        placeholder,
    )

    if exact_value is not None:
        return exact_value

    alias_value = resolve_legacy_alias(
        placeholder,
        input_values,
    )

    if alias_value is not None:
        return alias_value

    normalized_placeholder = normalize_name(placeholder)

    if normalized_placeholder in {
        "ip",
        "ipaddress",
        "ipaddr",
        "address",
    }:
        ip_inputs = find_inputs_by_data_type(
            input_values,
            "ip",
        )

        if len(ip_inputs) == 1:
            return ip_inputs[0][1]

        if len(ip_inputs) > 1:
            return choose_value_for_ambiguous_placeholder(
                placeholder,
                ip_inputs,
            )

    while True:
        value = input(
            f"Enter value for command placeholder "
            f"'{{{{{placeholder}}}}}': "
        ).strip()

        if value:
            return value

        print("A value is required.")


def insert_user_values(command_text, input_values):
    """Replace command placeholders using database-driven workflow inputs."""
    if not command_text:
        return ""

    resolved = {}

    for match in PLACEHOLDER_PATTERN.finditer(command_text):
        placeholder = match.group(1) or match.group(2)

        if placeholder not in resolved:
            resolved[placeholder] = resolve_placeholder(
                placeholder,
                input_values,
            )

    updated_command = command_text

    for placeholder, value in resolved.items():
        updated_command = updated_command.replace(
            "{{" + placeholder + "}}",
            value,
        )
        updated_command = updated_command.replace(
            "{" + placeholder + "}",
            value,
        )

    return updated_command


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
    """Display one workflow step without collecting a response."""
    command_text = insert_user_values(
        step["command"],
        input_values,
    )

    print("\n" + "=" * 72)
    print(f"STEP {step['step_number']}")
    print("=" * 72)

    print(
        f"Device: "
        f"{step['device_name'] or 'Not specified'}"
    )

    print(
        f"Purpose: "
        f"{step['description'] or 'Not specified'}"
    )

    if step["use"]:
        print(f"Use: {step['use']}")

    print("\nExplanation")
    print("-----------")
    print(
        step["explanation"]
        or "No explanation has been entered."
    )

    if command_text:
        print("\nCommand")
        print("-------")
        print(command_text)


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
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    connection.commit()
    return cursor.lastrowid


def save_session_inputs(connection, session_id, input_values):
    """Persist the workflow inputs collected for a session."""
    connection.execute(
        """
        DELETE FROM troubleshooting_session_inputs
        WHERE session_id = ?;
        """,
        (session_id,),
    )

    for input_name, details in input_values.items():
        connection.execute(
            """
            INSERT INTO troubleshooting_session_inputs (
                session_id,
                input_name,
                input_value,
                data_type
            )
            VALUES (?, ?, ?, ?);
            """,
            (
                session_id,
                input_name,
                details["value"],
                details["data_type"],
            ),
        )

    connection.commit()


def save_session_findings(connection, session_id, findings):
    """Replace saved findings with the final path currently in memory."""
    connection.execute(
        """
        DELETE FROM troubleshooting_findings
        WHERE session_id = ?;
        """,
        (session_id,),
    )

    for finding in findings:
        decision = finding["decision"]

        if decision is True:
            decision_text = "Yes"
        elif decision is False:
            decision_text = "No"
        else:
            decision_text = None

        connection.execute(
            """
            INSERT INTO troubleshooting_findings (
                session_id,
                workflow_step_id,
                step_number,
                description,
                step_type,
                response,
                decision,
                skipped,
                recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                session_id,
                finding["workflow_step_id"],
                finding["step_number"],
                finding["description"],
                finding["step_type"],
                finding["response"],
                decision_text,
                1 if finding["skipped"] else 0,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    connection.commit()


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


def write_summary_file(
    ticket_number,
    session_id,
    summary_text,
):
    """Write a text copy of the ticket summary beside the script."""
    safe_ticket = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        ticket_number,
    ).strip("_")

    if not safe_ticket:
        safe_ticket = "ticket"

    summary_directory = (
        Path(__file__).resolve().parent
        / "troubleshooting_summaries"
    )
    summary_directory.mkdir(exist_ok=True)

    summary_path = summary_directory / (
        f"{safe_ticket}_session_{session_id}.txt"
    )

    summary_path.write_text(
        summary_text,
        encoding="utf-8",
    )

    return summary_path


def finish_session(
    connection,
    session_id,
    status,
    findings,
    summary_text,
):
    """Save final session results and mark the session complete."""
    save_session_findings(
        connection,
        session_id,
        findings,
    )

    connection.execute(
        """
        UPDATE troubleshooting_sessions
        SET
            completed_at = ?,
            status = ?,
            summary_text = ?
        WHERE session_id = ?;
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            status,
            summary_text,
            session_id,
        ),
    )
    connection.commit()



def go_back(history, findings):
    """Return to the previously completed step and remove its old result."""
    if not history:
        return None

    previous_step_id = history.pop()

    if (
        findings
        and findings[-1]["workflow_step_id"]
        == previous_step_id
    ):
        findings.pop()

    return previous_step_id


def run_workflow(connection, workflow, ticket_number):
    """Run and persist a database-driven troubleshooting workflow."""
    steps = load_workflow_steps(
        connection,
        workflow["workflow_id"],
    )

    validate_workflow(
        steps,
    )

    session_id = create_session(
        connection,
        workflow["workflow_id"],
        ticket_number,
    )

    input_values = collect_workflow_inputs(
        connection,
        workflow["workflow_id"],
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

    findings = []
    history = []
    status = "completed"

    while current_step_id is not None:
        if current_step_id not in steps:
            raise RuntimeError(
                f"Workflow routed to missing step ID "
                f"{current_step_id}."
            )

        step = steps[current_step_id]

        display_step(
            step,
            input_values,
        )

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
            response = input(
                "Enter your findings for this step: "
            ).strip()

            following_step_id = (
                step["yes_next_step_id"]
                if decision
                else step["no_next_step_id"]
            )

        elif step["step_type"] == "observation":
            print("\nObservation")
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
                continue

            if action == "quit":
                status = "quit"
                break

            if action == "skip":
                skipped = True
            else:
                response = input(
                    "Enter your observation: "
                ).strip()

            following_step_id = step["yes_next_step_id"]

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
                continue

            if action == "quit":
                status = "quit"
                break

            if action == "skip":
                skipped = True
            else:
                print("\nWhat did you find?")
                print("------------------")
                response = input(
                    "Enter your findings for this step: "
                ).strip()

            following_step_id = step["yes_next_step_id"]

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
                continue

            if action == "quit":
                status = "quit"
                break

            print("\nConclusion")
            print("----------")
            response = input(
                "Enter your conclusion: "
            ).strip()

            following_step_id = None

        else:
            raise RuntimeError(
                f"Unsupported step type: "
                f"{step['step_type']}"
            )

        findings.append(
            {
                "workflow_step_id": current_step_id,
                "step_number": step["step_number"],
                "description": (
                    step["description"]
                    or step["prompt"]
                    or "No description"
                ),
                "step_type": step["step_type"],
                "response": response,
                "decision": decision,
                "skipped": skipped,
            }
        )

        history.append(current_step_id)

        save_session_findings(
            connection,
            session_id,
            findings,
        )

        current_step_id = following_step_id

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
    )

    print("\n" + "=" * 72)
    print("COPY-READY TICKET SUMMARY")
    print("=" * 72)
    print(summary_text)

    summary_path = write_summary_file(
        ticket_number,
        session_id,
        summary_text,
    )

    print(
        f"\nSession {session_id} saved to the database."
    )
    print(
        f"Ticket summary saved to: {summary_path}"
    )

    input("\nPress Enter to exit...")


def main():
    try:
        database_path = find_database()

        with connect_database(database_path) as connection:
            workflow = choose_workflow(connection)
            ticket_number = get_ticket_number()
            run_workflow(
                connection,
                workflow,
                ticket_number,
            )

    except (
        FileNotFoundError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    except (KeyboardInterrupt, EOFError):
        print("\nTroubleshooter closed.")


if __name__ == "__main__":
    main()
