"""Unit tests for the plain-HTTP Google AI mode transport."""
import json

import pytest

from arena import google_http as gh

COOKIE = ("NID=abc; SID=xyz; HSID=h; SSID=s; APISID=a; SAPISID=p; "
          "__Secure-1PSID=one; __Secure-3PSID=two; SEARCH_SAMESITE=CgQIt5gB")


class FakeResponse:
    def __init__(self, text="", status_code=200, url=""):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeSession:
    """Records the requests and replays canned bodies in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        if not self.responses:
            return FakeResponse("", 500)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SEARCH_PAGE = """
<html><head><title>Google</title></head><body>
<div data-ei="EI123" data-garc="GARC123" data-lro-signature="SIG"
     data-lro-token="TOKEN" data-srtst="SRT:123"
     data-xsrf-folif-token="FIF:1" data-xsrf-folwr-token="FWR:1"></div>
</body></html>
"""

ANSWER_BODY = """
<div data-sn-op="2"><div class="n6owBd awi2gc"><mark><strong><span>Нейропластичность</span>
</strong></mark><strong>.</strong> — это свойство мозга.<span class="dNwgB">
<span class="QNca8b">euromed.ru</span><span class="OkUHJe">+3</span></span></div></div>
<div><div class="ZLzG0d" role="heading" aria-level="3">Как это работает</div></div>
<div class="n6owBd awi2gc">Мозг перестраивает связи.</div>
<ul class="XSq4R KI3fJ"><li class="Z1qcYe"><span>Первый пункт</span></li>
<li class="Z1qcYe"><span>Второй <em>пункт</em></span></li></ul>
<table><tr><th>Параметр</th><th>Значение</th></tr>
<tr><td>Скорость</td><td>Высокая</td></tr></table>
<div class="AHmQrc">Хотите, подберу упражнения?</div>
<div class="IQTBxd notranslate">euromed.ru</div>
<!--["Нейропластичность - Википедия",null,"https://encrypted-tbn0.gstatic.com/faviconV2?url=ru","https://ru.wikipedia.org",["Википедия"],"https://ru.wikipedia.org/wiki/Нейропластичность"]-->
<!--["Про мозг",null,"https://encrypted-tbn0.gstatic.com/faviconV2?url=euromed","https://euromed.ru",["euromed.ru"],"https://euromed.ru/news/brain"]-->
<!--["Про мозг ещё раз",null,"https://encrypted-tbn0.gstatic.com/faviconV2?url=euromed","https://euromed.ru",["euromed.ru"],"https://euromed.ru/news/brain"]-->
"""


# -- cookies ---------------------------------------------------------------
def test_cookie_pairs_and_auth():
    pairs = gh.cookie_pairs(COOKIE)
    assert len(pairs) == 9
    assert gh.has_auth_cookie(COOKIE) is True
    assert gh.has_auth_cookie("NID=1; SEARCH_SAMESITE=x") is False
    assert gh.cookie_pairs("garbage;; =x; ok=1") == [("ok", "1")]


def test_clean_cookie_header_drops_noise():
    cleaned = gh._clean_cookie_header("  SID=xyz ;  junk  ; A=1 ")
    assert cleaned == "SID=xyz; A=1"


def test_client_refuses_cookies_without_a_session():
    with pytest.raises(gh.GoogleHTTPError) as err:
        gh.GoogleHTTP("")
    assert err.value.code == "no_cookie"
    with pytest.raises(gh.GoogleHTTPError) as err:
        gh.GoogleHTTP("NID=1; AEC=2")
    assert err.value.code == "no_auth_cookie"


# -- tokens ----------------------------------------------------------------
def test_page_tokens_reads_the_six_attributes():
    tokens = gh.page_tokens(SEARCH_PAGE)
    assert tokens == {"ei": "EI123", "garc": "GARC123", "mlro": "TOKEN",
                      "mlros": "SIG", "srtst": "SRT:123", "xsrf": "FWR:1"}


def test_folwr_params_keep_the_async_header():
    params = gh.folwr_params("q", gh.page_tokens(SEARCH_PAGE), "ru")
    assert params["async"] == "_fmt:adl,_xsrf:FWR:1"
    assert params["q"] == "q" and params["udm"] == "50"


# -- parsing ---------------------------------------------------------------
def test_answer_markdown_structure():
    md = gh.answer_markdown(ANSWER_BODY)
    assert md.startswith("**Нейропластичность.**")
    assert "[euromed.ru]" in md
    assert "## Как это работает" in md
    assert "- Первый пункт" in md
    assert "- Второй _пункт_" in md
    assert "| Параметр | Значение |" in md
    assert "| Скорость | Высокая |" in md
    assert md.rstrip().endswith("*Хотите, подберу упражнения?*")
    assert "****" not in md


def test_answer_markdown_stops_at_the_sources_card():
    md = gh.answer_markdown(ANSWER_BODY)
    assert "euromed.ru\n" not in md.split("*Хотите")[-1]


def test_answer_sources_are_unique_and_skip_gstatic():
    sources = gh.answer_sources(ANSWER_BODY)
    assert [s["url"] for s in sources] == [
        "https://ru.wikipedia.org/wiki/Нейропластичность",
        "https://euromed.ru/news/brain",
    ]
    assert sources[0]["domain"] == "Википедия"


def test_answer_markdown_empty_body():
    assert gh.answer_markdown("") == ""


# -- one turn --------------------------------------------------------------
def test_ask_does_two_requests_and_returns_text():
    session = FakeSession([FakeResponse(SEARCH_PAGE), FakeResponse(ANSWER_BODY)])
    client = gh.GoogleHTTP(COOKIE, hl="ru", session=session)
    result = client.ask("  что   такое   нейропластичность ")
    assert result["query"] == "что такое нейропластичность"
    assert result["text"].startswith("**Нейропластичность.**")
    assert len(result["sources"]) == 2
    assert len(session.calls) == 2
    search, folwr = session.calls
    assert search["url"] == gh.SEARCH_URL
    assert search["params"]["q"] == "что такое нейропластичность"
    assert search["headers"]["Cookie"].startswith("NID=abc")
    assert folwr["url"] == gh.FOLWR_URL
    assert folwr["params"]["srtst"] == "SRT:123"
    assert folwr["params"]["mlro"] == "TOKEN"
    assert folwr["params"]["ei"] == "EI123"


def test_ask_reports_rate_limit_and_human_check():
    client = gh.GoogleHTTP(COOKIE, session=FakeSession(
        [FakeResponse("<html><body>enablejs redirect</body></html>")]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask("x")
    assert err.value.code == "throttled"

    client = gh.GoogleHTTP(COOKIE, session=FakeSession(
        [FakeResponse("<html><body>Мы зарегистрировали подозрительный трафик</body></html>")]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask("x")
    assert err.value.code == "human_check"


def test_ask_reports_signed_out_and_no_answer():
    login = FakeResponse('<a href="https://accounts.google.com/ServiceLogin?x">Войти</a>')
    client = gh.GoogleHTTP(COOKIE, session=FakeSession([login]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask("x")
    assert err.value.code == "signed_out"

    client = gh.GoogleHTTP(COOKIE, session=FakeSession(
        [FakeResponse(SEARCH_PAGE), FakeResponse("<div class='IQTBxd'>источники</div>")]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask("x")
    assert err.value.code == "no_answer"


def test_ask_wraps_network_errors():
    client = gh.GoogleHTTP(COOKIE, session=FakeSession([OSError("boom")]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask("x")
    assert err.value.code == "network"


def test_ask_truncates_endless_queries():
    session = FakeSession([FakeResponse(SEARCH_PAGE), FakeResponse(ANSWER_BODY)])
    client = gh.GoogleHTTP(COOKIE, session=session)
    client.ask("слово " * 1000)
    assert len(session.calls[0]["params"]["q"]) <= gh.MAX_QUERY


# -- account ---------------------------------------------------------------
def test_account_reads_the_google_label():
    home = ('<a aria-label="Google Account: Ольга Митрофанова '
            '(olga@gmail.com)" href="https://accounts.google.com/SignOutOptions?x">x</a>')
    client = gh.GoogleHTTP(COOKIE, session=FakeSession([FakeResponse(home)]))
    info = client.account()
    assert info["signed_in"] is True
    assert info["email"] == "olga@gmail.com"
    assert "Ольга" in info["name"]


def test_account_reads_the_russian_label():
    """Google localises the account button; the email is still in it."""
    home = ('<a class="gb_C" aria-label="Аккаунт Google: Ольга Митрофанова  &#10;'
            '(22mitroshka22@gmail.com)" href="https://accounts.google.com/'
            'SignOutOptions?hl=ru">x</a>')
    client = gh.GoogleHTTP(COOKIE, session=FakeSession([FakeResponse(home)]))
    info = client.account()
    assert info["signed_in"] is True
    assert info["email"] == "22mitroshka22@gmail.com"
    assert info["name"] == "Ольга Митрофанова"


def test_account_signed_out():
    client = gh.GoogleHTTP(COOKIE, session=FakeSession(
        [FakeResponse('<a href="https://accounts.google.com/ServiceLogin">Войти</a>')]))
    info = client.account()
    assert info["signed_in"] is False
    assert info["email"] == ""


# -- history ---------------------------------------------------------------
def test_threads_build_reopen_urls():
    payload = json.dumps([[[["AUtExfThread1", "mtid-1"],
                            "что такое нейропластичность", None, 0],
                           [["AUtExfThread2", "mtid-2"], "второй тред", None, 0],
                           [["AUtExfThread1", "mtid-1"], "дубль", None, 0]]])
    body = ")]}'\n\n" + payload
    client = gh.GoogleHTTP(COOKIE, hl="ru", session=FakeSession([FakeResponse(body)]))
    items = client.threads(limit=10)
    assert len(items) == 2
    assert items[0]["title"] == "что такое нейропластичность"
    assert "mtid=mtid-1" in items[0]["url"]
    assert "udm=50" in items[0]["url"]


def test_threads_reports_broken_payload():
    client = gh.GoogleHTTP(COOKIE, session=FakeSession([FakeResponse("не json")]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.threads()
    assert err.value.code == "no_threads"


def test_ask_reports_google_declining_a_tool_protocol():
    """A prompt that is an instruction, not a question, gets Google's refusal."""
    body = ("<div class='n6owBd'>Не удается дать ответ на этот поисковый запрос. "
            "Попробуйте спросить что-нибудь другое.</div>")
    client = gh.GoogleHTTP(COOKIE, session=FakeSession(
        [FakeResponse(SEARCH_PAGE), FakeResponse(body)]))
    with pytest.raises(gh.GoogleHTTPError) as err:
        client.ask('Отвечай только JSON: {"tool":"list_files"}')
    assert err.value.code == "declined"
