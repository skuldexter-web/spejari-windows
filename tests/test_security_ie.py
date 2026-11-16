from spejari.security import CAP_IBSS, CAP_PRIVACY, derive_security, parse_ies
from spejari.sim import ie_rsn, ie_wpa

ESS = 0x0001
ESS_PRIVACY = 0x0011


def sec(ies: bytes, cap: int = ESS_PRIVACY):
    return derive_security(parse_ies(ies), cap)


def test_wpa2_psk():
    assert sec(ie_rsn([2])) == ("[WPA2-PSK-CCMP][ESS]", "WPA2")


def test_wpa3_sae():
    assert sec(ie_rsn([8])) == ("[WPA3-SAE-CCMP][ESS]", "WPA3")


def test_transition_mode_psk_sae():
    assert sec(ie_rsn([2, 8])) == ("[WPA2-PSK+SAE-CCMP][ESS]", "WPA2/3")


def test_enterprise():
    assert sec(ie_rsn([1])) == ("[WPA2-EAP-CCMP][ESS]", "WPA2-EAP")


def test_owe():
    assert sec(ie_rsn([18]), ESS) == ("[OWE-CCMP][ESS]", "OWE")


def test_wpa_and_wpa2_mixed_ciphers():
    auth, label = sec(ie_wpa([2], (2, 4)) + ie_rsn([2], (4, 2)))
    assert auth == "[WPA-PSK-TKIP+CCMP][WPA2-PSK-CCMP+TKIP][ESS]"
    assert label == "WPA/WPA2"


def test_gcmp256_suite():
    auth, _ = sec(ie_rsn([8], (9,), 9))
    assert auth == "[WPA3-SAE-GCMP-256][ESS]"


def test_open_and_wep_and_ibss():
    assert sec(b"", ESS) == ("[ESS]", "Open")
    assert sec(b"", ESS_PRIVACY) == ("[WEP]" + "[ESS]", "WEP")
    assert sec(b"", ESS | CAP_PRIVACY | CAP_IBSS)[0] == "[WEP][IBSS]"


def test_truncated_and_garbage_ies_do_not_crash():
    rsn = ie_rsn([2])
    for cut in range(2, len(rsn)):
        parse_ies(rsn[:cut])  # must never raise
    parse_ies(b"\x30")
    parse_ies(bytes([48, 200, 1, 2, 3]))


def test_ies_with_other_elements_in_front():
    ssid = bytes([0, 4]) + b"Test"
    rates = bytes([1, 2, 0x82, 0x84])
    assert sec(ssid + rates + ie_rsn([2]))[1] == "WPA2"
