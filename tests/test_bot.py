import pytest
from server.bot import parse_start_command, parse_stop_command, resolve_alias


def test_parse_start_with_path():
    server, path = parse_start_command("/run server-a /home/user/proj")
    assert server == "server-a"
    assert path == "/home/user/proj"


def test_parse_start_with_alias():
    server, alias_or_path = parse_start_command("/run server-a front")
    assert server == "server-a"
    assert alias_or_path == "front"


def test_parse_start_missing_args():
    result = parse_start_command("/run")
    assert result is None


def test_parse_stop_with_path():
    server, path = parse_stop_command("/stop server-a /home/user/proj")
    assert server == "server-a"
    assert path == "/home/user/proj"


def test_parse_stop_all():
    server, path = parse_stop_command("/stop server-a")
    assert server == "server-a"
    assert path is None


def test_resolve_alias_found():
    aliases = {"front": "/home/user/proj/frontend"}
    assert resolve_alias("front", aliases) == "/home/user/proj/frontend"


def test_resolve_alias_is_path():
    aliases = {"front": "/home/user/proj/frontend"}
    assert resolve_alias("/home/other", aliases) == "/home/other"
