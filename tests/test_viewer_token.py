"""同看链接里那把钥匙：生成、比较、以及从 settings.json 读回来时的清洗。"""
import pytest

from app import viewer


def test_new_token_is_unique_and_url_safe():
    tokens = [viewer.new_token() for _ in range(200)]
    assert len(set(tokens)) == 200
    for token in tokens:
        assert len(token) == viewer.TOKEN_LEN
        assert viewer.sanitize_token(token) == token


@pytest.mark.parametrize("given", [
    "",                     # 空
    None,                   # 没给
    123,                    # 不是字符串
    ["a" * 43],             # 更不是
    "a" * 42,               # 差一位
    "a" * 44,               # 多一位
    "üü" * 22,              # 非 ASCII（编码后长度和字符数不一样）
])
def test_token_ok_rejects_everything_that_is_not_the_token(given):
    expected = "a" * viewer.TOKEN_LEN
    assert viewer.token_ok(given, expected) is False


def test_token_ok_accepts_the_real_thing_and_rejects_a_prefix():
    token = viewer.new_token()
    assert viewer.token_ok(token, token) is True
    assert viewer.token_ok(token[:-1], token) is False
    assert viewer.token_ok(token + "x", token) is False
    # 没有 token 时任何输入都不该通过（别让「还没生成」变成「谁都能进」）
    assert viewer.token_ok(token, None) is False
    assert viewer.token_ok(token, "") is False


@pytest.mark.parametrize("bad", ["abc", "", None, 8766, True, "a/b" + "c" * 40,
                                 "a" * 43 + "=", "啊" * 43])
def test_sanitize_token_rejects_hand_edited_junk(bad):
    """settings.json 是用户可以手改的文件：读回来的东西一律当不可信。"""
    assert viewer.sanitize_token(bad) is None


@pytest.mark.parametrize("bad", [0, "x", 99999, 80, 1023, None, True, False, 65536])
def test_sanitize_port_rejects_out_of_range(bad):
    assert viewer.sanitize_port(bad) is None


@pytest.mark.parametrize("good,want", [(8766, 8766), ("8766", 8766), (1024, 1024),
                                       (65535, 65535)])
def test_sanitize_port_accepts_usable_ports(good, want):
    assert viewer.sanitize_port(good) == want


def test_viewer_url_keeps_the_token_in_the_fragment_only():
    """片段不会进服务端，也不会进 Referer——URL 里唯一能放钥匙的地方。"""
    token = viewer.new_token()
    url = viewer.viewer_url("192.168.1.23", 8766, token)
    assert url == "http://192.168.1.23:8766/#k=" + token
    assert "?" not in url
    head, _, fragment = url.partition("#")
    assert token not in head
    assert fragment == "k=" + token


def test_viewer_url_without_an_address_raises():
    with pytest.raises(viewer.ViewerError):
        viewer.viewer_url(None, 8766, viewer.new_token())
    with pytest.raises(viewer.ViewerError):
        viewer.viewer_url("", 8766, viewer.new_token())


def test_pick_ports_prefers_the_saved_one_then_walks_up():
    """存过的端口排第一：发出去的二维码要尽量还能用。"""
    assert viewer.pick_ports(8765, 8766)[0] == 8766
    assert viewer.pick_ports(8765, None) == [8766, 8767, 8768, 8769, 8770]
    # 存的等于控制端口、或是垃圾值：忽略它
    assert viewer.pick_ports(8765, 8765) == [8766, 8767, 8768, 8769, 8770]
    assert viewer.pick_ports(8765, "x") == [8766, 8767, 8768, 8769, 8770]
    # 存的在候选区间里：只出现一次
    ports = viewer.pick_ports(8765, 8768)
    assert ports[0] == 8768 and ports.count(8768) == 1
    assert len(ports) == len(set(ports))
    # 控制端口自己漂移过：候选跟着走，不写死 8766
    assert viewer.pick_ports(8770, None)[0] == 8771
