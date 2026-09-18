"""挑本机的局域网 IPv4。全部注入，零 socket——测试不该碰真的网络。

界面只陈述观察：挑不出地址就说「没读到本机的局域网地址」，多个地址就把它们
列出来让中控自己换。**不猜**是 VPN、不猜是隔离——没有对照组就没有结论。
"""
from app import viewer


def call(routed=None, hostname=(), boom=False):
    def probe():
        if boom:
            raise OSError("no route to host")
        return routed

    return viewer.lan_ipv4(probe=probe, hostname_addrs=lambda: list(hostname))


def test_route_probe_wins():
    out = call(routed="192.168.1.23", hostname=["10.1.1.1"])
    assert out["ip"] == "192.168.1.23"
    assert out["source"] == "route"
    assert out["all"] == ["192.168.1.23", "10.1.1.1"]
    assert out["ambiguous"] is True


def test_unusable_probe_results_fall_back_to_hostname():
    for bad in ("127.0.0.1", "169.254.3.4", "0.0.0.0", "255.255.255.255",
                "224.0.0.1", "not-an-ip", ""):
        out = call(routed=bad, hostname=["192.168.0.5"])
        assert out["ip"] == "192.168.0.5", bad
        assert out["source"] == "hostname", bad


def test_private_ranges_are_ranked_192_then_10_then_172():
    out = call(hostname=["172.16.0.9", "10.1.1.1", "192.168.0.5"])
    assert out["ip"] == "192.168.0.5"
    assert out["all"] == ["192.168.0.5", "10.1.1.1", "172.16.0.9"]


def test_several_interfaces_are_all_reported():
    """Wi-Fi + 有线 + 虚拟机桥：挑一个出码，其余全列出来让中控手工换。"""
    out = call(routed="10.211.55.2",
               hostname=["192.168.1.23", "10.211.55.2", "172.16.5.4"])
    assert out["ip"] == "10.211.55.2"       # probe 的结果排 rank 0
    assert out["ambiguous"] is True
    assert set(out["all"]) == {"10.211.55.2", "192.168.1.23", "172.16.5.4"}


def test_loopback_only_reports_no_address():
    out = call(routed="127.0.0.1", hostname=["127.0.0.1", "::1"])
    assert out == {"ip": None, "all": [], "source": None, "ambiguous": False}


def test_probe_failure_and_empty_hostname():
    assert call(boom=True, hostname=["192.168.1.9"])["ip"] == "192.168.1.9"
    assert call(boom=True, hostname=[])["ip"] is None
    # hostname 那一路也抛异常：整体仍然返回一个完整结构，不外抛
    out = viewer.lan_ipv4(probe=lambda: None,
                          hostname_addrs=lambda: (_ for _ in ()).throw(OSError("x")))
    assert out["ip"] is None and out["all"] == []


def test_ipv6_only_candidates_are_not_usable():
    """v1 不支持 IPv6 URL（要带方括号，且局域网里少见）——挑不出就是挑不出。"""
    out = call(routed="fe80::1", hostname=["2001:db8::1", "::1"])
    assert out["ip"] is None
    assert out["all"] == []


def test_probe_and_hostname_agreeing_is_not_ambiguous():
    """A10：两路给出同一个地址时不该显示「本机有多个网络地址」。"""
    out = call(routed="192.168.1.23", hostname=["192.168.1.23"])
    assert out["all"] == ["192.168.1.23"]
    assert out["ambiguous"] is False


def test_probe_and_hostname_disagreeing_lists_both():
    """A10：两个都是私网地址时，两个都要在 all 里，界面才能让人换着试。"""
    out = call(routed="10.0.0.7", hostname=["192.168.1.23"])
    assert out["ambiguous"] is True
    assert out["all"] == ["10.0.0.7", "192.168.1.23"]
    assert out["ip"] == "10.0.0.7"


def test_hostname_duplicates_are_collapsed():
    out = call(hostname=["192.168.1.23", "192.168.1.23", "192.168.1.23"])
    assert out["all"] == ["192.168.1.23"]
    assert out["ambiguous"] is False


def test_real_probe_does_not_send_anything_and_never_raises():
    """默认实现要能在断网的机器上安静地返回 None（UDP connect 不发包）。"""
    out = viewer._probe_route_ip()
    assert out is None or isinstance(out, str)
