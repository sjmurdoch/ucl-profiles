from pathlib import Path

import pytest

import logging
import urllib.error

from ucl_profiles import add_profile_details, api, render
from ucl_profiles.api import iter_department_users, publication_count, search_body, user_email, user_tags
from ucl_profiles.check import all_titles_match, check_users, has_any_tag, unknown_tags
from ucl_profiles.config import DEFAULT_AREA_TAGS, DEFAULT_REQUIRED_TAG, DEFAULT_PGTA_POSITIONS, DEFAULT_PS_POSITIONS, ConfigError, Settings, load_settings

DEPT = "Dept of Computer Science"


def user(n, tags=None, dept=DEPT, position="Lecturer"):
    record = {
        "firstNameLastName": f"Person {n}",
        "discoveryUrlId": f"{n}-person",
        "objectId": n,
        "positions": [{"position": position, "department": dept}, {"position": "Fellow", "department": "Elsewhere"}],
    }
    if tags is not None:
        record["tags"] = {"explicit": [{"value": t} for t in tags]}
    return record


class FakeServer:
    def __init__(self, users, cap=100):
        self.users, self.cap, self.calls = users, cap, []

    def __call__(self, url, body):
        self.calls.append((url, body))
        start = body["pagination"]["startFrom"]
        n = min(body["pagination"]["perPage"], self.cap)
        return {"pagination": {"total": len(self.users)}, "resource": self.users[start : start + n]}


def test_pagination_collects_every_user():
    server = FakeServer([user(i) for i in range(7)], cap=3)
    got = list(iter_department_users(DEPT, "https://x/", page_size=500, post=server))
    assert [u["discoveryUrlId"] for u in got] == [f"{i}-person" for i in range(7)]
    assert server.calls[0][0] == "https://x/api/users"
    assert [c[1]["pagination"]["startFrom"] for c in server.calls] == [0, 3, 6]
    assert server.calls[0][1]["pagination"]["perPage"] == 100


def test_max_pages_stops_early():
    server = FakeServer([user(i) for i in range(7)], cap=3)
    got = list(iter_department_users(DEPT, page_size=3, post=server, max_pages=1))
    assert len(got) == 3 and len(server.calls) == 1
    assert len(list(iter_department_users(DEPT, page_size=3, post=FakeServer([user(i) for i in range(7)]), max_pages=0))) == 7


def test_max_pages_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings([], env={}).max_pages == 0
    assert load_settings(["--max-pages", "1"], env={}).max_pages == 1
    assert load_settings([], env={"UCL_PROFILES_MAX_PAGES": "2"}).max_pages == 2


def test_pagination_stops_on_empty_page():
    calls = []

    def post(url, body):
        calls.append(body)
        return {"pagination": {"total": 999}, "resource": []}

    assert list(iter_department_users(DEPT, post=post)) == []
    assert len(calls) == 1


def test_search_body_filters_department():
    dept_filter = [f for f in search_body(DEPT, 0, 10)["filters"] if f["name"] == "department"][0]
    assert dept_filter["values"] == [DEPT] and dept_filter["useValuesToFilter"]


def test_user_tags_handles_missing_and_multiple_kinds():
    assert user_tags(user(1)) == []
    assert user_tags({"tags": {"explicit": [{"value": "A"}], "other": [{"value": "B"}]}}) == ["A", "B"]


@pytest.mark.parametrize(
    "tags,ok",
    [
        (["Computational logic and formal languages"], True),  # site capitalisation
        (["  machine   learning "], True),
        (["Software engineering"], False),
        (["Information and Computing Sciences"], False),  # the required tag isn't an area tag
        ([], False),
    ],
)
def test_has_area_tag(tags, ok):
    assert has_any_tag(tags, DEFAULT_AREA_TAGS) is ok


@pytest.mark.parametrize(
    "tags,missing_required,missing_area",
    [
        (["Information and computing sciences", "Machine learning"], False, False),
        (["Machine learning"], True, False),
        (["Information and computing sciences"], False, True),
        (["Information and computing sciences", "Software engineering"], False, True),
        ([], True, True),
    ],
)
def test_two_tag_checks(tags, missing_required, missing_area):
    r = check_users([user(1, tags)], DEFAULT_REQUIRED_TAG, DEFAULT_AREA_TAGS, DEPT)[0]
    assert (r.missing_required_tag, r.missing_area_tag) == (missing_required, missing_area)
    assert r.compliant is not (missing_required or missing_area)


