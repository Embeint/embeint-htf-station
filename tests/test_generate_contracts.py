from scripts.generate_contracts import CSharpGenerator, PythonGenerator, SchemaGraph


def test_csharp_generator_emits_referenced_string_enums() -> None:
    graph = SchemaGraph({
        "RunStatus": {"type": "string", "enum": ["queued", "in-progress"]},
        "Run": {
            "type": "object",
            "required": ["status", "attempts", "runs"],
            "properties": {
                "status": {
                    "allOf": [{"$ref": "#/components/schemas/RunStatus"}],
                    "nullable": True,
                },
                "attempts": {"type": "integer", "format": "int64"},
                "runs": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "format": "uuid"},
                },
            },
        },
    })

    generated = CSharpGenerator("openapi", graph).generate()

    assert "public enum RunStatus" in generated
    assert '[JsonStringEnumMemberName("in-progress")]' in generated
    assert "InProgress," in generated
    assert "public RunStatus? Status" in generated
    assert "public required long Attempts" in generated
    assert "public required IReadOnlyDictionary<string, Guid> Runs" in generated
    assert "public sealed record RunStatus" not in generated


def test_python_generator_emits_referenced_literal_enums() -> None:
    graph = SchemaGraph({
        "RunStatus": {"type": "string", "enum": ["queued", "in-progress"]},
        "Run": {
            "type": "object",
            "required": ["status", "runs"],
            "properties": {
                "status": {
                    "allOf": [{"$ref": "#/components/schemas/RunStatus"}],
                    "nullable": True,
                },
                "runs": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "format": "uuid"},
                },
            },
        },
    })

    generated = PythonGenerator(graph).generate()

    assert "from typing import Literal" in generated
    assert "RunStatus = Literal['queued', 'in-progress']" in generated
    assert "status: RunStatus | None = Field(...)" in generated
    assert "runs: dict[str, UUID] = Field(...)" in generated
