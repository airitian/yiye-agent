from pathlib import Path

from src.code_parser import CodeParser


def test_java_interface_and_enum_are_indexable(tmp_path: Path):
    interface_file = tmp_path / "UserMapper.java"
    interface_file.write_text(
        "package demo; public interface UserMapper { User findById(String id); }",
        encoding="utf-8",
    )
    enum_file = tmp_path / "UserStatus.java"
    enum_file.write_text(
        "package demo; public enum UserStatus { ACTIVE, DISABLED; }",
        encoding="utf-8",
    )

    parser = CodeParser()

    interface_chunks = parser.parse(interface_file)
    enum_chunks = parser.parse(enum_file)

    assert interface_chunks
    assert interface_chunks[0].name == "UserMapper"
    assert interface_chunks[0].chunk_type == "interface"
    assert enum_chunks
    assert enum_chunks[0].name == "UserStatus"
    assert enum_chunks[0].chunk_type == "enum"


def test_java_file_without_ast_type_still_has_a_source_chunk(tmp_path: Path):
    commented_file = tmp_path / "Legacy.java"
    commented_file.write_text("// public class Legacy {}\n", encoding="utf-8")
    empty_file = tmp_path / "Empty.java"
    empty_file.write_text("", encoding="utf-8")

    parser = CodeParser()

    assert parser.parse(commented_file)[0].chunk_type == "java_source"
    assert parser.parse(empty_file)[0].chunk_type == "java_source"


def test_overloads_and_multiple_exports_get_unique_chunk_ids(tmp_path: Path):
    java_file = tmp_path / "Overloaded.java"
    java_file.write_text(
        "public class Overloaded { void run() {} void run(String value) {} }",
        encoding="utf-8",
    )
    js_file = tmp_path / "exports.js"
    js_file.write_text(
        "export const first = 1; export const second = 2;",
        encoding="utf-8",
    )

    parser = CodeParser()
    java_ids = [c.chunk_id for c in parser.parse(java_file)]
    js_ids = [c.chunk_id for c in parser.parse(js_file)]

    assert len(java_ids) == len(set(java_ids))
    assert len(js_ids) == len(set(js_ids))


def test_standalone_javascript_and_config_files_are_indexable(tmp_path: Path):
    js_file = tmp_path / "api.js"
    js_file.write_text("export function createLandingPage() { return true; }", encoding="utf-8")
    xml_file = tmp_path / "mapper.xml"
    xml_file.write_text("<mapper namespace=\"demo.Mapper\"><select id=\"find\" /></mapper>", encoding="utf-8")
    yaml_file = tmp_path / "application.yml"
    yaml_file.write_text("server:\n  port: 8080\n", encoding="utf-8")

    parser = CodeParser()

    js_chunks = parser.parse(js_file)
    xml_chunks = parser.parse(xml_file)
    yaml_chunks = parser.parse(yaml_file)

    assert js_chunks and js_chunks[0].file_type == "js"
    assert xml_chunks and xml_chunks[0].chunk_type == "config"
    assert yaml_chunks and yaml_chunks[0].chunk_type == "config"
