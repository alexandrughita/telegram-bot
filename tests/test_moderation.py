from moderation import (
    Fingerprint, build_fingerprint, duplicate_reason, extract_phones, extract_urls,
    normalize_text, normalize_url, same_ad_text, violation_action,
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
    assert duplicate_reason(new, [old]) == "același număr de telefon"


def test_same_photo_is_a_duplicate():
    assert duplicate_reason(Fingerprint(media={"p1"}), [Fingerprint(media={"p1"})])


def test_short_chat_repeats_are_not_ad_text():
    assert not same_ad_text(normalize_text("mersi"), normalize_text("mersi"))


def test_reworded_ad_is_the_same_ad():
    # Real messages from the group, one member, 2026-10-05.
    a = normalize_text("Doamne/domnișoare nesatisfăcute din Brașov, pm me")
    b = normalize_text("Doamne/domnișoare nesatisfăcute pm me")
    assert same_ad_text(b, a)


def test_fancy_font_is_the_same_text():
    assert normalize_text("𝐃𝐢𝐬𝐩𝐨𝐧𝐢𝐛𝐢𝐥𝐚 𝐩𝐞𝐧𝐭𝐫𝐮 show web") == normalize_text("Disponibila pentru show web")


def test_unrelated_messages_are_not_the_same_ad():
    a = normalize_text("Bună dimineața tuturor, ce mai faceți azi?")
    b = normalize_text("Știe cineva un restaurant bun în Cluj pentru diseară?")
    assert not same_ad_text(b, a)


def test_text_alone_is_not_a_duplicate_violation():
    assert duplicate_reason(build_fingerprint("Același anunț lung, încă o dată"),
                            [build_fingerprint("Același anunț lung, încă o dată")]) is None


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
