import pytest

from neops_compose.urls import BadUrl, PublicUrl


def test_parse_https_default_port():
    u = PublicUrl.parse("https://cms.neops.example.com")
    assert (u.scheme, u.host, u.port, u.path) == ("https", "cms.neops.example.com", 443, "")
    assert u.origin == "https://cms.neops.example.com"
    assert str(u) == "https://cms.neops.example.com"
    assert u.is_default_port


def test_parse_with_port_and_path_normalises_trailing_slash():
    u = PublicUrl.parse("http://neops.example.com:8880/engine/")
    assert (u.port, u.path) == (8880, "/engine")
    assert u.origin == "http://neops.example.com:8880"
    assert str(u) == "http://neops.example.com:8880/engine"
    assert not u.is_default_port


@pytest.mark.parametrize(
    "raw",
    [
        "neops.example.com",  # no scheme
        "ftp://neops.example.com",  # bad scheme
        "https://user:pw@neops.example.com",  # userinfo
        "https://neops.example.com/?x=1",  # query
        "https://neops.example.com/#f",  # fragment
        "https://neops example.com",  # space
        'https://neops.example.com/a"b',  # quote (breaks the web client's injected JS)
        "https://",  # no host
    ],
)
def test_parse_rejects(raw):
    with pytest.raises(BadUrl):
        PublicUrl.parse(raw)


def test_same_origin():
    a = PublicUrl.parse("https://neops.example.com")
    b = PublicUrl.parse("https://neops.example.com/engine")
    c = PublicUrl.parse("https://neops.example.com:8443")
    assert a.same_origin(b) and not a.same_origin(c)


def test_parse_rejects_port_zero():
    with pytest.raises(BadUrl):
        PublicUrl.parse("https://x.example.com:0")


@pytest.mark.parametrize(
    "raw",
    [
        "https://neops.example.com\t/a",
        "https://neops.example.com\r",
        "https://neops.example.com\n",
    ],
)
def test_parse_rejects_control_characters(raw):
    with pytest.raises(BadUrl):
        PublicUrl.parse(raw)
