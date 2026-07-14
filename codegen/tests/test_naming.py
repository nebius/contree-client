from api_generator.naming import pascal_case, safe_ident, snake_case


def test_snake_case_camel() -> None:
    assert snake_case("listImages") == "list_images"
    assert snake_case("deleteImageTag") == "delete_image_tag"
    assert snake_case("whoAmI") == "who_am_i"


def test_snake_case_header_name() -> None:
    assert snake_case("Last-Event-Id") == "last_event_id"


def test_snake_case_plain() -> None:
    assert snake_case("sha256") == "sha256"
    assert snake_case("image_uuid") == "image_uuid"
    assert snake_case("imageUUID") == "image_uuid"


def test_snake_case_keyword() -> None:
    assert snake_case("import") == "import_"


def test_pascal_case() -> None:
    assert pascal_case("files") == "Files"
    assert pascal_case("resources_limits") == "ResourcesLimits"


def test_safe_ident_digit() -> None:
    assert safe_ident("2xx") == "n2xx"
