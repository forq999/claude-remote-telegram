import pytest
from server.bot import parse_start_command, parse_stop_command, resolve_path


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


def test_resolve_path_alias():
    aliases = {"front": "/home/user/proj/frontend"}
    assert resolve_path("front", aliases, "/home/user/proj") == "/home/user/proj/frontend"


def test_resolve_path_absolute_in_allowed():
    """allowed_path로 시작하면 절대경로 그대로"""
    assert resolve_path("/home/user/projects/aaa", {}, "/home/user/projects") == "/home/user/projects/aaa"


def test_resolve_path_absolute_outside():
    """/opt/other도 allowed_path 기준 상대경로로 처리"""
    assert resolve_path("/opt/other", {}, "/home/user/projects") == "/home/user/projects/opt/other"


def test_resolve_path_relative():
    assert resolve_path("myapp", {}, "/home/user/projects") == "/home/user/projects/myapp"