def test_empty_required_tag_is_never_missing():
    assert not check_users([user(1)], "", DEFAULT_AREA_TAGS, DEPT)[0].missing_required_tag


def test_check_users_reports_department_positions():
    results = check_users([user(1, ["Machine learning"]), user(2)], DEFAULT_REQUIRED_TAG, DEFAULT_AREA_TAGS, DEPT, "https://x")
    assert [(r.name, r.missing_area_tag) for r in results] == [("Person 1", False), ("Person 2", True)]
    assert results[0].positions == ["Lecturer"]
    assert results[1].url == "https://x/2-person"


def test_unknown_tags():
    assert unknown_tags(["Machine learning", "Machne learning"], ["machine learning"]) == ["Machne learning"]


def test_render_formats():
    results = check_users([user(1, ["A, b", "C"])], "R", ["C"], DEPT)
    assert render(results, "csv").splitlines()[1] == 'Person 1,,Yes,No,Yes,0,No,No,Lecturer,"A, b; C",https://profiles.ucl.ac.uk/1-person'
    assert '"missing_required_tag": true' in render(results, "json")
    assert render([], "csv").startswith("Name,Email,Missing InfComS,Missing area tag,Missing any tag,Publications,")


def test_settings_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings([], env={}) == Settings()

    cfg = tmp_path / "c.toml"
    cfg.write_text('department = "From file"\npage_size = 10\nrequired_tag = "R"\narea_tags = ["X"]\nemails = false\n')
    s = load_settings(["--config", str(cfg)], env={})
    assert (s.department, s.page_size, s.required_tag, s.area_tags, s.emails) == ("From file", 10, "R", ["X"], False)

    env = {"UCL_PROFILES_CONFIG": str(cfg), "UCL_PROFILES_DEPARTMENT": "From env", "UCL_PROFILES_AREA_TAGS": "A, b; C", "UCL_PROFILES_EMAILS": "yes"}
    s = load_settings([], env=env)
    assert (s.department, s.area_tags, s.emails, s.page_size) == ("From env", ["A, b", "C"], True, 10)

    s = load_settings(["--department", "From CLI", "--area-tag", "P", "--area-tag", "Q", "--required-tag", "T", "--format", "csv"], env=env)
    assert (s.department, s.area_tags, s.required_tag, s.format) == ("From CLI", ["P", "Q"], "T", "csv")


def test_default_config_file_is_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ucl-profiles.toml").write_text('format = "json"\n')
    assert load_settings([], env={}).format == "json"


def test_example_config_matches_defaults(monkeypatch):
    example = Path(__file__).parent.parent / "ucl-profiles.example.toml"
    assert load_settings(["--config", str(example)], env={}) == Settings()


@pytest.mark.parametrize(
    "contents,env",
    [
        ("nonsense = 1\n", {}),
        ('format = "xml"\n', {}),
        ("page_size = [1]\n", {}),
        ("", {"UCL_PROFILES_PAGE_SIZE": "lots"}),
        ("", {"UCL_PROFILES_EMAILS": "maybe"}),
    ],
)
def test_bad_settings(tmp_path, contents, env):
    cfg = tmp_path / "c.toml"
    cfg.write_text(contents)
    with pytest.raises(ConfigError):
        load_settings(["--config", str(cfg)], env=env)


def test_user_email():
    assert user_email({"emailAddress": {"address": "a@ucl.ac.uk"}}) == "a@ucl.ac.uk"
    assert user_email({}) == ""
    others = [{"typeDisplayName": "Work"}, {"address": "b@cs.ucl.ac.uk", "typeDisplayName": "Work"}, {"address": "c@ucl.ac.uk"}]
    assert user_email({"emailAddress": None, "otherEmailAddresses": others}) == "b@cs.ucl.ac.uk"
    assert user_email({"emailAddress": {"address": "a@ucl.ac.uk"}, "otherEmailAddresses": others}) == "a@ucl.ac.uk"
    assert user_email({"emailAddress": None, "otherEmailAddresses": None}) == ""


@pytest.mark.parametrize("fake", ["nobody@ucl.ac.uk", "Nobody@UCL.ac.uk", " nobody@ucl.ac.uk "])
def test_placeholder_email_is_absent(fake):
    assert user_email({"emailAddress": {"address": fake}}) == ""
    assert user_email({"emailAddress": {"address": fake}, "otherEmailAddresses": [{"address": "real@ucl.ac.uk"}]}) == "real@ucl.ac.uk"
    assert user_email({"emailAddress": None, "otherEmailAddresses": [{"address": fake}, {"address": "x@ucl.ac.uk"}]}) == "x@ucl.ac.uk"


