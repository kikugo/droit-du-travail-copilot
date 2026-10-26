from eval.gate import baseline_from, compare


def row(rid, type_, answerable, cit, refused, calc=None):
    scores = {"citation_accuracy": cit, "refused": refused}
    if calc is not None:
        scores["calculator_correct"] = calc
    return {"id": rid, "type": type_, "answerable": answerable, "question": f"q{rid}",
            "answer": f"a{rid}", "scores": scores}  # fmt: skip


def run(rows):
    return {"config": {"code": "x"}, "results": rows}


BASE_ROWS = [
    row("g1", "lookup", True, 1.0, False),
    row("g2", "lookup", True, 1.0, False),
    row("g3", "lookup", True, 0.0, False),
    row("g4", "refusal", False, 1.0, True),
    row("g5", "calculator", True, 1.0, False, calc=1.0),
]
BASELINE = baseline_from(run(BASE_ROWS))


def with_changes(**changes):
    out = []
    for r in BASE_ROWS:
        r = {**r, "scores": {**r["scores"]}}
        r["scores"].update(changes.get(r["id"], {}))
        out.append(r)
    return run(out)


def test_identical_run_passes():
    assert compare(BASELINE, run(BASE_ROWS))[0]


def test_one_citation_regression_is_tolerated_two_fail():
    assert compare(BASELINE, with_changes(g1={"citation_accuracy": 0.0}))[0]
    passed, lines = compare(
        BASELINE, with_changes(g1={"citation_accuracy": 0.0}, g2={"citation_accuracy": 0.0})
    )
    assert not passed and "['g1', 'g2']" in lines[0]


def test_any_refusal_regression_fails():
    passed, lines = compare(BASELINE, with_changes(g4={"refused": False, "citation_accuracy": 0.0}))
    assert not passed and "g4" in lines[1]


def test_any_calculator_regression_fails():
    assert not compare(BASELINE, with_changes(g5={"calculator_correct": 0.0}))[0]


def test_improvements_and_already_failing_rows_never_fail():
    assert compare(BASELINE, with_changes(g3={"citation_accuracy": 1.0}))[0]


def test_changed_row_set_fails_with_a_hint():
    passed, lines = compare(BASELINE, run(BASE_ROWS[:-1]))
    assert not passed and "make baseline" in lines[0]
