"""Unit tests for masking and secret removal (no database needed). Data is synthetic."""

from cryptography.fernet import Fernet

from app.pseudonym import Pseudonymizer, Term


def make() -> Pseudonymizer:
    """Pseudonymizer with a small synthetic dictionary."""
    pseudo = Pseudonymizer("test-hmac-key", Fernet.generate_key().decode())
    pseudo.load(
        [
            Term("PERSON", "Alice Walker"),
            Term("PERSON", "Alice"),
            Term("ORG", "acme", "substring"),
            Term("ID", r"\b005[A-Za-z0-9]{12}\b", "regex", False),
        ],
        {},
    )
    return pseudo


def roundtrip(pseudo: Pseudonymizer, text: str) -> tuple[str, str]:
    """Mask, remember new tokens (as the service does after commit), reveal."""
    result = pseudo.mask(text)
    for token, (_, surface) in result.new_tokens.items():
        pseudo.add_token(token, surface)
    return result.text, pseudo.reveal(result.text)


def test_names_and_company_are_masked_and_revealed() -> None:
    pseudo = make()
    text = "Alice Walker from ACME said Alice will check AcmeBox_b2b files."
    masked, revealed = roundtrip(pseudo, text)

    assert "Alice" not in masked and "acme" not in masked.lower()
    assert revealed == text
    assert masked.count("PERSON_") == 2 and masked.count("ORG_") == 2


def test_word_mode_does_not_hit_inside_words() -> None:
    pseudo = make()
    masked, _ = roundtrip(pseudo, "Malicey and Alice_notes")
    assert masked.startswith("Malicey") and "PERSON_" in masked


def test_email_and_regex_id() -> None:
    pseudo = make()
    text = "mail alice.w@example.com, user 005Ab12Cd34Ef56"
    masked, revealed = roundtrip(pseudo, text)
    assert "EMAIL_" in masked and "ID_" in masked and "example.com" not in masked
    assert revealed == text


def test_same_surface_gives_same_token() -> None:
    pseudo = make()
    first = pseudo.mask("Alice").text
    assert pseudo.mask("Alice again").text.startswith(first)


def test_secrets_are_removed_for_good() -> None:
    pseudo = make()
    text = (
        "conn: Server=x;Password=Hunter2xyz;AccountKey=abcdEFGHijklMNOPqrstUVWX1234==\n"
        "bot 1234567890:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQr\n"
        "client_secret = 'q8Zr2LmP0xY7tVb3Kc9N'\n"
        "secret_name = 'Vendor-Client-Secret'\n"
    )
    result = pseudo.mask(text)
    assert "Hunter2xyz" not in result.text
    assert "abcdEFGH" not in result.text
    assert "AAbbCC" not in result.text
    assert "q8Zr2LmP0xY7tVb3Kc9N" not in result.text
    assert "Vendor-Client-Secret" in result.text
    assert set(result.redactions) >= {"conn_password", "azure_account_key", "telegram_bot_token", "assigned_secret"}
    assert pseudo.reveal(result.text) == result.text


def test_ordinary_code_is_not_redacted() -> None:
    pseudo = make()
    code = "token = get_access_token(client)\nsecret = mssparkutils.credentials.getSecret(vault, name)\n"
    assert pseudo.mask(code).text == code


def test_existing_tokens_are_kept() -> None:
    pseudo = make()
    masked, _ = roundtrip(pseudo, "Alice")
    assert pseudo.mask(f"note about {masked}").text == f"note about {masked}"


def test_fuse_scores_keeps_strength_of_hits() -> None:
    from app.memories import fuse_scores

    # Strong keyword hit with weak meaning beats a weak keyword hit with slightly better meaning
    scores = fuse_scores([("a", 1.4), ("b", 0.4)], [("b", 0.74), ("a", 0.91)])
    assert scores["a"] > scores["b"]
    assert fuse_scores([], []) == {}


def test_decode_text_handles_utf16_and_nul() -> None:
    from app.common import decode_text

    assert decode_text("hello".encode("utf-16")) == "hello"
    assert decode_text(b"abc\x00def" + b"x" * 9000) is None
    assert decode_text(b"x" * 9000 + b"\x00tail").endswith("tail")