def test_add_profile_details_fetches_profiles_and_tolerates_failures(caplog):
    urls = []
    appointments = [
        {"position": "Student", "endDate": {"year": 2020}},
        {"position": "Postgraduate Teaching Assistant"},
    ]

    def get(url):
        urls.append(url)
        if url.endswith("/2"):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return {"emailAddress": {"address": f"p{url[-1]}@ucl.ac.uk"}, "institutionalAppointments": appointments}

    users = [user(1), user(2), user(3, position="")]
    results = check_users(users, DEFAULT_REQUIRED_TAG, DEFAULT_AREA_TAGS, DEPT, "https://x")
    got = add_profile_details(results, "https://x", get=get)
    assert urls == ["https://x/api/users/1", "https://x/api/users/2", "https://x/api/users/3"]
    assert [r.email for r in got] == ["p1@ucl.ac.uk", "", "p3@ucl.ac.uk"]
    # Appointment titles are used only when the department position has none, and only current ones.
    assert [r.positions for r in got] == [["Lecturer"], ["Lecturer"], ["Postgraduate Teaching Assistant"]]
    assert "could not fetch profile for Person 2" in caplog.text
    assert "1 of 3 profiles could not be fetched" in caplog.text
    assert add_profile_details(results[:1], "https://x", emails=False, get=get)[0].email == ""
    assert "p1@ucl.ac.uk" in render(got, "csv")


def test_emails_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings([], env={}).emails is True
    assert load_settings(["--no-emails"], env={}).emails is False
    assert load_settings([], env={"UCL_PROFILES_EMAILS": "false"}).emails is False
    assert load_settings(["--emails"], env={"UCL_PROFILES_EMAILS": "false"}).emails is True


@pytest.mark.parametrize(
    "positions,pgta,ps",
    [
        (["PGTA"], True, False),
        (["PGTAs"], True, False),
        (["Post Graduate Teaching Assistants"], True, False),
        (["Postgraduate Teaching Assistant"], True, False),
        (["Section Manager"], False, True),
        (["PGR Administration Manager"], False, True),  # "PGR" isn't PGTA
        (["Head of Operations"], False, True),
        (["Strategic Alliances Director"], False, True),
        (["Communications & Events Manager"], False, True),
        (["Lecturer", "Section Manager"], False, False),  # any other title means No
        (["PGTA", "Section Manager"], False, False),
        (["Associate Professor (Teaching)"], False, False),
        (["Research Technician for MSc System Engineering for the Internet of Things"], False, False),
        (["Student"], False, False),
        (["Honorary Professor"], False, False),
        ([], False, False),
        ([""], False, False),
    ],
)
def test_default_title_patterns(positions, pgta, ps):
    assert all_titles_match(positions, DEFAULT_PGTA_POSITIONS) is pgta
    assert all_titles_match(positions, DEFAULT_PS_POSITIONS) is ps


def test_no_patterns_flags_nobody():
    assert not all_titles_match(["PGTA"], [])


@pytest.mark.parametrize("name", ["pgta", "ps"])
def test_title_pattern_settings(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    key, flag = f"{name}_positions", f"--{name}-position"
    assert getattr(load_settings([], env={}), key) == {"pgta": DEFAULT_PGTA_POSITIONS, "ps": DEFAULT_PS_POSITIONS}[name]
    assert getattr(load_settings([], env={f"UCL_PROFILES_{key.upper()}": ""}), key) == []
    assert getattr(load_settings([flag, "^x$", flag, "y"], env={}), key) == ["^x$", "y"]
    with pytest.raises(ConfigError):
        load_settings([flag, "("], env={})


def test_publication_count():
    assert publication_count({}) == 0
    assert publication_count({"linkedObjectIds": {}}) == 0
    assert publication_count({"linkedObjectIds": {"publications": [1, 2, 3], "users": [4]}}) == 3
    assert check_users([{**user(1), "linkedObjectIds": {"publications": [7]}}], "R", ["X"], DEPT)[0].publications == 1


def test_xlsx_on_stdout_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError):
        load_settings(["--output", "-"], env={})
    assert load_settings(["--output", "-", "--format", "csv"], env={}).output == "-"


