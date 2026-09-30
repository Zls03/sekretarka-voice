"""Schematy narzędzi (function calling) widziane przez model — nazwa, opis i parametry
decydują o tym, kiedy i jak model woła narzędzie, więc muszą zostać nietknięte."""

from conftest import _project_modules, assert_golden, make_booking_tenant, make_tenant


def _find(name):
    import bot_gemini_test  # noqa: F401

    for module in _project_modules():
        if name in vars(module) and getattr(vars(module)[name], "__module__", "") == module.__name__:
            return vars(module)[name]
    raise AssertionError(f"Nie znaleziono definicji {name}")


def _schema(tool):
    return {
        "name": tool.name,
        "description": tool.description,
        "properties": tool.properties,
        "required": tool.required,
    }


def test_tool_schemas():
    tenant = make_booking_tenant(transfer_enabled=1, transfer_number="+48500600700")
    state = {"ended": False}
    task_box = {"task": None}
    context_box = {"context": None}
    tools = {
        "contact_owner": _find("build_contact_owner_tool")(make_tenant(), "+48600", task_box, state),
        "contact_owner_with_transfer": _find("build_contact_owner_tool")(
            make_tenant(),
            "+48600",
            task_box,
            state,
            has_transfer_tool=True,
        ),
        "end_conversation": _find("build_end_conversation_tool")(task_box, state),
        "transfer_to_owner": _find("build_transfer_tool")(
            tenant,
            "uuid-1",
            state,
            "https://api-eu-3.vonage.com",
            caller_phone="+48600",
            host="bot.test",
        ),
        "book_appointment": _find("build_book_appointment_tool")(tenant, "+48600", state, context_box),
        "manage_booking": _find("build_manage_booking_tool")(tenant, "+48600", state),
    }
    assert_golden("tool_schemas.json", {name: _schema(tool) for name, tool in tools.items()})
