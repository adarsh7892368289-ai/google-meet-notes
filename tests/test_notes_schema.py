from app.schemas.notes import ActionItem, NotesContent


def test_notes_content_parses():
    nc = NotesContent(
        summary="We discussed the roadmap.",
        decisions=["Ship X in Q3"],
        action_items=[ActionItem(who="Alice", what="Draft the spec")],
    )
    assert nc.summary.startswith("We discussed")
    assert nc.decisions == ["Ship X in Q3"]
    assert nc.action_items[0].who == "Alice"


def test_notes_content_defaults_empty_lists():
    nc = NotesContent(summary="Short call, nothing decided.")
    assert nc.decisions == []
    assert nc.action_items == []