USERS = [
    {**user(2, position="Lecturer"), "linkedObjectIds": {"publications": [1]}},
    user(1, position="PGTA"),  # no publications
    {**user(3, ["Machine learning"]), "linkedObjectIds": {"publications": [1, 2]}},
    user(4, position=""),  # title only in profile appointments
    user(5, position="Section Manager"),
    user(6, ["Information and computing sciences", "Machine learning"]),  # set up properly
]


@pytest.fixture
def fake_site(monkeypatch):
    import ucl_profiles

    fetched = []

    def get_user(oid, base, get):
        fetched.append(oid)
        appointments = [{"position": "Postgraduate Teaching Assistant"}] if oid == 4 else []
        return {"emailAddress": {"address": f"p{oid}@ucl.ac.uk"}, "institutionalAppointments": appointments}

    monkeypatch.setattr(ucl_profiles, "iter_department_users", lambda *a, **kw: iter(USERS))
    monkeypatch.setattr(ucl_profiles, "get_user", get_user)
    return fetched


def test_run_exports_everyone_unfiltered(fake_site, capsys, caplog):
    import ucl_profiles

    caplog.set_level(logging.INFO)

    assert ucl_profiles.run(Settings(format="csv", output="-")) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1:] == [
        "Person 1,p1@ucl.ac.uk,Yes,Yes,Yes,0,Yes,No,PGTA,,https://profiles.ucl.ac.uk/1-person",
        "Person 2,p2@ucl.ac.uk,Yes,Yes,Yes,1,No,No,Lecturer,,https://profiles.ucl.ac.uk/2-person",
        "Person 3,p3@ucl.ac.uk,Yes,No,Yes,2,No,No,Lecturer,Machine learning,https://profiles.ucl.ac.uk/3-person",
        "Person 4,p4@ucl.ac.uk,Yes,Yes,Yes,0,Yes,No,Postgraduate Teaching Assistant,,https://profiles.ucl.ac.uk/4-person",
        "Person 5,p5@ucl.ac.uk,Yes,Yes,Yes,0,No,Yes,Section Manager,,https://profiles.ucl.ac.uk/5-person",
        "Person 6,p6@ucl.ac.uk,No,No,No,0,No,No,Lecturer,Information and computing sciences; Machine learning,https://profiles.ucl.ac.uk/6-person",
    ]
    assert sorted(fake_site) == [1, 2, 3, 4, 5, 6]
    assert err == ""  # log records go through logging, not straight to stderr
    assert "wrote 6 people; 5 missing the required tag, 4 missing an area tag" in caplog.text


def test_run_writes_excel_table(fake_site, tmp_path, monkeypatch):
    import ucl_profiles
    from openpyxl import load_workbook

    monkeypatch.chdir(tmp_path)
    assert ucl_profiles.run(Settings()) == 0
    wb = load_workbook(tmp_path / "ucl-profiles.xlsx")
    ws = wb["Staff"]
    assert list(ws.tables) == ["Staff"]
    assert ws.tables["Staff"].ref == "A1:K7"
    assert [c.value for c in ws[1]][:8] == [
        "Name", "Email", "Missing InfComS", "Missing area tag", "Missing any tag", "Publications", "PGTA", "Professional services",
    ]
    assert [c.value for c in ws[2]][:8] == ["Person 1", "p1@ucl.ac.uk", "Yes", "Yes", "Yes", 0, "Yes", "No"]
    assert [c.value for c in ws[4]][2:5] == ["Yes", "No", "Yes"]  # Person 3: only the area tag
    assert [c.value for c in ws[6]][6:8] == ["No", "Yes"]
    assert [c.value for c in ws[7]][2:5] == ["No", "No", "No"]  # Person 6: set up properly
    for row in range(2, 8):
        cell = ws.cell(row, 6)
        assert cell.data_type == "n" and isinstance(cell.value, int) and cell.number_format == "0"
    assert ws["K2"].hyperlink.target == "https://profiles.ucl.ac.uk/1-person"
    assert ws.freeze_panes == "B2"
    about = [c.value for row in wb["About"].iter_rows() for c in row]
    assert "Machine learning" in about and "Dept of Computer Science" in about
    assert any("Information and Computing Sciences" in str(v) for v in about)


