from scripts.generate_protocol import OUTPUT, render


def test_generated_protocol_files_are_current() -> None:
    for filename, expected in render().items():
        assert (OUTPUT / filename).read_text(encoding="utf-8") == expected
