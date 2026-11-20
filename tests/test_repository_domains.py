from scripts.check_repository_domains import find_unapproved_domain_references


def test_finds_personal_domain_and_unconfigured_service_host():
    personal_domain = ".".join(("nora", "theoutpost", "network"))
    nested_host = ".".join(("matrix", "nora", "commons", "theoutpost", "network"))
    references = find_unapproved_domain_references(f"{personal_domain}\nhttps://{nested_host}/")

    assert references == [
        (1, personal_domain),
        (2, nested_host),
    ]


def test_allows_commons_and_configured_service_hostnames():
    assert not find_unapproved_domain_references(
        "theoutpost.network commons.theoutpost.network "
        "matrix.commons.theoutpost.network auth.commons.theoutpost.network"
    )


def test_domain_matching_is_case_insensitive():
    assert not find_unapproved_domain_references("MATRIX.COMMONS.THEOUTPOST.NETWORK")