def test_empty_excel_export(tmp_path):
    from openpyxl import load_workbook

    from ucl_profiles.excel import write_xlsx

    write_xlsx(str(tmp_path / "e.xlsx"), [], DEPT, "R", ["X"], [], [])
    assert load_workbook(tmp_path / "e.xlsx")["Staff"].tables["Staff"].ref == "A1:K2"


def test_with_retries_backs_off_exponentially_then_succeeds(caplog):
    calls, sleeps = [], []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return {"ok": True}

    got = api.with_retries(flaky, retries=3, backoff=1.0, sleep=sleeps.append)("https://x/api/users/1")
    assert got == {"ok": True}
    assert len(calls) == 3
    # 1s then 2s, each with up to 50% jitter.
    assert 1.0 <= sleeps[0] <= 1.5 and 2.0 <= sleeps[1] <= 3.0
    assert "https://x/api/users/1 failed" in caplog.text
    assert "retry 1 of 3" in caplog.text and "retry 2 of 3" in caplog.text


def test_with_retries_gives_up_after_limit():
    sleeps = []

    def always_times_out(url):
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        api.with_retries(always_times_out, retries=2, sleep=sleeps.append)("u")
    assert len(sleeps) == 2


def test_with_retries_does_not_retry_client_errors():
    calls = []

    def not_found(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    with pytest.raises(urllib.error.HTTPError):
        api.with_retries(not_found, retries=3, sleep=lambda s: None)("u")
    assert len(calls) == 1


def test_is_transient():
    assert api.is_transient(urllib.error.HTTPError("u", 503, "x", None, None))
    assert api.is_transient(urllib.error.HTTPError("u", 429, "x", None, None))
    assert not api.is_transient(urllib.error.HTTPError("u", 404, "x", None, None))
    assert api.is_transient(urllib.error.URLError("timed out"))
    assert api.is_transient(TimeoutError())


def test_timeout_and_retries_settings():
    s = load_settings(["--timeout", "5", "--retries", "1"], env={})
    assert (s.timeout, s.retries) == (5, 1)
    s = load_settings([], env={"UCL_PROFILES_TIMEOUT": "20", "UCL_PROFILES_RETRIES": "0"})
    assert (s.timeout, s.retries) == (20, 0)


def test_log_level_setting():
    assert load_settings([], env={}).log_level == "INFO"
    assert load_settings(["-v"], env={}).log_level == "DEBUG"
    assert load_settings(["-q"], env={}).log_level == "WARNING"
    assert load_settings(["--log-level", "error"], env={}).log_level == "ERROR"
    assert load_settings([], env={"UCL_PROFILES_LOG_LEVEL": "debug"}).log_level == "DEBUG"
    with pytest.raises(ConfigError, match="log_level"):
        load_settings([], env={"UCL_PROFILES_LOG_LEVEL": "loud"})


def test_settings_sources_describe_where_values_came_from(tmp_path):
    from ucl_profiles.config import load_settings_with_sources

    config = tmp_path / "c.toml"
    config.write_text('department = "X"\n')
    _, sources = load_settings_with_sources(["--config", str(config), "-v"], env={"UCL_PROFILES_RETRIES": "1"})
    assert sources == [f"{config}: department", "environment: retries", "command line: log_level"]


def test_debug_logging_explains_tag_checks(caplog):
    caplog.set_level(logging.DEBUG, logger="ucl_profiles.check")
    check_users([user(1)], DEFAULT_REQUIRED_TAG, DEFAULT_AREA_TAGS, DEPT, "https://x")
    assert "Person 1: missing required tag and area tag" in caplog.text


def test_main_reports_config_errors_via_logging(caplog, monkeypatch):
    import ucl_profiles

    monkeypatch.delenv("UCL_PROFILES_LOG_LEVEL", raising=False)
    assert ucl_profiles.main(["--format", "xlsx", "--output", "-"]) == 2
    assert any(r.levelname == "ERROR" and "stdout" in r.getMessage() for r in caplog.records)


def test_tag_typo_warning_only_for_complete_runs(fake_site, caplog, tmp_path):
    import ucl_profiles

    ucl_profiles.run(Settings(format="csv", output=str(tmp_path / "a.csv"), area_tags=["No such tag", "Machine learning"]))
    assert "has the tag 'No such tag' (typo?)" in caplog.text
    caplog.clear()
    ucl_profiles.run(Settings(format="csv", output=str(tmp_path / "b.csv"), area_tags=["No such tag"], max_pages=1))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
