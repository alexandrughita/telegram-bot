from moderation import (
    Fingerprint, build_fingerprint, duplicate_reason, extract_phones, extract_urls,
    normalize_url, violation_action,
)


def test_urls_found_everywhere_and_normalized():
    urls = extract_urls(
        "site1.com nu, dar https://WWW.Site2.com/Oferta/?utm_source=x și t.me/Ana_Bot",
        entity_urls=["https://hidden.example/x"])
    assert urls == {"site2.com/Oferta", "t.me/ana_bot", "hidden.example/x"}


def test_telegram_me_is_t_me():
    assert normalize_url("https://telegram.me/Ana") == normalize_url("t.me/ana")


def test_romanian_mobiles_match_whatever_the_format():
    assert extract_phones("Sună 0722 123 456 sau +40-733.111.222") == {"722123456", "733111222"}


def test_prices_and_dates_are_not_phones():
    assert extract_phones("300 400 500 lei, 12.10.2026") == set()


def test_shared_phone_is_the_same_ad_even_with_new_wording():
    old = build_fingerprint("Masaj relaxant în centru, sună 0722123456")
    new = build_fingerprint("Program nou azi! 0722 123 456")
    assert duplicate_reason(new, [old], 0.92) == "același număr de telefon"


def test_same_photo_is_a_duplicate():
    assert duplicate_reason(Fingerprint(media={"p1"}), [Fingerprint(media={"p1"})], 0.92)


def test_short_chat_repeats_are_allowed():
    assert duplicate_reason(build_fingerprint("mersi"), [build_fingerprint("mersi")], 0.92) is None


def test_near_identical_long_text_is_a_duplicate():
    a = build_fingerprint("Bună, sunt nouă în oraș și aștept mesajele voastre toată ziua")
    b = build_fingerprint("Buna, sunt noua in oras si astept mesajele voastre toata ziua!")
    assert duplicate_reason(b, [a], 0.85)


def test_unrelated_messages_pass():
    a = build_fingerprint("Bună dimineața tuturor, ce mai faceți azi?")
    b = build_fingerprint("Știe cineva un restaurant bun în Cluj pentru diseară?")
    assert duplicate_reason(b, [a], 0.92) is None


def test_escalation_ladder():
    assert [violation_action(n) for n in range(1, 6)] == ["delete", "warn", "warn", "mute", "mute"]


def test_links_to_approape_matches_the_domain_and_subdomains_only():
    from moderation import extract_urls, links_to_approape
    assert links_to_approape(extract_urls("profil: https://www.approape.ro/escorte/ana"))
    assert links_to_approape(extract_urls("vezi aici", ["approape.ro/creatoare/x"]))  # url entity
    assert links_to_approape(extract_urls("", ["https://blog.approape.ro"]))
    assert not links_to_approape(extract_urls("https://approape.ro.example.com/x"))
    assert not links_to_approape(extract_urls("https://approape.ro@evil.com/x"))
    assert not links_to_approape(extract_urls("https://notapproape.ro"))
    assert not links_to_approape(extract_urls("scrie approape ro"))
